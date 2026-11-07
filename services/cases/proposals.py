# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Proposals — the pure half of *Summarise into the file* and *Make this a case* (ADR-0138 §8).

Two gestures in the dock hand the conversation to Shirley in one
synchronous one-shot each (D-CS-M-12): *Summarise into the file* asks for a
**proposal package** — items for the briefed case's working file, which the
PM reviews and accepts one by one in the case; *Make this a case* asks for a
**draft** — title, framing, the question and a first thread — which the PM
edits and confirms before the case exists. This module builds both calls and
parses what comes back; the web layer (``web/routes/chat.py``) makes the
calls through ``AIServiceCore.run_synthesis`` with the tool forced, and the
Cases route (``web/routes/cases.py``) writes an item only when the PM accepts
it.

What it holds
-------------
* the two forced tools, :data:`PROPOSAL_TOOL` (``propose_for_file``) and
  :data:`CASE_DRAFT_TOOL` (``draft_case``), in OpenAI function-tool shape;
* their instructions, :data:`PROPOSAL_SYSTEM_PROMPT` and
  :data:`CASE_DRAFT_SYSTEM_PROMPT` — prompt text Shirley reads, code rather
  than Soul text (the Soul files stay byte-identical);
* the context of each call — :func:`conversation_text` (the session's turns
  as role-labelled text, bounded), :func:`proposal_messages`,
  :func:`draft_messages`;
* the parsers — :func:`items_from_tool_calls` normalises each proposed item
  to ``{"kind", "status": "pending", "fields", "why"}``, the shape
  ``CaseProposalRepository.land`` stores, and drops a malformed one with a
  reason for the log; :func:`draft_from_tool_calls` reads the draft;
* the review row's words — :func:`item_headline`, :func:`item_target` and
  :data:`MAIN_FIELDS`, the field of each kind the PM edits before accepting.

Fetcher-free by construction
----------------------------
The one-shot reads the working file as text (``services/cases/file_text.py``)
and the session's conversation, nothing else. No registry tool is offered —
the forced tool is the only one, and it executes nothing: its arguments come
back to the caller as data. :func:`conversation_text` drops tool messages,
so no tool result — an untrusted fetch's least of all — reaches the call.

The guards it lives under
-------------------------
``tests/regression/test_cases_stand_reads_only.py`` holds the whole
``services.cases`` package to two rules, and this module to both: **no
writes** — it is walked with the readers, so neither its code nor its
strings may carry one of the guard's write tokens: an insert, update or
delete call, a session add, a commit or a flush, an entry or milestone
write, a recorded decision, a hand-over, a wait or its lift, a close, a
create, a keep, an upsert, a status set or an effects add (the prompt texts
included — they are source); and **no analytics** — no import of, and no
name from, the pure calculation layer, and no observation path. Standard
library plus the chat message model only: no database, no LLM client.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID

from services.ai_models import Message, MessageRole

#: The forced tool of *Summarise into the file*.
PROPOSAL_TOOL_NAME: Final = "propose_for_file"

#: The forced tool of *Make this a case*.
CASE_DRAFT_TOOL_NAME: Final = "draft_case"

#: The seven kinds an item may be: the file's own sections plus the
#: generated document (ADR-0138 §8, D-CS-B-121), in the instructions' order.
KINDS: tuple[str, ...] = (
    "thread",
    "thread_conclusion",
    "figure",
    "point",
    "conversation",
    "generated_document",
    "prose_append",
)

#: The most items one package carries (D-CS-B-121).
MAX_ITEMS: Final = 12

#: The longest conversation text one call reads, in characters.
CONVERSATION_MAX_CHARS: Final = 12_000

#: A thread's statuses (ADR-0138 §3) — a conclusion may set one.
THREAD_STATUSES: tuple[str, ...] = ("open", "confirmed", "parked", "refuted")

#: A conversation's channels (ADR-0138 §3); none given reads ``other``.
CONVERSATION_CHANNELS: tuple[str, ...] = ("call", "meeting", "other")

