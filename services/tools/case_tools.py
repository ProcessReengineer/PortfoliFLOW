# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shirley's case tools: three reads and one write (ADR-0138 §10).

Three read-only AI-callable tools over the Cases area, so Shirley sees what
the PM sees — across cases and across time:

- ``list_cases`` — the cases of the book, one line each, filtered by state
  (open · waiting · closed · all) and, with ``mine``, to the cases the
  signed-in user owns; through :meth:`CaseRepository.list_cases`.
- ``get_case`` — one case's working file as text, in any state, by its
  number; through :func:`services.cases.file_text.render_case_file_text`,
  the composition Shirley's case brief uses too (one function, one truth).
- ``search_cases`` — a word or phrase over what the PM wrote or kept as
  text, **closed cases included** (the archive is the institution's memory);
  through :meth:`CaseRepository.search_working_file`, the helper the Cases
  list's search uses (D-CS-M-6). Never over an attachment's contents or its
  file name (ADR-0138 §11).

All three register with :class:`~services.tool_classes.ToolClass`
``READ_INTERNAL`` at import time; their results are trusted content — the
PM's own file (ADR-0022, D-CS-B-112). The descriptions carry the behaviour:
``prompts/Soul_Shirley.md`` is byte-identical (the Soul-hash rule).

The write tool (ADR-0138 §8 channel 3, §10)
-------------------------------------------
``case_add`` is the platform's first ``WRITE_INTERNAL`` tool. On the PM's
request it adds **one** row to the working file of the case it names by
number — never a case it guesses (D-CS-M-17): a ``figure``, a ``thread``, an
open ``point``, a ``conversation`` note, or a paragraph of ``prose``
appended to a live thread (D-CS-M-8, D-CS-B-113). It is additive only: it
never closes a case, records a decision, hands over, sets waiting, removes or
edits anything. Every write goes through the working-file repositories,
which refuse a closed case and bump ``cases.updated_at`` (D-CS-B-5); a
waiting case takes the write and stays waiting (a PM write is no §7 event).
A refusal is a plain line, and nothing is written.

The tool registry locks the class for the rest of a turn once a
``READ_EXTERNAL_UNTRUSTED`` tool ran (ADR-0022): the model then reads the
lock's refusal as the tool result, and the tool is never invoked.

The side channel (D-CS-B-114). A successful write returns a JSON envelope
shaped like the chart artefact's (ADR-0114) — ``{"__artifact__":
"case_write", "case_id", "badge", "kind", "summary", "href", "undo",
"llm_response"}``. :class:`~services.ai_service_core.AIServiceCore` turns it
into a ``case_write`` stream event and hands the model only
``llm_response``, the fixed acknowledgement (*Added to CASE-nnnn — open
point: «…».*) — true on every channel, so it names the case and the row and
nothing a channel may not show (D-CS-B-128). The web chat renders the event
as a line in the dock whose *Undo* (``undo``) takes the row out again
(``web/case_keep.py``); the voice call relays the acknowledgement, and an
adapter that ignores the event still leaves the row in the file.

