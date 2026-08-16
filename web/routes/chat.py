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

Two further per-session in-memory stores ride alongside the history
(``app.state.chat_histories``, ADR-0050): the case-brief stash
(``app.state.case_briefs``, ADR-0107 C6) and the chart-artefact sidecar
(``app.state.chat_chart_artifacts``, ADR-0114) that lets a chart survive a
tab reload and be pinned to a case. Both share the history's lifecycle
exactly — see their sections below.

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
The PyQt6 chat surface has **no** tool-confirmation prompt today —
``gui/widgets/shirley_chat_widget.py`` runs every tool through
``AIServiceCore`` without an interstitial dialog, and the only gating
is ADR-0022's per-turn class-lock for ``READ_EXTERNAL_UNTRUSTED``
followers (silently refused inside :meth:`ToolRegistry.execute_tool`).
No ``EXTERNAL_EFFECT`` tool is registered, so the "explicit
confirmation" path that ADR-0022 §EXTERNAL_EFFECT mandates is dormant.

Per the Phase-2 kickoff guidance — *identical semantics, no
relaxation, no tightening* — this module deliberately does not invent
a confirmation surface that PyQt6 does not have. When the first
``EXTERNAL_EFFECT`` tool ships, the web side will gain a
``confirmation_required`` SSE event whose Approve/Deny resolves an
``asyncio.Future`` parked on ``app.state.pending_confirmations``; that
work lands together with the corresponding desktop confirmation
dialog so the two surfaces stay in step.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from core.exceptions import CaseClosedError, CaseStateInvalid
from core.repositories._session import tenant_context
from core.repositories.case_repository import CaseDTO, CaseRepository
from core.repositories.irene_finding_repository import IreneFindingRepository
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
    _StopTokenStripper,
    get_ai_service_core,
)
from services.auth.session import SessionDTO
from services.investments.credential_resolver import (
    CredentialResolver,
    CredentialUnavailableError,
    ProviderCredential,
)
from services.tools._tool_context import ToolExecutionContext
from services.vision_capabilities import (
    ALLOWED_IMAGE_MIME_TYPES,
    MAX_IMAGE_BYTES,
    supports_vision,
)
from services.voice import (
    DEFAULT_STT_BASE_URL,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_VOICE,
    DEFAULT_VOICE_PROVIDER,
    EmptyTranscriptError,
    ResolvedVoice,
    UnsupportedAudioFormatError,
    VoiceError,
    build_provider,
)
from web.auth import require_session, verify_csrf
from web.errors import user_safe_error
from web.permissions import require_role