#: A case's framings (ADR-0138 §3); none given reads ``question``.
FRAMINGS: tuple[str, ...] = ("question", "task")

#: The field of each kind the review row offers for editing before acceptance
#: (D-CS-B-124): what is accepted is the text the PM left there.
MAIN_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "thread": "title",
        "thread_conclusion": "conclusion",
        "figure": "value",
        "point": "text",
        "conversation": "said",
        "generated_document": "body",
        "prose_append": "text",
    }
)

#: A title, a label, a value or a name: one line of at most this many characters.
_LINE_CHARS = 120
#: A ``why``, a point or a note: one line of at most this many characters.
_SENTENCE_CHARS = 280
#: The most sources a generated document carries.
_MAX_SOURCES = 10

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TURN_SEPARATOR = "\n\n"

#: How the conversation names its two speakers; every other role is dropped.
_SPEAKERS: Mapping[MessageRole, str] = MappingProxyType(
    {MessageRole.USER: "PM", MessageRole.ASSISTANT: "Shirley"}
)

_FILE_OPEN = "<<<WORKING FILE>>>"
_FILE_CLOSE = "<<<END WORKING FILE>>>"
_CONVERSATION_OPEN = "<<<CONVERSATION>>>"
_CONVERSATION_CLOSE = "<<<END CONVERSATION>>>"


# ---------------------------------------------------------------------------
# The tools (OpenAI function-tool shape)
# ---------------------------------------------------------------------------


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


#: One item's arguments — one flat object for all seven kinds, each property
#: naming the kinds it belongs to (a flat schema is what every model behind
#: the endpoint reads alike; a per-kind ``oneOf`` is not).
_ITEM_PROPERTIES: dict[str, Any] = {
    "kind": {
        "type": "string",
        "enum": list(KINDS),
        "description": "What the item adds to the file.",
    },
    "why": _string("One line: the turn of the conversation this item rests on."),
    "title": _string(
        "thread: the new thread's title, one line. generated_document: the document's title."
    ),
    "prose": _string("thread: a short paragraph on what the thread is chasing."),
    "thread": _string(
        "thread_conclusion and prose_append: the live thread's exact title, as the file lists it."
    ),
    "conclusion": _string("thread_conclusion: one line — what the thread established."),
    "status": {
        "type": "string",
        "enum": list(THREAD_STATUSES),
        "description": "thread_conclusion, optional: the thread's status after it.",
    },
    "label": _string("figure: what the number is."),
    "value": _string("figure: the value exactly as it was said, with its unit."),
    "note": _string("figure, optional: who said it, or where it comes from."),
    "as_of": _string("figure, optional: the date the value refers to, YYYY-MM-DD."),
    "text": _string(
        "point: the open point, one line. prose_append: the paragraph to add to the thread."
    ),
    "due_on": _string("point, optional: its due date, YYYY-MM-DD."),
    "held_on": _string("conversation: the day it was held, YYYY-MM-DD."),
    "with_whom": _string("conversation: who it was with."),
    "channel": {
        "type": "string",
        "enum": list(CONVERSATION_CHANNELS),
        "description": "conversation, optional: call, meeting or other.",
    },
    "said": _string("conversation: what was said."),
    "promised": _string("conversation, optional: what was promised."),
    "body": _string(
        "generated_document: the text as plain paragraphs separated by blank lines — no Markdown."
    ),
    "sources": {
        "type": "array",
        "description": "generated_document: what the text rests on.",
        "items": {
            "type": "object",
            "properties": {
                "label": _string("What the source is."),
                "href": _string("An in-app path from the file, such as /cases/…; else omit."),
            },
            "required": ["label"],
        },
    },
}

#: The forced tool of *Summarise into the file* (D-CS-B-121).
PROPOSAL_TOOL: Mapping[str, Any] = MappingProxyType(
    {
        "type": "function",
        "function": {
            "name": PROPOSAL_TOOL_NAME,
            "description": (
                "Propose items for the case's working file. The PM reviews them one by "
                "one; nothing reaches the file unless the PM accepts it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "maxItems": MAX_ITEMS,
                        "items": {
                            "type": "object",
                            "properties": _ITEM_PROPERTIES,
                            "required": ["kind", "why"],
                        },
                    }
                },
                "required": ["items"],
            },
        },
    }
)

