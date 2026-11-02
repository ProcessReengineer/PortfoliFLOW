# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Working-file repositories — the mutable layer of a case (ADR-0138 §2).

A case has two layers: the append-only **record** (``case_entries``,
:class:`~core.repositories.case_repository.CaseRepository`) and the
**working file** — what the PM types, keeps or links. This module backs the
working file's eight tables (migration b037) with eight small repositories:
threads, kept artefacts, figures, conversations, open points, decisions,
investment links and proposal packages.

Two rules hold for every write here, enforced once in the private base:

* **A closed case is read-only.** Every write first reads the parent case
  and refuses it with :class:`~core.exceptions.CaseClosedError` when it is
  closed (a missing case is :class:`~core.exceptions.CaseStateInvalid`). An
  open or waiting case is mutable alike (ADR-0138 §4).
* **Every write bumps ``cases.updated_at``** to the ``now`` the caller
  passes (D-CS-B-5): ``updated_at`` is the working-file stamp the list
  orders by.

Reads work on any state — the working-file rows of a closed case stay in
their tables, read-only, so search and Shirley read closed cases without
unpacking snapshots. Snapshots themselves (the ``frozen`` record entry of a
milestone) are not this module's concern; they land in P-CS-1b, which
reuses :func:`kept_payload_sha256` and reads removed kept rows through
:meth:`CaseKeptRepository.get`.

Vocabularies are TEXT columns enforced here (no SQL enums, no CHECKs), and
every write validates its arguments before any SQL runs. A row addressed by
an id that does not exist in the active tenant raises
:class:`~core.exceptions.CaseWorkingFileInvalid` naming the id field.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update

from core.exceptions import (
    CaseClosedError,
    CaseDecisionRecorded,
    CaseKeptImmutable,
    CasePointLinked,
    CaseStateInvalid,
    CaseWorkingFileInvalid,
)
from core.models.case import (
    Case,
    CaseAttachment,
    CaseConversation,
    CaseDecision,
    CaseFigure,
    CaseInvestment,
    CaseKept,
    CasePoint,
    CaseProposal,
    CaseThread,
)
from core.repositories.base import BaseRepository
from core.repositories._case_common import _MUTABLE_STATES, _UNSET

# ---------------------------------------------------------------------------
# Vocabularies (ADR-0138 §3, §8, §9 and Object model)
# ---------------------------------------------------------------------------

_THREAD_STATUSES: frozenset[str] = frozenset({"open", "confirmed", "parked", "refuted"})
_KEPT_ARTIFACTS: frozenset[str] = frozenset(
    {
        "document",
        "consultation",
        "scenario_snapshot",
        "chart_snapshot",
        "document_excerpt",
        "generated_document",
    }
)
# The kept classes that address an attachment; for every other class
# attachment_id must be null.
_ATTACHMENT_ARTIFACTS: frozenset[str] = frozenset({"document", "document_excerpt"})
# The one kept class whose payload is edited in place (§9).
_MUTABLE_PAYLOAD_ARTIFACT = "generated_document"
_KEPT_VIAS: frozenset[str] = frozenset({"keep", "shirley", "proposal"})
_FIGURE_SOURCES: frozenset[str] = frozenset(
    {"book", "typed", "document", "kept", "conversation", "external"}
)
# A book figure's source_ref is its read key (§3).
_BOOK_SOURCE_REF_KEYS: tuple[str, ...] = ("investment_id", "metric")
_CONVERSATION_CHANNELS: frozenset[str] = frozenset({"call", "meeting", "other"})
_PROPOSAL_RESOLUTIONS: frozenset[str] = frozenset({"accepted", "discarded"})


def kept_payload_sha256(payload: dict) -> str:
    """Return the canonical SHA-256 of a kept artefact's payload (D-CS-B-4).

    The canonical form is the payload serialised as JSON with sorted keys,
    no insignificant whitespace (``separators=(",", ":")``) and non-ASCII
    characters kept as they are (``ensure_ascii=False``), encoded as UTF-8.
    The hash is independent of key order and stable across processes; the
    freeze (P-CS-1b) and the render (P-CS-4b) reuse this function so all
    three agree on one digest.

    Args:
        payload: The kept payload (a JSON-serialisable dict).

    Returns:
        The lowercase hex digest.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _invalid(message: str, field: str) -> CaseWorkingFileInvalid:
    return CaseWorkingFileInvalid(message, field=field)


def _require_in(value: str, vocabulary: frozenset[str], field: str) -> None:
    if value not in vocabulary:
        raise _invalid(f"Invalid {field} {value!r}; expected one of {sorted(vocabulary)}.", field)


def _require_text(value: str | None, field: str) -> str:
    """Return ``value`` stripped; raise when nothing remains."""
    stripped = (value or "").strip()
    if not stripped:
        raise _invalid(f"The {field} must not be empty.", field)
    return stripped


# ---------------------------------------------------------------------------
# DTOs — one per table, fields in the migration's column order
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseThreadDTO:
    """Plain data-only view of a ``case_threads`` row."""

    id: UUID
    tenant_id: UUID
    case_id: UUID
    position: int
    title: str
    status: str
    prose: str
    conclusion: str | None
    created_at: datetime
    concluded_at: datetime | None
    removed_at: datetime | None


@dataclass(frozen=True)
class CaseKeptDTO:
    """Plain data-only view of a ``case_kept`` row (``payload`` is opaque)."""

    id: UUID
    tenant_id: UUID
    case_id: UUID
    thread_id: UUID | None
    position: int
    artifact: str
    caption: str | None
    comment: str | None
    payload: dict
    payload_sha256: str
    attachment_id: UUID | None
    kept_by: UUID
    via: str
    kept_at: datetime
    removed_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class CaseFigureDTO:
    """Plain data-only view of a ``case_figures`` row."""

    id: UUID
    tenant_id: UUID
    case_id: UUID
    position: int
    label: str
    source: str
    value_text: str | None
    source_ref: dict
    as_of: str | None
    note: str | None
    created_at: datetime


@dataclass(frozen=True)
class CaseConversationDTO:
    """Plain data-only view of a ``case_conversations`` row."""

    id: UUID
    tenant_id: UUID
    case_id: UUID
    position: int
    held_on: date
    channel: str
    with_whom: str
    what_was_said: str
    what_was_promised: str | None
    duration_minutes: int | None
    created_at: datetime


@dataclass(frozen=True)
class CasePointDTO:
    """Plain data-only view of a ``case_points`` row."""

    id: UUID
    tenant_id: UUID
    case_id: UUID
    position: int
    text: str
    owner_user_id: UUID | None
    due_on: date | None
    done_at: datetime | None
    ticket_id: UUID | None
    created_at: datetime


@dataclass(frozen=True)
class CaseDecisionDTO:
    """Plain data-only view of a ``case_decisions`` row.

    ``recorded_at`` null means the row is the case's draft.
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    options: list
    chosen_key: str | None
    rationale: str | None
    decided_by: str | None
    decided_at: date | None
    recorded_by: UUID | None
    recorded_at: datetime | None
    record_entry_id: UUID | None
    created_at: datetime


