# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shirley-Chat web surface — message submit, SSE stream, history.

The chat surface is embedded inside the Assistants area
(``/assistants#shirley``) via ``_partials/shirley_section.html``;
the standalone ``GET /chat`` page route was retired in ADR-0051.
The endpoints that remain are the backend wire surface the embedded
shell consumes:

* ``POST /chat/messages``        — accept the user's line, allocate a
  ``turn_id``, return the HTMX fragment that opens the SSE stream.
* ``GET /chat/stream/<turn_id>`` — Server-Sent Events; consumes
  :meth:`AIServiceCore.stream_response` and translates each
  ``StreamEvent`` into a wire-shape SSE event.
* ``POST /chat/new``             — drop the session's history.
* ``GET /chat/history``          — rehydrate the session's history.
* ``POST /chat/brief/watch-briefing/<id>`` — hand a written Watch Desk
  briefing to Shirley in place (ADR-0133 §6); ``GET /chat/brief/banner``
  re-renders the dock's consultation banner from the stash.
* ``POST /chat/brief/case/<id>``  — hand an open or waiting case to Shirley
  in place from its page (ADR-0138 §10, D-CS-B-110); ``GET /chat/dock``
  takes the same case as its ``?case=`` marker, so a dock opened on a case
  page is briefed on that case (D-CS-B-111).
* ``POST /chat/call/session``    — start a voice call: provision the
  tenant's vendor agent lazily and mint the browser's session credential
  (ADR-0137 §4, §8).
* ``POST /chat/call/ask``        — start a voice call's ``ask_shirley`` job
  (ADR-0137 §3); ``GET /chat/call/jobs/<job_id>`` polls it and
  ``POST /chat/call/jobs/<job_id>/cancel`` cancels it.
* ``POST /chat/call/events``     — relay a call's final transcript lines
  into the history.
* ``POST /chat/call/end``        — append the call's end line and re-render
  the history.

* ``GET /api/chat/keep/actions`` · ``GET /api/chat/keep/picker`` ·
  ``POST /api/chat/keep`` — Keep an answer or a figure into the active case
  in one click, or choose another target (ADR-0138 §8 channel 2, §9).
* ``GET /api/chat/case-write/line`` · ``POST /api/chat/case-write/undo`` —
  the dock line of a write Shirley made through ``case_add`` and its *Undo*
  (ADR-0138 §8 channel 3, §10; D-CS-B-115). The stream relays each write as
  a ``case_write`` SSE event; the line is live only — the file is the record.
* ``POST /api/chat/proposals/summarise`` — *Summarise into the file*: one
  synchronous Shirley one-shot over the briefed case's file and the session's
  conversation lands a proposal package on the case, bracketed by the
  ``job_requested`` / ``job_landed`` lines (ADR-0138 §7, §8; D-CS-M-12,
  D-CS-B-122, D-CS-B-123); the PM reviews it item by item in the case.
* ``POST /api/chat/case-draft`` — *Make this a case*: one one-shot drafts
  title, framing, the question and a first thread into a form the PM edits;
  the case exists only when the form posts to ``POST /api/cases/new``
  (ADR-0138 §8, D-CS-B-126).

Two further per-session in-memory stores ride alongside the history
(``app.state.chat_histories``, ADR-0050): the consultation-brief stash
(``app.state.brief_stash``, ADR-0107 C6 — since ADR-0133 §6 of kind
``case`` or ``watch_briefing``, one active, each replacing the other; since
ADR-0138 §8 also the keep target, ``web/brief_stash.py``) and
the chart-artefact sidecar (``app.state.chat_chart_artifacts``, ADR-0114)
that lets a chart survive a tab reload and be kept in a case. Both share
the history's lifecycle exactly — see their sections below. A voice call
(ADR-0137) adds two more: the call-line sidecar
(``app.state.chat_call_lines``), which holds the "Call started" / "Call
ended" lines beside the history and shares its lifecycle like the chart
sidecar, and the ``ask_shirley`` job records (``app.state.call_jobs``),
bounded like the pending-turn store.

A short-lived in-memory store (``app.state.pending_turns``) stashes the
user message and the turn metadata between the POST and the SSE GET.
The store is keyed by ``(session_id, turn_id)`` so cross-session
spoofing of a turn id never hands data to the wrong user. A bounded
LRU (default 100 entries) prevents the dict from growing without
limit when streams die before the SSE handler consumes them. Multi-
worker deployments will need Redis or similar — flagged below as a
Phase-5 follow-up.

Tool confirmation flow
----------------------
The web chat has **no** tool-confirmation prompt today — every tool
runs through ``AIServiceCore`` without an interstitial step, and the
only gating is ADR-0022's per-turn class-lock for
``READ_EXTERNAL_UNTRUSTED`` followers (silently refused inside
:meth:`ToolRegistry.execute_tool`). No ``EXTERNAL_EFFECT`` tool is
registered, so the "explicit confirmation" path that ADR-0022
§EXTERNAL_EFFECT mandates is dormant, and this module deliberately
builds no confirmation surface ahead of it. When the first
``EXTERNAL_EFFECT`` tool ships, the web side will gain a
``confirmation_required`` SSE event whose Approve/Deny resolves an
``asyncio.Future`` parked on ``app.state.pending_confirmations``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from core.exceptions import CaseClosedError, CaseStateInvalid, CaseWorkingFileInvalid
from core.models.watchpoint import WATCHPOINT_FAMILIES
from core.repositories import ScopedSettingRepository
from core.repositories._case_common import _MUTABLE_STATES
from core.repositories._session import tenant_context
from core.repositories.case_repository import CaseDTO, CaseRepository
from core.repositories.case_working_file_repository import (
    CaseProposalRepository,
    CaseThreadRepository,
)
from core.repositories.irene_briefing_repository import (
    IreneBriefingDTO,
    IreneBriefingRepository,
)
from core.repositories.irene_finding_repository import (
    IreneFindingDTO,
    IreneFindingRepository,
)
from core.repositories.irene_schedule_repository import IreneScheduleRepository
from core.repositories.user_repository import UserRepository
from services.ai_models import (
    Attachment,
    Conversation,
    Message,
    MessageRole,
    ToolCall,
)
from services.ai_service_core import (
    AIServiceCore,
    ResolvedLLM,
    SynthesisResult,
    _StopTokenStripper,
    get_ai_service_core,
)
from services.auth.session import SessionDTO
from services.cases.file_text import file_user_ids, render_case_file_text
from services.cases.proposals import (
    CASE_DRAFT_SYSTEM_PROMPT,
    CASE_DRAFT_TOOL,
    PROPOSAL_SYSTEM_PROMPT,
    PROPOSAL_TOOL,
    PROPOSAL_TOOL_NAME,
    conversation_text,
    draft_from_tool_calls,
    draft_messages,
    items_from_tool_calls,
    proposal_messages,
)
from services.cases.system_lines import job_landed, job_requested
from services.investments.credential_resolver import (
    CredentialResolver,
    CredentialUnavailableError,
    ProviderCredential,
)
from services.irene.briefing_material import MATERIAL_VERSION
from services.tools._tool_context import ToolExecutionContext
from services.vision_capabilities import (
    ALLOWED_IMAGE_MIME_TYPES,
    MAX_IMAGE_BYTES,
    supports_vision,
)
from services.voice import (
    EmptyTranscriptError,
    ResolvedSTT,
    ResolvedTTS,
    UnsupportedAudioFormatError,
    VoiceConfigurationError,
    VoiceError,
    assemble_stt,
    assemble_tts,
    build_stt,
    build_tts,
)
from services.voice_call import (
    CALL_TOOL_ALLOWLIST,
    DEFAULT_LANGUAGE,
    MAX_DURATION_SECONDS,
    SPEAK_TOOL,
    SPEAK_TOOL_CHOICE,
    SPEAKABLE_SYSTEM_PROMPT,
    CallConfigurationError,
    CallJob,
    CallJobStore,
    CallProvider,
    CallProviderError,
    ElevenLabsCallProvider,
    ResolvedCall,
    assemble_call,
    build_agent_definition,
    fallback_for,
    speakable_from_tool_calls,
    speakable_messages,
)
from web.auth import require_session, verify_csrf
from web.brief_stash import (
    BRIEF_KIND_CASE,
    BRIEF_KIND_WATCH_BRIEFING,
    ActiveBrief,
    _clear_active_brief,
    _get_active_brief,
    _set_active_brief,
)
from web.case_keep import (
    BRIEF_CHANGED_EVENT,
    CASE_WRITE_KIND_LABELS,
    KEEP_CASE_GONE_MESSAGE,
    KEEP_THREAD_GONE_MESSAGE,
    UNDO_ALREADY_REMOVED_MESSAGE,
    KeepTarget,
    case_write_href,
    consultation_caption,
    keep_artifact,
    keep_trigger,
    kept_line_context,
    picker_options,
    posted_target,
    resolve_keep_target,
    target_label,
    undo_case_write,
    undo_keep,
)
from web.errors import user_safe_error
from web.permissions import require_role

# The case brief is the working file as text (ADR-0138 §10, D-CS-B-108),
# composed in the services layer (``services/cases/file_text.py``) — the same
# function ``get_case`` reads — so only the name resolution is borrowed from
# the Cases route, and the draft form's template name (``POST /api/cases/new``
# re-renders the same form on a blank title). The import is one-directional:
# ``cases.py``'s own import chain (``watch_desk``, ``planning_desk``) never
# reaches ``chat.py``, so there is no cycle. ``cases.py`` therefore cannot import back the assistants URL — it
# holds its own ``CONSULT_SHIRLEY_URL`` constant.
from web.routes.cases import CASE_DRAFT_TEMPLATE, _resolve_owner_names

# The watch-briefing banner and brief block state a check in the words the
# Watch Desk uses (ADR-0133 §6): the family display names, the schedule-zone
# stamp and the Journal deep link are read from the Watch Desk route module.
# One-directional like the import above — ``watch_desk.py`` imports nothing
# from ``web.routes`` — which is why its partials write the hand-off URL
# literally, as they write ``/resolve``.
from web.routes.watch_desk import (
    JOURNAL_DEEP_LINK,
    _FAMILY_DISPLAY_NAMES,
    _resolve_zone,
    _stamp,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# In-memory pending-turns store
# ---------------------------------------------------------------------------

_PENDING_TURNS_LIMIT = 100


def _pending_turns(request: Request) -> OrderedDict[tuple[str, str], dict[str, Any]]:
    """Return (and lazily initialise) the per-app pending-turns store.

    Keyed by ``(session_id, turn_id)``. Bounded LRU — when the limit is
    hit the oldest entry is evicted. Single-worker only; a multi-worker
    deployment needs Redis or equivalent (Phase-5 follow-up).

    Args:
        request: The FastAPI request whose ``app.state`` carries the
            store.

    Returns:
        The ``OrderedDict`` instance attached to ``app.state``.
    """
    store = getattr(request.app.state, "pending_turns", None)
    if store is None:
        store = OrderedDict()
        request.app.state.pending_turns = store
    return cast("OrderedDict[tuple[str, str], dict[str, Any]]", store)


def _stash_turn(
    request: Request,
    session_id: str,
    turn_id: str,
    user_message: str,
    model_id: str | None,
) -> None:
    """Insert a turn into the pending-turns store, evicting the LRU
    entry when the bounded capacity is reached.

    Args:
        request: The FastAPI request whose store is mutated.
        session_id: The owning session's UUID (string form).
        turn_id: Newly allocated turn id (UUID, string form).
        user_message: The exact text the user submitted.
        model_id: The active Shirley model, or ``None`` if unset.
    """
    store = _pending_turns(request)
    while len(store) >= _PENDING_TURNS_LIMIT:
        evicted_key, _ = store.popitem(last=False)
        logger.warning(
            "chat: evicting pending turn %s (LRU cap %d)",
            evicted_key,
            _PENDING_TURNS_LIMIT,
        )
    store[(session_id, turn_id)] = {
        "user_message": user_message,
        "model_id": model_id,
        "created_at": time.time(),
    }


def _pop_turn(request: Request, session_id: str, turn_id: str) -> dict[str, Any] | None:
    """Remove and return the stashed turn, or ``None`` if not found.

    Args:
        request: The FastAPI request whose store is read.
        session_id: The owning session's UUID string form.
        turn_id: Turn id from the URL.

    Returns:
        The stashed metadata dict, or ``None`` when the key is absent.
    """
    store = _pending_turns(request)
    return store.pop((session_id, turn_id), None)


# ---------------------------------------------------------------------------
# In-memory chat-history store (ADR-0050)
# ---------------------------------------------------------------------------

_CHAT_HISTORIES_LIMIT = 100
_HISTORY_MAX_MESSAGES = 20
_HISTORY_MAX_CHARS = 24_000


def _chat_histories(request: Request) -> OrderedDict[str, Conversation]:
    """Return (and lazily initialise) the per-app chat-history store.

    Keyed by the authenticated session UUID (string). Bounded LRU at
    the session level — when a 101st session tries to start a chat,
    the oldest session's history is evicted. Per-history message
    trimming is applied at append time, not here.

    Single-worker only; a multi-worker deployment needs Redis or
    equivalent (ADR-0050 migration trigger).

    Args:
        request: The FastAPI request whose ``app.state`` carries the
            store.

    Returns:
        The ``OrderedDict`` instance attached to ``app.state``.
    """
    store = getattr(request.app.state, "chat_histories", None)
    if store is None:
        store = OrderedDict()
        request.app.state.chat_histories = store
    return cast("OrderedDict[str, Conversation]", store)


def _get_or_create_history(request: Request, session_id: str) -> Conversation:
    """Return the session's history, creating an empty one if absent.

    LRU-touches the entry so recent activity stays warm. Evicts the
    oldest entry when the per-app cap is reached.

    Args:
        request: The FastAPI request whose store is mutated.
        session_id: The owning session's UUID (string form).

    Returns:
        The :class:`Conversation` owned by this session.
    """
    store = _chat_histories(request)
    if session_id in store:
        store.move_to_end(session_id)
        return store[session_id]
    while len(store) >= _CHAT_HISTORIES_LIMIT:
        evicted_key, _ = store.popitem(last=False)
        # The chart-artefact sidecar (ADR-0114) and the call-line sidecar
        # (ADR-0137 §5) ride the same eviction: one lifecycle, three stores.
        _chart_artifacts(request).pop(evicted_key, None)
        _call_lines(request).pop(evicted_key, None)
        logger.warning(
            "chat: evicting chat history for session %s (LRU cap %d)",
            evicted_key,
            _CHAT_HISTORIES_LIMIT,
        )
    conversation = Conversation()
    store[session_id] = conversation
    return conversation


def _drop_history(request: Request, session_id: str) -> None:
    """Remove the session's history. Safe on absent keys.

    The chart-artefact sidecar (ADR-0114) and the call-line sidecar
    (ADR-0137 §5) are dropped in the same operation: the stores share one
    lifecycle, so "new chat" and logout never leave archived specs or call
    lines behind a conversation that no longer exists.

    Args:
        request: The FastAPI request whose store is mutated.
        session_id: The owning session's UUID (string form).
    """
    _chat_histories(request).pop(session_id, None)
    _chart_artifacts(request).pop(session_id, None)
    _call_lines(request).pop(session_id, None)


def _trim_history(
    conversation: Conversation,
    artifacts: list[dict[str, Any]] | None = None,
    call_lines: list[dict[str, Any]] | None = None,
) -> None:
    """Trim ``conversation`` to fit the per-history bounds.

    Role-safe FIFO eviction: drops whole turn-groups starting at the
    oldest user message. A turn-group is one contiguous span from a
    ``user`` message up to (but not including) the next ``user``
    message (or end-of-history). Dropping a whole group preserves the
    OpenAI invariant that every ``tool`` message is preceded by an
    ``assistant`` message carrying the matching ``tool_call_id``.

    The most recent user message is never evicted, even if it alone
    exceeds the character cap; a warning is logged in that case.
    System messages are not in the history (they are injected
    per-turn) so position 0 needs no special case.

    Args:
        conversation: The conversation whose ``messages`` list is
            mutated in place.
        artifacts: The session's chart-artefact sidecar (ADR-0114), when
            it has one. Records anchored to an evicted turn-group are
            dropped in the *same* operation, so an archived spec never
            outlives the assistant message it belongs to and orphaned
            specs cannot accumulate. ``None`` trims the history alone.
        call_lines: The session's call-line sidecar (ADR-0137 §5), when it
            has one. A line anchored to an evicted message goes with its
            turn-group, like that group's charts; a line anchored at ``""``
            (before everything) survives. ``None`` leaves no lines to trim.
    """

    def _char_count() -> int:
        return sum(len(m.content or "") for m in conversation.messages)

    while True:
        if (
            len(conversation.messages) <= _HISTORY_MAX_MESSAGES
            and _char_count() <= _HISTORY_MAX_CHARS
        ):
            return

        # Find the index of the second user message — the boundary
        # between the oldest turn-group and the next one.
        user_indices = [
            i for i, m in enumerate(conversation.messages) if m.role == MessageRole.USER
        ]
        if len(user_indices) < 2:
            # Only the current user message (or fewer) remains; cannot
            # safely evict further. Log a warning if the cap is still
            # exceeded — the API can usually still handle one oversized
            # user message; the limits are precautionary.
            if (
                len(conversation.messages) > _HISTORY_MAX_MESSAGES
                or _char_count() > _HISTORY_MAX_CHARS
            ):
                logger.warning(
                    "chat: history exceeds caps but only one user "
                    "turn-group remains (messages=%d, chars=%d).",
                    len(conversation.messages),
                    _char_count(),
                )
            return

        boundary = user_indices[1]
        evicted_ids = {m.id for m in conversation.messages[:boundary]}
        del conversation.messages[:boundary]
        if artifacts is not None:
            artifacts[:] = [a for a in artifacts if a["message_id"] not in evicted_ids]
        if call_lines is not None:
            call_lines[:] = [
                line for line in call_lines if line["after_message_id"] not in evicted_ids
            ]


# ---------------------------------------------------------------------------
# Per-session chart-artefact sidecar (ADR-0114)
# ---------------------------------------------------------------------------
#
# Shirley's Plotly specs are archived **parallel to** the chat history, never
# inside it: the conversation is the LLM-bound record, and a spec that entered
# it would ride back into the model's token stream on every later turn —
# exactly what ``ai_service_core`` strips before the tool message. The sidecar
# is keyed like ``chat_histories`` and shares its lifecycle verbatim: "new
# chat", logout and the session-level LRU drop the two together, the
# turn-group trim evicts their records together, and a server restart loses
# both (ADR-0050's contract for prose, unchanged).
#
# One record per archived chart::
#
#     {"artifact_id": "<12-hex>",       # the pin's transport handle
#      "message_id":  "<Message.id>",   # the assistant message it belongs to
#      "spec":        <Plotly spec dict | None>,
#      "caption":     "<chart caption>",
#      "created_at":  <epoch seconds>,
#      "oversized":   <bool>}
#
# ``spec`` is ``None`` exactly when ``oversized`` is True: the serialised spec
# exceeded ``_CHART_SPEC_BYTE_CAP`` and was not archived. The record survives
# so rehydration renders the calm placeholder (ADR-0114 §3) rather than
# silently dropping a chart the user saw; the live SSE stream is unaffected —
# degrade, never refuse the live render.

#: Serialised-spec ceiling for archival, a memory guard in the ``_DATA_ROW_CAP``
#: tradition (ADR-0114 §3) — not a token-budget concern (the spec never reaches
#: the model). Typical specs are 30–60 KB; only ``portfolio_nav_series`` near
#: its row cap can produce multi-megabyte figures.
_CHART_SPEC_BYTE_CAP: int = 1 * 1024 * 1024


def _chart_artifacts(request: Request) -> OrderedDict[str, list[dict[str, Any]]]:
    """Return (and lazily initialise) the per-app chart-artefact sidecar.

    Keyed by the authenticated session UUID (string), like
    :func:`_chat_histories`, and bounded by that store's eviction rather than
    a cap of its own — the sidecar never outlives the history it accompanies.

    Args:
        request: The FastAPI request whose ``app.state`` carries the store.

    Returns:
        The ``OrderedDict`` instance attached to ``app.state``.
    """
    store = getattr(request.app.state, "chat_chart_artifacts", None)
    if store is None:
        store = OrderedDict()
        request.app.state.chat_chart_artifacts = store
    return cast("OrderedDict[str, list[dict[str, Any]]]", store)


def _stored_chart_artifacts(request: Request, session_id: str) -> list[dict[str, Any]] | None:
    """Return the session's artefact records, or ``None`` when it has none.

    A read never creates the entry: a session that never asked for a chart
    carries no sidecar at all.
    """
    return _chart_artifacts(request).get(session_id)


def _ensure_chart_artifacts(request: Request, session_id: str) -> list[dict[str, Any]]:
    """Return the session's artefact records, creating the list on first use."""
    return _chart_artifacts(request).setdefault(session_id, [])


def _spec_byte_length(spec: Any) -> int:
    """Return the serialised JSON byte length of ``spec``.

    The cap is stated in bytes of the artefact as it would be stored, so the
    measurement is the serialisation itself. An unserialisable spec (never
    produced by ``render_chart``, but the sidecar must not raise inside the
    SSE generator) measures as oversized and is therefore not archived.
    """
    try:
        return len(json.dumps(spec).encode("utf-8"))
    except (TypeError, ValueError):
        logger.warning("chat: chart spec is not JSON-serialisable — not archived.")
        return _CHART_SPEC_BYTE_CAP + 1


def _new_chart_record(spec: Any, caption: str) -> dict[str, Any]:
    """Build one sidecar record for a freshly rendered Plotly artefact.

    ``message_id`` is filled in later, at ``stream_finished``, when the
    assistant message this chart belongs to exists (see
    :func:`_bind_chart_artifacts`). The id follows the :class:`Message` idiom —
    a 12-hex UUID slice — so both handles read alike on the wire.
    """
    oversized = _spec_byte_length(spec) > _CHART_SPEC_BYTE_CAP
    if oversized:
        logger.info(
            "chat: chart spec exceeds the %d-byte archival cap — streamed live, not archived.",
            _CHART_SPEC_BYTE_CAP,
        )
    return {
        "artifact_id": uuid.uuid4().hex[:12],
        "message_id": "",
        "spec": None if oversized else spec,
        "caption": caption,
        "created_at": time.time(),
        "oversized": oversized,
    }


def _bind_chart_artifacts(
    request: Request,
    session_id: str,
    pending: list[dict[str, Any]],
    message_id: str,
) -> list[dict[str, Any]] | None:
    """Anchor a turn's captured charts to that turn's assistant message.

    The spec is at hand in the SSE ``chart_artifact`` branch and nowhere else,
    but the assistant message that carries it only exists at
    ``stream_finished`` — so the turn's records are buffered locally and
    anchored here, to the very :class:`Message` id the recorder just appended
    to the history. That id is also what the ``done`` frame carries, so the
    chat surface, the sidecar and the pin dialog all speak of the same message.

    A turn that produced no assistant message (a bare error) anchors nothing:
    its charts are dropped, mirroring ADR-0050's rule that assistant content is
    not recorded on the error path.

    Args:
        request: The active request (owns the store).
        session_id: The owning session's UUID (string form).
        pending: This turn's records, in emission order.
        message_id: The final assistant message's id, or ``""``.

    Returns:
        The session's sidecar list when one exists — ready to hand to
        :func:`_trim_history` — or ``None``.
    """
    if pending and message_id:
        records = _ensure_chart_artifacts(request, session_id)
        for record in pending:
            record["message_id"] = message_id
            records.append(record)
    elif pending:
        logger.info(
            "chat: %d chart artefact(s) not archived — the turn produced no assistant message.",
            len(pending),
        )
    return _stored_chart_artifacts(request, session_id)


def _find_chart_artifact(
    request: Request,
    session_id: str,
    artifact_id: str,
) -> dict[str, Any] | None:
    """Return the session's record for ``artifact_id``, or ``None``.

    The lookup is session-scoped by construction — the store is keyed by
    session — so a handle from another session never resolves, the way the
    pending-turns store makes cross-session turn ids unusable.
    """
    if not artifact_id:
        return None
    for record in _stored_chart_artifacts(request, session_id) or []:
        if record["artifact_id"] == artifact_id:
            return record
    return None


def _history_items(
    conversation: Conversation,
    artifacts: list[dict[str, Any]],
    call_lines: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Interleave prose bubbles, chart artefacts and call lines in message order.

    The prose rule is ADR-0050's, unchanged: only ``user`` and final
    ``assistant`` messages with content and no tool calls become bubbles — the
    user does not see tool-call internals. Each message is then followed by the
    charts anchored to it, so a chart-only turn (an assistant message with an
    empty prose body) still rehydrates its figure, and no empty bubble is
    invented above it.

    The call lines (ADR-0137 §5) are history furniture, not conversation: the
    ``""``-anchored ones come first, and every other one follows the message
    it is anchored to, after that message's charts, in the order the lines
    were appended. A message bubble carries its ``channel`` so a call's
    bubbles wear the "Call" chip.

    Args:
        conversation: The session's history.
        artifacts: The session's sidecar records.
        call_lines: The session's call-line records.

    An assistant bubble carries its ``message_id`` — the handle Keep posts
    (ADR-0138 §8, D-CS-B-95), so a reloaded answer is keepable like a live
    one; a user bubble carries ``""``.

    Returns:
        Render items — ``{"kind": "message", role, content, channel,
        message_id}``, ``{"kind": "chart", artifact_id, caption, spec,
        oversized}`` and ``{"kind": "call_line", content}`` — in the order
        the surface shows them.
    """
    anchored: dict[str, list[dict[str, Any]]] = {}
    for record in artifacts:
        anchored.setdefault(record["message_id"], []).append(record)
    lines_after: dict[str, list[dict[str, Any]]] = {}
    for line in call_lines:
        lines_after.setdefault(line["after_message_id"], []).append(line)

    items: list[dict[str, Any]] = [_call_line_item(line) for line in lines_after.get("", [])]
    for message in conversation.messages:
        if (
            message.role in (MessageRole.USER, MessageRole.ASSISTANT)
            and message.content
            and not message.tool_calls
        ):
            items.append(
                {
                    "kind": "message",
                    "role": message.role.value,
                    "content": message.content,
                    "channel": message.channel,
                    "message_id": message.id if message.role == MessageRole.ASSISTANT else "",
                }
            )
        for record in anchored.get(message.id, []):
            items.append(
                {
                    "kind": "chart",
                    "artifact_id": record["artifact_id"],
                    "caption": record["caption"],
                    "spec": record["spec"],
                    "oversized": record["oversized"],
                }
            )
        items.extend(_call_line_item(line) for line in lines_after.get(message.id, []))
    return items


# ---------------------------------------------------------------------------
# Per-session consultation brief (ADR-0107 C6; ADR-0133 §6) — the loop's
# first half
# ---------------------------------------------------------------------------
#
# A case can send the PM to Shirley "consulting for" it, and a written Watch
# Desk briefing can be handed to her in place for discussion. Either is a
# per-session stash — the same ``app.state`` idiom as the chat-history store,
# and, like it, ephemeral (ADR-0050): no persistence, no ``ai_service_core``
# change. The stash stores the *kind and row id*, not a rendered brief, so
# every turn briefs from the live row: a case that closed or vanished since,
# or a briefing that vanished, clears the stash silently and the turn runs
# unbriefed. One context is active at a time — a briefing replaces a case and
# a case replaces a briefing; dismiss clears either. Web chat only — the
# Telegram surface is untouched (it has neither the banner nor the keep loop a
# brief presumes), so its prompt assembly never sees this. The stash
# primitives live in ``web/brief_stash.py`` (P-CS-4a), which the Planning
# Desk's Keep reads too; a case stash is also the session's keep target
# (ADR-0138 §8, D-CS-B-86).
#
# The two brief block formats (below) are the one place each is defined; the
# strands' closure notes reference them.

#: The in-place hand-off endpoint (ADR-0133 §6, D-WD-23): the Watch Desk's
#: "Discuss briefing with Shirley" action POSTs here with the briefing id in
#: the path. Its partials write the URL literally — ``watch_desk.py`` cannot
#: import this module (see the import note above).
DISCUSS_BRIEFING_URL: str = "/chat/brief/watch-briefing/{briefing_id}"

#: The case page's in-place hand-off (ADR-0138 §10, D-CS-B-110 — the D-WD-23
#: idiom): *Consult Shirley* POSTs here with the case id in the path; the case
#: page reads the URL as ``consult_shirley_post`` (``web/routes/cases.py``
#: writes it literally — it cannot import this module).
DISCUSS_CASE_URL: str = "/chat/brief/case/{case_id}"

#: The banner re-render endpoint: the dock's banner container fetches it on
#: ``pf:brief-changed`` so an in-place hand-off updates a dock that is
#: already open.
BRIEF_BANNER_URL: str = "/chat/brief/banner"

#: The ``HX-Trigger`` value a successful hand-off carries: ``shirley.js``
#: opens the dock on ``pf:shirley-dock``; ``shirley_dock.html`` re-renders
#: the banner on ``pf:brief-changed``. Neither fires on a dropped id.
_DISCUSS_TRIGGER: str = "pf:brief-changed, pf:shirley-dock"

#: The brief-dismiss endpoint — clears the stash and removes the banner.
DISMISS_BRIEF_URL: str = "/chat/brief/dismiss"

#: The Cases area page the empty picker links out to (no open or waiting case
#: to keep into — point the PM at Cases rather than a dead dropdown).
_CASES_AREA_URL: str = "/cases"

#: The Keep endpoints (ADR-0138 §8 channel 2, §9): the two gestures under an
#: answer or a figure, the picker, the write, and the kept line's Undo
#: (D-CS-B-99).
KEEP_URL: str = "/api/chat/keep"
KEEP_ACTIONS_URL: str = "/api/chat/keep/actions"
KEEP_PICKER_URL: str = "/api/chat/keep/picker"
KEEP_UNDO_URL: str = "/api/chat/keep/undo"

#: Shirley's writes (ADR-0138 §8 channel 3, §10; D-CS-B-115): the dock line a
#: ``case_write`` event becomes, and its Undo. The SSE event carries the Undo
#: URL and the name of the form field the CSRF token travels in.
CASE_WRITE_LINE_URL: str = "/api/chat/case-write/line"
CASE_WRITE_UNDO_URL: str = "/api/chat/case-write/undo"
_CSRF_FIELD: str = "csrf_token"

#: The dock's two proposal gestures (ADR-0138 §8, D-CS-B-123, D-CS-B-126):
#: *Summarise into the file* on the briefed case, and *Make this a case*.
PROPOSALS_SUMMARISE_URL: str = "/api/chat/proposals/summarise"
CASE_DRAFT_URL: str = "/api/chat/case-draft"

#: The kept class a Shirley answer becomes (ADR-0138 §9) — the payload is the
#: pin contract minus the comment: ``{"excerpt": <the message text>}``.
_CONSULTATION_ARTIFACT: str = "consultation"

#: The kept class a Shirley figure becomes (ADR-0114 §2, ADR-0138 §9) —
#: ``{"caption", "spec"}``, resolved from the session sidecar above.
_CHART_SNAPSHOT_ARTIFACT: str = "chart_snapshot"

_BRIEF_OPEN: str = "<<<PORTFOLIFLOW CASE BRIEF>>>"
_BRIEF_CLOSE: str = "<<<END PORTFOLIFLOW CASE BRIEF>>>"

#: The lead of the case brief (D-CS-B-108, D-CS-B-128 e): prompt text Shirley
#: reads, never rendered copy. The brief is the working file (ADR-0138 §10);
#: what reaches it does so by a PM gesture — Keep, a ``case_add`` the PM asks
#: for, or a proposal the PM accepts — never from the conversation by itself.
_BRIEF_LEAD: str = (
    "You are consulting on the case below — its working file as the PM sees it "
    "now. Ground your answer in it; cite the file's own figures and threads where "
    "they bear on the question. The conversation is ephemeral: the PM keeps what "
    "matters into the file with Keep, or asks you to add it with `case_add`; "
    "nothing is written without the PM's gesture."
)

#: Watch-briefing block delimiters (ADR-0133 §6) — the second fenced block
#: the chat call site can append; the same posture as the case brief.
_WATCH_BRIEF_OPEN: str = "<<<PORTFOLIFLOW WATCH DESK BRIEFING>>>"
_WATCH_BRIEF_CLOSE: str = "<<<END PORTFOLIFLOW WATCH DESK BRIEFING>>>"

#: The lead of the watch-briefing block: who wrote it, when its figures were
#: taken, and the two-clock rule (ADR-0133 §6). Prompt text Shirley reads,
#: never rendered copy — so the author is named here, while UI-023 keeps the
#: pages persona-free. ``{check}`` is the check's instant, ISO.
_WATCH_BRIEF_LEAD: str = (
    "The PM wants to discuss the Watch Desk briefing below. It was written by "
    "Irene, the Watch Desk's background analyst, at the check of {check}; its "
    "figures were taken at that check. Where your tools show different current "
    "figures, say which is the check's figure and which is today's. The "
    "briefing is immutable; nothing you say is written back to it."
)

#: The calm state an answer that left the session history resolves to — the
#: C6 "no longer available" posture, never a wrong excerpt.
_MESSAGE_UNAVAILABLE_MESSAGE: str = (
    "That answer is no longer available to keep — it has scrolled out of this "
    "session's memory. Ask again, then keep the fresh answer."
)

#: The calm state a stale, evicted or never-archived chart handle resolves to —
#: the C6 "no longer available" posture (ADR-0114 §3), never a wrong figure.
_CHART_UNAVAILABLE_MESSAGE: str = (
    "That chart is no longer available to keep — it has scrolled out of this "
    "session's memory. Ask Shirley for it again, then keep the fresh figure."
)

#: An oversized figure was streamed live but never archived, so there is no
#: spec to keep in the case file. Guidance, not a bare refusal.
_CHART_OVERSIZED_MESSAGE: str = (
    "This chart is too large to keep. Ask Shirley for a narrower range or a "
    "single investment, then keep that figure."
)


def _active_case_id(request: Request, session_id: str) -> UUID | None:
    """Return the stashed case id, or ``None`` when no *case* brief is active.

    A briefing stash names no case — a briefing is not a case, and nothing is
    kept into one (ADR-0133 §6); :func:`web.case_keep.resolve_keep_target`
    answers the same for Keep.
    """
    brief = _get_active_brief(request, session_id)
    if brief is None or brief.kind != BRIEF_KIND_CASE:
        return None
    return brief.id


def _parse_case_marker(raw: str | None) -> UUID | None:
    """Parse the ``?case=`` marker to a UUID, or ``None`` for anything unusable.

    A malformed or absent marker is dropped silently (binding decision 5) — a
    stale or hand-edited link never errors. Whether that case exists and is open
    is a DB question answered by the caller.
    """
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


async def _watch_briefing_banner(
    request: Request, db: Any, session_id: str, briefing_id: UUID
) -> dict[str, str] | None:
    """Return the banner context for a stashed briefing, validated fresh.

    The row is read in the caller's tenant context; one that vanished, or is
    not ``written``, clears the stash silently and yields no banner — the
    closed-case posture. The stamp is the check's ``beat_at`` in the
    schedule's zone, the Briefing view's own idiom, and the link is the
    Journal, where every check lives. Persona-free (UI-023): the banner
    names the check, not its writer.
    """
    row = await IreneBriefingRepository(db).get(briefing_id)
    if row is None or row.status != "written":
        _clear_active_brief(request, session_id)
        return None
    schedule = await IreneScheduleRepository(db).get_for_tenant()
    zone, utc_fallback = _resolve_zone(schedule.timezone if schedule is not None else None)
    return {
        "kind": BRIEF_KIND_WATCH_BRIEFING,
        "stamp": _stamp(row.beat_at, "%a %d %b %Y %H:%M", zone=zone, utc_fallback=utc_fallback),
        "href": JOURNAL_DEEP_LINK,
    }


async def resolve_active_brief_banner(
    request: Request, session: SessionDTO, case_marker: str | None
) -> dict[str, str] | None:
    """Adopt a fresh ``?case=`` marker and return the banner context, or ``None``.

    Marker hygiene (binding decision 5): a valid **open or waiting** case
    (D-CS-B-93) replaces any previous stash — a stashed briefing included;
    a malformed, unknown, foreign-tenant (RLS → ``None``) or **closed**
    marker is dropped silently — no banner, no stash change, no error
    (consulting *about* a closed case is legitimate reading, but the guided
    context is for cases that still take writes). A marker naming the case
    already stashed keeps the stash's thread. The banner is then rendered
    from the current stash, **validated fresh**: a stale (closed-since /
    vanished) stash clears silently and yields no banner, and a thread
    removed since reads as the file. Its ``target`` names where Keep writes
    (ADR-0138 §8, D-CS-B-86). A stashed briefing (ADR-0133 §6) renders
    through :func:`_watch_briefing_banner`. Called by the assistants area
    page render, the dock and the banner endpoint.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return None
    session_id = str(session.id)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        repo = CaseRepository(db)
        case: CaseDTO | None = None
        stashed = _get_active_brief(request, session_id)
        marker_id = _parse_case_marker(case_marker)
        if marker_id is not None:
            candidate = await repo.get(marker_id)
            if candidate is not None and candidate.state in _MUTABLE_STATES:
                keeps_thread = (
                    stashed is not None
                    and stashed.kind == BRIEF_KIND_CASE
                    and stashed.id == candidate.id
                )
                stashed = ActiveBrief(
                    BRIEF_KIND_CASE,
                    candidate.id,
                    stashed.thread_id if keeps_thread and stashed is not None else None,
                )
                _set_active_brief(request, session_id, stashed)
                case = candidate
        if case is None:
            if stashed is not None and stashed.kind == BRIEF_KIND_WATCH_BRIEFING:
                return await _watch_briefing_banner(request, db, session_id, stashed.id)
            if stashed is not None:
                candidate = await repo.get(stashed.id)
                if candidate is not None and candidate.state in _MUTABLE_STATES:
                    case = candidate
                else:  # stale stash — the case closed or vanished since.
                    _clear_active_brief(request, session_id)
        if case is None or stashed is None:
            return None
        target = await resolve_keep_target(db, brief=stashed, marker_case=case)
    return {
        "kind": BRIEF_KIND_CASE,
        "badge": f"CASE-{case.case_number:04d}",
        "title": case.title,
        "href": f"/cases/{case.id}",
        "target": target_label(target.thread if target is not None else None),
    }


def _format_brief(text: str) -> str:
    """Render the fenced case-brief block: the lead, then the working file (D-CS-B-108).

    ``text`` is :func:`services.cases.file_text.render_case_file_text` — the
    file as the PM sees it, the same text ``get_case`` returns, bounded there
    (D-CS-B-107); it opens on the case's badge and title, so the block needs
    no head of its own. The exact format lives here, in one place; the
    closure notes reference it.
    """
    return "\n".join((_BRIEF_OPEN, _BRIEF_LEAD, text, _BRIEF_CLOSE))


async def _case_file_text(db: Any, case: CaseDTO) -> str:
    """The working file as text, its users named (ADR-0138 §10, D-CS-B-108).

    The users the file names are resolved in one batch
    (:func:`_resolve_owner_names` over
    :func:`~services.cases.file_text.file_user_ids`) and handed to the one
    composition the page's readers share. An open or waiting case is
    composed with Where we stand read now. The case brief and the
    *Summarise into the file* one-shot read the same text.
    """
    names = await _resolve_owner_names(UserRepository(db), await file_user_ids(db, case))
    return await render_case_file_text(db, case, now=datetime.now(timezone.utc), user_names=names)


async def _compose_case_brief(db: Any, case: CaseDTO) -> str:
    """Compose the case brief: the working file as text (ADR-0138 §10, D-CS-B-108).

    :func:`_case_file_text` inside the fenced block; the caller has already
    dropped a closed or vanished stash.
    """
    return _format_brief(await _case_file_text(db, case))


def _stated(value: object) -> str:
    """State a persisted scalar of the material, or a dash for an absent one."""
    return str(value) if value not in (None, "") else "—"


def _figure_line(figure: Mapping[str, Any]) -> str:
    """State one figure of the check on one line (ADR-0133 §6).

    Label, value and unit first, then whatever the record carries — a
    ceiling and utilisation for a limit subject, a threshold for a signal,
    the window for a market move — the as-of date, and the source token.
    Nothing is computed; the persisted strings are repeated as written.
    """
    label = figure.get("label") or figure.get("id") or "figure"
    family = figure.get("family")
    head = f"{label} ({family})" if family else str(label)
    unit = str(figure.get("unit") or "")
    value = figure.get("value")
    stated = f"{value} {unit}".strip() if value is not None else "no figure"
    parts = [f"{head}: {stated}"]
    if figure.get("max_pct") is not None:
        parts.append(
            f"ceiling {figure['max_pct']} pct, utilisation {_stated(figure.get('utilisation_pct'))} pct"
        )
    if figure.get("threshold") is not None:
        parts.append(f"threshold {figure['threshold']} {unit}".rstrip())
    if figure.get("status"):
        parts.append(f"status {figure['status']}")
    if figure.get("window_days"):
        window = f"{figure['window_days']}-day window"
        if figure.get("window_start"):
            window += f" from {figure['window_start']}"
        parts.append(window)
    parts.append(f"as of {_stated(figure.get('as_of'))}")
    if figure.get("muted"):
        parts.append("muted")
    if figure.get("no_data_reason"):
        parts.append(f"no data: {figure['no_data_reason']}")
    if figure.get("source"):
        parts.append(f"source {figure['source']}")
    return "  - " + ", ".join(parts)


def _cluster_line(cluster: Mapping[str, Any]) -> str:
    """State one press cluster of the check on one line: tag, day, titles, sources."""
    items = [item for item in cluster.get("items") or () if isinstance(item, Mapping)]
    titles = "; ".join(
        f'"{item.get("title") or item.get("url") or "untitled"}" '
        f"({item.get('source') or 'unknown source'}"
        + (f", {item['url']}" if item.get("url") else "")
        + ")"
        for item in items
    )
    tag = cluster.get("tag") or "untagged"
    return f"  - {tag} cluster of {_stated(cluster.get('day'))}: {titles or 'no items'}"


def _material_lines(inputs: Mapping[str, Any]) -> list[str]:
    """State the figures and clusters of version-1 material, one line each."""
    markets = inputs.get("markets") or {}
    watch = inputs.get("watch") or {}
    figures = [
        figure
        for figure in (
            *markets.get("instruments", ()),
            *markets.get("asset_classes", ()),
            *markets.get("currencies", ()),
            *(
                subject
                for group in watch.get("limits", ())
                for subject in group.get("subjects", ())
            ),
            *watch.get("signals", ()),
        )
        if isinstance(figure, Mapping)
    ]
    clusters = [
        cluster for cluster in markets.get("press_clusters", ()) if isinstance(cluster, Mapping)
    ]
    lines = ["Figures of that check:" if figures else "Figures of that check: none"]
    lines.extend(_figure_line(figure) for figure in figures)
    lines.append(
        "Press clusters of that check:" if clusters else "Press clusters of that check: none"
    )
    lines.extend(_cluster_line(cluster) for cluster in clusters)
    return lines


def _families_off_line(inputs: Mapping[str, Any]) -> str:
    """Name the watch points switched off at the check — what the writer saw.

    ``WATCHPOINT_FAMILIES`` minus the material's ``families_on``: no second
    resolution, so the line states the check's own scope even after the
    switches changed (the D-WD-17 principle carried into the conversation).
    """
    watch = inputs.get("watch") or {}
    families_on = watch.get("families_on")
    if not isinstance(families_on, list):
        return "Watch points switched off at that check: not stated in this material."
    off = [
        f"{_FAMILY_DISPLAY_NAMES.get(family, family)} ({family})"
        for family in WATCHPOINT_FAMILIES
        if family not in families_on
    ]
    if not off:
        return "Watch points switched off at that check: none."
    return (
        "Watch points switched off at that check: "
        + ", ".join(off)
        + " — the check did not evaluate them; never claim it watched them."
    )


def _finding_line(finding: IreneFindingDTO) -> str:
    """State one finding of the check: band, subject, trigger, finding, resolution now."""
    payload = finding.payload or {}
    trigger = payload.get("trigger")
    text = payload.get("finding")
    parts = [f"{finding.band} · {finding.subject_key}"]
    if isinstance(trigger, str) and trigger:
        parts.append(trigger)
    if isinstance(text, str) and text:
        parts.append(text)
    parts.append(f"resolution now: {finding.resolution}")
    return "  - " + " — ".join(parts)


def _format_watch_briefing_brief(row: IreneBriefingDTO, findings: Sequence[IreneFindingDTO]) -> str:
    """Render the fenced watch-briefing block (ADR-0133 §6) from the immutable row.

    The lead names the writer and the check and states the two-clock rule;
    then the check's instant, both texts, the figures and clusters of the
    persisted ``inputs`` one line each, the watch points off at that check,
    and the findings the check raised. The material is read by its
    ``version``: a shape this build does not know is handed over as its
    persisted JSON rather than guessed at. The exact format lives here, in
    one place; the strand's closure note references it.
    """
    check = row.beat_at.isoformat()
    inputs = row.inputs if isinstance(row.inputs, Mapping) else {}
    written = row.written_at.isoformat()
    model = f", model {row.model_id}" if row.model_id else ""
    lines = [
        _WATCH_BRIEF_OPEN,
        _WATCH_BRIEF_LEAD.format(check=check),
        f"Check: {check} (briefing written {written}{model})",
        f"Markets and press: {row.text_markets or ''}",
        "Under observation: "
        + (row.text_watch or "(empty — no watch point was switched on at that check)"),
    ]
    if inputs.get("version") == MATERIAL_VERSION:
        lines.extend(_material_lines(inputs))
        lines.append(_families_off_line(inputs))
    else:
        lines.append(
            f"Material of that check (version {inputs.get('version')!r}, a shape this build "
            "does not know), as persisted:"
        )
        lines.append("  " + json.dumps(inputs, sort_keys=True, default=str))
        lines.append(
            "Watch points switched off at that check: not stated in this material version."
        )
    lines.append("Findings of that check:" if findings else "Findings of that check: none")
    lines.extend(_finding_line(finding) for finding in findings)
    lines.append(_WATCH_BRIEF_CLOSE)
    return "\n".join(lines)


async def _compose_watch_briefing_brief(db: Any, row: IreneBriefingDTO) -> str:
    """Compose the watch-briefing block from the row and its findings.

    The findings are the row's own ``finding_ids``, read fresh in the
    caller's tenant context so each line can state its resolution as it
    stands now; an id that no longer resolves is skipped. The texts and the
    material are the row's — immutable, never re-read live.
    """
    repo = IreneFindingRepository(db)
    findings: list[IreneFindingDTO] = []
    for finding_id in row.finding_ids:
        finding = await repo.get(finding_id)
        if finding is not None:
            findings.append(finding)
    return _format_watch_briefing_brief(row, findings)


async def _briefed_system_prompt(request: Request, session: SessionDTO, base_prompt: str) -> str:
    """Return the system prompt, brief-appended when a stash is active.

    Loads the stashed row fresh: a case that vanished or closed since, or a
    briefing that vanished (stale stash), clears the stash silently and the
    turn runs unbriefed. Otherwise the composed brief — the case brief or
    the watch-briefing block, by the stash's kind — is appended as its
    fenced block at this call site — no ``ai_service_core`` change (binding
    decision 1; ADR-0133 §6), so Shirley's own prompt stays byte-identical
    beneath it.
    """
    session_id = str(session.id)
    stashed = _get_active_brief(request, session_id)
    if stashed is None:
        return base_prompt
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return base_prompt
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        if stashed.kind == BRIEF_KIND_WATCH_BRIEFING:
            row = await IreneBriefingRepository(db).get(stashed.id)
            if row is None or row.status != "written":
                _clear_active_brief(request, session_id)
                return base_prompt
            brief = await _compose_watch_briefing_brief(db, row)
        else:
            case = await CaseRepository(db).get(stashed.id)
            if case is None or case.state not in _MUTABLE_STATES:
                _clear_active_brief(request, session_id)
                return base_prompt
            brief = await _compose_case_brief(db, case)
    return f"{base_prompt}\n\n{brief}" if base_prompt else brief


def _assistant_message_text(conversation: Conversation | None, message_id: str) -> str | None:
    """Return the text of the assistant message with ``message_id``, or ``None``.

    Robust to trimming: the id is the :class:`Message`'s own stable id, so a
    message trimmed out of history is simply not found — Keep then says the
    answer is no longer available rather than keeping the wrong one. An
    existing but empty (chart-only) message returns ``""``.
    """
    if conversation is None or not message_id:
        return None
    for message in conversation.messages:
        if message.id == message_id and message.role == MessageRole.ASSISTANT:
            return message.content or ""
    return None


# ---------------------------------------------------------------------------
# Per-turn message recorder for multi-turn history
# ---------------------------------------------------------------------------


class _TurnRecorder:
    """Reconstruct OpenAI-shaped messages from the SSE event stream.

    The streaming core appends to its *internal* ``messages: list[dict]``
    during the tool loop but that list is private; the SSE handler has
    to rebuild the same shape from the public :class:`StreamEvent`
    surface so the turn can be appended to the session's history at
    ``stream_finished``.

    The recorder owns the state of the "round in progress": one round
    begins when the first ``tool_called`` event of that round arrives
    and ends when ``stream_finished`` (or another round-starting
    ``tool_called``) closes it. Within a round, ``tool_called`` events
    accumulate :class:`ToolCall` records onto a pending
    ``assistant``-with-``tool_calls`` :class:`Message`; ``tool_completed``
    events emit a matching ``tool`` :class:`Message`.

    Multiple ``tool_called`` events within one round share the same
    pending assistant record; the next round begins when a
    ``tool_called`` arrives *after* at least one ``tool_completed`` has
    closed the prior one's last call.

    On ``stream_finished``, the recorder flushes any pending pieces
    and appends the final assistant message from the event payload.
    """

    def __init__(self) -> None:
        """Initialise with no round in progress."""
        self._messages: list[Message] = []
        self._pending_assistant: Message | None = None
        self._pending_call_ids: set[str] = set()
        self._completed_ids_for_round: set[str] = set()

    def observe(self, event_type: str, payload: Any) -> None:
        """Update internal state for one observed :class:`StreamEvent`.

        Args:
            event_type: The event's ``event_type``.
            payload: The event's ``payload`` mapping.
        """
        if event_type == "tool_called":
            # A ``tool_called`` arriving after one or more
            # ``tool_completed`` events for this round means the model
            # has started a new round of tool calls. Close the prior
            # round so the next assistant record is fresh.
            if self._pending_assistant is not None and self._completed_ids_for_round:
                self._pending_assistant = None
                self._pending_call_ids = set()
                self._completed_ids_for_round = set()

            if self._pending_assistant is None:
                self._pending_assistant = Message(
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_calls=[],
                )
                self._messages.append(self._pending_assistant)

            tool_call_id = str(payload.get("tool_call_id", ""))
            name = str(payload.get("name", ""))
            arguments_raw = str(payload.get("arguments", ""))
            try:
                arguments = json.loads(arguments_raw) if arguments_raw else {}
            except json.JSONDecodeError:
                arguments = {"_raw": arguments_raw}

            self._pending_assistant.tool_calls.append(
                ToolCall(id=tool_call_id, name=name, arguments=arguments)
            )
            self._pending_call_ids.add(tool_call_id)

        elif event_type == "tool_completed":
            tool_call_id = str(payload.get("tool_call_id", ""))
            result = str(payload.get("result", ""))
            self._messages.append(
                Message(
                    role=MessageRole.TOOL,
                    content=result,
                    tool_calls=[],
                    tool_call_id=tool_call_id,
                    model=str(payload.get("name", "")),
                )
            )
            # Track that this round produced at least one completion;
            # the next ``tool_called`` (if any) opens a new round.
            self._completed_ids_for_round.add(tool_call_id)

        elif event_type == "stream_finished":
            final_message = payload.get("message")
            if isinstance(final_message, Message):
                # Always append the final assistant message, even when
                # its prose content is empty — keeps the turn-group
                # well-formed for the trim function.
                self._messages.append(final_message)
            self._pending_assistant = None
            self._pending_call_ids = set()
            self._completed_ids_for_round = set()

    def collected(self) -> list[Message]:
        """Return the rebuilt messages in OpenAI-protocol order."""
        return list(self._messages)


# ---------------------------------------------------------------------------
# Helper accessors
# ---------------------------------------------------------------------------


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request):  # type: ignore[no-untyped-def]
    engine = request.app.state.engine
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database engine is not configured.",
        )
    return engine


def _ai_core(request: Request) -> AIServiceCore:
    """Return the AIServiceCore singleton, allowing test overrides."""
    override = getattr(request.app.state, "ai_core", None)
    return cast(AIServiceCore, override) if override is not None else get_ai_service_core()


# ---------------------------------------------------------------------------
# Per-turn LLM resolution (ADR-0112 §4b)
# ---------------------------------------------------------------------------
#
# Shirley's credential and model are resolved **per turn**, inside the
# requesting session's tenant context, through the one credential façade
# (``CredentialResolver``). There is no process-global "configured" state to
# consult any more: a tenant that writes an OpenRouter key in Admin →
# Providers & Credentials is served on its very next turn, with no restart,
# and one worker serves many tenants without their keys ever meeting.
#
# The chain, scope-major per ADR-0112 §1:
#
# * credential — vault user → vault tenant → env ``OPENROUTER_API_KEY``;
# * model      — vault user → vault tenant → env ``SHIRLEY_MODEL``;
# * base_url   — vault tenant → env ``OPENROUTER_BASE_URL`` → the
#   ``WebSettings`` default (the one field with a sane default, so it never
#   fails a turn).
#
# Resolution is never stashed (binding decision D3): the plain key exists only
# for the duration of one call, and both the POST (fail fast, 503) and the SSE
# GET (authoritative, drives the turn) resolve it independently. Two vault
# reads per turn is the price of never parking a key in the pending-turn store.

#: One user-facing message for every "this turn has no LLM" outcome. Points at
#: both scopes an operator can fix it in, and deliberately says nothing about
#: restarting: tenant and user rows apply on the next turn.
_NO_LLM_MESSAGE = (
    "Shirley has no API credential or model for this tenant. Set an "
    "OpenRouter API key and model in Admin → Providers & Credentials (they "
    "apply on your next message), or set OPENROUTER_API_KEY and SHIRLEY_MODEL "
    "in .env for the whole application."
)


class _LLMUnconfiguredError(Exception):
    """This turn's OpenRouter credential or model resolved to nothing.

    The single failure type the two entry points translate — the POST
    endpoints into a 503, the SSE endpoint into an ``error`` frame. It wraps
    :class:`~services.investments.credential_resolver.CredentialUnavailableError`
    (no credential anywhere) and also covers the no-model outcome, which is a
    config-chain miss rather than a credential one.

    A :class:`~services.credential_vault.VaultDecryptError` is deliberately
    **not** wrapped: a vault that will not decrypt is an operator emergency,
    not a "configure me" nudge, and it must not read as a missing key.
    """


async def _resolve_llm(request: Request, session: SessionDTO) -> ResolvedLLM:
    """Resolve this turn's endpoint, credential and model (ADR-0112 §4b).

    Runs inside the session's ``tenant_context`` so the vault sources see
    exactly the rows RLS allows this tenant, with the user axis carried for
    the user-scope rows. Without a database engine (a DB-less test rig, a
    contributor laptop) the resolver is built without a session and the
    environment is the only source — the same graceful degradation the tools
    take.

    Args:
        request: The active request (carries ``app.state`` and settings).
        session: The authenticated session, supplying tenant and user.

    Returns:
        The :class:`~services.ai_service_core.ResolvedLLM` for this turn.

    Raises:
        _LLMUnconfiguredError: If no source holds a credential, or none
            holds a model.
        VaultDecryptError: Propagated untouched — a wrong or rotated master
            key must never look like an absent credential.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return await _resolve_llm_through(
            CredentialResolver(), request, tenant_id=None, user_id=None
        )
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        return await _resolve_llm_through(
            CredentialResolver(session=db),
            request,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
        )


async def _resolve_llm_through(
    resolver: CredentialResolver,
    request: Request,
    *,
    tenant_id: UUID | None,
    user_id: UUID | None,
) -> ResolvedLLM:
    """Walk the three chains on ``resolver`` and assemble the resolution.

    Split from :func:`_resolve_llm` so the chain reads in one place,
    independent of whether a vault-backed session was available.
    """
    try:
        credential = await resolver.resolve("openrouter", tenant_id=tenant_id, user_id=user_id)
    except CredentialUnavailableError as exc:
        raise _LLMUnconfiguredError(_NO_LLM_MESSAGE) from exc
    if not isinstance(credential, ProviderCredential):
        # openrouter declares a secret field and is not optional, so the
        # resolver raises rather than returning NoCredential. Defensive.
        raise _LLMUnconfiguredError(_NO_LLM_MESSAGE)

    model = await resolver.resolve_config("openrouter", "model", user_id=user_id)
    if not model:
        raise _LLMUnconfiguredError(_NO_LLM_MESSAGE)

    settings = request.app.state.settings
    base_url = (
        await resolver.resolve_config("openrouter", "base_url") or settings.openrouter_base_url
    )
    return ResolvedLLM(base_url=base_url, api_key=credential.payload["api_key"], model=model)


async def _require_connected_core(
    request: Request, session: SessionDTO
) -> tuple[AIServiceCore, ResolvedLLM]:
    """Return the AI core and this turn's resolution, or raise 503.

    The fail-fast half of D3: both write entry points (text and voice) call
    this so an unconfigured tenant learns the fact from a plain 503 rather
    than from a half-open SSE stream. The SSE endpoint resolves again — that
    second resolution is the authoritative one, and it is what keeps a plain
    key out of the pending-turn store entirely.

    Returns:
        The core (for the turn machinery) and the turn's
        :class:`~services.ai_service_core.ResolvedLLM` (for the vision gate
        and the stash).

    Raises:
        HTTPException: 503 when nothing can serve this turn.
    """
    core = _ai_core(request)
    try:
        llm = await _resolve_llm(request, session)
    except _LLMUnconfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return core, llm


# ---------------------------------------------------------------------------
# Per-turn voice resolution and gating (ADR-0118 §4, ADR-0136 §4, §5)
# ---------------------------------------------------------------------------
#
# The voice twin of the block above, and for the same reason: a tenant that
# writes its speech settings in Admin → Providers & Credentials is served on
# its very next recording, with no restart, and one worker serves many
# tenants without their keys ever meeting. There is no process-global voice
# provider left for this surface to consult.
#
# The chains, scope-major per ADR-0112 §1. Every voice field is tenant-scope,
# so none of them carries a user axis; each field chains individually along
# vault tenant → env:
#
# * enabled  — ``VOICE_ENABLED``; unset is off (ADR-0118 §5);
# * provider — ``VOICE_STT_PROVIDER`` / ``VOICE_TTS_PROVIDER``, **per half**;
#   unset is that half inactive (ADR-0136 §4);
# * model, endpoint, persona voice, API key — the remaining ``VOICE_STT_*`` /
#   ``VOICE_TTS_*`` links; the adapter declaration fills what an operator
#   left empty.
#
# Availability is three config reads and never a credential probe. The rules
# — defaults, keyless adapters, the misconfiguration message — live once in
# the pure assembly (``assemble_stt`` / ``assemble_tts``); this surface does
# the I/O and resolves only the half an endpoint needs (ADR-0136 §5).
#
# Nothing is cached and nothing is stashed: a ``ResolvedSTT`` / ``ResolvedTTS``
# lives for exactly one call, so a tenant's key can never be held where
# another tenant's turn could reach it.

#: The voice chain: every voice field is tenant-scope with an env link.
_VOICE_SCOPES: tuple[str, ...] = ("tenant", "env")


class _VoiceUnconfiguredError(Exception):
    """This turn's voice half is active but its configuration is incomplete.

    The voice twin of :class:`_LLMUnconfiguredError`, and translated the same
    way — both voice endpoints answer 503 — because "configure me" is the
    same semantic. It wraps :class:`~services.voice.VoiceConfigurationError`
    raised by the assembly, whose message names the missing field and both
    scopes an operator can fix it in (ADR-0136 §4).

    A :class:`~services.credential_vault.VaultDecryptError` is deliberately
    **not** wrapped: a vault that will not decrypt is an operator emergency,
    not a "configure me" nudge, and it must not read as a missing key.

    The voice call's resolution raises it too, wrapping
    :class:`~services.voice_call.CallConfigurationError`, whose message names
    the ``Voice call`` card only (ADR-0137 §7).
    """


@asynccontextmanager
async def _voice_resolver(
    request: Request, session: SessionDTO
) -> AsyncIterator[CredentialResolver]:
    """Yield a resolver for this session's tenant voice chains.

    Inside the session's ``tenant_context`` so the vault source sees exactly
    the rows RLS allows this tenant. Without a database engine (a DB-less test
    rig, a contributor laptop) the resolver is built without a session and the
    environment is the only source — the same graceful degradation
    :func:`_resolve_llm` takes.

    Args:
        request: The active request (carries ``app.state``).
        session: The authenticated session, supplying the tenant.

    Yields:
        The :class:`CredentialResolver` every voice read of one call goes
        through.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        yield CredentialResolver()
        return
    async with tenant_context(engine, session.tenant_id) as db:
        yield CredentialResolver(session=db)


async def _voice_switch(resolver: CredentialResolver) -> bool:
    """Read ``voice.enabled``: ``"true"`` (case-insensitive) is on, anything else off."""
    value = await resolver.resolve_config("voice", "enabled", scopes=_VOICE_SCOPES)
    return value is not None and value.strip().lower() == "true"


_CALL_SCOPES: tuple[str, ...] = ("tenant",)


async def _call_switch(resolver: CredentialResolver) -> bool:
    """Read ``voice_call.enabled``: ``"true"`` (case-insensitive) is on, anything else off.

    Tenant scope only: the declaration has no environment link (ADR-0137
    §7), so the chain ends at the tenant row and an environment variable
    cannot switch the call on.
    """
    value = await resolver.resolve_config("voice_call", "enabled", scopes=_CALL_SCOPES)
    return value is not None and value.strip().lower() == "true"


async def _resolve_call_enabled(request: Request, session: SessionDTO) -> bool:
    """Report whether the voice call is offered to this session's tenant (ADR-0137 §4).

    One config read, never a credential probe — the switch alone offers the
    call button, as ``voice.enabled`` and a provider choice offer the voice
    controls (ADR-0118 §5). Whether the key is there is judged when a call
    is started, where a missing one is reported with the card's name.

    Args:
        request: The active request (carries ``app.state``).
        session: The authenticated session, supplying the tenant.

    Returns:
        ``True`` iff ``voice_call.enabled`` resolves to ``"true"`` on the tenant.
    """
    async with _voice_resolver(request, session) as resolver:
        return await _call_switch(resolver)


async def _call_values(resolver: CredentialResolver, *, tenant_id: UUID) -> ResolvedCall | None:
    """Read the call chain and assemble it (ADR-0137 §7).

    Tenant scope for every ``voice_call`` field — the declaration has no
    environment link — and the voice chains' scopes for the persona-voice
    fallback ``voice_tts.voice``. That fallback applies only when the
    text-to-speech half speaks ElevenLabs: a voice of another provider is
    meaningless to the call vendor and would fail only at agent creation.
    The key is read only when the switch is on, through the same credential
    read the voice halves use.

    Returns:
        ``None`` when the call is off, else the assembled :class:`ResolvedCall`.

    Raises:
        CallConfigurationError: If the call is on but incomplete.
    """
    enabled = await _call_switch(resolver)
    if not enabled:
        return None
    api_key = await _voice_api_key(resolver, "voice_call", tenant_id=tenant_id)
    agent_id = await resolver.resolve_config("voice_call", "agent_id", scopes=_CALL_SCOPES)
    model = await resolver.resolve_config("voice_call", "model", scopes=_CALL_SCOPES)
    voice = await resolver.resolve_config("voice_call", "voice", scopes=_CALL_SCOPES)
    language = await resolver.resolve_config("voice_call", "language", scopes=_CALL_SCOPES)
    base_url = await resolver.resolve_config("voice_call", "base_url", scopes=_CALL_SCOPES)
    tts_provider = await resolver.resolve_config("voice_tts", "provider", scopes=_VOICE_SCOPES)
    tts_voice: str | None = None
    if (tts_provider or "").strip().lower() == "elevenlabs":
        tts_voice = await resolver.resolve_config("voice_tts", "voice", scopes=_VOICE_SCOPES)
    return assemble_call(True, api_key, agent_id, model, voice, language, base_url, tts_voice)


async def _resolve_call(request: Request, session: SessionDTO) -> ResolvedCall:
    """Resolve this session's voice call — the call twin of :func:`_resolve_stt`.

    Raises:
        HTTPException: 404 when the call is off.
        _VoiceUnconfiguredError: If the call is on but incomplete — the
            message names the field and the ``Voice call`` card.
        VaultDecryptError: Propagated untouched.
    """
    async with _voice_resolver(request, session) as resolver:
        try:
            call = await _call_values(resolver, tenant_id=session.tenant_id)
        except CallConfigurationError as exc:
            raise _VoiceUnconfiguredError(str(exc)) from exc
    if call is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Voice call is not enabled."
        )
    return call