Pattern reuse (ADR-0047)
------------------------
Each tool mirrors ``analysis_tools.py``: a synchronous ``Callable[..., str]``
that reads the tool-execution context first — without one it returns the
graceful *not available in this context* string — then runs its async
workflow through
:func:`services.tools._async_bridge.run_async_in_fresh_loop` in a
loop-local, tenant-scoped session (:func:`services.tools._tool_session.tool_session`,
which carries the turn's user, ADR-0132). Users appear by name, resolved
through :class:`~core.repositories.user_repository.UserRepository` inside
that session in one batch per call; an id never reaches the text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import CaseClosedError, CaseStateInvalid
from core.repositories._case_common import _MUTABLE_STATES
from core.repositories.case_repository import CaseDTO, CaseRepository
from core.repositories.case_working_file_repository import (
    CaseConversationRepository,
    CaseFigureRepository,
    CasePointRepository,
    CaseThreadDTO,
    CaseThreadRepository,
)
from core.repositories.user_repository import UserRepository
from services.cases.file_text import file_user_ids, render_case_file_text
from services.tool_classes import ToolClass
from services.tool_registry import get_tool_registry
from services.tools._async_bridge import run_async_in_fresh_loop
from services.tools._tool_context import get_tool_context
from services.tools._tool_session import tool_session

#: The three read tools this module registers, in registration order; the
#: write tool :data:`_WRITE_TOOL_NAME` registers fourth.
_TOOL_NAMES: tuple[str, ...] = ("list_cases", "get_case", "search_cases")
_WRITE_TOOL_NAME = "case_add"

# Returned by every tool when no consumer has populated the tool-execution
# context — the graceful-degradation path of the analysis tools.
_CONTEXT_NOT_SET_MSG = (
    "Case data is not available in this context. (The tool-execution context "
    "was not set — the case tools read the persistent database and are only "
    "available from the web chat surface.)"
)

#: The state filter's vocabulary; ``all`` reads every state.
_STATES: tuple[str, ...] = ("open", "waiting", "closed", "all")
_ALL = "all"

#: At most this many cases per ``list_cases`` call, and lines per search.
_LIST_LIMIT = 50

#: A search needs at least this many characters.
_MIN_QUERY_CHARS = 2

# The tool results' calm lines (D-5a-3).
_UNKNOWN_STATE_MSG = "Unknown state — use open, waiting, closed or all."
_SHORT_QUERY_MSG = "Give at least two characters to search for."
_UNKNOWN_CASE_MSG = "No case {badge} in this book. `list_cases` names the open ones."
_BAD_NUMBER_MSG = "Give the case number as a whole number, e.g. 31 for CASE-0031."
_NO_USER_MSG = (
    "There is no signed-in user in this context, so mine cannot be applied — "
    "call list_cases without mine."
)
_NONE = "none"
_UNKNOWN_USER = "unknown user"

# --- case_add (D-CS-B-113, D-CS-B-114) ---------------------------------------

#: The five kinds ``case_add`` writes (D-CS-M-8), in the description's order.
_KINDS: tuple[str, ...] = ("figure", "thread", "point", "conversation", "prose")

#: How the acknowledgement and the dock line name each kind (D-CS-B-114);
#: ``prose`` names its thread. ``web/case_keep.py`` renders the dock line with
#: the same labels (a test pins the two equal).
_KIND_LABELS: dict[str, str] = {
    "figure": "figure",
    "thread": "thread",
    "point": "open point",
    "conversation": "conversation note",
    "prose": 'text in thread "{title}"',
}

#: The artefact marker of a write, beside the chart's ``"chart"`` (ADR-0114).
_CASE_WRITE_ARTIFACT = "case_write"

#: A thread title is one line of at most this many characters; the summary
#: of a write is clipped to the same width.
_TITLE_CHARS = 120

#: ``#n`` names the n-th live thread of the file, in position order.
_THREAD_NUMBER = re.compile(r"#\s*(\d+)")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: The figure source of a value the PM relays from a conversation (§3).
_CONVERSATION_SOURCE = "conversation"
#: A conversation note from Shirley is a call's (D-CS-B-113).
_CALL_CHANNEL = "call"

#: A paragraph of ``prose`` is at most this many characters (D-CS-B-128).
_PROSE_CHARS = 2_000

# The acknowledgement the model repeats (D-5b-3, D-CS-B-128) — true on every
# channel: web, call, Telegram — and the refusals (D-5b-5, D-5c-7).
_ACKNOWLEDGEMENT = "Added to {badge} — {label}: «{summary}»."
_NO_CASE_NUMBER_MSG = (
    "Name the case by its number — the brief opens on the case's badge, or `list_cases` lists them."
)
_CLOSED_CASE_MSG = "{badge} is closed; nothing is added to a closed case."
_UNKNOWN_KIND_MSG = "kind must be one of figure, thread, point, conversation, prose."
_NO_LABEL_MSG = "A figure needs a label."
_NO_THREAD_MSG = "Name the thread by its exact title or as #n; the file lists them."
_NO_WITH_WHOM_MSG = "Say who the conversation was with."
_BAD_DATE_MSG = "Dates are YYYY-MM-DD."
_EMPTY_TEXT_MSG = "Say what to add — text is empty."
_LONG_TITLE_MSG = "A thread title is one line of at most 120 characters."
_LONG_PROSE_MSG = "Keep a paragraph under 2 000 characters — add a second one if you need more."


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


def _badge(case_number: int) -> str:
    return f"CASE-{case_number:04d}"


def _day(value: date) -> str:
    """``dd Mon YYYY``."""
    return value.strftime("%d %b %Y")


def _short(moment: datetime) -> str:
    """``dd Mon``, in UTC."""
    return f"{moment.astimezone(timezone.utc):%d %b}"


def _case_line(case: CaseDTO, names: dict[UUID, str]) -> str:
    """One case: *CASE-0031 · open · title · owner Name · updated dd Mon · due d*.

    A waiting case adds the date it returns to open, a closed case when it
    closed.
    """
    parts = [
        _badge(case.case_number),
        case.state,
        " ".join(case.title.split()),
        f"owner {names.get(case.owner_user_id) or _UNKNOWN_USER}",
        f"updated {_short(case.updated_at)}",
    ]
    if case.state == "waiting" and case.wait_until is not None:
        parts.append(f"waiting until {_day(case.wait_until)}")
    if case.state == "closed" and case.closed_at is not None:
        parts.append(f"closed {_short(case.closed_at)}")
    elif case.due_on is not None:
        parts.append(f"due {_day(case.due_on)}")
    return " · ".join(parts)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 'es' if noun.endswith('ch') else 's'}"