@dataclass(frozen=True)
class CaseInvestmentDTO:
    """Plain data-only view of a ``case_investments`` row."""

    id: UUID
    tenant_id: UUID
    case_id: UUID
    investment_id: UUID
    linked_by: UUID
    linked_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class CaseProposalDTO:
    """Plain data-only view of a ``case_proposals`` row (``items`` is opaque)."""

    id: UUID
    tenant_id: UUID
    case_id: UUID
    requested_by: UUID
    requested_at: datetime
    request_text: str
    landed_at: datetime | None
    items: list
    resolved_at: datetime | None
    created_at: datetime


_THREAD_COLUMNS = (
    CaseThread.id,
    CaseThread.tenant_id,
    CaseThread.case_id,
    CaseThread.position,
    CaseThread.title,
    CaseThread.status,
    CaseThread.prose,
    CaseThread.conclusion,
    CaseThread.created_at,
    CaseThread.concluded_at,
    CaseThread.removed_at,
)
_KEPT_COLUMNS = (
    CaseKept.id,
    CaseKept.tenant_id,
    CaseKept.case_id,
    CaseKept.thread_id,
    CaseKept.position,
    CaseKept.artifact,
    CaseKept.caption,
    CaseKept.comment,
    CaseKept.payload,
    CaseKept.payload_sha256,
    CaseKept.attachment_id,
    CaseKept.kept_by,
    CaseKept.via,
    CaseKept.kept_at,
    CaseKept.removed_at,
    CaseKept.created_at,
)
_FIGURE_COLUMNS = (
    CaseFigure.id,
    CaseFigure.tenant_id,
    CaseFigure.case_id,
    CaseFigure.position,
    CaseFigure.label,
    CaseFigure.source,
    CaseFigure.value_text,
    CaseFigure.source_ref,
    CaseFigure.as_of,
    CaseFigure.note,
    CaseFigure.created_at,
)
_CONVERSATION_COLUMNS = (
    CaseConversation.id,
    CaseConversation.tenant_id,
    CaseConversation.case_id,
    CaseConversation.position,
    CaseConversation.held_on,
    CaseConversation.channel,
    CaseConversation.with_whom,
    CaseConversation.what_was_said,
    CaseConversation.what_was_promised,
    CaseConversation.duration_minutes,
    CaseConversation.created_at,
)
_POINT_COLUMNS = (
    CasePoint.id,
    CasePoint.tenant_id,
    CasePoint.case_id,
    CasePoint.position,
    CasePoint.text,
    CasePoint.owner_user_id,
    CasePoint.due_on,
    CasePoint.done_at,
    CasePoint.ticket_id,
    CasePoint.created_at,
)
_DECISION_COLUMNS = (
    CaseDecision.id,
    CaseDecision.tenant_id,
    CaseDecision.case_id,
    CaseDecision.options,
    CaseDecision.chosen_key,
    CaseDecision.rationale,
    CaseDecision.decided_by,
    CaseDecision.decided_at,
    CaseDecision.recorded_by,
    CaseDecision.recorded_at,
    CaseDecision.record_entry_id,
    CaseDecision.created_at,
)
_INVESTMENT_COLUMNS = (
    CaseInvestment.id,
    CaseInvestment.tenant_id,
    CaseInvestment.case_id,
    CaseInvestment.investment_id,
    CaseInvestment.linked_by,
    CaseInvestment.linked_at,
    CaseInvestment.created_at,
)
_PROPOSAL_COLUMNS = (
    CaseProposal.id,
    CaseProposal.tenant_id,
    CaseProposal.case_id,
    CaseProposal.requested_by,
    CaseProposal.requested_at,
    CaseProposal.request_text,
    CaseProposal.landed_at,
    CaseProposal.items,
    CaseProposal.resolved_at,
    CaseProposal.created_at,
)


# ---------------------------------------------------------------------------
# The shared base
# ---------------------------------------------------------------------------