async def _voice_api_key(
    resolver: CredentialResolver, provider: str, *, tenant_id: UUID
) -> str | None:
    """Resolve one half's API key, or ``None`` when no source holds one.

    Both halves are optional credentials (ADR-0136 §3), so an absent key is a
    :class:`~services.investments.credential_resolver.NoCredential`, not an
    error; whether the adapter needs one is the assembly's question.
    """
    credential = await resolver.resolve(provider, tenant_id=tenant_id)
    if isinstance(credential, ProviderCredential):
        return credential.payload["api_key"]
    return None


async def _resolve_voice_flags(request: Request, session: SessionDTO) -> tuple[bool, bool]:
    """Report which voice halves are active for this session's tenant (ADR-0136 §4).

    A half is active iff ``voice.enabled`` resolves true **and** that half's
    ``provider`` resolves non-empty. Three config reads — and one when voice
    is off — never a credential probe: whether the affordance is offered and
    whether the half is complete are separate questions, and completeness
    fails loudly at first use.

    Args:
        request: The active request (carries ``app.state``).
        session: The authenticated session, supplying the tenant.

    Returns:
        ``(stt_active, tts_active)``.
    """
    async with _voice_resolver(request, session) as resolver:
        if not await _voice_switch(resolver):
            return False, False
        stt_provider = await resolver.resolve_config("voice_stt", "provider", scopes=_VOICE_SCOPES)
        tts_provider = await resolver.resolve_config("voice_tts", "provider", scopes=_VOICE_SCOPES)
    return bool((stt_provider or "").strip()), bool((tts_provider or "").strip())