#: The forced tool of *Make this a case* (D-CS-B-121).
CASE_DRAFT_TOOL: Mapping[str, Any] = MappingProxyType(
    {
        "type": "function",
        "function": {
            "name": CASE_DRAFT_TOOL_NAME,
            "description": (
                "Draft a new case from the conversation. The PM edits the draft and "
                "confirms it; the case exists only then."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": _string("The case's title: one line, a few words."),
                    "framing": {
                        "type": "string",
                        "enum": list(FRAMINGS),
                        "description": (
                            "question when the PM is working out an answer, task when the "
                            "PM has something to get done."
                        ),
                    },
                    "question": _string("The question (or the task) as a short paragraph."),
                    "first_thread": {
                        "type": "object",
                        "description": "The first thread: a sub-question worth chasing.",
                        "properties": {
                            "title": _string("The thread's title, one line."),
                            "prose": _string("A short paragraph on what it chases."),
                        },
                        "required": ["title"],
                    },
                },
                "required": ["title", "framing", "question"],
            },
        },
    }
)


# ---------------------------------------------------------------------------
# The instructions — prompt text Shirley reads (D-5c-6)
# ---------------------------------------------------------------------------

#: The instructions of *Summarise into the file* (D-CS-B-121).
PROPOSAL_SYSTEM_PROMPT: Final = (
    "You are Shirley, the assistant of a portfolio manager (the PM). You receive the "
    "working file of one case, as the PM sees it now, and the PM's conversation with "
    f"you. Call the {PROPOSAL_TOOL_NAME} tool once with the items worth adding to the "
    "file; the PM reviews them one by one and nothing reaches the file unless the PM "
    "accepts it. Each item carries a why: one line naming the turn of the conversation "
    "it rests on.\n\n"
    "The kinds: thread — a new sub-question (title, prose); thread_conclusion — what a "
    "live thread established (thread, conclusion, optionally status: open, confirmed, "
    "parked or refuted); figure — a number (label, value, optionally note and as_of); "
    "point — an open point (text, optionally due_on); conversation — a call or meeting "
    "the PM reported (held_on, with_whom, said, optionally channel and promised); "
    "generated_document — a memo or summary text (title, body, sources); prose_append — "
    "a paragraph for a live thread (thread, text).\n\n"
    "Rules. Propose only what the conversation supports. Never invent a figure: a value "
    "appears only as it was said, with its unit. Propose a generated_document only when "
    "the PM asked for a memo or a summary text; its body is plain paragraphs separated "
    "by blank lines, and its sources are in-app paths from the file (/cases/…, "
    "/investments/…) or plain labels — never a path the file does not show. Prefer a "
    "thread_conclusion over a new thread when a live thread fits, and name a live "
    "thread by its exact title as the file lists it. Leave out what the file already "
    f"holds. Propose at most {MAX_ITEMS} items. Dates are YYYY-MM-DD. Write in the "
    "conversation's language. When the conversation supports nothing for the file, "
    "call the tool with an empty list."
)

#: The instructions of *Make this a case* (D-CS-B-121).
CASE_DRAFT_SYSTEM_PROMPT: Final = (
    "You are Shirley, the assistant of a portfolio manager (the PM). You receive the "
    f"PM's conversation with you. Call the {CASE_DRAFT_TOOL_NAME} tool once with a draft "
    "of a new case built from it: a title (one line, a few words); the framing — "
    "question when the PM is working out an answer, task when the PM has something to "
    "get done; the question (or the task) as a short paragraph in the PM's terms; and "
    "a first thread — a sub-question worth chasing first, with a short paragraph. Use "
    "only what the conversation says; never invent a figure or a fact. Write in the "
    "conversation's language. The PM edits the draft before the case exists."
)