# The case brief is composed from the very projections the case detail page
# renders (ADR-0107 C6, binding decision 4) — never from raw payloads
# reinterpreted. The import is one-directional: ``cases.py``'s own import chain
# (``watch_desk``, ``planning_desk``) never reaches ``chat.py``, so there
# is no cycle. ``cases.py`` therefore cannot import back the assistants URL — it
# holds its own ``CONSULT_SHIRLEY_URL`` constant.
from web.routes.cases import (
    _materiality_lines,
    _opened_text,
    _project_entry,
    _project_origin,
    _resolve_owner_names,
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
        # The chart-artefact sidecar rides the same eviction (ADR-0114):
        # one lifecycle, two stores.
        _chart_artifacts(request).pop(evicted_key, None)
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

    The chart-artefact sidecar (ADR-0114) is dropped in the same operation:
    the two stores share one lifecycle, so "new chat" and logout never leave
    archived specs behind a conversation that no longer exists.

    Args:
        request: The FastAPI request whose store is mutated.
        session_id: The owning session's UUID (string form).
    """
    _chat_histories(request).pop(session_id, None)
    _chart_artifacts(request).pop(session_id, None)


def _trim_history(
    conversation: Conversation,
    artifacts: list[dict[str, Any]] | None = None,
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
) -> list[dict[str, Any]]:
    """Interleave prose bubbles and chart artefacts in message order.

    The prose rule is ADR-0050's, unchanged: only ``user`` and final
    ``assistant`` messages with content and no tool calls become bubbles — the
    user does not see tool-call internals. Each message is then followed by the
    charts anchored to it, so a chart-only turn (an assistant message with an
    empty prose body) still rehydrates its figure, and no empty bubble is
    invented above it.

    Args:
        conversation: The session's history.
        artifacts: The session's sidecar records.

    Returns:
        Render items — ``{"kind": "message", role, content}`` and
        ``{"kind": "chart", artifact_id, caption, spec, oversized}`` — in the
        order the surface shows them.
    """
    anchored: dict[str, list[dict[str, Any]]] = {}
    for record in artifacts:
        anchored.setdefault(record["message_id"], []).append(record)

    items: list[dict[str, Any]] = []
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
    return items


# ---------------------------------------------------------------------------
# Per-session case brief (ADR-0107 C6) — the consultation loop's first half
# ---------------------------------------------------------------------------
#
# A case can send the PM to Shirley "consulting for" it. The brief is a
# per-session stash — the same ``app.state`` idiom as the chat-history store,
# and, like it, ephemeral (ADR-0050): no persistence, no ``ai_service_core``
# change. The stash stores the *case id*, not a rendered brief, so every turn
# briefs from live case state; a case that closed or vanished since clears the
# stash silently and the turn runs unbriefed. Web chat only — the Telegram
# surface is untouched (it has neither the banner nor the pin loop a brief
# presumes), so its prompt assembly never sees this.
#
# The brief block format (below) is the one place it is defined; the strand's
# closure note references it.

_CASE_BRIEFS_LIMIT = 100

#: The consultation-pin endpoint (ADR-0107 C6). One URL, two verbs — the C5
#: ``PIN_SCENARIO_URL`` idiom: ``GET`` renders the dialog, ``POST`` writes the
#: curated excerpt to the chosen case's timeline.
PIN_CONSULTATION_URL: str = "/api/chat/pin-consultation"

#: The brief-dismiss endpoint — clears the stash and removes the banner.
DISMISS_BRIEF_URL: str = "/chat/brief/dismiss"

#: The Cases area page the empty-picker dialog links out to (no open cases to
#: pin into — point the PM at Cases rather than a dead dropdown).
_CASES_AREA_URL: str = "/cases"

#: The chart-snapshot pin endpoint (ADR-0114). Same one-URL-two-verbs idiom as
#: ``PIN_CONSULTATION_URL``: ``GET`` renders the dialog, ``POST`` writes the
#: frozen Plotly spec to the chosen case's timeline.
PIN_CHART_URL: str = "/api/chat/pin-chart"

#: The pin artifact class this surface writes — the third, after C3b's
#: ``document`` and C5's ``scenario_snapshot`` (binding decision 3). Documented
#: beside them in the cases module docstring; the timeline reads exactly this.
_CONSULTATION_ARTIFACT: str = "consultation"

#: The fourth pin artifact class (ADR-0114) — a frozen chart snapshot, written
#: from the session sidecar above. Documented beside the other three in the
#: cases module docstring; the timeline reads exactly this.
_CHART_SNAPSHOT_ARTIFACT: str = "chart_snapshot"

#: Brief block delimiters. The brief is appended to Shirley's system prompt as
#: this clearly-fenced block so the model tells its standing instructions from
#: the per-turn case context (binding decisions 1/4).
_BRIEF_OPEN: str = "<<<PORTFOLIFLOW CASE BRIEF>>>"
_BRIEF_CLOSE: str = "<<<END PORTFOLIFLOW CASE BRIEF>>>"

#: The timeline digest is capped so a long-lived case never balloons the prompt:
#: the most recent entries, each summarised to its first line.
_BRIEF_DIGEST_MAX_ENTRIES: int = 12
_BRIEF_DIGEST_LINE_CHARS: int = 160

#: Shared by both pin flows on this surface (consultation and chart snapshot):
#: closed-case immutability is one rule (ADR-0107 §4), so it reads as one
#: sentence wherever it is refused.
_PIN_CLOSED_MESSAGE: str = (
    "This case is closed — closed cases are read-only and cannot be pinned into. Pick an open case."
)

#: The calm state a stale, evicted or never-archived chart handle resolves to —
#: the C6 "no longer available" posture (ADR-0114 §3), never a wrong figure.
_CHART_UNAVAILABLE_MESSAGE: str = (
    "That chart is no longer available to pin — it has scrolled out of this "
    "session's memory. Ask Shirley for it again, then pin the fresh figure."
)

#: An oversized figure was streamed live but never archived, so there is no
#: spec to freeze onto the case record. Guidance, not a bare refusal.
_CHART_OVERSIZED_MESSAGE: str = (
    "This chart is too large to pin to a case record. Ask Shirley for a "
    "narrower range or a single investment, then pin that figure."
)


def _case_briefs(request: Request) -> OrderedDict[str, UUID]:
    """Return (and lazily initialise) the per-app case-brief stash store.

    Keyed by the authenticated session UUID (string), value the active case id.
    Bounded LRU at the session level, mirroring :func:`_chat_histories`.
    Single-worker only; a multi-worker deployment needs Redis or equivalent.
    """
    store = getattr(request.app.state, "case_briefs", None)
    if store is None:
        store = OrderedDict()
        request.app.state.case_briefs = store
    return cast("OrderedDict[str, UUID]", store)


def _set_active_brief(request: Request, session_id: str, case_id: UUID) -> None:
    """Set the session's active brief to ``case_id``, replacing any previous.

    Evicts the oldest entry when the per-app cap is reached (the history-store
    idiom). Re-inserting moves the entry to the most-recently-used end.
    """
    store = _case_briefs(request)
    store.pop(session_id, None)
    while len(store) >= _CASE_BRIEFS_LIMIT:
        evicted_key, _ = store.popitem(last=False)
        logger.warning(
            "chat: evicting case brief for session %s (LRU cap %d)",
            evicted_key,
            _CASE_BRIEFS_LIMIT,
        )
    store[session_id] = case_id


def _get_active_brief(request: Request, session_id: str) -> UUID | None:
    """Return the session's active brief case id, or ``None`` if unset."""
    store = _case_briefs(request)
    if session_id in store:
        store.move_to_end(session_id)
        return store[session_id]
    return None


def _clear_active_brief(request: Request, session_id: str) -> None:
    """Drop the session's active brief. Safe on absent keys."""
    _case_briefs(request).pop(session_id, None)


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


async def resolve_active_brief_banner(
    request: Request, session: SessionDTO, case_marker: str | None
) -> dict[str, str] | None:
    """Adopt a fresh ``?case=`` marker and return the banner context, or ``None``.

    Marker hygiene (binding decision 5): a valid **open** case replaces any
    previous stash; a malformed, unknown, foreign-tenant (RLS → ``None``) or
    **closed** marker is dropped silently — no banner, no stash change, no error
    (consulting *about* a closed case is legitimate reading, but the guided
    context is open-cases-only). The banner is then rendered from the current
    stash, **validated fresh**: a stale (closed-since / vanished) stash clears
    silently and yields no banner. Called by the assistants area page render.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return None
    session_id = str(session.id)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        repo = CaseRepository(db)
        case: CaseDTO | None = None
        marker_id = _parse_case_marker(case_marker)
        if marker_id is not None:
            candidate = await repo.get(marker_id)
            if candidate is not None and candidate.state == "open":
                _set_active_brief(request, session_id, candidate.id)
                case = candidate
        if case is None:
            stashed = _get_active_brief(request, session_id)
            if stashed is not None:
                candidate = await repo.get(stashed)
                if candidate is not None and candidate.state == "open":
                    case = candidate
                else:  # stale stash — the case closed or vanished since.
                    _clear_active_brief(request, session_id)
    if case is None:
        return None
    return {
        "badge": f"CASE-{case.case_number:04d}",
        "title": case.title,
        "href": f"/cases/{case.id}",
    }


def _brief_digest_line(projected: dict[str, Any]) -> str:
    """Summarise one projected timeline entry as ``kind label: first line``.

    Reads the projected entry the detail page renders (binding decision 4) — its
    body field per kind — never the raw payload. Capped to one line, sanely
    truncated, so the digest stays compact.
    """
    kind = projected.get("kind", "")
    label = projected.get("kind_label", kind)
    if kind == "decision_record":
        body = projected.get("decision", "")
    elif kind == "closed":
        body = projected.get("closing_note", "")
    elif kind == "pin":
        # document / scenario_snapshot / consultation all carry a comment; an
        # unknown artifact projects none, and reads as its label alone.
        body = projected.get("comment", "")
    else:  # opened, note, and any body-less kind
        body = projected.get("text", "")
    lines = (body or "").strip().splitlines()
    snippet = lines[0].strip() if lines else ""
    if len(snippet) > _BRIEF_DIGEST_LINE_CHARS:
        snippet = snippet[: _BRIEF_DIGEST_LINE_CHARS - 1].rstrip() + "…"
    return f"{label}: {snippet}" if snippet else str(label)


def _format_brief(
    case: CaseDTO,
    origin: dict[str, Any] | None,
    materiality: list[str],
    digest: list[str],
) -> str:
    """Render the fenced case-brief block (binding decisions 1/4).

    Presentation strings only — badge/number, title, state, then the finding's
    trigger/finding/basis + band + subject (from-finding) or the manual
    description, the frozen materiality-at-opening lines, and the compact
    timeline digest. **No linked investments** (Gate-C0 decision E). The exact
    format lives here, in one place; the closure note references it.
    """
    lines = [
        _BRIEF_OPEN,
        (
            "You are consulting on the open case below. Ground your answer in "
            "it. The conversation is ephemeral; only what the PM deliberately "
            "pins becomes part of the case record."
        ),
        f"Case: CASE-{case.case_number:04d} — {case.title}",
        f"State: {case.state}",
    ]
    if origin is not None:
        lines.append(
            f"Origin: finding on {origin['subject_key']} "
            f"(band {origin['band']}, resolution: {origin['resolution_label']})"
        )
        if origin.get("trigger"):
            lines.append(f"  Trigger: {origin['trigger']}")
        if origin.get("finding"):
            lines.append(f"  Finding: {origin['finding']}")
        if origin.get("basis"):
            lines.append(f"  Basis at finding time: {origin['basis']}")
    elif case.description:
        lines.append(f"Description: {case.description}")
    if materiality:
        lines.append("Materiality at case opening:")
        lines.extend(f"  - {line}" for line in materiality)
    if digest:
        lines.append("Timeline so far (oldest first within the recent window):")
        lines.extend(f"  - {line}" for line in digest)
    lines.append(_BRIEF_CLOSE)
    return "\n".join(lines)


async def _compose_case_brief(db: Any, case: CaseDTO) -> str:
    """Compose the case brief from the same projections the detail page renders.

    Reads the case's entries (and originating finding) once, reuses the cases
    module's projection helpers for origin, materiality and per-entry bodies,
    and formats the fenced block (binding decision 4). Presentation strings
    only; the payloads are never reinterpreted here.
    """
    case_repo = CaseRepository(db)
    entries = await case_repo.list_entries(case.id)
    finding = None
    if case.finding_id is not None:
        finding = await IreneFindingRepository(db).get(case.finding_id)

    actor_ids: set[UUID] = {case.opened_by}
    if case.closed_by is not None:
        actor_ids.add(case.closed_by)
    actor_ids.update(e.actor_user_id for e in entries if e.actor_user_id is not None)
    owner_names = await _resolve_owner_names(UserRepository(db), actor_ids)
    opened_text = _opened_text(case, finding)
    origin = _project_origin(finding, entries) if finding is not None else None
    materiality = _materiality_lines(entries)
    projected = [_project_entry(entry, owner_names, opened_text=opened_text) for entry in entries]
    digest = [_brief_digest_line(p) for p in projected[-_BRIEF_DIGEST_MAX_ENTRIES:]]
    return _format_brief(case, origin, materiality, digest)


async def _briefed_system_prompt(request: Request, session: SessionDTO, base_prompt: str) -> str:
    """Return the system prompt, brief-appended when a stash is active.

    Loads the stashed case fresh: a case that vanished or closed since (stale
    stash) clears the stash silently and the turn runs unbriefed. Otherwise the
    composed brief is appended as its fenced block at this call site — no
    ``ai_service_core`` change (binding decision 1).
    """
    session_id = str(session.id)
    stashed = _get_active_brief(request, session_id)
    if stashed is None:
        return base_prompt
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return base_prompt
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        case = await CaseRepository(db).get(stashed)
        if case is None or case.state != "open":
            _clear_active_brief(request, session_id)
            return base_prompt
        brief = await _compose_case_brief(db, case)
    return f"{base_prompt}\n\n{brief}" if base_prompt else brief


def _assistant_message_text(conversation: Conversation | None, message_id: str) -> str | None:
    """Return the text of the assistant message with ``message_id``, or ``None``.

    Robust to trimming: the id is the :class:`Message`'s own stable id, so a
    message trimmed out of history is simply not found — the dialog then says
    the message is no longer available rather than prefilling the wrong one
    (Step 2). An existing but empty (chart-only) message returns ``""``.
    """
    if conversation is None or not message_id:
        return None
    for message in conversation.messages:
        if message.id == message_id and message.role == MessageRole.ASSISTANT:
            return message.content or ""
    return None


def _consultation_picker_options(
    cases: list[CaseDTO],
) -> list[dict[str, str]]:
    """Project open cases into the dialog's picker (``CASE-NNNN — title``).

    Order is the repository's — newest ``opened_at`` first; never re-sorted.
    """
    return [
        {
            "id": str(case.id),
            "label": f"CASE-{case.case_number:04d} — {case.title}",
        }
        for case in cases
    ]


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
# Per-turn voice resolution and gating (ADR-0118 §4, §5)
# ---------------------------------------------------------------------------
#
# The voice twin of the block above, and for the same reason: a tenant that
# writes its speech keys in Admin → Providers & Credentials is served on its
# very next recording, with no restart, and one worker serves many tenants
# without their keys ever meeting. There is no process-global voice provider
# left for this surface to consult.
#
# The chains, scope-major per ADR-0112 §1. Every voice field is tenant-scope,
# so none of them carries a user axis:
#
# * enabled     — vault tenant → env ``VOICE_ENABLED`` → off (ADR-0118 §5);
# * credential  — vault tenant → env ``VOICE_STT_API_KEY`` /
#   ``VOICE_TTS_API_KEY``, chained **per half** so a Groq-STT + OpenAI-TTS
#   deployment can hold the two keys in different scopes;
# * models, endpoint, persona voice — vault tenant → env → the ``DEFAULT_*``
#   constants, each field chained individually;
# * ``stt_provider`` / ``tts_provider`` — the environment alone (ADR-0118 §1).
#
# Nothing is cached and nothing is stashed: the ``ResolvedVoice`` lives for
# exactly one call, so a tenant's key can never be held where another tenant's
# turn could reach it.

#: One user-facing message for every "voice is on but has no credential"
#: outcome. Points at both scopes an operator can fix it in, and deliberately
#: says nothing about restarting: tenant rows apply on the next voice message.
_NO_VOICE_MESSAGE = (
    "Voice is enabled but no voice credential is configured for this "
    "tenant. Set the speech-to-text and text-to-speech API keys in "
    "Admin → Providers & Credentials (they apply on your next voice "
    "message), or set VOICE_STT_API_KEY and VOICE_TTS_API_KEY in .env "
    "for the whole application."
)


class _VoiceUnconfiguredError(Exception):
    """This turn's voice credential resolved to nothing (either half).

    The voice twin of :class:`_LLMUnconfiguredError`, and translated the same
    way — both voice endpoints answer 503 — because "configure me" is the
    same semantic. It wraps
    :class:`~services.investments.credential_resolver.CredentialUnavailableError`
    raised for **either** half of the credential.

    Enabled voice requires **both** halves: a tenant holding one key of two
    is a configuration error that surfaces loudly at first use. That is
    exactly what :meth:`~services.voice.config.VoiceConfig.__post_init__`
    enforced at startup, relocated to the turn by ADR-0118 §2 — which is why
    both endpoints resolve the full :class:`~services.voice.ResolvedVoice`
    even though each uses only one half of it.

    A :class:`~services.credential_vault.VaultDecryptError` is deliberately
    **not** wrapped: a vault that will not decrypt is an operator emergency,
    not a "configure me" nudge, and it must not read as a missing key.
    """


async def _resolve_voice_enabled(request: Request, session: SessionDTO) -> bool:
    """Report whether voice is enabled for this session's tenant (ADR-0118 §5).

    Walks the ``voice.enabled`` chain and nothing else — never a credential
    probe. Whether the affordance is offered and whether a key can be found
    are separate questions, and conflating them would hide a misconfiguration
    behind a silently absent button instead of surfacing it at first use.

    Runs inside the session's ``tenant_context`` so the vault source sees
    exactly the rows RLS allows this tenant. Without a database engine (a
    DB-less test rig, a contributor laptop) the resolver is built without a
    session and the environment is the only source — the same graceful
    degradation :func:`_resolve_llm` takes.

    Args:
        request: The active request (carries ``app.state``).
        session: The authenticated session, supplying the tenant.

    Returns:
        ``True`` when the chain yields ``"true"`` (case-insensitive) — the
        ``VOICE_ENABLED`` convention; ``False`` otherwise, including when the
        field is set nowhere.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        value = await CredentialResolver().resolve_config(
            "voice", "enabled", scopes=("tenant", "env")
        )
    else:
        async with tenant_context(engine, session.tenant_id) as db:
            value = await CredentialResolver(session=db).resolve_config(
                "voice", "enabled", scopes=("tenant", "env")
            )
    return value is not None and value.strip().lower() == "true"


async def _resolve_voice(request: Request, session: SessionDTO) -> ResolvedVoice:
    """Resolve this turn's voice endpoints, credentials and models (ADR-0118 §4).

    The voice twin of :func:`_resolve_llm`, with the same engine-present /
    engine-less split and the same one-call lifetime for the plain keys.

    Args:
        request: The active request (carries ``app.state``).
        session: The authenticated session, supplying the tenant.

    Returns:
        The :class:`~services.voice.ResolvedVoice` for this turn.

    Raises:
        _VoiceUnconfiguredError: If either half's credential resolves to
            nothing.
        VaultDecryptError: Propagated untouched — a wrong or rotated master
            key must never look like an absent credential.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return await _resolve_voice_through(CredentialResolver(), tenant_id=None)
    async with tenant_context(engine, session.tenant_id) as db:
        return await _resolve_voice_through(
            CredentialResolver(session=db), tenant_id=session.tenant_id
        )


async def _resolve_voice_through(
    resolver: CredentialResolver,
    *,
    tenant_id: UUID | None,
) -> ResolvedVoice:
    """Walk the voice chains on ``resolver`` and assemble the resolution.

    Split from :func:`_resolve_voice` so the chains read in one place,
    independent of whether a vault-backed session was available. Both halves
    resolve on every call, whichever endpoint asked: an enabled tenant
    holding one key of two is misconfigured, and which endpoint it notices
    on must not decide whether it hears about it (ADR-0118 §2).
    """
    stt_api_key = await _resolve_voice_key(resolver, "voice_stt", tenant_id=tenant_id)
    tts_api_key = await _resolve_voice_key(resolver, "voice_tts", tenant_id=tenant_id)

    stt_model = await resolver.resolve_config("voice_stt", "model") or DEFAULT_STT_MODEL
    stt_base_url = await resolver.resolve_config("voice_stt", "base_url") or DEFAULT_STT_BASE_URL
    tts_model = await resolver.resolve_config("voice_tts", "model") or DEFAULT_TTS_MODEL
    tts_voice = await resolver.resolve_config("voice_tts", "voice") or DEFAULT_TTS_VOICE

    # The two provider keys are env-only by design: the taxonomy deliberately
    # does not declare them until a second adapter's ADR does (ADR-0118 §1),
    # so there is no per-tenant chain here for the façade to walk.
    stt_provider = os.getenv("VOICE_STT_PROVIDER", DEFAULT_VOICE_PROVIDER)
    tts_provider = os.getenv("VOICE_TTS_PROVIDER", DEFAULT_VOICE_PROVIDER)

    return ResolvedVoice(
        stt_provider=stt_provider,
        stt_model=stt_model,
        stt_api_key=stt_api_key,
        stt_base_url=stt_base_url,
        tts_provider=tts_provider,
        tts_model=tts_model,
        tts_voice=tts_voice,
        tts_api_key=tts_api_key,
    )


async def _resolve_voice_key(
    resolver: CredentialResolver,
    provider: str,
    *,
    tenant_id: UUID | None,
) -> str:
    """Resolve one half's API key, or raise the one voice-unconfigured error.

    ``voice_stt`` and ``voice_tts`` are separate providers precisely so the
    two keys chain independently (ADR-0118 §1); either one missing is the
    same outcome for the caller.
    """
    try:
        credential = await resolver.resolve(provider, tenant_id=tenant_id)
    except CredentialUnavailableError as exc:
        raise _VoiceUnconfiguredError(_NO_VOICE_MESSAGE) from exc
    if not isinstance(credential, ProviderCredential):
        # Both halves declare a secret field and neither is optional, so the
        # resolver raises rather than returning NoCredential. Defensive.
        raise _VoiceUnconfiguredError(_NO_VOICE_MESSAGE)
    return credential.payload["api_key"]


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
        is_voice: Whether this turn began as a voice recording. Threaded
            into the SSE-bootstrap element as ``data-pf-voice``.

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
    _trim_history(history, _stored_chart_artifacts(request, str(session.id)))

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
    The returned fragment carries ``data-pf-voice=1`` so the client speaks
    the reply after the SSE turn finishes. STT failures return the inline
    ``chat_error`` fragment (no silent fallback). When voice is disabled
    **for this tenant** the endpoint 404s so the surface degrades to
    text-only; enabled with no resolvable credential is a 503 naming both
    scopes an operator can fix it in (ADR-0118 §2, §5).
    """
    if not await _resolve_voice_enabled(request, session):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Voice is not enabled.",
        )
    # Refuse before STT: do not pay for a transcription we cannot answer.
    _core, llm = await _require_connected_core(request, session)
    try:
        voice = await _resolve_voice(request, session)
    except _VoiceUnconfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    provider = build_provider(voice)
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
        request, session, transcript, attachments, model_id=llm.model, is_voice=True
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
    already rendered, so a TTS failure must not break the turn. When voice
    is disabled **for this tenant** the endpoint 404s; enabled with no
    resolvable credential is a 503, exactly as on ``/chat/voice``.
    """
    if not await _resolve_voice_enabled(request, session):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Voice is not enabled.",
        )
    clean = text.strip()
    if not clean:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    try:
        voice = await _resolve_voice(request, session)
    except _VoiceUnconfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    provider = build_provider(voice)
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
    ``chart`` / ``done`` / ``error``).

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
            # model the data is unavailable.
            database_url = request.app.state.settings.database_url
            tool_context = (
                ToolExecutionContext(
                    tenant_id=session.tenant_id,
                    database_url=database_url,
                )
                if database_url
                else None
            )
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
                    # (the web path, ADR-0048) emits a Plotly ``spec``;
                    # ``generate_chart`` (the GUI path) emits a PNG.
                    # The web model uses ``render_chart``, so the PNG
                    # branch is defensive — kept so a GUI-shaped
                    # envelope never silently breaks the stream.
                    chart_format = str(payload.get("chart_format", "png"))
                    if chart_format == "plotly":
                        spec = payload.get("spec") or {}
                        caption = str(payload.get("caption", ""))
                        # ADR-0114: this is the one place the spec is at hand,
                        # so it is also the one capture point. The record is
                        # anchored to the turn's assistant message below; the
                        # browser is handed its ``artifact_id`` so the live
                        # figure can carry the "Pin to case…" affordance. An
                        # oversized spec streams unchanged but is not archived
                        # and gets no id — hence no pin affordance for it. The
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
                    # ``done`` frame so the client can offer "Pin to case…" for
                    # exactly this message (ADR-0107 C6, Step 2). The id is the
                    # one the recorder just appended to history, so the pin
                    # dialog can prefill its text server-side. Empty when no
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
                    _trim_history(conversation, artifacts)
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
                    _trim_history(conversation, _stored_chart_artifacts(request, str(session.id)))
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
    """
    templates = _templates(request)
    session_id = str(session.id)
    conversation = _chat_histories(request).get(session_id)
    if conversation is None or not conversation.messages:
        return cast(
            HTMLResponse,
            templates.TemplateResponse(request, "_partials/chat_empty.html", {}),
        )

    items = _history_items(conversation, _stored_chart_artifacts(request, session_id) or [])
    return cast(
        HTMLResponse,
        templates.TemplateResponse(
            request,
            "_partials/chat_history.html",
            {"items": items, "pin_chart_url": PIN_CHART_URL},
        ),
    )