async def _resolve_stt(request: Request, session: SessionDTO) -> ResolvedSTT:
    """Resolve this call's speech-to-text half (ADR-0136 §5).

    Reads the raw values along the voice chains and hands them to
    :func:`~services.voice.assemble_stt`. The key is read only when the half
    is active.

    Args:
        request: The active request (carries ``app.state``).
        session: The authenticated session, supplying the tenant.

    Returns:
        The :class:`~services.voice.ResolvedSTT` for this call.

    Raises:
        HTTPException: 404 when the half is inactive — the race-safe twin of
            the gate the endpoint ran a moment earlier.
        _VoiceUnconfiguredError: If the half is active but incomplete.
        VaultDecryptError: Propagated untouched — a wrong or rotated master
            key must never look like an absent credential.
    """
    async with _voice_resolver(request, session) as resolver:
        enabled = await _voice_switch(resolver)
        provider = await resolver.resolve_config("voice_stt", "provider", scopes=_VOICE_SCOPES)
        model = await resolver.resolve_config("voice_stt", "model", scopes=_VOICE_SCOPES)
        base_url = await resolver.resolve_config("voice_stt", "base_url", scopes=_VOICE_SCOPES)
        api_key = None
        if enabled and (provider or "").strip():
            api_key = await _voice_api_key(resolver, "voice_stt", tenant_id=session.tenant_id)
    try:
        stt = assemble_stt(enabled, provider, model, base_url, api_key)
    except VoiceConfigurationError as exc:
        raise _VoiceUnconfiguredError(str(exc)) from exc
    if stt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Voice is not enabled.")
    return stt


