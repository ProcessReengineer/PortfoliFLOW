# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Keep — the target, the picker's options and the one write (ADR-0138 §8 channel 2, §9).

Every Shirley answer, every Shirley figure and every Planning Desk result
carries **Keep →** (one click into the active target) and **Keep to…** (the
picker: a thread, the file itself, another open or waiting case). No comment,
no dialog. The target is the session's case brief plus its optional thread
(:class:`web.brief_stash.ActiveBrief`, D-CS-B-86); the Planning Desk's
capturing marker wins over it (D-CS-B-89).

Helpers only, no router — the ``web/htmx_poll.py`` precedent: the chat routes
and the Planning Desk route both use them. This module imports neither route
module (``planning_desk.py`` is imported by ``cases.py``, which ``chat.py``
imports), so it reads the persistence layer directly.

Keep writes ``case_kept`` rows with ``via = "keep"`` through
:meth:`CaseKeptRepository.keep` and nothing else; the record is untouched (a
keep is no milestone).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.repositories._case_common import _MUTABLE_STATES
from core.repositories.case_repository import CaseDTO, CaseRepository
from core.repositories.case_working_file_repository import (
    CaseKeptDTO,
    CaseKeptRepository,
    CaseThreadDTO,
    CaseThreadRepository,
)
from web.brief_stash import BRIEF_KIND_CASE, ActiveBrief

#: A consultation's caption: the excerpt's first non-empty line, clipped
#: (D-CS-B-85) the way the brief digest clips its lines.
_CAPTION_LINE_CHARS: int = 120

#: The picker's two refusals (D-4a-6): the target moved under the PM's feet.
KEEP_CASE_GONE_MESSAGE: str = "That case is closed or gone — pick another."
KEEP_THREAD_GONE_MESSAGE: str = "That thread is gone — pick another."

#: The event a successful keep raises, and the one a changed brief raises
#: (D-CS-B-88): the case's Threads block re-renders on the first, the dock's
#: banner on the second.
CASE_KEPT_EVENT: str = "pf:case-kept"
BRIEF_CHANGED_EVENT: str = "pf:brief-changed"


@dataclass(frozen=True)
class KeepTarget:
    """Where a keep writes: an open or waiting case, and a live thread or the file."""

    case: CaseDTO
    thread: CaseThreadDTO | None


def _badge(case: CaseDTO) -> str:
    """``CASE-nnnn``."""
    return f"CASE-{case.case_number:04d}"


def target_label(thread: CaseThreadDTO | None) -> str:
    """Name a keep target's place: ``the file`` or ``thread "title"`` (D-4a-2, D-4a-3)."""
    return f'thread "{thread.title}"' if thread is not None else "the file"


async def live_thread(
    db: AsyncSession, case_id: UUID, thread_id: UUID | None
) -> CaseThreadDTO | None:
    """Return ``thread_id`` when it is a live thread of ``case_id``, else ``None``.

    A removed thread, another case's thread or an unknown id reads as the
    file — validated fresh on every read (D-CS-B-86).
    """
    if thread_id is None:
        return None
    thread = await CaseThreadRepository(db).get(thread_id)
    if thread is None or thread.case_id != case_id or thread.removed_at is not None:
        return None
    return thread


async def resolve_keep_target(
    db: AsyncSession,
    *,
    brief: ActiveBrief | None,
    marker_case: CaseDTO | None = None,
) -> KeepTarget | None:
    """Resolve the session's keep target, or ``None`` when there is none.

    The marker case wins (the Planning Desk's "capturing for", D-CS-B-89),
    with the brief's thread only when the brief names the same case. Else
    the brief's case when it is a *case* brief and the case is still open
    or waiting; a stale case — closed or vanished — yields ``None``, and so
    does a Watch Desk briefing (nothing is kept into a briefing). The thread
    is validated through :func:`live_thread`: removed or foreign reads as
    the file.

    Args:
        db: A tenant-scoped session.
        brief: The session's active brief, or ``None``.
        marker_case: An already validated marker case, or ``None``.

    Returns:
        The target, or ``None``.
    """
    is_case_brief = brief is not None and brief.kind == BRIEF_KIND_CASE
    if marker_case is not None and marker_case.state in _MUTABLE_STATES:
        thread_id = (
            brief.thread_id
            if brief is not None and is_case_brief and brief.id == marker_case.id
            else None
        )
        return KeepTarget(marker_case, await live_thread(db, marker_case.id, thread_id))
    if brief is None or not is_case_brief:
        return None
    case = await CaseRepository(db).get(brief.id)
    if case is None or case.state not in _MUTABLE_STATES:
        return None
    return KeepTarget(case, await live_thread(db, case.id, brief.thread_id))


async def posted_target(db: AsyncSession, *, case_id: str, thread_id: str) -> KeepTarget | str:
    """Resolve a target the picker posted, or return the refusal to show.

    ``thread_id`` ``""`` is the file. A case that is unknown, foreign
    (RLS → ``None``) or closed is :data:`KEEP_CASE_GONE_MESSAGE`; a thread
    that is not a live thread of it is :data:`KEEP_THREAD_GONE_MESSAGE`.
    """
    try:
        case_uuid = UUID(case_id.strip())
    except ValueError:
        return KEEP_CASE_GONE_MESSAGE
    case = await CaseRepository(db).get(case_uuid)
    if case is None or case.state not in _MUTABLE_STATES:
        return KEEP_CASE_GONE_MESSAGE
    raw_thread = thread_id.strip()
    if not raw_thread:
        return KeepTarget(case, None)
    try:
        thread_uuid = UUID(raw_thread)
    except ValueError:
        return KEEP_THREAD_GONE_MESSAGE
    thread = await live_thread(db, case.id, thread_uuid)
    if thread is None:
        return KEEP_THREAD_GONE_MESSAGE
    return KeepTarget(case, thread)