# ---------------------------------------------------------------------------
# POST /chat/brief/dismiss — clear the active case brief
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
    """Clear the session's active case brief and remove the banner (Step 1).

    The banner's dismiss posts here; the empty body swaps the banner out of the
    DOM. Subsequent turns run unbriefed until a new ``?case=`` marker sets the
    stash again. Safe to call with no active stash.
    """
    _clear_active_brief(request, str(session.id))
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# Consultation pin (ADR-0107 C6) — the loop's second half. One URL, two verbs:
# GET renders the dialog, POST writes the curated excerpt to the case timeline.
# ---------------------------------------------------------------------------


def _pin_dialog_context(
    session: SessionDTO,
    *,
    cases: list[CaseDTO],
    message_id: str,
    preselect: str | None,
    excerpt: str,
    unavailable: bool,
    comment: str,
    error: str | None,
) -> dict[str, Any]:
    """Assemble the consultation pin dialog's Jinja context (one shape)."""
    return {
        "csrf_token": session.csrf_token,
        "pin_url": PIN_CONSULTATION_URL,
        "cases_area_url": _CASES_AREA_URL,
        "message_id": message_id,
        "cases": _consultation_picker_options(cases),
        "preselect": preselect,
        "excerpt": excerpt,
        "unavailable": unavailable,
        "comment": comment,
        "error": error,
    }