async def _resolve_tts(request: Request, session: SessionDTO) -> ResolvedTTS:
    """Resolve this call's text-to-speech half (ADR-0136 §5).

    The twin of :func:`_resolve_stt`, through
    :func:`~services.voice.assemble_tts`.

    Args:
        request: The active request (carries ``app.state``).
        session: The authenticated session, supplying the tenant.

    Returns:
        The :class:`~services.voice.ResolvedTTS` for this call.

    Raises:
        HTTPException: 404 when the half is inactive.
        _VoiceUnconfiguredError: If the half is active but incomplete.
        VaultDecryptError: Propagated untouched.
    """
    async with _voice_resolver(request, session) as resolver:
        enabled = await _voice_switch(resolver)
        provider = await resolver.resolve_config("voice_tts", "provider", scopes=_VOICE_SCOPES)
        model = await resolver.resolve_config("voice_tts", "model", scopes=_VOICE_SCOPES)
        base_url = await resolver.resolve_config("voice_tts", "base_url", scopes=_VOICE_SCOPES)
        voice = await resolver.resolve_config("voice_tts", "voice", scopes=_VOICE_SCOPES)
        api_key = None
        if enabled and (provider or "").strip():
            api_key = await _voice_api_key(resolver, "voice_tts", tenant_id=session.tenant_id)
    try:
        tts = assemble_tts(enabled, provider, model, base_url, voice, api_key)
    except VoiceConfigurationError as exc:
        raise _VoiceUnconfiguredError(str(exc)) from exc
    if tts is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Voice is not enabled.")
    return tts


# ---------------------------------------------------------------------------
# Image-upload validation messages (ADR-0075) and the error fragment helper
# ---------------------------------------------------------------------------

# English user-facing copy, matching the web surface convention.
_VISION_GATE_MESSAGE = (
    "The current model can't read images. Switch Shirley to a "
    "vision-capable model to analyse photos."
)
_UNSUPPORTED_TYPE_MESSAGE = "Unsupported image type — please send a JPEG, PNG, WebP, or GIF."
_OVERSIZE_MESSAGE = "That image is too large (max 8 MB)."

# Voice-error copy (ADR-0076). Surfaced inline via the same chat_error
# fragment image failures use — no silent fallback.
_VOICE_EMPTY_MESSAGE = "I didn't catch any speech — please try again."
_VOICE_FORMAT_MESSAGE = "That audio format isn't supported — please try again."
_VOICE_STT_FAIL_MESSAGE = "Transcription failed — please try again, or type your message."


def _chat_error(request: Request, message: str) -> HTMLResponse:
    """Render the inline chat-error fragment for an image-upload failure.

    The fragment drops into ``#chat-history`` via the composer's existing
    ``hx-swap="beforeend"`` so a rejected upload surfaces a clear message
    instead of a bare 4xx (ADR-0075).

    Args:
        request: The active request (carries the Jinja environment).
        message: The human-readable error copy to render.

    Returns:
        The rendered ``_partials/chat_error.html`` fragment.
    """
    templates = _templates(request)
    return cast(
        HTMLResponse,
        templates.TemplateResponse(
            request,
            "_partials/chat_error.html",
            {"message": message},
        ),
    )


# ---------------------------------------------------------------------------
# GET /  → /front-office
# ---------------------------------------------------------------------------