class _WorkingFileRepository(BaseRepository):
    """The two things every working-file write does, plus row plumbing."""

    async def _mutable_case(self, case_id: UUID) -> tuple[UUID, str]:
        """Return ``(tenant_id, state)``; raise if the case is absent or closed.

        Raises:
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        row = (
            await self._session.execute(
                select(Case.tenant_id, Case.state).where(Case.id == case_id)
            )
        ).one_or_none()
        if row is None:
            raise CaseStateInvalid(f"No case {case_id} in this tenant.", field="state")
        tenant_id, state = row
        if state not in _MUTABLE_STATES:
            raise CaseClosedError(
                f"Case {case_id} is closed; its working file is read-only (ADR-0138 §2)."
            )
        return tenant_id, state

    async def _touch(self, case_id: UUID, now: datetime) -> None:
        """Set ``cases.updated_at = now`` — the working-file stamp (D-CS-B-5)."""
        await self._session.execute(update(Case).where(Case.id == case_id).values(updated_at=now))

    async def _next_position(self, model: Any, case_id: UUID) -> int:
        """Return ``COALESCE(MAX(position), 0) + 1`` over the case's rows.

        Removed rows are included, so a position is never reused.
        """
        result = await self._session.execute(
            select(func.coalesce(func.max(model.position), 0) + 1).where(model.case_id == case_id)
        )
        return int(result.scalar_one())

    async def _fetch(self, columns: tuple[Any, ...], row_id: UUID) -> Any | None:
        """Return the row with ``row_id`` (as a SQLAlchemy Row), or ``None``."""
        model_id = columns[0]
        result = await self._session.execute(select(*columns).where(model_id == row_id))
        return result.one_or_none()

    async def _fetch_or_raise(self, columns: tuple[Any, ...], row_id: UUID, field: str) -> Any:
        row = await self._fetch(columns, row_id)
        if row is None:
            raise _invalid(f"No {field.removesuffix('_id')} {row_id} in this tenant.", field)
        return row

    async def _insert(self, model: Any, columns: tuple[Any, ...], values: dict[str, Any]) -> Any:
        stmt = insert(model).values(**values).returning(*columns)
        return (await self._session.execute(stmt)).one()

    async def _update(
        self, model: Any, columns: tuple[Any, ...], row_id: UUID, values: dict[str, Any]
    ) -> Any:
        stmt = update(model).where(model.id == row_id).values(**values).returning(*columns)
        return (await self._session.execute(stmt)).one()

    async def _list(self, columns: tuple[Any, ...], *clauses: Any, order_by: tuple) -> list[Any]:
        result = await self._session.execute(select(*columns).where(*clauses).order_by(*order_by))
        return list(result.all())


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


class CaseThreadRepository(_WorkingFileRepository):
    """Threads — one sub-question or hypothesis each (ADR-0138 §3)."""

    async def add(
        self, case_id: UUID, *, title: str, now: datetime, prose: str = ""
    ) -> CaseThreadDTO:
        """Add a thread at the next position, status ``open``.

        Raises:
            CaseWorkingFileInvalid: If ``title`` is empty.
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        clean_title = _require_text(title, "title")
        tenant_id, _ = await self._mutable_case(case_id)
        position = await self._next_position(CaseThread, case_id)
        row = await self._insert(
            CaseThread,
            _THREAD_COLUMNS,
            {
                "tenant_id": tenant_id,
                "case_id": case_id,
                "position": position,
                "title": clean_title,
                "status": "open",
                "prose": prose,
                "created_at": now,
            },
        )
        await self._touch(case_id, now)
        return CaseThreadDTO(**row._mapping)

    async def update(
        self,
        thread_id: UUID,
        *,
        now: datetime,
        title: str = _UNSET,
        prose: str = _UNSET,
        status: str = _UNSET,
        conclusion: str | None = _UNSET,
    ) -> CaseThreadDTO:
        """Update the given fields of a thread.

        A ``status`` that leaves ``open`` sets ``concluded_at = now``; a
        return to ``open`` clears it.

        Raises:
            CaseWorkingFileInvalid: On an empty title, a status outside
                ``open`` · ``confirmed`` · ``parked`` · ``refuted``, or an
                unknown ``thread_id``.
            CaseClosedError: If the case is closed.
        """
        values: dict[str, Any] = {}
        if title is not _UNSET:
            values["title"] = _require_text(title, "title")
        if prose is not _UNSET:
            values["prose"] = prose
        if conclusion is not _UNSET:
            values["conclusion"] = conclusion
        if status is not _UNSET:
            _require_in(status, _THREAD_STATUSES, "status")
            values["status"] = status
        current = await self._fetch_or_raise(_THREAD_COLUMNS, thread_id, "thread_id")
        await self._mutable_case(current.case_id)
        if status is not _UNSET and status != current.status:
            if status == "open":
                values["concluded_at"] = None
            elif current.status == "open":
                values["concluded_at"] = now
        row = (
            await self._update(CaseThread, _THREAD_COLUMNS, thread_id, values)
            if values
            else current
        )
        await self._touch(current.case_id, now)
        return CaseThreadDTO(**row._mapping)

    async def remove(self, thread_id: UUID, *, now: datetime) -> CaseThreadDTO:
        """Remove a thread from the file by setting ``removed_at``.

        The row stays: kept rows keep their ``thread_id``. A second removal
        keeps the first ``removed_at``.

        Raises:
            CaseWorkingFileInvalid: If ``thread_id`` is unknown.
            CaseClosedError: If the case is closed.
        """
        current = await self._fetch_or_raise(_THREAD_COLUMNS, thread_id, "thread_id")
        await self._mutable_case(current.case_id)
        row = await self._update(
            CaseThread,
            _THREAD_COLUMNS,
            thread_id,
            {"removed_at": current.removed_at or now},
        )
        await self._touch(current.case_id, now)
        return CaseThreadDTO(**row._mapping)

    async def get(self, thread_id: UUID) -> CaseThreadDTO | None:
        """Return one thread, removed ones included, or ``None``."""
        row = await self._fetch(_THREAD_COLUMNS, thread_id)
        return CaseThreadDTO(**row._mapping) if row is not None else None

    async def list_for_case(
        self, case_id: UUID, *, include_removed: bool = False
    ) -> list[CaseThreadDTO]:
        """Return a case's threads by ``position``; removed ones only on request."""
        clauses: list[Any] = [CaseThread.case_id == case_id]
        if not include_removed:
            clauses.append(CaseThread.removed_at.is_(None))
        rows = await self._list(_THREAD_COLUMNS, *clauses, order_by=(CaseThread.position.asc(),))
        return [CaseThreadDTO(**row._mapping) for row in rows]


# ---------------------------------------------------------------------------
# Kept artefacts
# ---------------------------------------------------------------------------