@router.get("/api/chat/pin-consultation", response_class=HTMLResponse)
async def get_pin_consultation_dialog(
    request: Request,
    message_id: str = "",
    cancel: bool = False,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Render the "Pin to case…" dialog into ``#chat-pin-dialog`` (Step 2).

    The excerpt textarea is **prefilled server-side** from the identified
    assistant message in the session history, and is editable — the PM trims it
    to what belongs on the record (binding decision 2). A message no longer in
    history (trimmed away, or a stale id) yields the calm "no longer available"
    state rather than a wrong prefill. The picker is the tenant's open cases,
    newest first, preselected to the active brief's case; with none, the dialog
    says so and links to Cases rather than offering a dead dropdown. ``cancel``
    clears the slot — the composer-cancel idiom.
    """
    if cancel:
        return HTMLResponse("")
    session_id = str(session.id)
    conversation = _chat_histories(request).get(session_id)
    text = _assistant_message_text(conversation, message_id)

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        open_cases = await CaseRepository(db).list_open()

    preselect = _get_active_brief(request, session_id)
    preselect_str = str(preselect) if preselect is not None else None
    context = _pin_dialog_context(
        session,
        cases=open_cases,
        message_id=message_id,
        preselect=preselect_str,
        excerpt=text or "",
        unavailable=text is None,
        comment="",
        error=None,
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(request, "_partials/chat_pin_dialog.html", context),
    )


@router.post("/api/chat/pin-consultation", response_class=HTMLResponse)
async def post_pin_consultation(
    request: Request,
    message_id: str = Form(""),
    case_id: str = Form(""),
    comment: str = Form(""),
    excerpt: str = Form(""),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Pin a curated Shirley excerpt to a case (Step 2, binding decision 3).

    The client posts the **curated** text (never scraped bubble HTML). Gates in
    the C5 order and idiom, each re-rendering the dialog with an inline error
    and the inputs preserved: comment non-empty → excerpt non-empty (stripped;
    the PM may have deleted everything) → case exists → case open. On success one
    ``pin`` entry is appended — ``actor="pm"`` with the session user id (pinning
    is the PM's curation act; the anatomy attributes the *words* to Shirley) —
    carrying ``{artifact: "consultation", comment, excerpt}``. A quiet
    confirmation with a case link replaces the dialog; the conversation stays.
    """
    clean_comment = comment.strip()
    clean_excerpt = excerpt.strip()
    case_id_raw = case_id.strip()

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        open_cases = await CaseRepository(db).list_open()

        def _dialog_error(message: str, *, status_code: int = 200) -> HTMLResponse:
            """Re-render the dialog inline with an error and preserved inputs.

            200 for a reliable HTMX swap (the composer idiom); the picked case,
            the comment and the edited excerpt survive so the PM does not retype.
            """
            return cast(
                HTMLResponse,
                _templates(request).TemplateResponse(
                    request,
                    "_partials/chat_pin_dialog.html",
                    _pin_dialog_context(
                        session,
                        cases=open_cases,
                        message_id=message_id,
                        preselect=case_id_raw or None,
                        excerpt=excerpt,
                        unavailable=False,
                        comment=comment,
                        error=message,
                    ),
                    status_code=status_code,
                ),
            )

        # Gate 1: a curation comment is mandatory.
        if not clean_comment:
            return _dialog_error(
                "A curation comment is required — say why this belongs on the case record."
            )
        # Gate 2: the excerpt must not be empty after the PM's edits.
        if not clean_excerpt:
            return _dialog_error(
                "The excerpt is empty — keep the part of Shirley's answer that "
                "belongs on the record."
            )
        # Gate 3: the case exists in this tenant. The picker only offers real
        # open cases, so a miss is a race (closed since) or a hand-built id.
        try:
            case_uuid = UUID(case_id_raw)
        except ValueError:
            return _dialog_error("Choose a case to pin to.")
        case = await CaseRepository(db).get(case_uuid)
        if case is None:
            return _dialog_error("That case could not be found — it may have just been closed.")
        # Gate 4: the case is open (closed cases are immutable, ADR-0107 §4).
        if case.state != "open":
            return _dialog_error(_PIN_CLOSED_MESSAGE)

        try:
            await CaseRepository(db).append_entry(
                case_uuid,
                kind="pin",
                actor="pm",
                actor_user_id=session.user_id,
                payload={
                    "artifact": _CONSULTATION_ARTIFACT,
                    "comment": clean_comment,
                    "excerpt": clean_excerpt,
                },
                now=datetime.now(timezone.utc),
            )
        except (CaseClosedError, CaseStateInvalid):
            # Raced to closed between the open-gate and the write.
            return _dialog_error(_PIN_CLOSED_MESSAGE)

    logger.info(
        "chat pin-consultation: tenant=%s user=%s case=%s",
        session.tenant_id,
        session.user_id,
        case_uuid,
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/chat_pin_confirm.html",
            {
                "pinned_label": "Excerpt",
                "case_badge": f"CASE-{case.case_number:04d}",
                "case_href": f"/cases/{case_uuid}",
                "pin_url": PIN_CONSULTATION_URL,
            },
        ),
    )