@router.get("/")
async def root_redirect(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> Response:
    """Authenticated landing page redirects to the Front Office area.

    Sub-stream 6F-1 of Phase 6 Block 1 changed the landing target
    from ``/chat`` to ``/front-office`` — the area sidebar is now
    the canonical navigation surface (ADR-0046). ADR-0051 retired
    the standalone ``GET /chat`` page; the chat surface now lives
    at ``/assistants#shirley``.
    """
    del session  # only used to gate access
    del request
    return RedirectResponse(url="/front-office", status_code=303)


# ---------------------------------------------------------------------------
# Shared turn-begin helpers (text + voice)
# ---------------------------------------------------------------------------


async def _validate_uploads(
    request: Request, model: str, uploads: list[UploadFile]
) -> tuple[list[Attachment], HTMLResponse | None]:
    """Validate image uploads (ADR-0075 order).

    Returns ``(attachments, None)`` on success, or ``([], error_fragment)``
    on the first failure. Shared by ``POST /chat/messages`` and
    ``POST /chat/voice``. The validation order is fixed: vision gate, then
    MIME type, then per-image size ceiling.

    Args:
        request: The active request (carries the Jinja environment for
            the error fragment).
        model: The model **this turn resolved** (ADR-0112 §4b), which the
            vision gate is asked about. Takes the resolved model rather than
            ``core.get_model()``: since the model is per tenant and per user,
            the singleton's model would gate one tenant's upload on another
            tenant's (or on nothing at all).
        uploads: The non-empty-filename uploads to validate.

    Returns:
        A tuple of the materialised attachments and an optional inline
        error fragment. Exactly one of the two is meaningful.
    """
    if not uploads:
        return [], None
    if not supports_vision(model):
        return [], _chat_error(request, _VISION_GATE_MESSAGE)
    attachments: list[Attachment] = []
    for img in uploads:
        mime = (img.content_type or "").lower()
        if mime not in ALLOWED_IMAGE_MIME_TYPES:
            return [], _chat_error(request, _UNSUPPORTED_TYPE_MESSAGE)
        data = await img.read()
        if len(data) > MAX_IMAGE_BYTES:
            return [], _chat_error(request, _OVERSIZE_MESSAGE)
        attachments.append(Attachment(filename=img.filename or "upload", mime_type=mime, data=data))
    return attachments, None


async def _begin_turn(
    request: Request,
    session: SessionDTO,
    text: str,
    attachments: list[Attachment],
    *,
    model_id: str,
    is_voice: bool = False,
) -> HTMLResponse:
    """Shared turn-begin body for the text and voice entry points.

    Assumes ``text`` and ``attachments`` are already resolved and
    validated and the core is connected. Applies the image-only default
    instruction, allocates the turn id, appends the user message,
    trims and stashes it, builds inline thumbnails, and renders
    ``turn_started.html`` with the ``data-pf-voice`` flag so the client
    knows whether to speak the reply on ``done``.

    Args:
        request: The active request.
        session: The authenticated session.
        text: The user's text (the STT transcript for a voice turn).
        attachments: Already-validated image attachments (may be empty).
        model_id: The model this turn resolved (ADR-0112 §4b), recorded in
            the stash as turn metadata. The **model** only — the credential
            is never stashed (binding decision D3).
        is_voice: Whether the reply is to be spoken — a voice-initiated
            turn with text-to-speech active (ADR-0136 §4). Threaded into the
            SSE-bootstrap element as ``data-pf-voice``.

    Returns:
        The rendered ``_partials/turn_started.html`` fragment, or an empty
        fragment when there is nothing to send.
    """
    if not text and attachments:
        text = "Please analyse the attached image in the context of my portfolio."
    if not text and not attachments:
        return HTMLResponse("")

    turn_id = uuid.uuid4().hex
    assistant_bubble_id = f"assistant-{turn_id}"

    # Append the user line to the session's conversation history first,
    # then stash the per-turn metadata. Order matters: the SSE handler
    # reads the populated conversation, so the user message it is about
    # to answer must already be present when the stream opens. The trim
    # runs immediately after the append so the history never grows
    # unbounded between the POST and the SSE GET (ADR-0050).
    history = _get_or_create_history(request, str(session.id))
    history.add_message(Message(role=MessageRole.USER, content=text, attachments=attachments))
    _trim_history(
        history,
        _stored_chart_artifacts(request, str(session.id)),
        _stored_call_lines(request, str(session.id)),
    )

    _stash_turn(
        request,
        str(session.id),
        turn_id,
        user_message=text,
        model_id=model_id or None,
    )

    # Inline thumbnails for the user bubble. No resizing — CSS bounds the
    # display size; the data URI is built from the same bytes already
    # validated above.
    thumbnails = [
        {
            "src": (f"data:{att.mime_type};base64,{base64.b64encode(att.data).decode()}"),
            "alt": att.filename,
        }
        for att in attachments
    ]

    templates = _templates(request)
    timestamp = datetime.now(timezone.utc).strftime("%H:%M")

    return cast(
        HTMLResponse,
        templates.TemplateResponse(
            request,
            "_partials/turn_started.html",
            {
                "turn_id": turn_id,
                "user_content": text,
                "assistant_bubble_id": assistant_bubble_id,
                "timestamp": timestamp,
                "thumbnails": thumbnails,
                "is_voice": is_voice,
            },
        ),
    )


# ---------------------------------------------------------------------------
# POST /chat/messages
# ---------------------------------------------------------------------------


@router.post(
    "/chat/messages",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def post_message(
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
    message: str = Form(""),
    images: list[UploadFile] = File(default=[]),
) -> HTMLResponse:
    """Accept a user message, allocate a turn, return the HTMX fragment.

    The fragment renders the user's line (plus any image thumbnails),
    places an empty assistant bubble, and embeds an SSE-connect element
    pointing at ``/chat/stream/<turn_id>``. HTMX opens the connection
    immediately; the SSE handler reads the populated conversation and
    starts streaming.

    A turn may carry zero or more raster images alongside (or instead of)
    text (ADR-0075). Each image is validated against the active model's
    vision capability, the allowed MIME set, and the per-image size
    ceiling; a failure returns the inline error fragment rather than a
    bare 4xx. Images ride on the user :class:`Message`'s ``attachments``;
    the existing :meth:`Conversation.to_openai_messages` serialisation
    turns them into vision content blocks. The image bytes are dropped
    from history after the turn finishes (see :func:`chat_stream`).
    """
    text = message.strip()

    # An empty file input still submits a part with no filename; ignore
    # those so a text-only send is unaffected.
    uploads = [img for img in images if img is not None and img.filename]

    if not text and not uploads:
        # Empty message, no images — nothing to do. Return an empty
        # fragment so HTMX swaps nothing.
        return HTMLResponse("")

    # Surface "no LLM for this tenant" up front rather than via a half-open
    # SSE stream so the operator sees the cause without opening browser
    # devtools. The credential and model are resolved here for *this* tenant
    # and user (ADR-0112 §4b); the SSE endpoint resolves again and that
    # second resolution drives the turn.
    _core, llm = await _require_connected_core(request, session)

    # Validate and materialise image attachments before any history
    # mutation, in the ADR-0075 order: vision gate, then MIME, then size.
    attachments, error = await _validate_uploads(request, llm.model, uploads)
    if error is not None:
        return error

    return await _begin_turn(
        request, session, text, attachments, model_id=llm.model, is_voice=False
    )


# ---------------------------------------------------------------------------
# POST /chat/voice  (STT in) and POST /chat/tts  (TTS out) — ADR-0076
# ---------------------------------------------------------------------------


@router.post(
    "/chat/voice",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def post_voice(
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
    audio: UploadFile = File(...),
    images: list[UploadFile] = File(default=[]),
) -> HTMLResponse:
    """Transcribe a recorded question and begin the same turn text would.

    Mirrors :func:`post_message` but the user text comes from STT over the
    uploaded audio. Optional images ride along for mixed mode (ADR-0075).
    The returned fragment carries ``data-pf-voice=1`` when text-to-speech
    is active, so the client speaks the reply after the SSE turn finishes;
    with speech-to-text alone the question is spoken and the answer arrives
    in text (``data-pf-voice=0``). STT failures return the inline
    ``chat_error`` fragment (no silent fallback). When speech-to-text is
    inactive **for this tenant** the endpoint 404s so the surface degrades to
    text-only; active but incomplete is a 503 naming the missing field and
    both scopes an operator can fix it in (ADR-0136 §4).
    """
    stt_active, tts_active = await _resolve_voice_flags(request, session)
    if not stt_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Voice is not enabled.",
        )
    # Refuse before STT: do not pay for a transcription we cannot answer.
    _core, llm = await _require_connected_core(request, session)
    try:
        stt = await _resolve_stt(request, session)
    except _VoiceUnconfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    provider = build_stt(stt)
    audio_bytes = await audio.read()
    mime = (audio.content_type or "").lower()
    try:
        transcript = (await provider.transcribe(audio_bytes, mime)).strip()
    except EmptyTranscriptError:
        return _chat_error(request, _VOICE_EMPTY_MESSAGE)
    except UnsupportedAudioFormatError:
        return _chat_error(request, _VOICE_FORMAT_MESSAGE)
    except VoiceError:
        logger.warning("STT failed for session %s", session.id, exc_info=True)
        return _chat_error(request, _VOICE_STT_FAIL_MESSAGE)

    uploads = [img for img in images if img is not None and img.filename]
    attachments, error = await _validate_uploads(request, llm.model, uploads)
    if error is not None:
        return error
    return await _begin_turn(
        request, session, transcript, attachments, model_id=llm.model, is_voice=tts_active
    )


@router.post(
    "/chat/tts",
    dependencies=[Depends(require_role("owner", "member"))],
)
async def post_tts(
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
    text: str = Form(""),
) -> Response:
    """Synthesise assistant prose to MP3 for browser playback.

    Returns 204 for empty text (a chart-only turn speaks nothing), audio
    bytes on success, or 502 if synthesis fails — the text answer is
    already rendered, so a TTS failure must not break the turn. When
    text-to-speech is inactive **for this tenant** the endpoint 404s; active
    but incomplete is a 503, exactly as on ``/chat/voice``.
    """
    _stt_active, tts_active = await _resolve_voice_flags(request, session)
    if not tts_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Voice is not enabled.",
        )
    clean = text.strip()
    if not clean:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        tts = await _resolve_tts(request, session)
    except _VoiceUnconfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    provider = build_tts(tts)
    try:
        audio_bytes, out_mime = await provider.synthesize(clean, fmt="mp3")
    except VoiceError:
        logger.warning("TTS synthesis failed for session %s", session.id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Voice reply unavailable.",
        )
    return Response(content=audio_bytes, media_type=out_mime)


# ---------------------------------------------------------------------------
# GET /chat/stream/<turn_id>  (SSE)
# ---------------------------------------------------------------------------


def _format_sse(event_name: str, data: str) -> str:
    """Encode one SSE frame.

    Args:
        event_name: The ``event:`` line value.
        data: The payload. Multi-line strings are split across multiple
            ``data:`` lines per the SSE specification.

    Returns:
        A complete SSE frame including the trailing blank line.
    """
    lines = [f"event: {event_name}"]
    for piece in data.splitlines() or [""]:
        lines.append(f"data: {piece}")
    return "\n".join(lines) + "\n\n"


def _html_escape(text: str) -> str:
    """Minimal HTML escape for stream chunks.

    The chunks land inside a chat bubble; protecting against ``<``,
    ``>``, ``&`` is enough — the bubble does not interpret HTML
    attributes from the model output.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tool_context_for(request: Request, session: SessionDTO) -> ToolExecutionContext | None:
    """Build the per-turn tool-execution context for ``session``, or ``None``.

    Shared by the typed turn (:func:`chat_stream`) and a voice call's
    ``ask_shirley`` job (:func:`_run_call_job`), so both hand the core the
    same context.

    Args:
        request: The active request (carries the settings).
        session: The authenticated session, supplying tenant and user.

    Returns:
        The context, or ``None`` when ``DATABASE_URL`` is unset.
    """
    # Build the per-turn tool-execution context from the
    # authenticated session and hand it to the core. The core
    # sets it under ``_TURN_LOCK`` and clears it — together with
    # the turn-scoped data cache — in a ``finally`` when the turn
    # ends, so the web chat surface and the in-process Telegram
    # bot can never race on the shared module-level context
    # (ADR-0063). Two distinct seams: the tenant comes from the
    # authenticated session's ``tenant_id`` (the chat route is
    # the *producer* of the context), while the database URL is
    # ordinary request-scoped plumbing from settings. The tools
    # build their own loop-local engine from this URL; the shared
    # ``app.state.engine`` must not cross the tool's thread/loop
    # boundary (ADR-0047, amended). The URL is ``None`` when
    # ``DATABASE_URL`` is unset — in that case the context is
    # ``None`` and the tools degrade gracefully, telling the
    # model the data is unavailable. The user axis rides along
    # for the one tool that resolves its own credential per
    # call — the web research tool walks the user scope of the
    # chain before the tenant's (ADR-0132).
    database_url = request.app.state.settings.database_url
    return (
        ToolExecutionContext(
            tenant_id=session.tenant_id,
            database_url=database_url,
            user_id=session.user_id,
        )
        if database_url
        else None
    )


@router.get("/chat/stream/{turn_id}")
async def chat_stream(
    request: Request,
    turn_id: str,
    session: SessionDTO = Depends(require_session),
) -> StreamingResponse:
    """Stream Shirley's response for ``turn_id`` as Server-Sent Events.

    The endpoint reads the stashed user message, builds a one-shot
    :class:`Conversation`, drives :meth:`AIServiceCore.stream_response`,
    and translates each :class:`StreamEvent` into the matching SSE
    event (``message`` / ``tool_called`` / ``tool_completed`` /
    ``chart`` / ``case_write`` / ``done`` / ``error``).

    A request for someone else's turn (or a stale id) returns 404 —
    the store is keyed by ``(session_id, turn_id)``, so the lookup
    naturally fails on a cross-session attempt.
    """
    pending = _pop_turn(request, str(session.id), turn_id)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Unknown turn.",
        )

    core = _ai_core(request)

    async def event_stream() -> Any:
        """Async generator producing SSE-formatted bytes."""
        # Drive the turn against the session's accumulated history. The
        # user message has already been appended by POST /chat/messages
        # (see ADR-0050); the recorder reconstructs the assistant and
        # tool messages emitted during this turn so they can be appended
        # to the same Conversation on ``stream_finished``.
        conversation = _get_or_create_history(request, str(session.id))
        recorder = _TurnRecorder()
        # This turn's chart artefacts (ADR-0114), buffered until the assistant
        # message they belong to exists at ``stream_finished``.
        pending_charts: list[dict[str, Any]] = []

        # Defence in depth: the core also strips control tokens, but
        # repeating the filter here keeps the wire clean even if the
        # core is bypassed (test rigs, future direct-injection paths)
        # and isolates the SSE adapter from upstream changes.
        stripper = _StopTokenStripper()
        try:
            # The authoritative resolution for this turn (ADR-0112 §4b,
            # binding decision D3). The POST already resolved once to fail
            # fast; this one feeds the turn, so a key never has to survive
            # in the pending-turn store between the two requests. A
            # VaultDecryptError is not caught here — it falls to the
            # generic handler below and reads as the operator emergency it
            # is, never as "configure me".
            llm = await _resolve_llm(request, session)
            tool_context = _tool_context_for(request, session)
            # Append the active case brief (if any) to Shirley's system prompt
            # for this turn only — no ``ai_service_core`` change, no persistence
            # (ADR-0107 C6, binding decision 1). A stale stash clears here.
            system_prompt = await _briefed_system_prompt(request, session, core.get_system_prompt())
            async for event in core.stream_response(
                conversation,
                system_prompt=system_prompt,
                tool_context=tool_context,
                llm=llm,
            ):
                if await request.is_disconnected():
                    logger.info("chat-stream: client disconnected for turn %s", turn_id)
                    return
                et = event.event_type
                payload = event.payload
                if et == "chunk":
                    safe = stripper.process(str(payload.get("text", "")))
                    if safe:
                        yield _format_sse("message", _html_escape(safe))
                elif et == "tool_called":
                    recorder.observe(et, payload)
                    yield _format_sse("tool_called", str(payload.get("name", "")))
                elif et == "tool_completed":
                    recorder.observe(et, payload)
                    yield _format_sse("tool_completed", str(payload.get("name", "")))
                elif et == "chart_artifact":
                    # Two artefact formats reach here. ``render_chart``
                    # (ADR-0048) emits a Plotly ``spec``;
                    # ``generate_chart`` emits a PNG. Both tools are
                    # registered for every consumer, so the PNG branch
                    # runs whenever the model picks ``generate_chart``.
                    chart_format = str(payload.get("chart_format", "png"))
                    if chart_format == "plotly":
                        spec = payload.get("spec") or {}
                        caption = str(payload.get("caption", ""))
                        # ADR-0114: this is the one place the spec is at hand,
                        # so it is also the one capture point. The record is
                        # anchored to the turn's assistant message below; the
                        # browser is handed its ``artifact_id`` so the live
                        # figure can carry Keep's gestures. An
                        # oversized spec streams unchanged but is not archived
                        # and gets no id — hence no Keep for it. The
                        # SSE event vocabulary is untouched: same ``chart``
                        # event, one additive key in the plotly payload.
                        record = _new_chart_record(spec, caption)
                        pending_charts.append(record)
                        chart_data = json.dumps(
                            {
                                "chart_format": "plotly",
                                "spec": spec,
                                "caption": caption,
                                "artifact_id": (
                                    "" if record["oversized"] else record["artifact_id"]
                                ),
                            }
                        )
                    else:
                        chart_data = json.dumps(
                            {
                                "chart_format": "png",
                                "src": (
                                    "data:image/png;base64," + str(payload.get("image_base64", ""))
                                ),
                                "caption": str(payload.get("caption", "")),
                            }
                        )
                    yield _format_sse("chart", chart_data)
                elif et == "case_write":
                    # A write Shirley made through ``case_add`` (D-CS-B-114):
                    # the browser renders it as a dock line with Undo
                    # (D-CS-B-115). The model's acknowledgement never rides
                    # here — the core kept only it for the model.
                    yield _format_sse(
                        "case_write",
                        json.dumps(
                            {
                                **payload,
                                "undo_url": CASE_WRITE_UNDO_URL,
                                "csrf_field": _CSRF_FIELD,
                            }
                        ),
                    )
                elif et == "stream_finished":
                    tail = stripper.flush()
                    if tail:
                        yield _format_sse("message", _html_escape(tail))
                    # Flush the recorder, append the reconstructed
                    # messages to the session's history, and trim. The
                    # user message is already in ``conversation`` (added
                    # by POST /chat/messages); the recorder only adds
                    # the assistant + tool messages produced this turn.
                    recorder.observe(et, payload)
                    for collected in recorder.collected():
                        conversation.messages.append(collected)
                    # ADR-0075 single-turn vision contract: drop the image
                    # bytes from this turn's user message so they never
                    # replay on later turns. The trim math counts only
                    # ``m.content``, so dropping attachments is independent
                    # of trimming.
                    for m in reversed(conversation.messages):
                        if m.role == MessageRole.USER and m.attachments:
                            m.attachments = []
                            break
                    # Carry the final assistant message's stable id on the
                    # ``done`` frame so the client can offer Keep for exactly
                    # this message (ADR-0138 §8). The id is the one the
                    # recorder just appended to history, so Keep resolves its
                    # text server-side. Empty when no
                    # assistant message was produced (a bare error), in which
                    # case the client renders no affordance.
                    final_message = payload.get("message")
                    done_data = final_message.id if isinstance(final_message, Message) else ""
                    # Anchor this turn's charts to that same message, then trim
                    # both stores in one operation (ADR-0114): an evicted
                    # turn-group takes its archived specs with it.
                    artifacts = _bind_chart_artifacts(
                        request, str(session.id), pending_charts, done_data
                    )
                    _trim_history(
                        conversation, artifacts, _stored_call_lines(request, str(session.id))
                    )
                    yield _format_sse("done", done_data)
                    return
                elif et == "error":
                    # Partial assistant content is intentionally not
                    # appended to the history on error (ADR-0050). The
                    # user message stays in place so a retry re-asks the
                    # same question; nothing pollutes the assistant
                    # side. The trim runs in case the user message alone
                    # changed the bounds.
                    logger.info(
                        "chat-stream: error path for turn %s — assistant "
                        "content dropped from history.",
                        turn_id,
                    )
                    # The turn's charts are dropped with the assistant content
                    # they belong to: there is no message to anchor them to
                    # (ADR-0114, following ADR-0050's error-path rule).
                    _trim_history(
                        conversation,
                        _stored_chart_artifacts(request, str(session.id)),
                        _stored_call_lines(request, str(session.id)),
                    )
                    yield _format_sse("error", str(payload.get("message", "Unknown error.")))
                    yield _format_sse("done", "")
                    return
        except asyncio.CancelledError:
            # Client closed the connection. Re-raise so Starlette
            # terminates the task cleanly.
            raise
        except _LLMUnconfiguredError as exc:
            # No credential or no model for this tenant/user. The message
            # names both scopes an operator can fix it in and does not
            # mention restarting — vault rows apply on the next turn.
            logger.info("chat-stream: no LLM resolved for turn %s", turn_id)
            yield _format_sse("error", str(exc))
            yield _format_sse("done", "")
            return
        except Exception as exc:  # noqa: BLE001 — surface as SSE error
            logger.exception("chat-stream: unexpected error in turn %s", turn_id)
            user_msg, _error_id = user_safe_error(exc)
            yield _format_sse("error", f"Unexpected error: {user_msg}")
            yield _format_sse("done", "")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Voice call — the session start, the relay, the ask_shirley job and the
# call's history furniture (ADR-0137 §3–§5, §8)
# ---------------------------------------------------------------------------
#
# A voice call talks to a vendor-hosted agent in the browser; this section is
# its server side. The browser starts a call with ``POST /chat/call/session``,
# which provisions the tenant's agent and mints the session credential. It
# relays the call's final transcript lines into
# the session's history (``POST /chat/call/events``), hands every question the
# agent's ``ask_shirley`` tool asks to a job (``POST /chat/call/ask``, polled
# at ``GET /chat/call/jobs/<job_id>``, cancelled on barge-in) and marks the
# call's end (``POST /chat/call/end``).
#
# ``call_id`` is the handle one call carries on every request: the session
# endpoint mints it (a 12-hex UUID slice) and returns it with the vendor
# credential. Here it is opaque — it keys the relay cursor, and nothing else.
#
# A job runs one ordinary Shirley turn under the turn lock, with the tool
# allowlist, through the typed turn's own machinery — the recorder, the chart
# binding, the trim, the briefed system prompt, the per-turn LLM resolution
# and the tool context — and then rewrites the written answer for the ear.
# The job outlives the request that started it; the browser stops polling at
# 75 s and the job runs to completion, so the answer still lands in the chat.
#
# Six stores ride on ``app.state``, all single-worker like the rest of this
# surface (ADR-0050):
#
# * ``call_agent_memo`` — the agent memo per tenant (below);
# * ``call_provision_locks`` — one ``asyncio.Lock`` per tenant (below);
# * ``call_jobs``       — the job records, a bounded ``CallJobStore``;
# * ``call_tasks``      — strong references to the running job tasks, keyed
#   like the jobs and removed when a task ends;
# * ``call_seq``        — the relay cursor per ``(session_id, call_id)``: the
#   highest ``seq`` appended so far, bounded like the pending-turn store;
# * ``chat_call_lines`` — the call-line sidecar.
#
# The "Call started HH:MM" / "Call ended HH:MM" lines are history furniture,
# not conversation, so they never reach a model: they live **beside** the
# history, as the chart artefacts do (ADR-0114), never in
# ``Conversation.messages``. One record per line::
#
#     {"kind": "started" | "ended",
#      "at": "HH:MM",
#      "after_message_id": "" | "<Message.id>"}   # "" = before everything
#
# The sidecar is keyed like ``chat_histories`` and shares its lifecycle
# verbatim: "new chat", logout and the session-level LRU drop it with the
# history, and the turn-group trim drops the lines anchored to the messages it
# evicts.
#
# The agent memo (ADR-0137 §8) maps a tenant id to the ``(agent_id,
# definition_hash)`` pair this process last verified at the vendor, so the
# vendor's agent read happens once per process and definition rather than on
# every call start. It is invalidated by what it keys on: an operator who
# clears or edits ``voice_call.agent_id`` in Admin changes the resolved id,
# and a release that changes the definition changes the hash — either way the
# pair no longer matches and the agent is checked again. A restart empties it.
#
# The provisioning lock serialises, per tenant, the memo check, the vendor's
# agent read or create and the row write, so two call starts arriving
# together on a fresh tenant provision one agent, not two: the second waits,
# finds the pair the first verified and uses it. The token mint runs outside
# the lock. One lock per tenant, never dropped, so no cap; single-worker
# (ADR-0050), an in-process lock is the whole story.
#
# The agent id is the one ``voice_call`` field PortfoliFLOW writes (ADR-0137
# §8): provisioning is lazy, so the chat route that creates the agent is the
# one place that knows its id, and the row is what the Admin card's state line
# reads. The write reuses the Admin surface's repository call and its audit
# line rather than adding a hook to the Admin write path.

_CALL_STORES_LIMIT = 100

#: The failure a job reports when the turn ended without an answer.
_CALL_NO_ANSWER_MESSAGE = "Shirley did not answer."

#: The rewrite call's ceiling, seconds: a slow rewrite must not hold the
#: answer back, and its failure only costs the fallback sentence.
_SPEAKABLE_TIMEOUT_SECONDS = 30.0


class _CallSessionBody(BaseModel):
    """The body of ``POST /chat/call/session``: the browser's ``HH:MM``, optional."""

    at: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")


class _CallEvent(BaseModel):
    """One final transcript line of a call, as the browser relays it."""

    seq: int = Field(ge=0)
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=8_000)
    at: str | None = None  # the browser's clock, informational


class _CallEventsBody(BaseModel):
    """The body of ``POST /chat/call/events``: a batch of one call's lines."""

    call_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    events: list[_CallEvent] = Field(default_factory=list, max_length=200)


class _CallAskBody(_CallEventsBody):
    """The body of ``POST /chat/call/ask``: the question and the lines not yet relayed."""

    question: str = Field(min_length=1, max_length=4_000)
    language: str | None = Field(default=None, max_length=16)


class _CallEndBody(BaseModel):
    """The body of ``POST /chat/call/end``: the call and the browser's ``HH:MM``."""

    call_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    at: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")


def _call_jobs(request: Request) -> CallJobStore:
    """Return (and lazily initialise) the per-app job store (``app.state.call_jobs``).

    Keyed ``(session_id, job_id)``, so another session's job id never
    resolves; bounded, the oldest record evicted with a warning.
    """
    store = getattr(request.app.state, "call_jobs", None)
    if store is None:
        store = CallJobStore(limit=_CALL_STORES_LIMIT)
        request.app.state.call_jobs = store
    return cast(CallJobStore, store)


def _call_tasks(request: Request) -> dict[tuple[str, str], asyncio.Task[None]]:
    """Return (and lazily initialise) the running job tasks, keyed like the jobs.

    The event loop holds only weak references to its tasks; this store keeps
    a running job alive until it ends, when its done-callback removes it.
    """
    store = getattr(request.app.state, "call_tasks", None)
    if store is None:
        store = {}
        request.app.state.call_tasks = store
    return cast("dict[tuple[str, str], asyncio.Task[None]]", store)


def _call_seq(request: Request) -> OrderedDict[tuple[str, str], int]:
    """Return (and lazily initialise) the relay cursors, keyed ``(session_id, call_id)``."""
    store = getattr(request.app.state, "call_seq", None)
    if store is None:
        store = OrderedDict()
        request.app.state.call_seq = store
    return cast("OrderedDict[tuple[str, str], int]", store)