class CaseKeptRepository(_WorkingFileRepository):
    """Kept artefacts (ADR-0138 §9). There is deliberately no delete method:
    frozen snapshots reference kept rows, so removal is ``removed_at``.
    """

    async def _check_thread(self, case_id: UUID, thread_id: UUID) -> None:
        """Raise unless ``thread_id`` is a live thread of ``case_id``."""
        row = (
            await self._session.execute(
                select(CaseThread.id).where(
                    CaseThread.id == thread_id,
                    CaseThread.case_id == case_id,
                    CaseThread.removed_at.is_(None),
                )
            )
        ).one_or_none()
        if row is None:
            raise _invalid(
                f"Thread {thread_id} is not a live thread of case {case_id}.", "thread_id"
            )

    async def _check_attachment(self, case_id: UUID, attachment_id: UUID) -> None:
        """Raise unless ``attachment_id`` is an attachment of ``case_id``."""
        row = (
            await self._session.execute(
                select(CaseAttachment.id).where(
                    CaseAttachment.id == attachment_id, CaseAttachment.case_id == case_id
                )
            )
        ).one_or_none()
        if row is None:
            raise _invalid(
                f"Attachment {attachment_id} is not an attachment of case {case_id}.",
                "attachment_id",
            )

    async def keep(
        self,
        case_id: UUID,
        *,
        artifact: str,
        payload: dict,
        kept_by: UUID,
        via: str,
        now: datetime,
        thread_id: UUID | None = None,
        caption: str | None = None,
        comment: str | None = None,
        attachment_id: UUID | None = None,
    ) -> CaseKeptDTO:
        """Keep an artefact in a thread or at file level (``thread_id=None``).

        ``payload_sha256`` is computed with :func:`kept_payload_sha256`.

        Args:
            case_id: The case.
            artifact: One of the six kept classes of ADR-0138 §9.
            payload: The class contract (opaque JSONB).
            kept_by: The user who kept (or accepted) it.
            via: ``keep`` · ``shirley`` · ``proposal`` — the channel of §8.
            now: ``kept_at`` and the case's new ``updated_at``.
            thread_id: A live thread of this case, or ``None``.
            caption: Optional caption.
            comment: Optional curation comment.
            attachment_id: Required for ``document`` / ``document_excerpt``
                (an attachment of this case), forbidden otherwise.

        Raises:
            CaseWorkingFileInvalid: On a class or ``via`` outside the
                vocabulary, a non-dict payload, a broken attachment rule, or
                a thread that is not a live thread of the case.
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        _require_in(artifact, _KEPT_ARTIFACTS, "artifact")
        _require_in(via, _KEPT_VIAS, "via")
        if not isinstance(payload, dict):
            raise _invalid("A kept payload must be a JSON object.", "payload")
        if artifact in _ATTACHMENT_ARTIFACTS and attachment_id is None:
            raise _invalid(f"A kept {artifact!r} requires an attachment_id.", "attachment_id")
        if artifact not in _ATTACHMENT_ARTIFACTS and attachment_id is not None:
            raise _invalid(f"A kept {artifact!r} carries no attachment_id.", "attachment_id")
        tenant_id, _ = await self._mutable_case(case_id)
        if thread_id is not None:
            await self._check_thread(case_id, thread_id)
        if attachment_id is not None:
            await self._check_attachment(case_id, attachment_id)
        position = await self._next_position(CaseKept, case_id)
        row = await self._insert(
            CaseKept,
            _KEPT_COLUMNS,
            {
                "tenant_id": tenant_id,
                "case_id": case_id,
                "thread_id": thread_id,
                "position": position,
                "artifact": artifact,
                "caption": caption,
                "comment": comment,
                "payload": payload,
                "payload_sha256": kept_payload_sha256(payload),
                "attachment_id": attachment_id,
                "kept_by": kept_by,
                "via": via,
                "kept_at": now,
                "created_at": now,
            },
        )
        await self._touch(case_id, now)
        return CaseKeptDTO(**row._mapping)

    async def update_placement(
        self,
        kept_id: UUID,
        *,
        now: datetime,
        thread_id: UUID | None = _UNSET,
        caption: str | None = _UNSET,
        comment: str | None = _UNSET,
    ) -> CaseKeptDTO:
        """Move a kept row or edit its caption / comment; the payload is untouched.

        Raises:
            CaseWorkingFileInvalid: On an unknown ``kept_id`` or a thread
                that is not a live thread of the case.
            CaseClosedError: If the case is closed.
        """
        current = await self._fetch_or_raise(_KEPT_COLUMNS, kept_id, "kept_id")
        await self._mutable_case(current.case_id)
        values: dict[str, Any] = {}
        if thread_id is not _UNSET:
            if thread_id is not None:
                await self._check_thread(current.case_id, thread_id)
            values["thread_id"] = thread_id
        if caption is not _UNSET:
            values["caption"] = caption
        if comment is not _UNSET:
            values["comment"] = comment
        row = await self._update(CaseKept, _KEPT_COLUMNS, kept_id, values) if values else current
        await self._touch(current.case_id, now)
        return CaseKeptDTO(**row._mapping)

    async def update_generated_document(
        self, kept_id: UUID, *, payload: dict, now: datetime
    ) -> CaseKeptDTO:
        """Rewrite the body of a ``generated_document`` and recompute its hash.

        Raises:
            CaseWorkingFileInvalid: On an unknown ``kept_id`` or a non-dict
                payload.
            CaseKeptImmutable: If the kept row is of any other class (§9).
            CaseClosedError: If the case is closed.
        """
        if not isinstance(payload, dict):
            raise _invalid("A kept payload must be a JSON object.", "payload")
        current = await self._fetch_or_raise(_KEPT_COLUMNS, kept_id, "kept_id")
        if current.artifact != _MUTABLE_PAYLOAD_ARTIFACT:
            raise CaseKeptImmutable(
                f"The payload of a kept {current.artifact!r} is immutable once kept; only a "
                f"{_MUTABLE_PAYLOAD_ARTIFACT!r} is edited in place (ADR-0138 §9).",
                field="payload",
            )
        await self._mutable_case(current.case_id)
        row = await self._update(
            CaseKept,
            _KEPT_COLUMNS,
            kept_id,
            {"payload": payload, "payload_sha256": kept_payload_sha256(payload)},
        )
        await self._touch(current.case_id, now)
        return CaseKeptDTO(**row._mapping)

    async def remove(self, kept_id: UUID, *, now: datetime) -> CaseKeptDTO:
        """Remove a kept row from the file by setting ``removed_at``.

        The row and its payload stay — frozen snapshots reference them. A
        second removal keeps the first ``removed_at``.

        Raises:
            CaseWorkingFileInvalid: If ``kept_id`` is unknown.
            CaseClosedError: If the case is closed.
        """
        current = await self._fetch_or_raise(_KEPT_COLUMNS, kept_id, "kept_id")
        await self._mutable_case(current.case_id)
        row = await self._update(
            CaseKept, _KEPT_COLUMNS, kept_id, {"removed_at": current.removed_at or now}
        )
        await self._touch(current.case_id, now)
        return CaseKeptDTO(**row._mapping)

    async def get(self, kept_id: UUID) -> CaseKeptDTO | None:
        """Return one kept row **including a removed one**, or ``None``.

        The D-CS-M-5 read: a snapshot frozen before a removal still renders
        the artefact it references.
        """
        row = await self._fetch(_KEPT_COLUMNS, kept_id)
        return CaseKeptDTO(**row._mapping) if row is not None else None

    async def list_for_case(
        self, case_id: UUID, *, include_removed: bool = False
    ) -> list[CaseKeptDTO]:
        """Return a case's kept rows — file level first, then by thread and position."""
        clauses: list[Any] = [CaseKept.case_id == case_id]
        if not include_removed:
            clauses.append(CaseKept.removed_at.is_(None))
        rows = await self._list(
            _KEPT_COLUMNS,
            *clauses,
            order_by=(CaseKept.thread_id.asc().nulls_first(), CaseKept.position.asc()),
        )
        return [CaseKeptDTO(**row._mapping) for row in rows]


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------


def _check_figure(source: str, value_text: str | None, source_ref: Any) -> None:
    """Validate a figure's source vocabulary and the ``book`` rule."""
    _require_in(source, _FIGURE_SOURCES, "source")
    if not isinstance(source_ref, dict):
        raise _invalid("A figure's source_ref must be a JSON object.", "source_ref")
    if source == "book":
        if value_text is not None:
            raise _invalid(
                "A book figure carries no value_text; it is read live from source_ref.",
                "value_text",
            )
        missing = [key for key in _BOOK_SOURCE_REF_KEYS if key not in source_ref]
        if missing:
            raise _invalid(f"A book figure's source_ref lacks {missing}.", "source_ref")