def _listing(head: str, cases: list[CaseDTO], names: dict[UUID, str]) -> str:
    """The head line, then one line per case (at most :data:`_LIST_LIMIT`), or *none*."""
    if not cases:
        return f"{head}\n{_NONE}"
    lines = [head, *(_case_line(case, names) for case in cases[:_LIST_LIMIT])]
    if len(cases) > _LIST_LIMIT:
        lines.append(f"… {len(cases) - _LIST_LIMIT} more")
    return "\n".join(lines)


def _parse_case_number(raw: object) -> int | None:
    """A case number ≥ 1 from the argument: an integer, or ``"31"`` / ``"CASE-0031"``.

    The schema asks for an integer; a model that sends the badge or a numeric
    string is read the same. Anything else — a bool, a fraction, zero — is
    ``None``.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    if isinstance(raw, str):
        digits = raw.strip().upper().removeprefix("CASE-").strip()
        raw = int(digits) if digits.isdigit() else None
    return raw if isinstance(raw, int) and raw >= 1 else None


async def _resolve_names(session: AsyncSession, ids: Iterable[UUID]) -> dict[UUID, str]:
    """Display names by user id, one batch — the Journal idiom (display name, else email)."""
    users = UserRepository(session)
    names: dict[UUID, str] = {}
    for user_id in set(ids):
        user = await users.get_by_id(user_id)
        if user is not None:
            names[user_id] = user.display_name or user.email
    return names


# ---------------------------------------------------------------------------
# case_add — arguments, threads, writes and the artefact
# ---------------------------------------------------------------------------


def _clean(value: object) -> str:
    """An optional string argument, stripped; anything else is empty."""
    return value.strip() if isinstance(value, str) else ""


def _summary(text: str) -> str:
    """A write's summary: one line, clipped to :data:`_TITLE_CHARS` with an ellipsis."""
    line = " ".join(text.split())
    if len(line) > _TITLE_CHARS:
        return line[: _TITLE_CHARS - 1].rstrip() + "…"
    return line


def _quoted(summary: str) -> str:
    """A summary as the acknowledgement quotes it: a trailing ``.``, ``!`` or ``?`` dropped.

    The quote closes the sentence (``«…».``), so the summary's own full stop
    would double it (D-CS-B-128). A summary of punctuation only stays as it is.
    """
    return summary.rstrip(".!?") or summary


def _parse_iso_date(raw: object) -> date | None:
    """``YYYY-MM-DD`` as a date; ``None`` when absent.

    Raises:
        ValueError: On anything but a calendar date in that one form — the
            week and compact forms ``date.fromisoformat`` accepts included.
    """
    text = _clean(raw)
    if not text:
        return None
    if not _ISO_DATE.fullmatch(text):
        raise ValueError(text)
    return date.fromisoformat(text)


def _kind_label(kind: str, thread_title: str = "") -> str:
    """The kind's name in the acknowledgement (D-CS-B-114)."""
    return _KIND_LABELS[kind].format(title=thread_title)