async def picker_case(
    db: AsyncSession, *, requested: str = "", target: KeepTarget | None = None
) -> CaseDTO | None:
    """The case the picker shows: the requested one, else the target's, else the first unclosed.

    ``requested`` is the *Another case…* select's choice; it counts only
    while the case is open or waiting. With no target either, the picker
    offers the most recently worked open case (then the waiting ones) rather
    than claiming there is none; ``None`` only when no case takes writes.
    """
    if requested.strip():
        try:
            case = await CaseRepository(db).get(UUID(requested.strip()))
        except ValueError:
            case = None
        if case is not None and case.state in _MUTABLE_STATES:
            return case
    if target is not None:
        return target.case
    unclosed = await CaseRepository(db).list_unclosed()
    return unclosed[0] if unclosed else None


async def keep_target_options(db: AsyncSession, case: CaseDTO) -> dict[str, Any]:
    """The picker's options for ``case`` (D-CS-B-87).

    Returns:
        ``{"case": {id, badge, title}, "threads": [{id, title, status_label}],
        "others": [{id, label}]}`` — the case's live threads in position
        order, and every other open or waiting case
        (:meth:`CaseRepository.list_unclosed`), a waiting one labelled
        ``CASE-nnnn — title · waiting``.
    """
    threads = await CaseThreadRepository(db).list_for_case(case.id)
    others = [other for other in await CaseRepository(db).list_unclosed() if other.id != case.id]
    return {
        "case": {"id": str(case.id), "badge": _badge(case), "title": case.title},
        "threads": [
            {
                "id": str(thread.id),
                "title": thread.title,
                "status_label": thread.status.capitalize(),
            }
            for thread in threads
        ],
        "others": [
            {
                "id": str(other.id),
                "label": f"{_badge(other)} — {other.title}"
                + (" · waiting" if other.state == "waiting" else ""),
            }
            for other in others
        ],
    }


async def picker_options(
    db: AsyncSession, *, requested: str = "", target: KeepTarget | None = None
) -> tuple[dict[str, Any] | None, str]:
    """The picker's options and the place preselected in them.

    The case is :func:`picker_case`'s; ``None`` options mean no case takes
    writes (the no-case state, D-4a-5). The place preselected is the
    target's thread when the picker shows the target's case, else the file
    (``""``).

    Returns:
        ``(options, selected_thread)`` — :func:`keep_target_options`'s dict
        or ``None``, and a thread id or ``""``.
    """
    case = await picker_case(db, requested=requested, target=target)
    if case is None:
        return None, ""
    selected = ""
    if target is not None and target.thread is not None and target.case.id == case.id:
        selected = str(target.thread.id)
    return await keep_target_options(db, case), selected


async def keep_artifact(
    db: AsyncSession,
    *,
    target: KeepTarget,
    artifact: str,
    payload: dict[str, Any],
    caption: str,
    kept_by: UUID,
    now: datetime,
) -> CaseKeptDTO:
    """Keep one artefact into ``target`` — the one ``via="keep"`` write (D-CS-B-85).

    No comment: Keep carries none (ADR-0138 §8). The payload is immutable once
    kept (§9).

    Raises:
        CaseClosedError: If the case closed since the target was resolved.
        CaseStateInvalid: If the case vanished.
        CaseWorkingFileInvalid: If the thread was removed since (``field``
            ``thread_id``).
    """
    return await CaseKeptRepository(db).keep(
        target.case.id,
        artifact=artifact,
        payload=payload,
        kept_by=kept_by,
        via="keep",
        now=now,
        thread_id=target.thread.id if target.thread is not None else None,
        caption=caption,
    )


def kept_line_context(kept: CaseKeptDTO, target: KeepTarget) -> dict[str, str]:
    """The kept line's context: ``{badge, href, where, when}`` (D-CS-B-88, D-4a-2).

    ``href`` opens the case at the thread (``#cs-thread-{id}``) or at the
    Threads block (``#cs-threads``); ``when`` is ``HH:MM`` in UTC.
    """
    case = target.case
    anchor = f"cs-thread-{target.thread.id}" if target.thread is not None else "cs-threads"
    return {
        "badge": _badge(case),
        "href": f"/cases/{case.id}#{anchor}",
        "where": target_label(target.thread),
        "when": f"{kept.kept_at.astimezone(timezone.utc):%H:%M}",
    }


def keep_trigger(case_id: UUID, *, brief_changed: bool) -> str:
    """The ``HX-Trigger`` JSON a successful keep answers with (D-CS-B-88)."""
    events: dict[str, dict[str, str]] = {CASE_KEPT_EVENT: {"case_id": str(case_id)}}
    if brief_changed:
        events[BRIEF_CHANGED_EVENT] = {}
    return json.dumps(events)


def consultation_caption(text: str) -> str:
    """A kept answer's caption: its first non-empty line, clipped to 120 characters (D-CS-B-85)."""
    for line in text.splitlines():
        snippet = line.strip()
        if snippet:
            if len(snippet) > _CAPTION_LINE_CHARS:
                return snippet[: _CAPTION_LINE_CHARS - 1].rstrip() + "…"
            return snippet
    return ""