class CaseFigureRepository(_WorkingFileRepository):
    """Figures — a value and its provenance, never a formula (ADR-0138 §3)."""

    async def add(
        self,
        case_id: UUID,
        *,
        label: str,
        source: str,
        now: datetime,
        value_text: str | None = None,
        source_ref: dict | None = None,
        as_of: str | None = None,
        note: str | None = None,
    ) -> CaseFigureDTO:
        """Add a figure at the next position.

        A ``book`` figure carries no ``value_text`` and its ``source_ref``
        names ``investment_id`` and ``metric``.

        Raises:
            CaseWorkingFileInvalid: On an empty label, a source outside the
                vocabulary, or a broken ``book`` rule.
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        clean_label = _require_text(label, "label")
        ref = {} if source_ref is None else source_ref
        _check_figure(source, value_text, ref)
        tenant_id, _ = await self._mutable_case(case_id)
        position = await self._next_position(CaseFigure, case_id)
        row = await self._insert(
            CaseFigure,
            _FIGURE_COLUMNS,
            {
                "tenant_id": tenant_id,
                "case_id": case_id,
                "position": position,
                "label": clean_label,
                "source": source,
                "value_text": value_text,
                "source_ref": ref,
                "as_of": as_of,
                "note": note,
                "created_at": now,
            },
        )
        await self._touch(case_id, now)
        return CaseFigureDTO(**row._mapping)

    async def update(
        self,
        figure_id: UUID,
        *,
        now: datetime,
        label: str = _UNSET,
        source: str = _UNSET,
        value_text: str | None = _UNSET,
        source_ref: dict = _UNSET,
        as_of: str | None = _UNSET,
        note: str | None = _UNSET,
    ) -> CaseFigureDTO:
        """Update the given fields; the ``book`` rule is checked on the result.

        Raises:
            CaseWorkingFileInvalid: On an empty label, a source outside the
                vocabulary, a broken ``book`` rule on the resulting row, or
                an unknown ``figure_id``.
            CaseClosedError: If the case is closed.
        """
        values: dict[str, Any] = {}
        if label is not _UNSET:
            values["label"] = _require_text(label, "label")
        for name, value in (
            ("source", source),
            ("value_text", value_text),
            ("source_ref", source_ref),
            ("as_of", as_of),
            ("note", note),
        ):
            if value is not _UNSET:
                values[name] = value
        current = await self._fetch_or_raise(_FIGURE_COLUMNS, figure_id, "figure_id")
        _check_figure(
            values.get("source", current.source),
            values.get("value_text", current.value_text),
            values.get("source_ref", current.source_ref),
        )
        await self._mutable_case(current.case_id)
        row = (
            await self._update(CaseFigure, _FIGURE_COLUMNS, figure_id, values)
            if values
            else current
        )
        await self._touch(current.case_id, now)
        return CaseFigureDTO(**row._mapping)

    async def delete(self, figure_id: UUID, *, now: datetime) -> None:
        """Hard-delete a figure (snapshots embed figures by value).

        Raises:
            CaseWorkingFileInvalid: If ``figure_id`` is unknown.
            CaseClosedError: If the case is closed.
        """
        current = await self._fetch_or_raise(_FIGURE_COLUMNS, figure_id, "figure_id")
        await self._mutable_case(current.case_id)
        await self._session.execute(delete(CaseFigure).where(CaseFigure.id == figure_id))
        await self._touch(current.case_id, now)

    async def get(self, figure_id: UUID) -> CaseFigureDTO | None:
        """Return one figure, or ``None``."""
        row = await self._fetch(_FIGURE_COLUMNS, figure_id)
        return CaseFigureDTO(**row._mapping) if row is not None else None

    async def list_for_case(self, case_id: UUID) -> list[CaseFigureDTO]:
        """Return a case's figures by ``position``."""
        rows = await self._list(
            _FIGURE_COLUMNS,
            CaseFigure.case_id == case_id,
            order_by=(CaseFigure.position.asc(),),
        )
        return [CaseFigureDTO(**row._mapping) for row in rows]


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


class CaseConversationRepository(_WorkingFileRepository):
    """Conversations — calls and meetings (ADR-0138 §3)."""

    async def add(
        self,
        case_id: UUID,
        *,
        held_on: date,
        now: datetime,
        channel: str = "call",
        with_whom: str = "",
        what_was_said: str = "",
        what_was_promised: str | None = None,
        duration_minutes: int | None = None,
    ) -> CaseConversationDTO:
        """Add a conversation at the next position.

        Raises:
            CaseWorkingFileInvalid: If ``channel`` is not ``call`` ·
                ``meeting`` · ``other``.
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        _require_in(channel, _CONVERSATION_CHANNELS, "channel")
        tenant_id, _ = await self._mutable_case(case_id)
        position = await self._next_position(CaseConversation, case_id)
        row = await self._insert(
            CaseConversation,
            _CONVERSATION_COLUMNS,
            {
                "tenant_id": tenant_id,
                "case_id": case_id,
                "position": position,
                "held_on": held_on,
                "channel": channel,
                "with_whom": with_whom,
                "what_was_said": what_was_said,
                "what_was_promised": what_was_promised,
                "duration_minutes": duration_minutes,
                "created_at": now,
            },
        )
        await self._touch(case_id, now)
        return CaseConversationDTO(**row._mapping)

    async def update(
        self,
        conversation_id: UUID,
        *,
        now: datetime,
        held_on: date = _UNSET,
        channel: str = _UNSET,
        with_whom: str = _UNSET,
        what_was_said: str = _UNSET,
        what_was_promised: str | None = _UNSET,
        duration_minutes: int | None = _UNSET,
    ) -> CaseConversationDTO:
        """Update the given fields of a conversation.

        Raises:
            CaseWorkingFileInvalid: On a channel outside the vocabulary or an
                unknown ``conversation_id``.
            CaseClosedError: If the case is closed.
        """
        if channel is not _UNSET:
            _require_in(channel, _CONVERSATION_CHANNELS, "channel")
        values = {
            name: value
            for name, value in (
                ("held_on", held_on),
                ("channel", channel),
                ("with_whom", with_whom),
                ("what_was_said", what_was_said),
                ("what_was_promised", what_was_promised),
                ("duration_minutes", duration_minutes),
            )
            if value is not _UNSET
        }
        current = await self._fetch_or_raise(
            _CONVERSATION_COLUMNS, conversation_id, "conversation_id"
        )
        await self._mutable_case(current.case_id)
        row = (
            await self._update(CaseConversation, _CONVERSATION_COLUMNS, conversation_id, values)
            if values
            else current
        )
        await self._touch(current.case_id, now)
        return CaseConversationDTO(**row._mapping)

    async def delete(self, conversation_id: UUID, *, now: datetime) -> None:
        """Hard-delete a conversation.

        Raises:
            CaseWorkingFileInvalid: If ``conversation_id`` is unknown.
            CaseClosedError: If the case is closed.
        """
        current = await self._fetch_or_raise(
            _CONVERSATION_COLUMNS, conversation_id, "conversation_id"
        )
        await self._mutable_case(current.case_id)
        await self._session.execute(
            delete(CaseConversation).where(CaseConversation.id == conversation_id)
        )
        await self._touch(current.case_id, now)

    async def get(self, conversation_id: UUID) -> CaseConversationDTO | None:
        """Return one conversation, or ``None``."""
        row = await self._fetch(_CONVERSATION_COLUMNS, conversation_id)
        return CaseConversationDTO(**row._mapping) if row is not None else None

    async def list_for_case(self, case_id: UUID) -> list[CaseConversationDTO]:
        """Return a case's conversations, newest ``held_on`` first."""
        rows = await self._list(
            _CONVERSATION_COLUMNS,
            CaseConversation.case_id == case_id,
            order_by=(CaseConversation.held_on.desc(), CaseConversation.position.desc()),
        )
        return [CaseConversationDTO(**row._mapping) for row in rows]