def _match_thread(threads: Sequence[CaseThreadDTO], ref: str) -> CaseThreadDTO | None:
    """The live thread ``ref`` names: its exact title (case-insensitive), else ``#n``.

    ``threads`` are the case's live threads in position order; ``#n`` is the
    n-th of them, counted from 1. A title shared by two threads names the
    first. ``None`` when nothing matches.
    """
    wanted = ref.strip().casefold()
    if not wanted:
        return None
    for thread in threads:
        if thread.title.strip().casefold() == wanted:
            return thread
    numbered = _THREAD_NUMBER.fullmatch(ref.strip())
    if numbered is not None:
        index = int(numbered.group(1))
        if 1 <= index <= len(threads):
            return threads[index - 1]
    return None


def _thread_refusal(threads: Sequence[CaseThreadDTO]) -> str:
    """The unknown-thread refusal, listing the live threads as ``#n "title"``."""
    listed = ", ".join(f'#{n} "{thread.title}"' for n, thread in enumerate(threads, 1))
    return f"{_NO_THREAD_MSG} Live threads: {listed or _NONE}."


async def _write_figure(
    db: AsyncSession,
    case: CaseDTO,
    *,
    label: str,
    text: str,
    with_whom: str,
    held_on: date | None,
    now: datetime,
) -> tuple[UUID, str, str]:
    """A ``conversation`` figure: the value as written, who said it, from when."""
    row = await CaseFigureRepository(db).add(
        case.id,
        label=label,
        source=_CONVERSATION_SOURCE,
        value_text=text,
        as_of=held_on.isoformat() if held_on is not None else None,
        note=with_whom or None,
        now=now,
    )
    return row.id, _summary(f"{row.label}: {text}"), "figures"


async def _write_thread(
    db: AsyncSession, case: CaseDTO, *, text: str, now: datetime
) -> tuple[UUID, str, str]:
    """A thread with the title and no prose, status open."""
    row = await CaseThreadRepository(db).add(case.id, title=text, now=now)
    return row.id, _summary(row.title), f"thread-{row.id}"


async def _write_point(
    db: AsyncSession, case: CaseDTO, *, text: str, due_on: date | None, now: datetime
) -> tuple[UUID, str, str]:
    """An open point without an owner, due when asked."""
    row = await CasePointRepository(db).add(case.id, text=text, due_on=due_on, now=now)
    return row.id, _summary(row.text), "points"


async def _write_conversation(
    db: AsyncSession,
    case: CaseDTO,
    *,
    text: str,
    with_whom: str,
    held_on: date,
    now: datetime,
) -> tuple[UUID, str, str]:
    """A call: with whom, when, what was said — nothing promised."""
    row = await CaseConversationRepository(db).add(
        case.id,
        held_on=held_on,
        channel=_CALL_CHANNEL,
        with_whom=with_whom,
        what_was_said=text,
        now=now,
    )
    return row.id, _summary(f"{with_whom}: {text}"), "conversations"


async def _write_prose(
    db: AsyncSession, thread: CaseThreadDTO, *, text: str, now: datetime
) -> tuple[UUID, str, str]:
    """``text`` appended to the thread's prose as a new paragraph."""
    prose = thread.prose + "\n\n" + text if thread.prose else text
    await CaseThreadRepository(db).update(thread.id, prose=prose, now=now)
    return thread.id, _summary(text), f"thread-{thread.id}"