def _advance_call_cursor(request: Request, key: tuple[str, str], seq: int) -> None:
    """Set the relay cursor of ``key`` to ``seq``, evicting the oldest at the cap.

    Re-inserting moves the entry to the most-recently-used end, so a live
    call's cursor is never the one evicted.
    """
    store = _call_seq(request)
    store.pop(key, None)
    while len(store) >= _CALL_STORES_LIMIT:
        evicted_key, _ = store.popitem(last=False)
        logger.warning(
            "chat: evicting call relay cursor %s (LRU cap %d)",
            evicted_key,
            _CALL_STORES_LIMIT,
        )
    store[key] = seq


def _call_lines(request: Request) -> OrderedDict[str, list[dict[str, Any]]]:
    """Return (and lazily initialise) the per-app call-line sidecar.

    Keyed by the authenticated session UUID (string), like
    :func:`_chart_artifacts`, and bounded the same way: by the history
    store's eviction rather than a cap of its own.
    """
    store = getattr(request.app.state, "chat_call_lines", None)
    if store is None:
        store = OrderedDict()
        request.app.state.chat_call_lines = store
    return cast("OrderedDict[str, list[dict[str, Any]]]", store)


def _stored_call_lines(request: Request, session_id: str) -> list[dict[str, Any]] | None:
    """Return the session's call lines, or ``None`` when it has none.

    A read never creates the entry: a session that never called carries no
    sidecar at all.
    """
    return _call_lines(request).get(session_id)


def _append_call_line(request: Request, session_id: str, kind: str, at: str | None) -> None:
    """Append a "Call started" or "Call ended" line, anchored to the last message.

    The history is touched first, so the line's session is in the history
    store and the line shares its eviction.

    Args:
        request: The active request (owns the stores).
        session_id: The owning session's UUID (string form).
        kind: ``"started"`` or ``"ended"``.
        at: The browser's ``HH:MM``; ``None`` stamps the server's UTC time,
            as a typed turn's bubble is stamped.
    """
    conversation = _get_or_create_history(request, session_id)
    anchor = conversation.messages[-1].id if conversation.messages else ""
    _call_lines(request).setdefault(session_id, []).append(
        {
            "kind": kind,
            "at": at or datetime.now(timezone.utc).strftime("%H:%M"),
            "after_message_id": anchor,
        }
    )


def _call_line_item(line: dict[str, Any]) -> dict[str, Any]:
    """Project one call-line record into its render item."""
    label = "Call started" if line["kind"] == "started" else "Call ended"
    return {"kind": "call_line", "content": f"{label} {line['at']}"}


def _apply_call_events(
    request: Request,
    session_id: str,
    call_id: str,
    events: Sequence[_CallEvent],
) -> int:
    """Append a call's final transcript lines to the history, each once, in order.

    The batch is sorted by ``seq`` and every line at or below the call's
    cursor is skipped, so a batch that arrives out of order or twice — the
    browser retries — appends nothing twice. The lines are already shown on
    the browser's side; nothing is rendered here.

    Args:
        request: The active request (owns the stores).
        session_id: The owning session's UUID (string form).
        call_id: The call's opaque handle.
        events: The relayed lines.

    Returns:
        The number of lines appended.
    """
    key = (session_id, call_id)
    cursor = _call_seq(request).get(key, -1)
    conversation: Conversation | None = None
    appended = 0
    for event in sorted(events, key=lambda item: item.seq):
        if event.seq <= cursor:
            continue
        if conversation is None:
            conversation = _get_or_create_history(request, session_id)
        role = MessageRole.USER if event.role == "user" else MessageRole.ASSISTANT
        conversation.add_message(Message(role=role, content=event.text, channel="call"))
        cursor = event.seq
        appended += 1
    if conversation is not None:
        _advance_call_cursor(request, key, cursor)
        _trim_history(
            conversation,
            _stored_chart_artifacts(request, session_id),
            _stored_call_lines(request, session_id),
        )
    return appended


async def _run_call_job(
    request: Request,
    session: SessionDTO,
    job: CallJob,
    *,
    core: AIServiceCore,
    llm: ResolvedLLM,
    language: str,
) -> None:
    """Run one ``ask_shirley`` job to its end (ADR-0137 §3).

    One ordinary Shirley turn on the session's history, under the turn lock
    and with :data:`CALL_TOOL_ALLOWLIST`, consumed through the typed turn's
    recorder, chart binding and trim; then a short non-streaming rewrite of
    the written answer for the ear. The model is handed a transient copy of
    the history that ends on the question — the agent's question is the
    precise ask, and the user's own words may not have been relayed yet —
    but the question is never stored: the history receives only what the
    recorder collects, every message marked ``channel="call"``.

    Cancellation is the job's flag and nothing else. This task is never
    cancelled and its generator is never closed from another coroutine: the
    turn may be parked in the thread that waits for the turn lock, or inside
    a thread-bound tool, and cancelling that await would leave a thread that
    takes the lock with nobody to release it — while ``aclose()`` on a
    running generator raises. Instead the loop reads the flag at the next
    event, ``aclosing()`` closes the generator at that yield, and the core's
    ``finally`` chain (end of turn, client close, tool context cleared, lock
    released) runs in order; a tool already running completes first. A
    cancelled turn appends nothing. A cancel that arrives after the answer
    was appended cannot take it back — the race ADR-0137 accepts — and
    leaves the job cancelled.

    The job is ``waiting_for_turn`` until the turn's first event, which
    proves the lock is held; nothing peeks at the lock. There is no
    server-side timeout. A failed rewrite never fails the job: the answer is
    in the history, and the fallback sentence says so.

    Any unexpected exception fails the job with a user-safe message and is
    logged — a background task must never die silently.

    Args:
        request: The request that started the job (owns the stores).
        session: The authenticated session.
        job: The job's record, already stored.
        core: The AI core.
        llm: This turn's resolution (ADR-0112 §4b).
        language: The call language, for the fallback sentence.
    """
    session_id = str(session.id)
    try:
        conversation = _get_or_create_history(request, session_id)
        system_prompt = await _briefed_system_prompt(request, session, core.get_system_prompt())
        tool_context = _tool_context_for(request, session)
        model_conversation = Conversation(
            messages=[
                *conversation.messages,
                Message(role=MessageRole.USER, content=job.question, channel="call"),
            ]
        )
        recorder = _TurnRecorder()
        pending_charts: list[dict[str, Any]] = []
        error_message: str | None = None
        finished = False
        done_id = ""
        prose = ""

        async with aclosing(
            core.stream_response(
                model_conversation,
                system_prompt=system_prompt,
                tool_context=tool_context,
                llm=llm,
                tool_names=CALL_TOOL_ALLOWLIST,
            )
        ) as events:
            async for event in events:
                if job.cancel_requested:
                    break  # aclosing() closes the generator; nothing is kept
                if job.state == "waiting_for_turn":
                    job.start()  # the first event proves the lock is ours
                et = event.event_type
                payload = event.payload
                if et in ("tool_called", "tool_completed"):
                    recorder.observe(et, payload)
                elif et == "chart_artifact":
                    # A PNG artefact is not archived, as on the typed turn.
                    if str(payload.get("chart_format", "png")) == "plotly":
                        pending_charts.append(
                            _new_chart_record(
                                payload.get("spec") or {}, str(payload.get("caption", ""))
                            )
                        )
                elif et == "error":
                    error_message = str(payload.get("message", "Unknown error."))
                    break
                elif et == "stream_finished":
                    recorder.observe(et, payload)
                    for collected in recorder.collected():
                        # The final assistant message is the core's own
                        # object; it is marked like every tool round.
                        collected.channel = "call"
                        conversation.messages.append(collected)
                    final_message = payload.get("message")
                    if isinstance(final_message, Message):
                        done_id = final_message.id
                        prose = final_message.content or ""
                    artifacts = _bind_chart_artifacts(request, session_id, pending_charts, done_id)
                    _trim_history(conversation, artifacts, _stored_call_lines(request, session_id))
                    finished = True
                    break

        if job.cancel_requested:
            return
        if error_message is not None:
            # ADR-0050's error-path rule: nothing of the turn is appended, and
            # its charts go with it.
            logger.info("chat-call: error path for job %s — nothing appended.", job.job_id)
            _trim_history(
                conversation,
                _stored_chart_artifacts(request, session_id),
                _stored_call_lines(request, session_id),
            )
            job.fail(error_message)
            return
        if not finished:
            job.fail(_CALL_NO_ANSWER_MESSAGE)
            return

        charts = [
            {
                "artifact_id": record["artifact_id"],
                "caption": record["caption"],
                "spec": record["spec"],
                "oversized": record["oversized"],
            }
            for record in _stored_chart_artifacts(request, session_id) or []
            if done_id and record["message_id"] == done_id
        ]
        speakable: str | None = None
        try:
            result = await core.run_synthesis(
                system_prompt=SPEAKABLE_SYSTEM_PROMPT,
                context_messages=speakable_messages(prose, chart_count=len(charts)),
                tool=dict(SPEAK_TOOL),
                tool_choice=SPEAK_TOOL_CHOICE,
                llm=llm,
                timeout=_SPEAKABLE_TIMEOUT_SECONDS,
            )
            speakable = speakable_from_tool_calls(result.tool_calls)
        except Exception as exc:  # noqa: BLE001 — a failed rewrite speaks the fallback
            logger.warning(
                "chat-call: speakable rewrite failed for job %s (%s) — the fallback is spoken.",
                job.job_id,
                type(exc).__name__,
            )
        if job.is_terminal:
            return  # cancelled while the rewrite ran; the answer stays in the chat
        job.complete(
            speakable=speakable or fallback_for(language),
            prose=prose,
            charts=charts,
            message_id=done_id,
        )
    except Exception as exc:  # noqa: BLE001 — a background task must never die silently
        logger.exception("chat-call: job %s failed unexpectedly", job.job_id)
        if not job.is_terminal:
            job.fail(user_safe_error(exc)[0])


def _require_job(request: Request, session: SessionDTO, job_id: str) -> CallJob:
    """Return the session's job with ``job_id``, or raise 404."""
    job = _call_jobs(request).get(str(session.id), job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown job.")
    return job


async def _require_call_enabled(request: Request, session: SessionDTO) -> None:
    """Raise 404 unless the voice call is switched on for the session's tenant.

    The switch alone, never a key probe — the same answer the dock's call
    button is offered on (ADR-0137 §4).
    """
    if not await _resolve_call_enabled(request, session):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Voice call is not enabled."
        )


def _call_agent_memo(request: Request) -> dict[UUID, tuple[str, str]]:
    """Return (and lazily initialise) the agent memo (``app.state.call_agent_memo``).

    Tenant id → ``(agent_id, definition_hash)``, the pair verified at the
    vendor by this process. One entry per tenant, so it needs no cap.
    """
    store = getattr(request.app.state, "call_agent_memo", None)
    if store is None:
        store = {}
        request.app.state.call_agent_memo = store
    return cast("dict[UUID, tuple[str, str]]", store)


def _call_provision_locks(request: Request) -> dict[UUID, asyncio.Lock]:
    """Return (and lazily initialise) the per-tenant provisioning locks.

    ``app.state.call_provision_locks``: tenant id → the lock that serialises
    that tenant's call starts across the memo check, the vendor's agent read
    or create and the row write. One entry per tenant, so it needs no cap.
    """
    store = getattr(request.app.state, "call_provision_locks", None)
    if store is None:
        store = {}
        request.app.state.call_provision_locks = store
    return cast("dict[UUID, asyncio.Lock]", store)


def _call_provider(resolved: ResolvedCall) -> CallProvider:
    """Build the vendor adapter for one call start (ADR-0137 §2).

    The one construction site, and the seam a test or a second vendor
    replaces.

    Args:
        resolved: The tenant's call resolution, supplying the key and host.

    Returns:
        The :class:`~services.voice_call.CallProvider` for this request.
    """
    return ElevenLabsCallProvider(api_key=resolved.api_key, base_url=resolved.base_url)