# ---------------------------------------------------------------------------
# Open points
# ---------------------------------------------------------------------------


class CasePointRepository(_WorkingFileRepository):
    """Open points — a checklist with owner and due date (ADR-0138 §3, §5)."""

    async def add(
        self,
        case_id: UUID,
        *,
        text: str,
        now: datetime,
        owner_user_id: UUID | None = None,
        due_on: date | None = None,
    ) -> CasePointDTO:
        """Add an open point at the next position.

        Raises:
            CaseWorkingFileInvalid: If ``text`` is empty.
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        clean_text = _require_text(text, "text")
        tenant_id, _ = await self._mutable_case(case_id)
        position = await self._next_position(CasePoint, case_id)
        row = await self._insert(
            CasePoint,
            _POINT_COLUMNS,
            {
                "tenant_id": tenant_id,
                "case_id": case_id,
                "position": position,
                "text": clean_text,
                "owner_user_id": owner_user_id,
                "due_on": due_on,
                "created_at": now,
            },
        )
        await self._touch(case_id, now)
        return CasePointDTO(**row._mapping)

    async def _write(self, point_id: UUID, values: dict[str, Any], now: datetime) -> CasePointDTO:
        current = await self._fetch_or_raise(_POINT_COLUMNS, point_id, "point_id")
        await self._mutable_case(current.case_id)
        row = await self._update(CasePoint, _POINT_COLUMNS, point_id, values) if values else current
        await self._touch(current.case_id, now)
        return CasePointDTO(**row._mapping)

    async def update(
        self,
        point_id: UUID,
        *,
        now: datetime,
        text: str = _UNSET,
        owner_user_id: UUID | None = _UNSET,
        due_on: date | None = _UNSET,
    ) -> CasePointDTO:
        """Update the given fields of an open point.

        Raises:
            CaseWorkingFileInvalid: On an empty text or an unknown
                ``point_id``.
            CaseClosedError: If the case is closed.
        """
        values: dict[str, Any] = {}
        if text is not _UNSET:
            values["text"] = _require_text(text, "text")
        if owner_user_id is not _UNSET:
            values["owner_user_id"] = owner_user_id
        if due_on is not _UNSET:
            values["due_on"] = due_on
        return await self._write(point_id, values, now)

    async def mark_done(self, point_id: UUID, *, now: datetime) -> CasePointDTO:
        """Tick a point off (``done_at = now``)."""
        return await self._write(point_id, {"done_at": now}, now)

    async def mark_open(self, point_id: UUID, *, now: datetime) -> CasePointDTO:
        """Re-open a ticked point (``done_at = NULL``)."""
        return await self._write(point_id, {"done_at": None}, now)

    async def link_ticket(self, point_id: UUID, *, ticket_id: UUID, now: datetime) -> CasePointDTO:
        """Link the ticket created from this point — once.

        Raises:
            CasePointLinked: If a ticket is already linked.
            CaseWorkingFileInvalid: If ``point_id`` is unknown.
            CaseClosedError: If the case is closed.
        """
        current = await self._fetch_or_raise(_POINT_COLUMNS, point_id, "point_id")
        if current.ticket_id is not None:
            raise CasePointLinked(
                f"Point {point_id} is already linked to ticket {current.ticket_id}.",
                field="ticket_id",
            )
        return await self._write(point_id, {"ticket_id": ticket_id}, now)

    async def delete(self, point_id: UUID, *, now: datetime) -> None:
        """Hard-delete a point while no ticket is linked.

        Raises:
            CasePointLinked: If a ticket is linked.
            CaseWorkingFileInvalid: If ``point_id`` is unknown.
            CaseClosedError: If the case is closed.
        """
        current = await self._fetch_or_raise(_POINT_COLUMNS, point_id, "point_id")
        if current.ticket_id is not None:
            raise CasePointLinked(
                f"Point {point_id} is linked to ticket {current.ticket_id}; it cannot be "
                "deleted (ADR-0138 §5).",
                field="ticket_id",
            )
        await self._mutable_case(current.case_id)
        await self._session.execute(delete(CasePoint).where(CasePoint.id == point_id))
        await self._touch(current.case_id, now)

    async def get(self, point_id: UUID) -> CasePointDTO | None:
        """Return one point, or ``None``."""
        row = await self._fetch(_POINT_COLUMNS, point_id)
        return CasePointDTO(**row._mapping) if row is not None else None

    async def list_for_case(self, case_id: UUID, *, open_only: bool = False) -> list[CasePointDTO]:
        """Return a case's points — undone first, then by due date, then position."""
        clauses: list[Any] = [CasePoint.case_id == case_id]
        if open_only:
            clauses.append(CasePoint.done_at.is_(None))
        rows = await self._list(
            _POINT_COLUMNS,
            *clauses,
            order_by=(
                CasePoint.done_at.asc().nulls_first(),
                CasePoint.due_on.asc().nulls_last(),
                CasePoint.position.asc(),
            ),
        )
        return [CasePointDTO(**row._mapping) for row in rows]

    async def next_due(self, case_id: UUID) -> CasePointDTO | None:
        """Return the undone point with the earliest ``due_on``, or ``None``.

        Undated and done points are ignored (the list's "next due point").
        """
        result = await self._session.execute(
            select(*_POINT_COLUMNS)
            .where(
                CasePoint.case_id == case_id,
                CasePoint.done_at.is_(None),
                CasePoint.due_on.is_not(None),
            )
            .order_by(CasePoint.due_on.asc(), CasePoint.position.asc())
            .limit(1)
        )
        row = result.one_or_none()
        return CasePointDTO(**row._mapping) if row is not None else None


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def _clean_options(options: Any) -> list[dict[str, str]]:
    """Validate a decision's options and return them as ``{key, text}`` dicts."""
    if not isinstance(options, list):
        raise _invalid("Decision options must be a list of {key, text}.", "options")
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            raise _invalid("Each decision option must be an object {key, text}.", "options")
        key = option.get("key")
        option_text = option.get("text")
        if not isinstance(key, str) or not key.strip():
            raise _invalid("Each decision option needs a non-empty string key.", "options")
        if not isinstance(option_text, str):
            raise _invalid(f"Decision option {key!r} needs a string text.", "options")
        if key in seen:
            raise _invalid(f"Duplicate decision option key {key!r}.", "options")
        seen.add(key)
        cleaned.append({"key": key, "text": option_text})
    return cleaned