def _case_write_artifact(
    case: CaseDTO,
    *,
    kind: str,
    row_id: UUID,
    summary: str,
    section: str,
    label: str,
    appended: str | None,
) -> str:
    """The write's side-channel envelope (D-CS-B-114), as a JSON string."""
    badge = _badge(case.case_number)
    undo: dict[str, str] = {"kind": kind, "row_id": str(row_id), "case_id": str(case.id)}
    if appended is not None:
        undo["appended"] = appended
    return json.dumps(
        {
            "__artifact__": _CASE_WRITE_ARTIFACT,
            "case_id": str(case.id),
            "badge": badge,
            "kind": kind,
            "summary": summary,
            "href": f"/cases/{case.id}#cs-{section}",
            "undo": undo,
            "llm_response": _ACKNOWLEDGEMENT.format(
                badge=badge, label=label, summary=_quoted(summary)
            ),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


def list_cases(state: str = "open", mine: bool = False) -> str:
    """List the book's cases, one line each, filtered by state and owner.

    Args:
        state: ``open`` (the default), ``waiting``, ``closed`` or ``all``.
        mine: Keep only the cases the turn's signed-in user owns.

    Returns:
        A head line *n cases (state, mine)* and one line per case (at most
        fifty), or *none*; a calm line for an unknown state, a missing user
        with ``mine``, or an unset context.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG
    chosen = (state or "open").strip().lower()
    if chosen not in _STATES:
        return _UNKNOWN_STATE_MSG
    if mine and ctx.user_id is None:
        return _NO_USER_MSG
    owner = ctx.user_id if mine else None

    async def _workflow() -> tuple[list[CaseDTO], dict[UUID, str]]:
        async with tool_session(ctx) as db:
            cases = await CaseRepository(db).list_cases(
                state=None if chosen == _ALL else chosen,
                owner_user_id=owner,
                limit=_LIST_LIMIT,
            )
            return cases, await _resolve_names(db, (case.owner_user_id for case in cases))

    cases, names = run_async_in_fresh_loop(_workflow)
    scope = f"{chosen}, mine" if mine else chosen
    return _listing(f"{_count(len(cases), 'case')} ({scope})", cases, names)


def get_case(case_number: int) -> str:
    """Return one case's working file as text, in any state.

    Args:
        case_number: The case's number — ``31`` for ``CASE-0031``.

    Returns:
        The file as text (:func:`render_case_file_text`), or a calm line for
        an unknown number, a number that is no whole number, or an unset
        context.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG
    number = _parse_case_number(case_number)
    if number is None:
        return _BAD_NUMBER_MSG

    async def _workflow() -> str | None:
        async with tool_session(ctx) as db:
            case = await CaseRepository(db).get_by_number(number)
            if case is None:
                return None
            names = await _resolve_names(db, await file_user_ids(db, case))
            return await render_case_file_text(
                db, case, now=datetime.now(timezone.utc), user_names=names
            )

    text = run_async_in_fresh_loop(_workflow)
    if text is None:
        return _UNKNOWN_CASE_MSG.format(badge=_badge(number))
    return text


def search_cases(query: str, state: str = "all") -> str:
    """Search every case — closed ones included — over what the PM wrote or kept as text.

    Args:
        query: The word or phrase, at least two characters once stripped.
        state: ``all`` (the default), ``open``, ``waiting`` or ``closed``.

    Returns:
        A head line *n matches for "q"* and one line per matching case (at
        most fifty), or *none*; a calm line for a short query, an unknown
        state, or an unset context.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG
    stripped = " ".join((query or "").split())
    if len(stripped) < _MIN_QUERY_CHARS:
        return _SHORT_QUERY_MSG
    chosen = (state or _ALL).strip().lower()
    if chosen not in _STATES:
        return _UNKNOWN_STATE_MSG

    async def _workflow() -> tuple[list[CaseDTO], dict[UUID, str]]:
        async with tool_session(ctx) as db:
            cases = await CaseRepository(db).search_working_file(
                stripped, state=None if chosen == _ALL else chosen
            )
            shown = cases[:_LIST_LIMIT]
            return cases, await _resolve_names(db, (case.owner_user_id for case in shown))

    cases, names = run_async_in_fresh_loop(_workflow)
    return _listing(f'{_count(len(cases), "match")} for "{stripped}"', cases, names)


def case_add(  # noqa: PLR0913 — the schema's flat arguments
    case_number: int | None = None,
    kind: str = "",
    text: str = "",
    label: str | None = None,
    thread: str | None = None,
    with_whom: str | None = None,
    held_on: str | None = None,
    due_on: str | None = None,
) -> str:
    """Add one row to an open or waiting case's working file, on the PM's request.

    Additive only (ADR-0138 §10): a figure, a thread, an open point, a
    conversation note, or a paragraph appended to a live thread's prose.

    The schema requires ``case_number``, ``kind`` and ``text``; the
    signature defaults them only so that a call that omits one is refused in
    words (D-CS-M-17: no case reference, no write) rather than by a
    ``TypeError``.

    Args:
        case_number: The case's number — ``31`` for ``CASE-0031``; required.
        kind: ``figure`` · ``thread`` · ``point`` · ``conversation`` · ``prose``.
        text: The value (figure), the title (thread, one line of at most 120
            characters), the point, what was said (conversation), or the
            paragraph to append (prose, at most 2 000 characters).
        label: A figure's label; required for ``figure``.
        thread: For ``prose``: the live thread's exact title
            (case-insensitive) or ``#n``, the n-th live thread.
        with_whom: Who said it — a figure's note, and required for
            ``conversation``.
        held_on: ``YYYY-MM-DD`` — a figure's as-of, a conversation's date
            (default today, UTC).
        due_on: ``YYYY-MM-DD`` — an open point's due date.

    Returns:
        The write's artefact JSON (:func:`_case_write_artifact`), or a calm
        refusal line — then nothing was written.
    """
    ctx = get_tool_context()
    if ctx is None:
        return _CONTEXT_NOT_SET_MSG
    number = _parse_case_number(case_number)
    if number is None:
        return _NO_CASE_NUMBER_MSG
    chosen = _clean(kind).lower()
    if chosen not in _KINDS:
        return _UNKNOWN_KIND_MSG
    body = _clean(text)
    if not body:
        return _EMPTY_TEXT_MSG
    figure_label = _clean(label)
    if chosen == "figure" and not figure_label:
        return _NO_LABEL_MSG
    if chosen == "thread":
        body = " ".join(body.split())
        if len(body) > _TITLE_CHARS:
            return _LONG_TITLE_MSG
    if chosen == "prose" and len(body) > _PROSE_CHARS:
        return _LONG_PROSE_MSG
    who = _clean(with_whom)
    if chosen == "conversation" and not who:
        return _NO_WITH_WHOM_MSG
    thread_ref = _clean(thread)
    try:
        held = _parse_iso_date(held_on) if chosen in ("figure", "conversation") else None
        due = _parse_iso_date(due_on) if chosen == "point" else None
    except ValueError:
        return _BAD_DATE_MSG

    async def _workflow() -> str:
        async with tool_session(ctx) as db:
            case = await CaseRepository(db).get_by_number(number)
            if case is None:
                return _UNKNOWN_CASE_MSG.format(badge=_badge(number))
            if case.state not in _MUTABLE_STATES:
                return _CLOSED_CASE_MSG.format(badge=_badge(number))
            now = datetime.now(timezone.utc)
            title, appended = "", None
            try:
                if chosen == "figure":
                    written = await _write_figure(
                        db,
                        case,
                        label=figure_label,
                        text=body,
                        with_whom=who,
                        held_on=held,
                        now=now,
                    )
                elif chosen == "thread":
                    written = await _write_thread(db, case, text=body, now=now)
                elif chosen == "point":
                    written = await _write_point(db, case, text=body, due_on=due, now=now)
                elif chosen == "conversation":
                    written = await _write_conversation(
                        db, case, text=body, with_whom=who, held_on=held or now.date(), now=now
                    )
                else:
                    threads = await CaseThreadRepository(db).list_for_case(case.id)
                    target = _match_thread(threads, thread_ref)
                    if target is None:
                        return _thread_refusal(threads)
                    title, appended = target.title, body
                    written = await _write_prose(db, target, text=body, now=now)
            except CaseClosedError:
                return _CLOSED_CASE_MSG.format(badge=_badge(number))
            except CaseStateInvalid:
                return _UNKNOWN_CASE_MSG.format(badge=_badge(number))
            row_id, summary, section = written
            return _case_write_artifact(
                case,
                kind=chosen,
                row_id=row_id,
                summary=summary,
                section=section,
                label=_kind_label(chosen, title),
                appended=appended,
            )

    return run_async_in_fresh_loop(_workflow)


# ---------------------------------------------------------------------------
# Register tools at import time
# ---------------------------------------------------------------------------

_STATE_ENUM = list(_STATES)

_registry = get_tool_registry()

_registry.register_tool(
    name="list_cases",
    function=list_cases,
    description=(
        "List the cases of the active tenant's book, one line per case: its number "
        "(CASE-nnnn), state, title, owner, when its file was last updated, and its due "
        "date — a waiting case also the date it returns to open, a closed case when it "
        "closed. state filters to open (the default), waiting, closed or all; mine=true "
        "keeps only the cases the signed-in user owns. Use it for 'what is open / waiting "
        "/ what did we close' and to find a case's number for get_case. At most 50 cases "
        "per call. Read-only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "enum": _STATE_ENUM,
                "description": ("Which cases: open (default), waiting, closed, or all of them."),
            },
            "mine": {
                "type": "boolean",
                "description": (
                    "When true, only the cases the signed-in user owns. Defaults to false."
                ),
            },
        },
        "required": [],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="get_case",
    function=get_case,
    description=(
        "Read one case's working file as text, by its number (31 for CASE-0031), in any "
        "state — open, waiting or closed. The text holds the question or task, where the "
        "case stands (on a closed case: as frozen at close), its threads with status, "
        "conclusion and what was kept in them (captions and quoted passages only), its "
        "figures, conversations, open points or questions to ask, the decision draft and "
        "the recorded decisions, and the record's milestones and system lines. It is "
        "digest-bounded: a long section ends with '… n more'. It is the PM's own file — "
        "cite its figures and threads where they bear on the question. Attachment contents "
        "are never read. Read-only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "case_number": {
                "type": "integer",
                "minimum": 1,
                "description": "The case's number: 31 for CASE-0031.",
            },
        },
        "required": ["case_number"],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name="search_cases",
    function=search_cases,
    description=(
        "Search every case of the book for a word or phrase (case-insensitive, at least "
        "two characters) and list the matching cases, one line each. Searched is what the "
        "PM wrote or kept as text: titles and descriptions, threads (title, prose, "
        "conclusion), figures (label, value, note), conversations (with whom, what was said "
        "and promised), decisions (options, rationale, decided by), closing notes, and kept "
        "captions, comments and the quoted passages of document excerpts. Closed cases are "
        "included — the archive is the institution's memory: use it for 'have we had a "
        "gated fund before?'. Attachment contents and file names are never read. state "
        "narrows to open, waiting or closed (default all); read a match with get_case. "
        "Read-only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The word or phrase to look for, at least two characters.",
            },
            "state": {
                "type": "string",
                "enum": _STATE_ENUM,
                "description": "Which cases to search: all (default), open, waiting or closed.",
            },
        },
        "required": ["query"],
    },
    tool_class=ToolClass.READ_INTERNAL,
)

_registry.register_tool(
    name=_WRITE_TOOL_NAME,
    function=case_add,
    description=(
        "Add one thing to a case's working file when the PM asks for it — a figure, a "
        "thread, an open point, a conversation note, or a paragraph of text in a thread. "
        "kind=figure: label (required) is what it is, text the value as said, with_whom "
        "who said it, held_on when. kind=thread: text is the title, one line of at most "
        "120 characters. kind=point: text is the open point, due_on optional. "
        "kind=conversation: text is what was said, with_whom (required) who it was with, "
        "held_on the day (default today). kind=prose: text is appended as a new paragraph "
        "to a live thread, named in thread by its exact title or as #n (the n-th thread of "
        "the file). It is additive only — it never closes a case, records a decision, "
        "hands over, sets waiting, removes or edits anything. Name the case by its number "
        "(case_number: the brief opens on the case's badge, list_cases lists them); never guess "
        "a case. The case must be open or waiting. Write only what the PM asked for in "
        "this turn, one row per request; never add on your own initiative. After writing, "
        "your answer states what was added and to which case, as the result says — the "
        "user sees the same as a line in the dock with Undo. Locked for the rest of a "
        "turn once web_research ran. Dates are YYYY-MM-DD."
    ),
    parameters={
        "type": "object",
        "properties": {
            "case_number": {
                "type": "integer",
                "minimum": 1,
                "description": "The case's number: 31 for CASE-0031.",
            },
            "kind": {
                "type": "string",
                "enum": list(_KINDS),
                "description": "What to add: figure, thread, point, conversation or prose.",
            },
            "text": {
                "type": "string",
                "description": (
                    "The figure's value, the thread's title, the open point, what was said, "
                    "or the paragraph to append."
                ),
            },
            "label": {
                "type": "string",
                "description": "A figure's label — required for kind=figure.",
            },
            "thread": {
                "type": "string",
                "description": (
                    "For kind=prose: the live thread's exact title, or #n for the n-th thread."
                ),
            },
            "with_whom": {
                "type": "string",
                "description": (
                    "Who said it: a figure's source person, required for kind=conversation."
                ),
            },
            "held_on": {
                "type": "string",
                "description": "YYYY-MM-DD: a figure's as-of, or the conversation's day.",
            },
            "due_on": {
                "type": "string",
                "description": "YYYY-MM-DD: an open point's due date.",
            },
        },
        "required": ["case_number", "kind", "text"],
    },
    tool_class=ToolClass.WRITE_INTERNAL,
)