async def _store_call_agent_id(request: Request, session: SessionDTO, agent_id: str) -> None:
    """Write a newly provisioned agent id to the tenant's ``voice_call.agent_id`` row.

    The Admin surface's repository call and audit line: ``scoped_settings``
    carries no audit trigger, so the line is the trail — never a value.

    Raises:
        HTTPException: 503 when no database engine is configured.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        await ScopedSettingRepository(db).upsert(
            scope="tenant",
            provider="voice_call",
            key="agent_id",
            is_secret=False,
            value_plain=agent_id,
        )
    logger.info(
        "scoped-setting mutation: scope=%s provider=%s key=%s action=%s actor=%s",
        "tenant",
        "voice_call",
        "agent_id",
        "provision",
        session.user_id,
    )


@router.post(
    "/chat/call/session",
    dependencies=[Depends(require_role("owner", "member"))],
)
async def post_call_session(
    request: Request,
    body: _CallSessionBody | None = None,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Start a voice call: ensure the agent, mint the credential (ADR-0137 §4, §8).

    Provisioning is lazy: the tenant's agent is created at its first call
    start, not at startup or on an Admin save, and its id is written to
    ``voice_call.agent_id``. The definition's hash is carried in the agent's
    name, so a changed definition is a changed hash. At the vendor the agent
    is read back and its hash compared — tool and agent are PATCHed only when
    they differ, and a vanished agent is adopted by name or created afresh.
    A memo per tenant and process remembers the verified pair, so that read
    happens once per process and definition. First starts of one tenant are
    serialised by a per-tenant lock, so two starts arriving together
    provision one agent; a refused credential drops the tenant's memo entry,
    so the next start reads the agent again.

    Of the call itself nothing but the chat history is persisted (ADR-0137
    §10): the session credential is minted on every start and goes to the
    browser alone. The response carries ``vendor``, ``token``,
    ``conversation_id``, ``call_id``, ``max_duration_seconds`` and
    ``language``; the "Call started" line is appended to the history.

    Raises:
        HTTPException: 404 when the call is off; 503 when it is on but
            incomplete (the message names the field and the ``Voice call``
            card) or no database engine is configured; 502 when the vendor
            refuses or fails.
    """
    try:
        resolved = await _resolve_call(request, session)
    except _VoiceUnconfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    provider = _call_provider(resolved)
    definition = build_agent_definition(resolved)
    memo = _call_agent_memo(request)
    lock = _call_provision_locks(request).setdefault(session.tenant_id, asyncio.Lock())
    try:
        async with lock:
            verified = memo.get(session.tenant_id)
            if (
                verified is not None
                and verified[1] == definition.definition_hash
                and resolved.agent_id in (None, verified[0])
            ):
                # Verified by this process — possibly by the start that held
                # the lock a moment ago, after this one's resolution read the
                # row still empty.
                agent_id = verified[0]
            else:
                agent_id = await provider.ensure_agent(definition, resolved.agent_id)
            if agent_id != resolved.agent_id:
                await _store_call_agent_id(request, session, agent_id)
            memo[session.tenant_id] = (agent_id, definition.definition_hash)
        try:
            credentials = await provider.session_credentials(agent_id)
        except CallProviderError:
            # A refused mint may mean the agent is gone at the vendor: forget
            # the verified pair, so the next start reads the agent again.
            memo.pop(session.tenant_id, None)
            raise
    except CallProviderError as exc:
        # The message carries the operation, the status and a scrubbed
        # excerpt — user-safe by the provider's contract.
        logger.warning("chat-call: call start failed for tenant %s: %s", session.tenant_id, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    logger.info(
        "chat-call: session credential minted for tenant %s (vendor %s)",
        session.tenant_id,
        credentials.vendor,
    )

    call_id = uuid.uuid4().hex[:12]
    _append_call_line(request, str(session.id), "started", body.at if body else None)
    return JSONResponse(
        {
            "vendor": credentials.vendor,
            "token": credentials.token,
            "conversation_id": credentials.conversation_id,
            "call_id": call_id,
            "max_duration_seconds": MAX_DURATION_SECONDS,
            "language": resolved.language,
        }
    )


@router.post(
    "/chat/call/ask",
    dependencies=[Depends(require_role("owner", "member"))],
)
async def post_call_ask(
    request: Request,
    body: _CallAskBody,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Start an ``ask_shirley`` job and answer 202 ``{job_id}`` at once (ADR-0137 §3).

    The lines the body carries are relayed first, so the history is in order
    before the question is answered. Shirley's LLM is resolved here, before
    any job exists: an unconfigured tenant gets the 503 a typed turn gets.
    The job then runs in a task that outlives this request.
    """
    await _require_call_enabled(request, session)
    core, llm = await _require_connected_core(request, session)
    session_id = str(session.id)
    _apply_call_events(request, session_id, body.call_id, body.events)

    job = CallJob(job_id=uuid.uuid4().hex[:12], session_id=session_id, question=body.question)
    _call_jobs(request).add(job)
    key = (session_id, job.job_id)
    tasks = _call_tasks(request)
    task = asyncio.create_task(
        _run_call_job(
            request,
            session,
            job,
            core=core,
            llm=llm,
            language=body.language or DEFAULT_LANGUAGE,
        )
    )
    tasks[key] = task
    task.add_done_callback(lambda _done: tasks.pop(key, None))
    return JSONResponse({"job_id": job.job_id}, status_code=status.HTTP_202_ACCEPTED)


@router.get(
    "/chat/call/jobs/{job_id}",
    dependencies=[Depends(require_role("owner", "member"))],
)
async def get_call_job(
    request: Request,
    job_id: str,
    session: SessionDTO = Depends(require_session),
) -> JSONResponse:
    """Report a job's state, and its answer once it is done (ADR-0137 §4)."""
    return JSONResponse(_require_job(request, session, job_id).result())


@router.post(
    "/chat/call/jobs/{job_id}/cancel",
    dependencies=[Depends(require_role("owner", "member"))],
)
async def cancel_call_job(
    request: Request,
    job_id: str,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Cancel a job on barge-in and answer with its state (ADR-0137 §3 step 6).

    Idempotent: a job that already ended keeps its state and says so. The
    running turn notices the flag at its next event (see
    :func:`_run_call_job`).
    """
    job = _require_job(request, session, job_id)
    job.cancel()
    return JSONResponse({"job_id": job.job_id, "state": job.state})


@router.post(
    "/chat/call/events",
    dependencies=[Depends(require_role("owner", "member"))],
)
async def post_call_events(
    request: Request,
    body: _CallEventsBody,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> JSONResponse:
    """Relay a call's final transcript lines into the history (ADR-0137 §5).

    Answers ``{"accepted": n}``, the number of lines appended; a line already
    relayed counts nothing.
    """
    await _require_call_enabled(request, session)
    accepted = _apply_call_events(request, str(session.id), body.call_id, body.events)
    return JSONResponse({"accepted": accepted})


@router.post(
    "/chat/call/end",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def post_call_end(
    request: Request,
    body: _CallEndBody,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Mark the call ended and re-render the history (ADR-0137 §4, §10).

    Appends the "Call ended HH:MM" line — the browser's clock when the body
    carries it — forgets the call's relay cursor, and answers with the
    history fragment ``GET /chat/history`` renders.
    """
    await _require_call_enabled(request, session)
    session_id = str(session.id)
    _append_call_line(request, session_id, "ended", body.at)
    _call_seq(request).pop((session_id, body.call_id), None)
    return _render_history(request, session_id, session.csrf_token)


# ---------------------------------------------------------------------------
# POST /chat/new
# ---------------------------------------------------------------------------


@router.post(
    "/chat/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def new_chat(
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Clear the session's chat history and reset the DOM to empty.

    Returns the empty-state placeholder fragment used in chat.html's
    initial render. The client swaps it into ``#chat-history`` with
    ``hx-target``.
    """
    _drop_history(request, str(session.id))
    templates = _templates(request)
    return cast(
        HTMLResponse,
        templates.TemplateResponse(
            request,
            "_partials/chat_empty.html",
            {},
        ),
    )


# ---------------------------------------------------------------------------
# GET /chat/history
# ---------------------------------------------------------------------------


@router.get("/chat/history", response_class=HTMLResponse)
async def get_chat_history(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the session's history rendered as chat-message partials.

    Used on page load (``hx-trigger="load"``) so a tab reload restores
    the conversation. Empty history returns the empty-state placeholder.

    Only ``user`` and final ``assistant`` messages are rendered — the
    user does not see tool-call internals in the UI; on reload we
    restore the prose surface only.

    Charts *are* restored (ADR-0114, resolving the artefact-rehydration
    strand ADR-0050 deferred): the session's sidecar records are
    interleaved at their message positions, each carrying the frozen
    Plotly spec the browser re-plots. Nothing is recomputed — the
    restored figure is the artefact, not a fresh query. An artefact that
    was too large to archive renders a calm placeholder in its place.

    A voice call's bubbles carry the "Call" chip and its start and end
    lines are restored at their positions (ADR-0137 §5).
    """
    return _render_history(request, str(session.id), session.csrf_token)


def _render_history(request: Request, session_id: str, csrf_token: str) -> HTMLResponse:
    """Render the session's history fragment — shared by the reload and the call's end.

    The empty-state placeholder is returned only when the session has
    neither messages nor call lines.

    Args:
        request: The active request (carries the Jinja environment).
        session_id: The owning session's UUID (string form).
        csrf_token: The session's CSRF token — the Keep forms under the
            rehydrated answers and figures post it (D-CS-B-95).

    Returns:
        ``_partials/chat_history.html``, or ``_partials/chat_empty.html``.
    """
    templates = _templates(request)
    conversation = _chat_histories(request).get(session_id)
    messages = conversation.messages if conversation is not None else []
    lines = _stored_call_lines(request, session_id) or []
    if not messages and not lines:
        return cast(
            HTMLResponse,
            templates.TemplateResponse(request, "_partials/chat_empty.html", {}),
        )

    items = _history_items(
        conversation or Conversation(),
        _stored_chart_artifacts(request, session_id) or [],
        lines,
    )
    return cast(
        HTMLResponse,
        templates.TemplateResponse(
            request,
            "_partials/chat_history.html",
            {"items": items, "csrf_token": csrf_token, "keep_url": KEEP_URL},
        ),
    )


# ---------------------------------------------------------------------------
# GET /chat/dock — the one chat instance, loaded on first open
# ---------------------------------------------------------------------------


@router.get("/chat/dock", response_class=HTMLResponse)
async def get_chat_dock(
    request: Request,
    case: str | None = None,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return Shirley's conversation as a bare fragment for the dock.

    P-UX-A0e made Shirley a shell element (record §2.10): the rail, the
    dock host and the stage host render on **every** page, but empty.
    ``#dock-chat-host`` fetches this fragment once per page life, on the
    first open (``hx-trigger="pf:shirley-open once"``), and ``shirley.js``
    then *moves* that one subtree between the dock and the stage.

    Loading it lazily rather than inlining it in ``base.html`` is what
    keeps every area render as DB-free as it was: the three reads below —
    the per-tenant voice chain, the voice-call switch and the case-brief
    validation — are paid
    once, when the reader actually opens the conversation, not on each of
    the nine Areas. The history inside the fragment stays lazy too: the
    ``#chat-history`` div carries its own ``hx-get="/chat/history"`` on
    ``load``, which fires when HTMX processes this swap.

    ``brief_banner`` is resolved from the **session stash**, so a ``?case=``
    set earlier by ``GET /assistants`` is picked up here — the marker sets
    the stash on the area page, the dock renders the banner from it. A stale
    stash (case closed or vanished) clears silently and yields no banner.

    ``case`` is the dock's own marker (D-CS-B-111): ``base.html`` writes it
    into the dock host's URL on a case page (``cs_case_id``), so a dock
    opened there — Ctrl J, the rail — is briefed on that case without a
    click. It has the ``/assistants`` marker's semantics exactly
    (:func:`resolve_active_brief_banner`): an open or waiting case replaces
    the stash — a different case or a briefing included; a closed, unknown,
    foreign or malformed one is dropped silently. Every other page keeps the
    host bare.

    Args:
        request: The request.
        case: The case page's case id, or ``None``.
        session: The authenticated session.

    Returns:
        The ``_partials/shirley_dock.html`` fragment — no shell, no
        ``<html>`` wrapper.
    """
    templates = _templates(request)
    stt_active, tts_active = await _resolve_voice_flags(request, session)
    call_active = await _resolve_call_enabled(request, session)
    return cast(
        HTMLResponse,
        templates.TemplateResponse(
            request,
            "_partials/shirley_dock.html",
            {
                "csrf_token": session.csrf_token,
                "voice_stt_enabled": stt_active,
                "voice_tts_enabled": tts_active,
                "voice_call_enabled": call_active,
                "brief_banner": await resolve_active_brief_banner(request, session, case),
                "summarise_url": PROPOSALS_SUMMARISE_URL,
                "draft_url": CASE_DRAFT_URL,
            },
        ),
    )


# ---------------------------------------------------------------------------
# POST /chat/brief/dismiss — clear the active consultation brief
# ---------------------------------------------------------------------------


@router.post(
    DISMISS_BRIEF_URL,
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def dismiss_brief(
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Clear the session's active brief — case or briefing — and remove the banner.

    The banner's dismiss posts here; the empty body swaps the banner out of the
    DOM. Subsequent turns run unbriefed until a new ``?case=`` marker or a
    Watch Desk hand-off sets the stash again. Safe to call with no active
    stash. ``HX-Trigger: pf:brief-changed`` makes the container fetch the
    banner again, so the dock's actions row — *Make this a case* stays
    without a brief (D-CS-B-126) — comes back at once.
    """
    _clear_active_brief(request, str(session.id))
    return HTMLResponse("", headers={"HX-Trigger": BRIEF_CHANGED_EVENT})


# ---------------------------------------------------------------------------
# POST /chat/brief/watch-briefing/{id} — hand a written briefing to Shirley
# in place (ADR-0133 §6, D-WD-23); GET /chat/brief/banner — the banner alone
# ---------------------------------------------------------------------------


@router.post(
    DISCUSS_BRIEFING_URL,
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def discuss_watch_briefing(
    request: Request,
    briefing_id: str,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Set the session's consultation context to a written briefing (ADR-0133 §6).

    The id is resolved inside the reader's tenant context. A ``written`` row
    sets the ``watch_briefing`` stash — replacing a case stash — and the
    response carries ``HX-Trigger`` so the shell opens the dock
    (``pf:shirley-dock``) and a dock that is already open re-renders its
    banner (``pf:brief-changed``); the reader stays on the Watch Desk. A
    malformed, unknown, foreign-tenant (RLS → ``None``) or skipped id is
    dropped silently (obligation 17): no stash change, no trigger, no error
    — the action simply re-renders. The body is the action itself, which
    the form swaps in place; nothing is written anywhere.
    """
    session_id = str(session.id)
    row: IreneBriefingDTO | None = None
    try:
        parsed: UUID | None = UUID(briefing_id)
    except ValueError:
        parsed = None
    if parsed is not None:
        engine = _engine(request)
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            row = await IreneBriefingRepository(db).get(parsed)
    headers: dict[str, str] = {}
    if row is not None and row.status == "written":
        _set_active_brief(request, session_id, ActiveBrief(BRIEF_KIND_WATCH_BRIEFING, row.id))
        headers["HX-Trigger"] = _DISCUSS_TRIGGER
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/watch_desk_discuss_action.html",
            {"csrf_token": session.csrf_token, "briefing_id": briefing_id},
            headers=headers,
        ),
    )


@router.post(
    DISCUSS_CASE_URL,
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def discuss_case(
    request: Request,
    case_id: str,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Hand a case to Shirley in place from its page (ADR-0138 §10, D-CS-B-110).

    The case page's *Consult Shirley* posts here — the D-WD-23 idiom of the
    Watch Desk's briefing hand-off — so the PM stays on the case. The id is
    resolved inside the reader's tenant context. An **open or waiting** case
    sets the ``case`` stash, replacing a briefing or another case; the
    stash's thread is kept when it already names this case (D-CS-B-86), else
    the file is the keep target. The response then carries ``HX-Trigger`` so
    the shell opens the dock (``pf:shirley-dock``) and a dock already open
    re-renders its banner (``pf:brief-changed``). A closed, unknown,
    foreign-tenant (RLS → ``None``) or malformed id is dropped silently: no
    stash change, no trigger, no error. The body is the action itself, which
    the form swaps in place; nothing is written anywhere.
    """
    session_id = str(session.id)
    case: CaseDTO | None = None
    try:
        parsed: UUID | None = UUID(case_id)
    except ValueError:
        parsed = None
    if parsed is not None:
        engine = _engine(request)
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            case = await CaseRepository(db).get(parsed)
    headers: dict[str, str] = {}
    if case is not None and case.state in _MUTABLE_STATES:
        stashed = _get_active_brief(request, session_id)
        keeps_thread = (
            stashed is not None and stashed.kind == BRIEF_KIND_CASE and stashed.id == case.id
        )
        thread_id = stashed.thread_id if keeps_thread and stashed is not None else None
        _set_active_brief(request, session_id, ActiveBrief(BRIEF_KIND_CASE, case.id, thread_id))
        headers["HX-Trigger"] = _DISCUSS_TRIGGER
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/cases_consult_action.html",
            {
                "csrf_token": session.csrf_token,
                "consult_shirley_post": DISCUSS_CASE_URL.format(case_id=case_id),
            },
            headers=headers,
        ),
    )


@router.get(BRIEF_BANNER_URL, response_class=HTMLResponse)
async def get_brief_banner(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Re-render the dock's consultation banner from the current stash.

    Fetched by ``#chat-brief-banner`` on ``pf:brief-changed`` — the trigger a
    hand-off response carries — so a dock that is already open shows the new
    context; a dock opened later renders the banner with its fragment. No
    marker of its own, like ``GET /chat/dock``: the stash is validated fresh
    and a stale one yields the empty banner — the actions row stays
    (D-CS-B-126). The draft form's *Cancel* fetches it too.
    """
    return await _render_banner(request, session)


# ---------------------------------------------------------------------------
# Proposals (ADR-0138 §8, D-CS-M-12) — the dock's two gestures under the
# banner: *Summarise into the file* on the briefed case and *Make this a case*.
# Each is one synchronous Shirley one-shot through ``run_synthesis`` with its
# tool forced and no registry tool offered, so it is Fetcher-free by
# construction: it reads the working file as text and the session's
# conversation, nothing else (``services/cases/proposals.py`` builds both and
# parses what comes back). Nothing reaches a working file here: a package
# lands on ``case_proposals`` for the PM to review item by item in the case,
# and a draft is a form the PM confirms before the case exists.
# ---------------------------------------------------------------------------

_BRIEF_BANNER_TEMPLATE: str = "_partials/chat_brief_banner.html"

#: What *Summarise into the file* asks: the package's request text, which its
#: ``job_requested`` line quotes and the review block repeats.
_SUMMARISE_REQUEST_TEXT: str = "Summarise this conversation into the file"

#: The one-shot's per-request timeout, in seconds (D-CS-B-122).
_ONE_SHOT_TIMEOUT_SECONDS: float = 60.0

# The banner notices (D-5c-2) and the draft form's hint (D-5c-5).
_LANDED_NOTICE: str = "Proposals landed in {badge} — review {count} {noun}"
_NOTHING_TO_PROPOSE_NOTICE: str = "Shirley found nothing to propose from this conversation."
_NO_ANSWER_NOTICE: str = "Shirley could not answer — try again."
_NO_CASE_NOTICE: str = "Consult a case first — the dock has no case."
_NOTHING_TO_DRAFT_NOTICE: str = "Nothing to draft yet — talk to Shirley first."
_NO_LLM_NOTICE: str = "Shirley is not configured for this tenant."
_REOPENED_SUFFIX: str = " — the case is open again"
_DRAFT_FAILED_HINT: str = "Shirley could not draft this — fill it in yourself."


def _notice(text: str, *, href: str | None = None, reopened: bool = False) -> dict[str, Any]:
    """A banner notice: its line, the link it may carry, and the wake it may report.

    A case the request woke says so at the end of the line (D-CS-B-127); a
    line's own full stop moves behind the suffix.
    """
    if reopened:
        stop = "." if text.endswith(".") else ""
        text = text.removesuffix(".") + _REOPENED_SUFFIX + stop
    return {"text": text, "href": href}


async def _banner_context(
    request: Request, session: SessionDTO, *, notice: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The banner's context: the brief validated fresh, the two gestures, the notice."""
    return {
        "csrf_token": session.csrf_token,
        "brief_banner": await resolve_active_brief_banner(request, session, None),
        "summarise_url": PROPOSALS_SUMMARISE_URL,
        "draft_url": CASE_DRAFT_URL,
        "notice": notice,
    }


async def _render_banner(
    request: Request,
    session: SessionDTO,
    *,
    notice: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    """Render ``#chat-brief-banner``'s content: the banner, the actions row, a notice."""
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            _BRIEF_BANNER_TEMPLATE,
            await _banner_context(request, session, notice=notice),
            headers=headers,
        ),
    )


def _has_user_turn(conversation: Conversation) -> bool:
    """Whether the PM has said anything in this session's conversation."""
    return any(
        message.role == MessageRole.USER and (message.content or "").strip()
        for message in conversation.messages
    )


def _conversation_for(request: Request, session_id: str) -> str:
    """The session's conversation as the one-shot reads it (:func:`conversation_text`)."""
    return conversation_text(_get_or_create_history(request, session_id).messages)


async def _connected_or_none(
    request: Request, session: SessionDTO
) -> tuple[AIServiceCore, ResolvedLLM] | None:
    """The core and this request's resolution, or ``None`` when the tenant has no LLM.

    :func:`_require_connected_core`'s 503 becomes the banner's calm line: a
    5xx is not swapped into the dock, so the PM would see nothing at all.
    """
    try:
        return await _require_connected_core(request, session)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE:
            raise
        return None


async def _one_shot(
    core: AIServiceCore,
    llm: ResolvedLLM,
    *,
    system_prompt: str,
    messages: list[dict[str, str]],
    tool: Mapping[str, Any],
) -> SynthesisResult | None:
    """One non-streaming call with ``tool`` forced (D-CS-B-122); ``None`` on any failure.

    The speakable idiom (``services/voice_call/speakable.py``): the tool is
    the request's only one and its name the forced choice, so no registry
    tool can run. A timeout, an SDK error or any other exception is logged
    as a warning and answers ``None`` — the caller lands an empty package or
    renders an empty form, never an error page.
    """
    name = str(tool["function"]["name"])
    try:
        return await core.run_synthesis(
            system_prompt=system_prompt,
            context_messages=list(messages),
            tool=dict(tool),
            tool_choice={"type": "function", "function": {"name": name}},
            llm=llm,
            timeout=_ONE_SHOT_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — a failed one-shot answers calmly
        logger.warning("chat proposals: the %s one-shot failed (%s).", name, type(exc).__name__)
        return None


@dataclass(frozen=True)
class _RequestedPackage:
    """What the first transaction of *Summarise into the file* hands the one-shot."""

    case: CaseDTO
    proposal_id: UUID
    file_text: str
    live_threads: dict[str, UUID]
    reopened: bool


async def _request_package(
    db: Any, case_id: UUID, session: SessionDTO, *, now: datetime
) -> _RequestedPackage | None:
    """Request the package and write ``job_requested`` — one transaction (D-CS-B-123).

    The file text and the live threads are read first, as the PM sees them.
    ``job_requested`` lifts a waiting case (the 2c write-and-wake contract,
    D-CS-B-127); ``reopened`` reports it. A closed or vanished case answers
    ``None`` and nothing is written.
    """
    cases = CaseRepository(db)
    case = await cases.get(case_id)
    if case is None or case.state not in _MUTABLE_STATES:
        return None
    file_text = await _case_file_text(db, case)
    threads = await CaseThreadRepository(db).list_for_case(case.id)
    proposal = await CaseProposalRepository(db).request(
        case.id,
        requested_by=session.user_id,
        request_text=_SUMMARISE_REQUEST_TEXT,
        now=now,
    )
    await job_requested(
        cases,
        case=case,
        proposal_id=proposal.id,
        request_text=_SUMMARISE_REQUEST_TEXT,
        now=now,
    )
    after = await cases.get(case.id) or case
    return _RequestedPackage(
        case=after,
        proposal_id=proposal.id,
        file_text=file_text,
        live_threads={thread.title: thread.id for thread in threads},
        reopened=case.state == "waiting" and after.state == "open",
    )


@router.post(
    PROPOSALS_SUMMARISE_URL,
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def post_proposals_summarise(
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """*Summarise into the file*: one package for the briefed case (ADR-0138 §8, D-CS-B-123).

    The brief's case (open or waiting) is the target; no case brief, a stash
    closed or vanished since, no LLM for the tenant, or a conversation in
    which the PM has said nothing answers the banner with its calm line and
    writes nothing. Otherwise, in one transaction, the package is requested
    on ``case_proposals`` and ``job_requested`` written; the one-shot runs
    outside any transaction (:func:`_one_shot`, ``PROPOSAL_TOOL`` forced);
    then, in a second transaction, the normalised items land and
    ``job_landed`` follows with their count. A failed or empty one-shot
    still lands, empty (D-CS-B-122): the record says what was asked and
    what came back. Nothing reaches the working file — the PM accepts item
    by item in the case.

    Answers the banner re-rendered with the result line — *Proposals landed
    in CASE-nnnn — review n items*, linking to the case's review block, or
    *Shirley found nothing to propose …* / *Shirley could not answer …* —
    suffixed *the case is open again* when the request woke it; and
    ``HX-Trigger: pf:case-kept`` with the case id, so a case page on screen
    re-fetches its blocks and shows the package (D-CS-B-88). A second
    request while a package is pending lands a second package.
    """
    session_id = str(session.id)
    case_id = _active_case_id(request, session_id)
    if case_id is None:
        return await _render_banner(request, session, notice=_notice(_NO_CASE_NOTICE))
    connected = await _connected_or_none(request, session)
    if connected is None:
        return await _render_banner(request, session, notice=_notice(_NO_LLM_NOTICE))
    core, llm = connected
    if not _has_user_turn(_get_or_create_history(request, session_id)):
        return await _render_banner(request, session, notice=_notice(_NOTHING_TO_PROPOSE_NOTICE))
    conversation = _conversation_for(request, session_id)
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        requested = await _request_package(db, case_id, session, now=datetime.now(timezone.utc))
    if requested is None:  # closed or gone since the stash; the banner render clears it
        return await _render_banner(request, session, notice=_notice(_NO_CASE_NOTICE))
    case = requested.case

    result = await _one_shot(
        core,
        llm,
        system_prompt=PROPOSAL_SYSTEM_PROMPT,
        messages=proposal_messages(requested.file_text, conversation),
        tool=PROPOSAL_TOOL,
    )
    calls = result.tool_calls if result is not None else []
    items, reasons = items_from_tool_calls(calls, live_threads=requested.live_threads)
    for reason in reasons:
        logger.info(
            "chat proposals: case=%s proposal=%s — %s", case.id, requested.proposal_id, reason
        )
    answered = any(call.get("name") == PROPOSAL_TOOL_NAME for call in calls)

    landed_at = datetime.now(timezone.utc)
    try:
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            await CaseProposalRepository(db).land(requested.proposal_id, items=items, now=landed_at)
            cases = CaseRepository(db)
            await job_landed(
                cases,
                case=await cases.get(case.id) or case,
                proposal_id=requested.proposal_id,
                item_count=len(items),
                now=landed_at,
            )
    except (CaseClosedError, CaseStateInvalid):
        logger.warning(
            "chat proposals: case %s closed before proposal %s landed.",
            case.id,
            requested.proposal_id,
        )
        return await _render_banner(request, session, notice=_notice(_NO_CASE_NOTICE))

    logger.info(
        "chat proposals: tenant=%s user=%s case=%s proposal=%s items=%d answered=%s",
        session.tenant_id,
        session.user_id,
        case.id,
        requested.proposal_id,
        len(items),
        answered,
    )
    if items:
        notice = _notice(
            _LANDED_NOTICE.format(
                badge=f"CASE-{case.case_number:04d}",
                count=len(items),
                noun="item" if len(items) == 1 else "items",
            ),
            href=f"/cases/{case.id}#cs-proposals",
            reopened=requested.reopened,
        )
    else:
        notice = _notice(
            _NOTHING_TO_PROPOSE_NOTICE if answered else _NO_ANSWER_NOTICE,
            reopened=requested.reopened,
        )
    return await _render_banner(
        request,
        session,
        notice=notice,
        headers={"HX-Trigger": keep_trigger(case.id, brief_changed=False)},
    )


def _render_draft(
    request: Request, session: SessionDTO, draft: Mapping[str, Any] | None
) -> HTMLResponse:
    """The draft form, prefilled from ``draft`` — or empty, with the hint, without one."""
    first = draft.get("first_thread") if draft is not None else None
    form = {
        "title": draft["title"] if draft is not None else "",
        "framing": draft["framing"] if draft is not None else "question",
        "description": draft["question"] if draft is not None else "",
        "thread_title": first["title"] if first else "",
        "thread_prose": first["prose"] if first else "",
    }
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            CASE_DRAFT_TEMPLATE,
            {
                "csrf_token": session.csrf_token,
                "form": form,
                "hint": _DRAFT_FAILED_HINT if draft is None else None,
            },
        ),
    )


@router.post(
    CASE_DRAFT_URL,
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def post_case_draft(
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """*Make this a case*: a draft of a new case, from the conversation (D-CS-B-126).

    The conversation needs at least one PM turn (else the banner's calm
    *Nothing to draft yet …*) and the tenant an LLM (else *Shirley is not
    configured …*). One one-shot (``CASE_DRAFT_TOOL`` forced) drafts title,
    framing, the question and a first thread; the banner container then
    holds the form ``chat_case_draft.html`` — prefilled, or empty with the
    hint *Shirley could not draft this — fill it in yourself.* when the call
    failed or drafted nothing. Nothing is written here: the form posts to
    ``POST /api/cases/new``, and the case exists only then. Any case brief
    stays as it is — a new case from this conversation is legitimate while
    another is briefed.
    """
    session_id = str(session.id)
    if not _has_user_turn(_get_or_create_history(request, session_id)):
        return await _render_banner(request, session, notice=_notice(_NOTHING_TO_DRAFT_NOTICE))
    connected = await _connected_or_none(request, session)
    if connected is None:
        return await _render_banner(request, session, notice=_notice(_NO_LLM_NOTICE))
    core, llm = connected
    result = await _one_shot(
        core,
        llm,
        system_prompt=CASE_DRAFT_SYSTEM_PROMPT,
        messages=draft_messages(_conversation_for(request, session_id)),
        tool=CASE_DRAFT_TOOL,
    )
    draft = draft_from_tool_calls(result.tool_calls) if result is not None else None
    logger.info(
        "chat case draft: tenant=%s user=%s drafted=%s",
        session.tenant_id,
        session.user_id,
        draft is not None,
    )
    return _render_draft(request, session, draft)


# ---------------------------------------------------------------------------
# Keep (ADR-0138 §8 channel 2, §9) — every Shirley answer and every figure
# carries "Keep →" (one click into the active target) and "Keep to…" (the
# picker). No comment, no dialog: the consultation and chart pin dialogs
# (ADR-0107 C6, ADR-0114 §2) are retired. The three endpoints answer into the
# bubble's own ``.chat-keep`` container (``hx-target="closest .chat-keep"``,
# ``outerHTML``), so the gestures, the picker and the kept line take turns in
# one place. Transport by reference: the client posts a message id or the
# sidecar ``artifact_id``; the server resolves the artefact.
# ---------------------------------------------------------------------------

_KEEP_ACTIONS_TEMPLATE: str = "_partials/chat_keep_actions.html"
_KEEP_PICKER_TEMPLATE: str = "_partials/chat_keep_picker.html"
_KEEP_DONE_TEMPLATE: str = "_partials/chat_keep_done.html"


def _chart_unavailable_message(record: dict[str, Any] | None) -> str | None:
    """Return the calm state a chart handle resolves to, or ``None`` when fine.

    Two distinct non-keepable states, told apart because the remedies differ:
    a handle that no longer resolves (trimmed, a new chat, a restart, or a
    stale id) versus a figure that was streamed live but never archived
    because it exceeded the cap.
    """
    if record is None:
        return _CHART_UNAVAILABLE_MESSAGE
    if record["oversized"] or record["spec"] is None:
        return _CHART_OVERSIZED_MESSAGE
    return None


def _keep_ref(message_id: str, artifact_id: str) -> dict[str, str]:
    """The artefact one ``.chat-keep`` container speaks of.

    ``kind`` is ``chart`` (a sidecar handle) or ``message`` (an assistant
    message id); ``ref`` is the handle itself, ``ref_query`` the query string
    the container's GETs carry.
    """
    if artifact_id:
        return {
            "kind": "chart",
            "message_id": "",
            "artifact_id": artifact_id,
            "ref": artifact_id,
            "ref_query": urlencode({"artifact_id": artifact_id}),
        }
    return {
        "kind": "message",
        "message_id": message_id,
        "artifact_id": "",
        "ref": message_id,
        "ref_query": urlencode({"message_id": message_id}),
    }


def _keep_context(session: SessionDTO, ref: dict[str, str], **extra: Any) -> dict[str, Any]:
    """The shared context of the three Keep partials (and the kept line's Undo)."""
    return {
        **ref,
        "csrf_token": session.csrf_token,
        "keep_url": KEEP_URL,
        "actions_url": KEEP_ACTIONS_URL,
        "picker_url": KEEP_PICKER_URL,
        "undo_url": KEEP_UNDO_URL,
        "cases_area_url": _CASES_AREA_URL,
        **extra,
    }


def _keep_render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    """Render one Keep partial (200, the composer idiom)."""
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(request, template, context, headers=headers),
    )


def _resolve_keep_artifact(
    request: Request, session_id: str, ref: dict[str, str]
) -> tuple[str, dict[str, Any], str] | str:
    """Resolve the artefact to ``(class, payload, caption)``, or the calm state's text.

    The payloads are the pin contracts minus the comment (D-CS-B-85): a chart
    is ``{"caption", "spec"}`` from *this* session's sidecar — a stale handle
    or an oversized record is its calm message; an answer is ``{"excerpt"}``,
    the message's text as stored — one that left the history, or a
    chart-only answer without prose, is the calm "no longer available".
    """
    if ref["kind"] == "chart":
        record = _find_chart_artifact(request, session_id, ref["artifact_id"])
        unavailable = _chart_unavailable_message(record)
        if unavailable is not None or record is None:
            return unavailable or _CHART_UNAVAILABLE_MESSAGE
        caption = str(record["caption"] or "")
        return _CHART_SNAPSHOT_ARTIFACT, {"caption": caption, "spec": record["spec"]}, caption
    text = _assistant_message_text(_chat_histories(request).get(session_id), ref["message_id"])
    if not text or not text.strip():
        return _MESSAGE_UNAVAILABLE_MESSAGE
    return _CONSULTATION_ARTIFACT, {"excerpt": text}, consultation_caption(text)


async def _picker_context(
    db: Any,
    session: SessionDTO,
    ref: dict[str, str],
    *,
    target: KeepTarget | None,
    requested: str = "",
    error: str | None = None,
) -> dict[str, Any]:
    """The picker's context: :func:`web.case_keep.picker_options` plus the artefact.

    With no case that takes writes the partial renders the no-case state
    (D-4a-5).
    """
    options, selected = await picker_options(db, requested=requested, target=target)
    return _keep_context(session, ref, options=options, selected_thread=selected, error=error)


@router.get(KEEP_ACTIONS_URL, response_class=HTMLResponse)
async def get_keep_actions(
    request: Request,
    message_id: str = "",
    artifact_id: str = "",
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """The two gestures under one answer or figure — ``Keep →`` · ``Keep to…`` (D-CS-B-87).

    Loaded by the live path (``chat.js`` appends a placeholder that fetches
    this on ``load``) and rendered inline by the rehydrated history, so the
    gestures' markup has one source; the picker's Cancel re-renders it. An
    answer without prose (a chart-only turn) has nothing to keep and answers
    an empty body, as does a request naming neither handle.
    """
    if not message_id and not artifact_id:
        return HTMLResponse("")
    ref = _keep_ref(message_id, artifact_id)
    if ref["kind"] == "message":
        text = _assistant_message_text(_chat_histories(request).get(str(session.id)), message_id)
        if text is not None and not text.strip():
            return HTMLResponse("")
    return _keep_render(request, _KEEP_ACTIONS_TEMPLATE, _keep_context(session, ref))


@router.get(KEEP_PICKER_URL, response_class=HTMLResponse)
async def get_keep_picker(
    request: Request,
    message_id: str = "",
    artifact_id: str = "",
    case_id: str = "",
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """The picker for one artefact (D-CS-B-87): ``case_id`` when it takes writes, else the target's.

    Re-rendered by the *Another case…* select (``hx-include="closest form"``):
    the case's live threads and *The file* as radios, the other open and
    waiting cases in the select. Nothing is written.
    """
    ref = _keep_ref(message_id, artifact_id)
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        target = await resolve_keep_target(db, brief=_get_active_brief(request, str(session.id)))
        context = await _picker_context(db, session, ref, target=target, requested=case_id)
    return _keep_render(request, _KEEP_PICKER_TEMPLATE, context)


@router.post(KEEP_URL, response_class=HTMLResponse)
async def post_keep(  # noqa: PLR0913 — one flat form
    request: Request,
    message_id: str = Form(""),
    artifact_id: str = Form(""),
    case_id: str = Form(""),
    thread_id: str = Form(""),
    pick: str = Form(""),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Keep an answer or a figure into a case (ADR-0138 §8 channel 2, §9; D-CS-B-85…88).

    Gates in order, nothing written on any of them: the artefact resolves
    (the calm unavailable / oversized states otherwise) → a target exists —
    the posted ``case_id`` / ``thread_id`` from the picker (``""`` = the
    file), else the session's target (:func:`resolve_keep_target`); none,
    or ``pick=1`` (*Keep to…*), answers the picker → the case still takes
    writes (else the picker with *That case is closed or gone*; a thread
    removed since, *That thread is gone*). Then one ``case_kept`` row,
    ``via="keep"``, no comment.

    A posted target that differs from the stash becomes the brief — one
    active case per session, the brief and the keep target the same
    (D-CS-B-86). The kept line replaces the gestures; ``HX-Trigger`` carries
    ``pf:case-kept`` with the case id (a case on screen re-renders its
    Threads block) and ``pf:brief-changed`` when the brief changed (the
    dock's banner follows).
    """
    session_id = str(session.id)
    ref = _keep_ref(message_id, artifact_id)
    resolved = _resolve_keep_artifact(request, session_id, ref)
    if isinstance(resolved, str):
        return _keep_render(
            request, _KEEP_ACTIONS_TEMPLATE, _keep_context(session, ref, unavailable=resolved)
        )
    artifact, payload, caption = resolved
    stashed = _get_active_brief(request, session_id)

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        current = await resolve_keep_target(db, brief=stashed)
        target: KeepTarget | None = current
        refusal: str | None = None
        if case_id.strip():
            posted = await posted_target(db, case_id=case_id, thread_id=thread_id)
            if isinstance(posted, str):
                refusal, target = posted, None
            else:
                target = posted
        elif pick == "1":
            target = None
        if target is not None:
            try:
                kept = await keep_artifact(
                    db,
                    target=target,
                    artifact=artifact,
                    payload=payload,
                    caption=caption,
                    kept_by=session.user_id,
                    now=datetime.now(timezone.utc),
                )
            except (CaseClosedError, CaseStateInvalid):
                # Closed or vanished between the gate and the write.
                refusal, target = KEEP_CASE_GONE_MESSAGE, None
            except CaseWorkingFileInvalid as exc:
                if exc.field != "thread_id":
                    raise
                refusal, target = KEEP_THREAD_GONE_MESSAGE, None
        if target is None:
            context = await _picker_context(
                db,
                session,
                ref,
                target=current,
                requested=case_id if refusal == KEEP_THREAD_GONE_MESSAGE else "",
                error=refusal,
            )
            return _keep_render(request, _KEEP_PICKER_TEMPLATE, context)
        line = kept_line_context(kept, target)

    # A target the picker posted becomes the brief (one active case per
    # session, D-CS-B-86); the default target already is the brief.
    kept_brief = ActiveBrief(
        BRIEF_KIND_CASE, target.case.id, target.thread.id if target.thread is not None else None
    )
    brief_changed = bool(case_id.strip()) and kept_brief != stashed
    if brief_changed:
        _set_active_brief(request, session_id, kept_brief)
    logger.info(
        "chat keep: tenant=%s user=%s case=%s artifact=%s thread=%s",
        session.tenant_id,
        session.user_id,
        target.case.id,
        artifact,
        target.thread.id if target.thread is not None else None,
    )
    return _keep_render(
        request,
        _KEEP_DONE_TEMPLATE,
        _keep_context(session, ref, **line),
        headers={"HX-Trigger": keep_trigger(target.case.id, brief_changed=brief_changed)},
    )


@router.post(KEEP_UNDO_URL, response_class=HTMLResponse)
async def post_keep_undo(
    request: Request,
    kept_id: str = Form(""),
    message_id: str = Form(""),
    artifact_id: str = Form(""),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Undo a keep from its kept line (D-CS-B-99): the row's ``removed_at``, never a delete.

    Answers the two gestures again in the artefact's container, so the PM can
    keep elsewhere, with ``HX-Trigger`` ``pf:case-kept`` (a case on screen
    re-renders its Threads block without the row). A row already removed, an
    unknown or another tenant's id answers the calm *Already removed.*; a
    case closed since, *That case is closed.* — the container's calm state,
    nothing written (:func:`web.case_keep.undo_keep`).
    """
    ref = _keep_ref(message_id, artifact_id)
    try:
        kept_uuid = UUID(kept_id.strip())
    except ValueError:
        kept_uuid = None
    if kept_uuid is None:
        return _keep_render(
            request,
            _KEEP_ACTIONS_TEMPLATE,
            _keep_context(session, ref, unavailable=UNDO_ALREADY_REMOVED_MESSAGE),
        )
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        undone = await undo_keep(db, kept_id=kept_uuid)
    if isinstance(undone, str):
        return _keep_render(
            request, _KEEP_ACTIONS_TEMPLATE, _keep_context(session, ref, unavailable=undone)
        )
    logger.info(
        "chat keep undo: tenant=%s user=%s case=%s kept=%s",
        session.tenant_id,
        session.user_id,
        undone.case_id,
        undone.id,
    )
    return _keep_render(
        request,
        _KEEP_ACTIONS_TEMPLATE,
        _keep_context(session, ref),
        headers={"HX-Trigger": keep_trigger(undone.case_id, brief_changed=False)},
    )


# ---------------------------------------------------------------------------
# Shirley's writes — the dock line and its Undo (ADR-0138 §8 channel 3, §10)
#
# Every ``case_add`` write reaches the browser as a ``case_write`` SSE event;
# chat.js appends a placeholder to the bubble's tools container that fetches
# the line below on ``load`` (the Keep idiom), so the markup has one source.
# The line, its Undo and the calm states take turns in one ``.chat-write``
# (``hx-target="closest .chat-write"``, ``outerHTML``). Live only: a reload
# shows no write line — the file is the record, and each row carries Remove.
# ---------------------------------------------------------------------------

_CASE_WRITE_LINE_TEMPLATE: str = "_partials/chat_write_line.html"

#: The dock line after an Undo (D-5b-4).
_CASE_WRITE_UNDONE_MESSAGE: str = "Undone — removed from {badge}."


def _case_write_render(
    request: Request,
    session: SessionDTO,
    *,
    state: str,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> HTMLResponse:
    """Render the dock line in one of its states: ``line``, ``undone`` or ``unavailable``."""
    context = {
        "state": state,
        "csrf_token": session.csrf_token,
        "csrf_field": _CSRF_FIELD,
        "undo_url": CASE_WRITE_UNDO_URL,
        **extra,
    }
    return _keep_render(request, _CASE_WRITE_LINE_TEMPLATE, context, headers=headers)


@router.get(CASE_WRITE_LINE_URL, response_class=HTMLResponse)
async def get_case_write_line(  # noqa: PLR0913 — the event's flat fields
    request: Request,
    badge: str = "",
    case_id: str = "",
    kind: str = "",
    summary: str = "",
    row_id: str = "",
    appended: str = "",
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """The dock line of one write: *Added to CASE-nnnn · {kind}: {summary} · Undo* (D-CS-B-115).

    Rendered from the event's fields; a prose write reads its thread's title
    for the label. A request without a usable case, row or kind answers an
    empty body. ``HX-Trigger`` carries ``pf:case-kept`` with the case id, so
    a case page on screen re-fetches its blocks and shows the new row — the
    keep's posture (D-CS-B-88).
    """
    case_uuid = _parse_case_marker(case_id.strip())
    row_uuid = _parse_case_marker(row_id.strip())
    if case_uuid is None or row_uuid is None or kind not in CASE_WRITE_KIND_LABELS:
        return HTMLResponse("")
    title = ""
    if kind == "prose":
        engine = _engine(request)
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            thread = await CaseThreadRepository(db).get(row_uuid)
        title = thread.title if thread is not None else ""
    return _case_write_render(
        request,
        session,
        state="line",
        kind=kind,
        kind_label=CASE_WRITE_KIND_LABELS[kind].format(title=title),
        badge=badge,
        href=case_write_href(case_uuid, kind, row_uuid),
        summary=summary,
        case_id=str(case_uuid),
        row_id=str(row_uuid),
        appended=appended,
        headers={"HX-Trigger": keep_trigger(case_uuid, brief_changed=False)},
    )


@router.post(CASE_WRITE_UNDO_URL, response_class=HTMLResponse)
async def post_case_write_undo(
    request: Request,
    kind: str = Form(""),
    row_id: str = Form(""),
    case_id: str = Form(""),
    appended: str = Form(""),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Undo one of Shirley's writes from its dock line (D-CS-B-115).

    Through :func:`web.case_keep.undo_case_write`: a figure, a point or a
    conversation deleted, a thread removed, an appended paragraph stripped
    while the prose still ends with it. Success re-renders the line as
    *Undone — removed from CASE-nnnn.* with ``HX-Trigger`` ``pf:case-kept``
    (a case on screen re-fetches its blocks); a refusal is the line's calm
    state — *Already removed.*, *CASE-nnnn is closed.*, *The thread's text
    has changed since — edit it in the file.* — and nothing is written.
    """
    case_uuid = _parse_case_marker(case_id.strip())
    row_uuid = _parse_case_marker(row_id.strip())
    if case_uuid is None or row_uuid is None:
        return _case_write_render(
            request, session, state="unavailable", message=UNDO_ALREADY_REMOVED_MESSAGE
        )
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        refusal = await undo_case_write(
            db, kind=kind, row_id=row_uuid, case_id=case_uuid, appended=appended
        )
        case = await CaseRepository(db).get(case_uuid) if refusal is None else None
    if refusal is not None or case is None:
        return _case_write_render(
            request, session, state="unavailable", message=refusal or UNDO_ALREADY_REMOVED_MESSAGE
        )
    logger.info(
        "chat case-write undo: tenant=%s user=%s case=%s kind=%s row=%s",
        session.tenant_id,
        session.user_id,
        case.id,
        kind,
        row_uuid,
    )
    return _case_write_render(
        request,
        session,
        state="undone",
        message=_CASE_WRITE_UNDONE_MESSAGE.format(badge=f"CASE-{case.case_number:04d}"),
        headers={"HX-Trigger": keep_trigger(case.id, brief_changed=False)},
    )