# ---------------------------------------------------------------------------
# The context of a call
# ---------------------------------------------------------------------------


def conversation_text(
    messages: Sequence[Message], *, max_chars: int = CONVERSATION_MAX_CHARS
) -> str:
    """Render the session's conversation as role-labelled turns for a one-shot.

    Each PM and Shirley message becomes one turn, ``PM: …`` or
    ``Shirley: …``, separated by a blank line, oldest first. Tool messages
    and every other role are dropped, attachments are never rendered and an
    empty message is skipped. When the turns exceed ``max_chars`` the oldest
    are cut first; a newest turn that alone exceeds it keeps its tail behind
    a leading ``…``.

    Args:
        messages: The session history's messages, oldest first.
        max_chars: The most characters the text may have.

    Returns:
        The conversation as text, never longer than ``max_chars``; ``""``
        when nothing remains.
    """
    if max_chars <= 0:
        return ""
    turns: list[str] = []
    for message in messages:
        speaker = _SPEAKERS.get(message.role)
        content = (message.content or "").strip()
        if speaker is None or not content:
            continue
        turns.append(f"{speaker}: {content}")
    kept: list[str] = []
    total = 0
    for turn in reversed(turns):
        cost = len(turn) + (len(_TURN_SEPARATOR) if kept else 0)
        if total + cost > max_chars:
            if not kept:
                tail = max_chars - 1
                kept.append("…" + (turn[len(turn) - tail :] if tail > 0 else ""))
            break
        kept.append(turn)
        total += cost
    return _TURN_SEPARATOR.join(reversed(kept))


def proposal_messages(file_text: str, conversation: str) -> list[dict[str, str]]:
    """Build the context of the *Summarise into the file* call.

    Args:
        file_text: The working file as text
            (``services.cases.file_text.render_case_file_text``).
        conversation: The conversation as text (:func:`conversation_text`).

    Returns:
        One ``user`` message in OpenAI shape carrying the file and the
        conversation, each fenced.
    """
    content = (
        "The working file of the case, as the PM sees it now:\n"
        f"{_FILE_OPEN}\n{file_text}\n{_FILE_CLOSE}\n\n"
        "The conversation, oldest turn first:\n"
        f"{_CONVERSATION_OPEN}\n{conversation}\n{_CONVERSATION_CLOSE}"
    )
    return [{"role": "user", "content": content}]


def draft_messages(conversation: str) -> list[dict[str, str]]:
    """Build the context of the *Make this a case* call.

    Args:
        conversation: The conversation as text (:func:`conversation_text`).

    Returns:
        One ``user`` message in OpenAI shape carrying the fenced conversation.
    """
    content = (
        "The conversation, oldest turn first:\n"
        f"{_CONVERSATION_OPEN}\n{conversation}\n{_CONVERSATION_CLOSE}"
    )
    return [{"role": "user", "content": content}]


# ---------------------------------------------------------------------------
# Parsing what comes back
# ---------------------------------------------------------------------------


def _collapsed(value: object) -> str:
    """A string argument as one line, whitespace collapsed; anything else is empty."""
    return " ".join(value.split()) if isinstance(value, str) else ""


def _line(value: object, limit: int = _LINE_CHARS) -> str:
    """A one-line argument, clipped to ``limit`` characters with an ellipsis."""
    line = _collapsed(value)
    if len(line) > limit:
        return line[: limit - 1].rstrip() + "…"
    return line


def _text(value: object) -> str:
    """A multi-line argument, stripped, its line breaks normalised."""
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").strip()


def _iso_date(value: object) -> str | None:
    """A ``YYYY-MM-DD`` calendar date as given, or ``None``."""
    text = _collapsed(value)
    if not _ISO_DATE.fullmatch(text):
        return None
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _live_thread(
    raw: Mapping[str, Any], live_threads: Mapping[str, UUID]
) -> tuple[str, UUID] | None:
    """The live thread an item names: its exact title, else the same title in any case."""
    named = _collapsed(raw.get("thread"))
    if not named:
        return None
    by_title = {_collapsed(title): (title, thread_id) for title, thread_id in live_threads.items()}
    if named in by_title:
        return by_title[named]
    folded = named.casefold()
    for title, match in by_title.items():
        if title.casefold() == folded:
            return match
    return None