class CaseDecisionRepository(_WorkingFileRepository):
    """Decisions — a draft until recorded; recorded rows immutable (ADR-0138 §14)."""

    async def get_draft(self, case_id: UUID) -> CaseDecisionDTO | None:
        """Return the case's draft decision, or ``None``."""
        result = await self._session.execute(
            select(*_DECISION_COLUMNS).where(
                CaseDecision.case_id == case_id, CaseDecision.recorded_at.is_(None)
            )
        )
        row = result.one_or_none()
        return CaseDecisionDTO(**row._mapping) if row is not None else None

    async def upsert_draft(
        self,
        case_id: UUID,
        *,
        now: datetime,
        options: list = _UNSET,
        chosen_key: str | None = _UNSET,
        rationale: str | None = _UNSET,
        decided_by: str | None = _UNSET,
        decided_at: date | None = _UNSET,
    ) -> CaseDecisionDTO:
        """Create the case's draft decision, or update the given fields of it.

        At most one draft exists per case (``uq_case_decisions_one_draft``).
        ``options`` is a list of ``{"key": str, "text": str}`` with unique
        non-empty keys; ``chosen_key`` must be one of the resulting keys or
        ``None``.

        Raises:
            CaseWorkingFileInvalid: On a malformed options list or a
                ``chosen_key`` that names no option.
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        values: dict[str, Any] = {}
        if options is not _UNSET:
            values["options"] = _clean_options(options)
        for name, value in (
            ("chosen_key", chosen_key),
            ("rationale", rationale),
            ("decided_by", decided_by),
            ("decided_at", decided_at),
        ):
            if value is not _UNSET:
                values[name] = value
        tenant_id, _ = await self._mutable_case(case_id)
        draft = await self.get_draft(case_id)
        result_options = values.get("options", draft.options if draft is not None else [])
        result_chosen = values.get("chosen_key", draft.chosen_key if draft is not None else None)
        if result_chosen is not None and result_chosen not in {
            option["key"] for option in result_options
        }:
            raise _invalid(f"chosen_key {result_chosen!r} names no option.", "chosen_key")
        if draft is None:
            row = await self._insert(
                CaseDecision,
                _DECISION_COLUMNS,
                {
                    "tenant_id": tenant_id,
                    "case_id": case_id,
                    "options": [],
                    "created_at": now,
                    **values,
                },
            )
        elif values:
            row = await self._update(CaseDecision, _DECISION_COLUMNS, draft.id, values)
        else:
            await self._touch(case_id, now)
            return draft
        await self._touch(case_id, now)
        return CaseDecisionDTO(**row._mapping)

    async def mark_recorded(
        self,
        decision_id: UUID,
        *,
        recorded_by: UUID,
        record_entry_id: UUID,
        now: datetime,
    ) -> CaseDecisionDTO:
        """Turn the draft into a recorded decision — the seam of P-CS-1b's milestone.

        Raises:
            CaseDecisionRecorded: If the row is already recorded.
            CaseWorkingFileInvalid: If ``decision_id`` is unknown.
            CaseClosedError: If the case is closed.
        """
        current = await self._fetch_or_raise(_DECISION_COLUMNS, decision_id, "decision_id")
        if current.recorded_at is not None:
            raise CaseDecisionRecorded(
                f"Decision {decision_id} is already recorded; a recorded decision is "
                "immutable (ADR-0138 §14).",
                field="recorded_at",
            )
        await self._mutable_case(current.case_id)
        row = await self._update(
            CaseDecision,
            _DECISION_COLUMNS,
            decision_id,
            {
                "recorded_by": recorded_by,
                "recorded_at": now,
                "record_entry_id": record_entry_id,
            },
        )
        await self._touch(current.case_id, now)
        return CaseDecisionDTO(**row._mapping)

    async def list_recorded(self, case_id: UUID) -> list[CaseDecisionDTO]:
        """Return a case's recorded decisions, oldest ``recorded_at`` first."""
        rows = await self._list(
            _DECISION_COLUMNS,
            CaseDecision.case_id == case_id,
            CaseDecision.recorded_at.is_not(None),
            order_by=(CaseDecision.recorded_at.asc(),),
        )
        return [CaseDecisionDTO(**row._mapping) for row in rows]

    async def get(self, decision_id: UUID) -> CaseDecisionDTO | None:
        """Return one decision (draft or recorded), or ``None``."""
        row = await self._fetch(_DECISION_COLUMNS, decision_id)
        return CaseDecisionDTO(**row._mapping) if row is not None else None


# ---------------------------------------------------------------------------
# Investment links
# ---------------------------------------------------------------------------