# ---------------------------------------------------------------------------
# Chart-snapshot pin (ADR-0114) — the sidecar's second consumer. Same one-URL-
# two-verbs shape as the consultation pin above, one artefact class further on.
# ---------------------------------------------------------------------------


def _chart_pin_dialog_context(
    session: SessionDTO,
    *,
    cases: list[CaseDTO],
    artifact_id: str,
    preselect: str | None,
    caption: str,
    unavailable_message: str | None,
    comment: str,
    error: str | None,
) -> dict[str, Any]:
    """Assemble the chart pin dialog's Jinja context (one shape).

    The sibling of :func:`_pin_dialog_context`, differing in exactly what the
    artefact classes differ in: a chart carries no editable excerpt, so the
    dialog identifies the figure by its stored caption and posts the
    ``artifact_id`` — never the spec (ADR-0114: transport by reference; the
    server resolves the spec from its own sidecar).
    """
    return {
        "csrf_token": session.csrf_token,
        "pin_url": PIN_CHART_URL,
        "cases_area_url": _CASES_AREA_URL,
        "artifact_id": artifact_id,
        "cases": _consultation_picker_options(cases),
        "preselect": preselect,
        "caption": caption,
        "unavailable_message": unavailable_message,
        "comment": comment,
        "error": error,
    }