_Fields = tuple[dict[str, Any] | None, str]


def _thread_fields(raw: Mapping[str, Any], _live: Mapping[str, UUID]) -> _Fields:
    title = _line(raw.get("title"))
    if not title:
        return None, "a thread without a title"
    return {"title": title, "prose": _text(raw.get("prose"))}, ""


def _conclusion_fields(raw: Mapping[str, Any], live: Mapping[str, UUID]) -> _Fields:
    match = _live_thread(raw, live)
    if match is None:
        return None, "a thread_conclusion naming no live thread"
    conclusion = _text(raw.get("conclusion"))
    if not conclusion:
        return None, "a thread_conclusion without a conclusion"
    title, thread_id = match
    fields: dict[str, Any] = {
        "thread": title,
        "thread_id": str(thread_id),
        "conclusion": conclusion,
    }
    status = _collapsed(raw.get("status")).lower()
    if status in THREAD_STATUSES:
        fields["status"] = status
    return fields, ""


def _figure_fields(raw: Mapping[str, Any], _live: Mapping[str, UUID]) -> _Fields:
    label = _line(raw.get("label"))
    if not label:
        return None, "a figure without a label"
    value = _line(raw.get("value"))
    if not value:
        return None, "a figure without a value"
    fields: dict[str, Any] = {"label": label, "value": value}
    note = _line(raw.get("note"), _SENTENCE_CHARS)
    if note:
        fields["note"] = note
    as_of = _line(raw.get("as_of"))
    if as_of:
        fields["as_of"] = as_of
    return fields, ""


def _point_fields(raw: Mapping[str, Any], _live: Mapping[str, UUID]) -> _Fields:
    text = _line(raw.get("text"), _SENTENCE_CHARS)
    if not text:
        return None, "a point without a text"
    fields: dict[str, Any] = {"text": text}
    due_on = _iso_date(raw.get("due_on"))
    if due_on is not None:
        fields["due_on"] = due_on
    return fields, ""


def _conversation_fields(raw: Mapping[str, Any], _live: Mapping[str, UUID]) -> _Fields:
    said = _text(raw.get("said"))
    if not said:
        return None, "a conversation without what was said"
    with_whom = _line(raw.get("with_whom"))
    if not with_whom:
        return None, "a conversation without with_whom"
    fields: dict[str, Any] = {"with_whom": with_whom, "said": said}
    held_on = _iso_date(raw.get("held_on"))
    if held_on is not None:
        fields["held_on"] = held_on
    channel = _collapsed(raw.get("channel")).lower()
    if channel in CONVERSATION_CHANNELS:
        fields["channel"] = channel
    promised = _text(raw.get("promised"))
    if promised:
        fields["promised"] = promised
    return fields, ""


def _sources(raw: object) -> list[dict[str, str]]:
    """A generated document's sources: ``{label}`` or ``{label, href}``, at most ten."""
    sources: list[dict[str, str]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, Mapping):
            continue
        href = _collapsed(entry.get("href"))
        label = _line(entry.get("label")) or _line(href)
        if not label:
            continue
        source = {"label": label}
        if href:
            source["href"] = href
        sources.append(source)
        if len(sources) == _MAX_SOURCES:
            break
    return sources


def _document_fields(raw: Mapping[str, Any], _live: Mapping[str, UUID]) -> _Fields:
    title = _line(raw.get("title"))
    if not title:
        return None, "a generated_document without a title"
    body = _text(raw.get("body"))
    if not body:
        return None, "a generated_document without a body"
    return {"title": title, "body": body, "sources": _sources(raw.get("sources"))}, ""