class CaseInvestmentRepository(_WorkingFileRepository):
    """Investment links — unique per pair (ADR-0138 §5)."""

    async def link(
        self, case_id: UUID, *, investment_id: UUID, linked_by: UUID, now: datetime
    ) -> CaseInvestmentDTO:
        """Link an investment to the case — idempotent.

        An existing pair is returned unchanged and the case is not touched.

        Raises:
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        tenant_id, _ = await self._mutable_case(case_id)
        existing = (
            await self._session.execute(
                select(*_INVESTMENT_COLUMNS).where(
                    CaseInvestment.case_id == case_id,
                    CaseInvestment.investment_id == investment_id,
                )
            )
        ).one_or_none()
        if existing is not None:
            return CaseInvestmentDTO(**existing._mapping)
        row = await self._insert(
            CaseInvestment,
            _INVESTMENT_COLUMNS,
            {
                "tenant_id": tenant_id,
                "case_id": case_id,
                "investment_id": investment_id,
                "linked_by": linked_by,
                "linked_at": now,
                "created_at": now,
            },
        )
        await self._touch(case_id, now)
        return CaseInvestmentDTO(**row._mapping)

    async def unlink(self, case_id: UUID, *, investment_id: UUID, now: datetime) -> None:
        """Remove the link; a pair that is not linked is a no-op (no touch).

        Raises:
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        await self._mutable_case(case_id)
        result = await self._session.execute(
            delete(CaseInvestment)
            .where(
                CaseInvestment.case_id == case_id,
                CaseInvestment.investment_id == investment_id,
            )
            .returning(CaseInvestment.id)
        )
        if result.first() is not None:
            await self._touch(case_id, now)

    async def list_for_case(self, case_id: UUID) -> list[CaseInvestmentDTO]:
        """Return a case's investment links, oldest ``linked_at`` first."""
        rows = await self._list(
            _INVESTMENT_COLUMNS,
            CaseInvestment.case_id == case_id,
            order_by=(CaseInvestment.linked_at.asc(),),
        )
        return [CaseInvestmentDTO(**row._mapping) for row in rows]

    async def list_case_ids_for_investment(self, investment_id: UUID) -> list[UUID]:
        """Return the ids of the open or waiting cases linking ``investment_id``.

        The system-line writers' read (ADR-0138 §7, P-CS-2b): a closed case
        receives nothing. Ordered by ``case_number`` for determinism.
        """
        result = await self._session.execute(
            select(Case.id)
            .join(CaseInvestment, CaseInvestment.case_id == Case.id)
            .where(
                CaseInvestment.investment_id == investment_id,
                Case.state.in_(sorted(_MUTABLE_STATES)),
            )
            .order_by(Case.case_number.asc())
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


class CaseProposalRepository(_WorkingFileRepository):
    """Proposal packages — the landing place of a Shirley job (ADR-0138 §8)."""

    async def request(
        self, case_id: UUID, *, requested_by: UUID, request_text: str, now: datetime
    ) -> CaseProposalDTO:
        """Record a request for a proposal package.

        Raises:
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        tenant_id, _ = await self._mutable_case(case_id)
        row = await self._insert(
            CaseProposal,
            _PROPOSAL_COLUMNS,
            {
                "tenant_id": tenant_id,
                "case_id": case_id,
                "requested_by": requested_by,
                "requested_at": now,
                "request_text": request_text,
                "items": [],
                "created_at": now,
            },
        )
        await self._touch(case_id, now)
        return CaseProposalDTO(**row._mapping)

    async def land(self, proposal_id: UUID, *, items: list, now: datetime) -> CaseProposalDTO:
        """Store the landed package — once.

        ``items`` is a list of dicts, each at least ``{"kind": str,
        "status": "pending"}``; the rest of the item shape is P-CS-5b's
        contract and is stored opaquely.

        Raises:
            CaseWorkingFileInvalid: On a malformed item list, an already
                landed proposal, or an unknown ``proposal_id``.
            CaseClosedError: If the case is closed.
        """
        if not isinstance(items, list):
            raise _invalid("Proposal items must be a list.", "items")
        for item in items:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("kind"), str)
                or item.get("status") != "pending"
            ):
                raise _invalid(
                    'Each proposal item must be an object with a string "kind" and '
                    '"status": "pending".',
                    "items",
                )
        current = await self._fetch_or_raise(_PROPOSAL_COLUMNS, proposal_id, "proposal_id")
        if current.landed_at is not None:
            raise _invalid(f"Proposal {proposal_id} has already landed.", "landed_at")
        await self._mutable_case(current.case_id)
        row = await self._update(
            CaseProposal,
            _PROPOSAL_COLUMNS,
            proposal_id,
            {"items": items, "landed_at": now, "resolved_at": None if items else now},
        )
        await self._touch(current.case_id, now)
        return CaseProposalDTO(**row._mapping)

    async def resolve_item(
        self,
        proposal_id: UUID,
        *,
        index: int,
        status: str,
        now: datetime,
        created_id: UUID | None = None,
    ) -> CaseProposalDTO:
        """Accept or discard one pending item; resolve the package when none is pending.

        Raises:
            CaseWorkingFileInvalid: On a status other than ``accepted`` /
                ``discarded``, an index out of range, an item that is not
                pending, or an unknown ``proposal_id``.
            CaseClosedError: If the case is closed.
        """
        _require_in(status, _PROPOSAL_RESOLUTIONS, "status")
        current = await self._fetch_or_raise(_PROPOSAL_COLUMNS, proposal_id, "proposal_id")
        items = [dict(item) for item in current.items]
        if not 0 <= index < len(items):
            raise _invalid(f"Proposal {proposal_id} has no item {index}.", "index")
        if items[index].get("status") != "pending":
            raise _invalid(f"Item {index} of proposal {proposal_id} is not pending.", "index")
        await self._mutable_case(current.case_id)
        items[index]["status"] = status
        if created_id is not None:
            items[index]["created_id"] = str(created_id)
        values: dict[str, Any] = {"items": items}
        if not any(item.get("status") == "pending" for item in items):
            values["resolved_at"] = now
        row = await self._update(CaseProposal, _PROPOSAL_COLUMNS, proposal_id, values)
        await self._touch(current.case_id, now)
        return CaseProposalDTO(**row._mapping)

    async def get(self, proposal_id: UUID) -> CaseProposalDTO | None:
        """Return one proposal, or ``None``."""
        row = await self._fetch(_PROPOSAL_COLUMNS, proposal_id)
        return CaseProposalDTO(**row._mapping) if row is not None else None

    async def list_for_case(
        self, case_id: UUID, *, open_only: bool = False
    ) -> list[CaseProposalDTO]:
        """Return a case's proposals, newest ``requested_at`` first.

        ``open_only`` keeps the unresolved ones (``resolved_at`` null).
        """
        clauses: list[Any] = [CaseProposal.case_id == case_id]
        if open_only:
            clauses.append(CaseProposal.resolved_at.is_(None))
        rows = await self._list(
            _PROPOSAL_COLUMNS, *clauses, order_by=(CaseProposal.requested_at.desc(),)
        )
        return [CaseProposalDTO(**row._mapping) for row in rows]