def _chart_unavailable_message(record: dict[str, Any] | None) -> str | None:
    """Return the calm state a chart handle resolves to, or ``None`` when fine.

    Two distinct non-pinnable states, told apart because the remedies differ:
    a handle that no longer resolves (trimmed, a new chat, a restart, or a
    stale id) versus a figure that was streamed live but never archived
    because it exceeded the cap.
    """
    if record is None:
        return _CHART_UNAVAILABLE_MESSAGE
    if record["oversized"] or record["spec"] is None:
        return _CHART_OVERSIZED_MESSAGE
    return None


@router.get(PIN_CHART_URL, response_class=HTMLResponse)
async def get_pin_chart_dialog(
    request: Request,
    artifact_id: str = "",
    cancel: bool = False,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Render the chart "Pin to case…" dialog into ``#chat-pin-dialog``.

    The C6 dialog, one artefact class on: the figure is identified by its
    stored caption (there is nothing to curate in a frozen spec — the curation
    is the comment), the picker is the tenant's open cases, newest first,
    preselected to the active brief's case. A handle that no longer resolves in
    this session's sidecar yields the calm "no longer available" state rather
    than an empty pin; an oversized figure says so and points at a narrower
    ask. ``cancel`` clears the slot — the composer-cancel idiom.
    """
    if cancel:
        return HTMLResponse("")
    session_id = str(session.id)
    record = _find_chart_artifact(request, session_id, artifact_id)
    unavailable = _chart_unavailable_message(record)

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        open_cases = await CaseRepository(db).list_open()

    preselect = _get_active_brief(request, session_id)
    context = _chart_pin_dialog_context(
        session,
        cases=open_cases,
        artifact_id=artifact_id,
        preselect=str(preselect) if preselect is not None else None,
        caption=str(record["caption"]) if record is not None else "",
        unavailable_message=unavailable,
        comment="",
        error=None,
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request, "_partials/chat_pin_chart_dialog.html", context
        ),
    )


@router.post(PIN_CHART_URL, response_class=HTMLResponse)
async def post_pin_chart(
    request: Request,
    artifact_id: str = Form(""),
    case_id: str = Form(""),
    comment: str = Form(""),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Pin a frozen chart snapshot to a case — the fourth artifact class.

    The client posts the sidecar **handle**, never the figure: the server
    resolves the spec from its own store, which is what makes the record
    trustworthy (ADR-0114 §2, the analog of C6's "the client never scrapes
    bubble HTML"). Gates in the C5/C6 order, each re-rendering the dialog with
    the inputs preserved: comment non-empty → the artefact resolves in *this*
    session's sidecar → case exists → case open. On success one ``pin`` entry
    is appended — ``actor="pm"`` with the session user id, pinning being the
    PM's curation act — carrying
    ``{artifact: "chart_snapshot", comment, caption, spec}``. The spec is
    embedded, so the case record is self-contained: no reference back into an
    ephemeral session store.
    """
    clean_comment = comment.strip()
    case_id_raw = case_id.strip()
    session_id = str(session.id)

    record = _find_chart_artifact(request, session_id, artifact_id)
    unavailable = _chart_unavailable_message(record)

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        open_cases = await CaseRepository(db).list_open()

        def _dialog(
            *,
            error: str | None = None,
            unavailable_message: str | None = None,
        ) -> HTMLResponse:
            """Re-render the dialog inline (200, the composer idiom).

            200 rather than 4xx so the HTMX swap is reliable; the picked case
            and the comment survive so the PM does not retype.
            """
            return cast(
                HTMLResponse,
                _templates(request).TemplateResponse(
                    request,
                    "_partials/chat_pin_chart_dialog.html",
                    _chart_pin_dialog_context(
                        session,
                        cases=open_cases,
                        artifact_id=artifact_id,
                        preselect=case_id_raw or None,
                        caption=str(record["caption"]) if record is not None else "",
                        unavailable_message=unavailable_message,
                        comment=comment,
                        error=error,
                    ),
                ),
            )

        # Gate 1: a curation comment is mandatory.
        if not clean_comment:
            return _dialog(
                error="A curation comment is required — say why this belongs on the case record."
            )
        # Gate 2: the artefact must still resolve in this session's sidecar.
        # A stale or evicted handle is calm, not an error the PM can retry.
        if unavailable is not None or record is None:
            return _dialog(unavailable_message=unavailable or _CHART_UNAVAILABLE_MESSAGE)
        # Gate 3: the case exists in this tenant. The picker only offers real
        # open cases, so a miss is a race (closed since) or a hand-built id.
        try:
            case_uuid = UUID(case_id_raw)
        except ValueError:
            return _dialog(error="Choose a case to pin to.")
        case = await CaseRepository(db).get(case_uuid)
        if case is None:
            return _dialog(error="That case could not be found — it may have just been closed.")
        # Gate 4: the case is open (closed cases are immutable, ADR-0107 §4).
        if case.state != "open":
            return _dialog(error=_PIN_CLOSED_MESSAGE)

        try:
            await CaseRepository(db).append_entry(
                case_uuid,
                kind="pin",
                actor="pm",
                actor_user_id=session.user_id,
                payload={
                    "artifact": _CHART_SNAPSHOT_ARTIFACT,
                    "comment": clean_comment,
                    "caption": record["caption"],
                    "spec": record["spec"],
                },
                now=datetime.now(timezone.utc),
            )
        except (CaseClosedError, CaseStateInvalid):
            # Raced to closed between the open-gate and the write.
            return _dialog(error=_PIN_CLOSED_MESSAGE)

    logger.info(
        "chat pin-chart: tenant=%s user=%s case=%s artifact=%s",
        session.tenant_id,
        session.user_id,
        case_uuid,
        artifact_id,
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/chat_pin_confirm.html",
            {
                "pinned_label": "Chart",
                "case_badge": f"CASE-{case.case_number:04d}",
                "case_href": f"/cases/{case_uuid}",
                "pin_url": PIN_CHART_URL,
            },
        ),
    )