def _append_fields(raw: Mapping[str, Any], live: Mapping[str, UUID]) -> _Fields:
    match = _live_thread(raw, live)
    if match is None:
        return None, "a prose_append naming no live thread"
    text = _text(raw.get("text"))
    if not text:
        return None, "a prose_append without a text"
    title, thread_id = match
    return {"thread": title, "thread_id": str(thread_id), "text": text}, ""


_FIELD_READERS: Mapping[str, Callable[[Mapping[str, Any], Mapping[str, UUID]], _Fields]] = (
    MappingProxyType(
        {
            "thread": _thread_fields,
            "thread_conclusion": _conclusion_fields,
            "figure": _figure_fields,
            "point": _point_fields,
            "conversation": _conversation_fields,
            "generated_document": _document_fields,
            "prose_append": _append_fields,
        }
    )
)


def _normalised(raw: object, live_threads: Mapping[str, UUID]) -> tuple[dict[str, Any] | None, str]:
    """One proposed item in the stored shape, or ``None`` and the reason it was dropped."""
    if not isinstance(raw, Mapping):
        return None, "not an object"
    kind = _collapsed(raw.get("kind")).lower()
    reader = _FIELD_READERS.get(kind)
    if reader is None:
        return None, f"unknown kind {kind!r}"
    fields, reason = reader(raw, live_threads)
    if fields is None:
        return None, reason
    why = _line(raw.get("why"), _SENTENCE_CHARS)
    return {"kind": kind, "status": "pending", "fields": fields, "why": why}, ""


def items_from_tool_calls(
    tool_calls: Sequence[Mapping[str, Any]], *, live_threads: Mapping[str, UUID]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalise the proposal call's items into the package ``land`` stores (D-CS-B-121).

    Every item of every ``propose_for_file`` call, in order, becomes
    ``{"kind", "status": "pending", "fields": {...}, "why"}``: one-line
    fields collapsed and clipped, texts stripped, dates kept only as
    ``YYYY-MM-DD``. A ``thread_conclusion`` or ``prose_append`` names its
    thread by title; the title is resolved against the live threads —
    exactly, else the same title in any letter case — and the item carries
    the thread's id as ``fields["thread_id"]``. A malformed item — not an
    object, an unknown kind, a missing required field, a thread that is not
    live — is dropped with one reason; past :data:`MAX_ITEMS` valid items the
    rest are dropped too. The reasons are for the log, never for the file.

    Args:
        tool_calls: The parsed tool calls of one ``run_synthesis`` result,
            each ``{"name": str, "arguments": dict, "id": str}``.
        live_threads: The case's live threads, title → id.

    Returns:
        ``(items, reasons)`` — the normalised items, at most
        :data:`MAX_ITEMS`, and one reason per dropped item (or per call
        without an item list, or one when no proposal call came back).
    """
    reasons: list[str] = []
    raw_items: list[object] = []
    calls = [call for call in tool_calls if call.get("name") == PROPOSAL_TOOL_NAME]
    if not calls:
        reasons.append(f"no {PROPOSAL_TOOL_NAME} call")
    for call in calls:
        arguments = call.get("arguments")
        listed = arguments.get("items") if isinstance(arguments, Mapping) else None
        if not isinstance(listed, list):
            reasons.append(f"a {PROPOSAL_TOOL_NAME} call without an items list")
            continue
        raw_items.extend(listed)
    items: list[dict[str, Any]] = []
    for position, raw in enumerate(raw_items, start=1):
        item, reason = _normalised(raw, live_threads)
        if item is None:
            reasons.append(f"item {position}: {reason}")
        elif len(items) >= MAX_ITEMS:
            reasons.append(f"item {position}: past the first {MAX_ITEMS} items")
        else:
            items.append(item)
    return items, reasons


def draft_from_tool_calls(tool_calls: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Read the draft of a new case from the draft call (D-CS-B-121, D-CS-B-126).

    Args:
        tool_calls: The parsed tool calls of one ``run_synthesis`` result.

    Returns:
        ``{"title", "framing", "question", "first_thread"}`` from the first
        ``draft_case`` call with a title — ``framing`` ``question`` unless
        the call says ``task``, ``first_thread`` ``{"title", "prose"}`` or
        ``None`` when the call drafts none; ``None`` when no call drafts a
        title.
    """
    for call in tool_calls:
        if call.get("name") != CASE_DRAFT_TOOL_NAME:
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, Mapping):
            continue
        title = _line(arguments.get("title"))
        if not title:
            continue
        framing = _collapsed(arguments.get("framing")).lower()
        first = arguments.get("first_thread")
        first_thread: dict[str, str] | None = None
        if isinstance(first, Mapping):
            thread_title = _line(first.get("title"))
            if thread_title:
                first_thread = {"title": thread_title, "prose": _text(first.get("prose"))}
        return {
            "title": title,
            "framing": framing if framing in FRAMINGS else "question",
            "question": _text(arguments.get("question")),
            "first_thread": first_thread,
        }
    return None


