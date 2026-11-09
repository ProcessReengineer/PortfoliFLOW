# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The whole-file snapshot a ``frozen`` record entry carries (ADR-0138 §2).

Every milestone of a case freezes the working file as it stands into a
``frozen`` entry written in the milestone's own transaction (the milestone
path of :class:`~core.repositories.case_repository.CaseRepository`). This
module builds that snapshot: the **whole file** — the case's working-file
columns, its investment links, its live threads with their kept artefacts,
its file-level kept artefacts, its figures, conversations, open points and
decisions (recorded and draft).

By reference and by value:

* **Kept artefacts are frozen by reference** — ``kept_id``, class, caption,
  comment, ``payload_sha256`` and ``attachment_id``. A kept row is never
  deleted (``removed_at`` only), so a reference made before a removal still
  resolves (D-CS-M-5). Attachment bytes are never copied.
* **``generated_document`` is the one class frozen by value** (ADR-0138
  §9): its payload is editable, so its kept-ref also carries ``payload``.
* **Everything else is by value** — figures, conversations, points and
  decisions are hard-deletable or mutable, so the snapshot embeds them.

Two inputs the builder cannot know are injected by the caller (D-CS-B-8):
``stand`` — Where we stand as rendered (CS-2's composition) — and
``book_values`` — the resolved live values of ``book`` figures (CS-2's
reads). Absent, the snapshot records ``where_we_stand: null`` and the
figure's stored ``value_text`` / ``as_of`` (null for ``book``).

The builder reads the tables through the ORM models, never through the
repositories, so :mod:`core.repositories.case_repository` can import it
without a cycle. It reads under the session's RLS and never writes.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import CaseStateInvalid
from core.models.case import (
    Case,
    CaseConversation,
    CaseDecision,
    CaseFigure,
    CaseInvestment,
    CaseKept,
    CasePoint,
    CaseThread,
)

#: The version of the snapshot shape; a ``frozen`` payload records it.
SNAPSHOT_VERSION: int = 1

# The kept class whose payload is copied into the snapshot (ADR-0138 §9).
_BY_VALUE_ARTIFACT = "generated_document"

_CASE_FIELDS: tuple[str, ...] = (
    "id",
    "case_number",
    "title",
    "description",
    "state",
    "framing",
    "counterparty",
    "due_on",
    "wait_until",
    "owner_user_id",
    "opened_by",
    "opened_at",
    "finding_id",
    "closed_by",
    "closed_at",
    "closing_note",
)
_INVESTMENT_FIELDS: tuple[str, ...] = ("investment_id", "linked_by", "linked_at")
_THREAD_FIELDS: tuple[str, ...] = (
    "id",
    "position",
    "title",
    "status",
    "prose",
    "conclusion",
    "created_at",
    "concluded_at",
)
_FIGURE_FIELDS: tuple[str, ...] = (
    "id",
    "position",
    "label",
    "source",
    "value_text",
    "as_of",
    "note",
    "source_ref",
)
_CONVERSATION_FIELDS: tuple[str, ...] = (
    "id",
    "position",
    "held_on",
    "channel",
    "with_whom",
    "what_was_said",
    "what_was_promised",
    "duration_minutes",
)
_POINT_FIELDS: tuple[str, ...] = (
    "id",
    "position",
    "text",
    "owner_user_id",
    "due_on",
    "done_at",
    "ticket_id",
)
_DECISION_FIELDS: tuple[str, ...] = (
    "id",
    "options",
    "chosen_key",
    "rationale",
    "decided_by",
    "decided_at",
    "recorded_by",
    "recorded_at",
    "record_entry_id",
)


def _plain(value: Any) -> Any:
    """Return ``value`` JSON-ready: UUIDs as ``str``, dates as ISO 8601."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _fields(model: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: _plain(getattr(model, name)) for name in names}


def _kept_ref(kept: CaseKept) -> dict[str, Any]:
    """Return the snapshot reference of one kept row (by value for one class)."""
    ref: dict[str, Any] = {
        "kept_id": str(kept.id),
        "artifact": kept.artifact,
        "caption": kept.caption,
        "comment": kept.comment,
        "payload_sha256": kept.payload_sha256,
        "attachment_id": _plain(kept.attachment_id),
        "kept_by": str(kept.kept_by),
        "via": kept.via,
        "kept_at": _plain(kept.kept_at),
        "position": kept.position,
    }
    if kept.artifact == _BY_VALUE_ARTIFACT:
        ref["payload"] = kept.payload
    return ref


def _figure(figure: CaseFigure, book_values: dict[str, dict] | None) -> dict[str, Any]:
    """Return one figure by value, a ``book`` figure's live value applied."""
    snap = _fields(figure, _FIGURE_FIELDS)
    if figure.source == "book" and book_values is not None:
        resolved = book_values.get(str(figure.id))
        if resolved is not None:
            snap["value_text"] = resolved.get("value_text")
            snap["as_of"] = resolved.get("as_of")
    return snap


async def build_file_snapshot(
    session: AsyncSession,
    case_id: UUID,
    *,
    stand: dict | None = None,
    book_values: dict[str, dict] | None = None,
) -> dict:
    """Return the whole working file of a case as a JSON-serialisable dict.

    Reads the ``cases`` row and the working-file tables under the session's
    RLS. Removed threads and removed kept rows are absent; a live kept row
    that sits in a removed thread joins the file-level ``kept``, as the live
    file shows it under *Kept in the file* (D-CS-B-100). The
    ``milestone_entry_id`` / ``milestone_kind`` keys of the ``frozen``
    payload are the repository's, not the builder's.

    Args:
        session: A tenant-scoped session.
        case_id: The case to snapshot.
        stand: Where we stand as rendered, or ``None`` (recorded as
            ``where_we_stand: null``).
        book_values: ``str(figure_id) → {"value_text", "as_of"}`` for
            ``book`` figures; a value for any other source is ignored, and a
            ``book`` figure without an entry keeps its stored nulls.

    Returns:
        The snapshot dict (``snapshot_version`` :data:`SNAPSHOT_VERSION`):
        UUIDs as ``str``, dates and datetimes as ISO 8601 strings.

    Raises:
        CaseStateInvalid: If no such case exists in the active tenant.
    """
    case = (await session.execute(select(Case).where(Case.id == case_id))).scalar_one_or_none()
    if case is None:
        raise CaseStateInvalid(f"No case {case_id} in this tenant to snapshot.", field="state")

    investments = (
        (
            await session.execute(
                select(CaseInvestment)
                .where(CaseInvestment.case_id == case_id)
                .order_by(CaseInvestment.linked_at.asc(), CaseInvestment.investment_id.asc())
            )
        )
        .scalars()
        .all()
    )
    threads = (
        (
            await session.execute(
                select(CaseThread)
                .where(CaseThread.case_id == case_id, CaseThread.removed_at.is_(None))
                .order_by(CaseThread.position.asc())
            )
        )
        .scalars()
        .all()
    )
    kept_rows = (
        (
            await session.execute(
                select(CaseKept)
                .where(CaseKept.case_id == case_id, CaseKept.removed_at.is_(None))
                .order_by(CaseKept.thread_id.asc().nulls_first(), CaseKept.position.asc())
            )
        )
        .scalars()
        .all()
    )
    figures = (
        (
            await session.execute(
                select(CaseFigure)
                .where(CaseFigure.case_id == case_id)
                .order_by(CaseFigure.position.asc())
            )
        )
        .scalars()
        .all()
    )
    conversations = (
        (
            await session.execute(
                select(CaseConversation)
                .where(CaseConversation.case_id == case_id)
                .order_by(CaseConversation.held_on.desc(), CaseConversation.position.desc())
            )
        )
        .scalars()
        .all()
    )
    points = (
        (
            await session.execute(
                select(CasePoint)
                .where(CasePoint.case_id == case_id)
                .order_by(
                    CasePoint.done_at.asc().nulls_first(),
                    CasePoint.due_on.asc().nulls_last(),
                    CasePoint.position.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    decisions = (
        (
            await session.execute(
                select(CaseDecision)
                .where(CaseDecision.case_id == case_id)
                .order_by(
                    CaseDecision.recorded_at.asc().nulls_last(), CaseDecision.created_at.asc()
                )
            )
        )
        .scalars()
        .all()
    )

    thread_snaps: list[dict[str, Any]] = []
    kept_by_thread: dict[UUID, list[dict[str, Any]]] = {}
    for thread in threads:
        snap = _fields(thread, _THREAD_FIELDS)
        snap["kept"] = kept_by_thread.setdefault(thread.id, [])
        thread_snaps.append(snap)
    file_kept: list[dict[str, Any]] = []
    for kept in kept_rows:
        if kept.thread_id is not None and kept.thread_id in kept_by_thread:
            kept_by_thread[kept.thread_id].append(_kept_ref(kept))
        else:
            file_kept.append(_kept_ref(kept))

    recorded: list[dict[str, Any]] = []
    draft: dict[str, Any] | None = None
    for decision in decisions:
        if decision.recorded_at is None:
            draft = _fields(decision, _DECISION_FIELDS)
        else:
            recorded.append(_fields(decision, _DECISION_FIELDS))

    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "case": _fields(case, _CASE_FIELDS),
        "where_we_stand": stand,
        "investments": [_fields(link, _INVESTMENT_FIELDS) for link in investments],
        "threads": thread_snaps,
        "kept": file_kept,
        "figures": [_figure(figure, book_values) for figure in figures],
        "conversations": [_fields(c, _CONVERSATION_FIELDS) for c in conversations],
        "open_points": [_fields(point, _POINT_FIELDS) for point in points],
        "decisions": {"recorded": recorded, "draft": draft},
    }


def snapshot_kept_refs(snapshot: dict) -> list[dict]:
    """Return every kept-ref of a snapshot — thread-level first, then file-level.

    Pure: reads the dict :func:`build_file_snapshot` returned (or the
    ``file`` of a ``frozen`` payload) and returns the references in thread
    order, then the file-level ones.

    Args:
        snapshot: A whole-file snapshot.

    Returns:
        The kept-ref dicts, as stored in the snapshot.
    """
    refs: list[dict] = []
    for thread in snapshot.get("threads", []):
        refs.extend(thread.get("kept", []))
    refs.extend(snapshot.get("kept", []))
    return refs
