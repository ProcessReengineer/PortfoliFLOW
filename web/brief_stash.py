# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The session's consultation context and keep target (ADR-0107 C6, ADR-0133 §6, ADR-0138 §8).

A per-session stash on ``app.state.brief_stash`` — the same idiom as the chat
history store, and, like it, ephemeral (ADR-0050): no persistence, bounded LRU
at the session level, single-worker only. It holds the *kind and row id* of
the one active context — a case (``BRIEF_KIND_CASE``) or a written Watch Desk
briefing (``BRIEF_KIND_WATCH_BRIEFING``) — never a rendered brief, so every
reader validates the live row.

Since ADR-0138 §8 a case stash is also the session's **keep target**: Keep
writes into the stashed case, at file level or into ``thread_id`` when one is
set (D-CS-B-86). One active case per session — the brief and the keep target
are the same case.

The primitives moved here from ``web/routes/chat.py`` verbatim (P-CS-4a) so
the Planning Desk route, which cannot import the chat routes without a cycle,
reads the same stash. ``chat.py`` imports them back, so
``from web.routes.chat import ActiveBrief, _set_active_brief`` keeps working.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from fastapi import Request

logger = logging.getLogger(__name__)

_BRIEF_STASH_LIMIT = 100

#: The two consultation-context kinds the stash can hold (ADR-0133 §6).
BRIEF_KIND_CASE: str = "case"
BRIEF_KIND_WATCH_BRIEFING: str = "watch_briefing"


@dataclass(frozen=True)
class ActiveBrief:
    """The session's active consultation context: its kind and the row id.

    ``kind`` is :data:`BRIEF_KIND_CASE` (``id`` a case) or
    :data:`BRIEF_KIND_WATCH_BRIEFING` (``id`` an ``irene_briefing`` row).
    ``thread_id`` names the case's thread Keep writes into; ``None`` keeps to
    the file (ADR-0138 §8, D-CS-B-86). A briefing carries none.
    """

    kind: str
    id: UUID
    thread_id: UUID | None = None


def _brief_stash(request: Request) -> OrderedDict[str, ActiveBrief]:
    """Return (and lazily initialise) the per-app consultation-brief stash.

    Keyed by the authenticated session UUID (string), value the active
    :class:`ActiveBrief` — a case or a written briefing, never both
    (ADR-0133 §6). Bounded LRU at the session level, mirroring
    :func:`_chat_histories`. Single-worker only; a multi-worker deployment
    needs Redis or equivalent.
    """
    store = getattr(request.app.state, "brief_stash", None)
    if store is None:
        store = OrderedDict()
        request.app.state.brief_stash = store
    return cast("OrderedDict[str, ActiveBrief]", store)


def _set_active_brief(request: Request, session_id: str, brief: ActiveBrief) -> None:
    """Set the session's active brief to ``brief``, replacing any previous.

    One context per session, whatever its kind: a briefing replaces a case
    and a case replaces a briefing. Evicts the oldest entry when the per-app
    cap is reached (the history-store idiom). Re-inserting moves the entry
    to the most-recently-used end.
    """
    store = _brief_stash(request)
    store.pop(session_id, None)
    while len(store) >= _BRIEF_STASH_LIMIT:
        evicted_key, _ = store.popitem(last=False)
        logger.warning(
            "chat: evicting consultation brief for session %s (LRU cap %d)",
            evicted_key,
            _BRIEF_STASH_LIMIT,
        )
    store[session_id] = brief


def _get_active_brief(request: Request, session_id: str) -> ActiveBrief | None:
    """Return the session's active brief, or ``None`` if unset."""
    store = _brief_stash(request)
    if session_id in store:
        store.move_to_end(session_id)
        return store[session_id]
    return None


def _clear_active_brief(request: Request, session_id: str) -> None:
    """Drop the session's active brief, of either kind. Safe on absent keys."""
    _brief_stash(request).pop(session_id, None)