# ---------------------------------------------------------------------------
# The review row's words
# ---------------------------------------------------------------------------


def _fields_of(item: Mapping[str, Any]) -> Mapping[str, Any]:
    fields = item.get("fields")
    return fields if isinstance(fields, Mapping) else {}


def item_headline(item: Mapping[str, Any]) -> str:
    """The review row's headline: what the item is, beside the text it offers for editing.

    Args:
        item: A stored item (:func:`items_from_tool_calls`).

    Returns:
        *New thread* · *Conclusion for "…"* (· status) · *Figure · label*
        (· as of …) · *Open point* (· due …) · *Conversation with …* (· day)
        · *Document · title* · *Text for "…"*.
    """
    fields = _fields_of(item)
    kind = item.get("kind")
    if kind == "thread":
        return "New thread"
    if kind == "thread_conclusion":
        status = fields.get("status")
        head = f'Conclusion for "{fields.get("thread", "")}"'
        return f"{head} · {status}" if status else head
    if kind == "figure":
        as_of = fields.get("as_of")
        head = f"Figure · {fields.get('label', '')}"
        return f"{head} · as of {as_of}" if as_of else head
    if kind == "point":
        due_on = fields.get("due_on")
        return f"Open point · due {due_on}" if due_on else "Open point"
    if kind == "conversation":
        held_on = fields.get("held_on")
        head = f"Conversation with {fields.get('with_whom', '')}"
        return f"{head} · {held_on}" if held_on else head
    if kind == "generated_document":
        return f"Document · {fields.get('title', '')}"
    if kind == "prose_append":
        return f'Text for "{fields.get("thread", "")}"'
    return str(kind)


def item_target(item: Mapping[str, Any]) -> str:
    """Where an accepted item goes in the file — the row's ``→ …``.

    Args:
        item: A stored item (:func:`items_from_tool_calls`).

    Returns:
        *Threads* · *thread "…"* · *Figures* · *Open points* ·
        *Conversations* · *generated document*.
    """
    kind = item.get("kind")
    if kind in ("thread_conclusion", "prose_append"):
        return f'thread "{_fields_of(item).get("thread", "")}"'
    return {
        "thread": "Threads",
        "figure": "Figures",
        "point": "Open points",
        "conversation": "Conversations",
        "generated_document": "generated document",
    }.get(str(kind), "")


__all__ = [
    "CASE_DRAFT_SYSTEM_PROMPT",
    "CASE_DRAFT_TOOL",
    "CASE_DRAFT_TOOL_NAME",
    "CONVERSATION_CHANNELS",
    "CONVERSATION_MAX_CHARS",
    "FRAMINGS",
    "KINDS",
    "MAIN_FIELDS",
    "MAX_ITEMS",
    "PROPOSAL_SYSTEM_PROMPT",
    "PROPOSAL_TOOL",
    "PROPOSAL_TOOL_NAME",
    "THREAD_STATUSES",
    "conversation_text",
    "draft_from_tool_calls",
    "draft_messages",
    "item_headline",
    "item_target",
    "items_from_tool_calls",
    "proposal_messages",
]
