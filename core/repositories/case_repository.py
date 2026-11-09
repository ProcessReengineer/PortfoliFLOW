# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""CaseRepository — persistence for the Cases area (ADR-0107, ADR-0138).

Backs the ``cases`` and ``case_entries`` tables introduced in migration
b031 and extended in b037. Shape mirrors the other tenant-scoped repositories: a tenant-scoped
:class:`AsyncSession` is passed in, methods return frozen DTOs, and
``tenant_id`` is implicit in the session context (RLS derives it from
``app.tenant_id``).

A case has two layers (ADR-0138 §2), and three invariants are enforced by
the *absence* of code paths as much as by the guards below:

* **The record is append-only.** ``case_entries`` rows are never updated or
  deleted; a new situation is a new entry (ADR-0107 §2, confirmed by
  ADR-0138 §2). There is deliberately no ``update_entry`` /
  ``delete_entry``.
* **The working file is mutable while open or waiting.** The working-file
  columns of a case (:meth:`update_working_file`), its owner
  (:meth:`hand_over`) and its state transitions (:meth:`wait`,
  :meth:`lift_wait`, :meth:`close`) are the permitted mutations of a
  case row; each one bumps ``updated_at``. The eight working-file tables
  live in :mod:`core.repositories.case_working_file_repository`.
* **A closed case is read-only in both layers** (ADR-0107 §4, confirmed by
  ADR-0138 §4). There is deliberately no ``delete_case`` and no
  ``reopen``.

**The milestone path** (ADR-0138 §2, D-CS-B-7). Every milestone —
``opened`` (:meth:`create`), ``waiting_set`` (:meth:`wait`),
``waiting_lifted`` (:meth:`lift_wait`), ``handed_over``
(:meth:`hand_over`), ``decision_record`` (:meth:`record_decision`) and
``closed`` (:meth:`close`) — goes through one path: its column transition,
the milestone entry and a ``frozen`` entry carrying the whole-file
snapshot (:mod:`core.repositories.case_snapshot`), all in the caller's one
transaction. The column writes behind them are private, so nothing outside
this module moves a case between states without a record, and no generic
entry writer exists: the milestone path and :meth:`add_system_line` are the
only writers of the record. The ``frozen`` rows
are the record's hidden half: :meth:`list_entries` leaves them out unless
asked, and they are fetched by milestone (:meth:`get_frozen_for`,
:meth:`latest_frozen`) — the milestone and its snapshot render as one
record row (D-CS-B-6).

The repository never touches an Irene finding. A case *references* its
finding (``finding_id``) and reads it, but resolving that finding as
``opened_case`` is the C4 route's concern, composed in the route's own
transaction — keeping this seam clean of the finding vocabulary (ADR-0085).
Likewise no Journal is written: the Watch Desk Journal is a
projection that gains closed cases as a render-time source in C4 (Gate-C0
decision B), not a table this layer writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Text,
    and_,
    cast,
    exists,
    func,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy import case as sql_case
from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    CaseActorInvalid,
    CaseClosedError,
    CaseClosingNoteMissing,
    CaseStateInvalid,
    CaseWorkingFileInvalid,
)
from core.models.case import (
    Case,
    CaseConversation,
    CaseDecision,
    CaseEntry,
    CaseFigure,
    CaseKept,
    CaseThread,
)
from core.models.irene_finding import IreneFinding
from core.repositories._case_common import _MUTABLE_STATES, _UNSET
from core.repositories.base import BaseRepository
from core.repositories.case_snapshot import SNAPSHOT_VERSION, build_file_snapshot
from core.repositories.case_working_file_repository import CaseDecisionRepository

# Canonical lowercase vocabularies (ADR-0107 §2, ADR-0138 §4 and Object
# model). The columns are plain TEXT; the vocabularies are enforced here,
# not as SQL enums, matching the codebase's TEXT-for-status convention.
_VALID_STATES: frozenset[str] = frozenset({"open", "waiting", "closed"})
# ``note`` / ``pin`` / ``decision_record`` stay in the vocabulary: legacy rows
# are read and validated against it (D-CS-M-7), though none is written any
# more — D-CS-B-1's staged retirement closed with P-CS-4a, and the generic
# writer that refused them (``append_entry``) was retired with P-CS-H1.
_VALID_ENTRY_KINDS: frozenset[str] = frozenset(
    {
        "opened",
        "note",
        "pin",
        "decision_record",
        "closed",
        "handed_over",
        "waiting_set",
        "waiting_lifted",
        "frozen",
        "system_line",
    }
)
# Written only by the program — the milestone path or add_system_line
# (ADR-0138 §7, D-CS-B-25).
_PROGRAM_ONLY_KINDS: frozenset[str] = frozenset(
    {"closed", "handed_over", "waiting_set", "waiting_lifted", "frozen", "system_line"}
)
_VALID_ACTORS: frozenset[str] = frozenset({"pm", "shirley", "system"})
_VALID_FRAMINGS: frozenset[str] = frozenset({"question", "task"})
# The kinds a milestone entry may carry (the snapshot itself is ``frozen``).
_MILESTONE_KINDS: frozenset[str] = (_PROGRAM_ONLY_KINDS | {"opened", "decision_record"}) - {
    "frozen",
    "system_line",
}
# Why a wait is lifted (ADR-0138 §4, D-CS-M-4): by the PM, or by the
# program on the date or on an event.
_LIFT_REASONS: frozenset[str] = frozenset({"manual", "date", "event"})
# The events a system line records (ADR-0138 §7). ``waiting_lifted`` is a
# milestone, not a line.
_SYSTEM_LINE_EVENTS: frozenset[str] = frozenset(
    {
        "subject_within_ceiling",
        "nav_restated",
        "press_cluster",
        "ticket_booked",
        "job_requested",
        "job_landed",
    }
)

# The record's order (P-CS-2b finding e). Several rows of one case share an
# instant by construction: a milestone and its ``frozen`` snapshot, and the
# system line that woke a waiting case with the ``waiting_lifted`` pair it
# caused — all written with the writer's one ``now``. ``created_at`` alone
# leaves their order to the heap, and the row id is no tiebreak either: it is
# a random UUID, so ordering by it would shuffle same-instant rows between
# reads. Within an instant, therefore: a system line first (the event
# precedes the milestone it woke), then the milestone, then its ``frozen``
# snapshot; several lines of one write (the NAV seam) in subject order; and
# only then the id, which merely makes two otherwise-equal rows stable.
_RECORD_ORDER = (
    CaseEntry.created_at.asc(),
    sql_case(
        (CaseEntry.kind == "system_line", 0),
        (CaseEntry.kind == "frozen", 2),
        else_=1,
    ),
    CaseEntry.payload["subject"].astext.asc(),
    CaseEntry.id.asc(),
)

# The unique constraint that guarantees tenant-sequential numbering; a
# concurrent allocation collides on it and the write is retried once.
_CASE_NUMBER_CONSTRAINT = "uq_cases_tenant_case_number"

_CASE_COLUMNS = (
    Case.id,
    Case.tenant_id,
    Case.case_number,
    Case.title,
    Case.description,
    Case.state,
    Case.finding_id,
    Case.opened_by,
    Case.opened_at,
    Case.closed_by,
    Case.closed_at,
    Case.closing_note,
    Case.wait_until,
    Case.owner_user_id,
    Case.counterparty,
    Case.due_on,
    Case.framing,
    Case.updated_at,
)

_CASE_ENTRY_COLUMNS = (
    CaseEntry.id,
    CaseEntry.tenant_id,
    CaseEntry.case_id,
    CaseEntry.kind,
    CaseEntry.actor,
    CaseEntry.actor_user_id,
    CaseEntry.payload,
    CaseEntry.created_at,
)


@dataclass(frozen=True)
class CaseDTO:
    """Plain data-only view of a ``cases`` row."""

    id: UUID
    tenant_id: UUID
    case_number: int
    title: str
    description: str | None
    state: str
    finding_id: UUID | None
    opened_by: UUID
    opened_at: datetime
    closed_by: UUID | None
    closed_at: datetime | None
    closing_note: str | None
    wait_until: date | None
    owner_user_id: UUID
    counterparty: str | None
    due_on: date | None
    framing: str | None
    updated_at: datetime


@dataclass(frozen=True)
class CaseEntryDTO:
    """Plain data-only view of a ``case_entries`` row.

    ``payload`` is opaque JSONB — the per-kind timeline contract lives above
    this layer (the finding-payload idiom).
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    kind: str
    actor: str
    actor_user_id: UUID | None
    payload: dict
    created_at: datetime


def _is_case_number_conflict(exc: IntegrityError) -> bool:
    """Return True when ``exc`` is the tenant case-number unique violation.

    Reads asyncpg's ``constraint_name`` off the wrapped error, falling back
    to a substring match so a hand-built test error is recognised too.
    """
    constraint = getattr(getattr(exc, "orig", None), "constraint_name", None)
    if constraint == _CASE_NUMBER_CONSTRAINT:
        return True
    return _CASE_NUMBER_CONSTRAINT in str(exc)


def _escape_like(term: str) -> str:
    """Escape LIKE/ILIKE wildcards so user input matches literally.

    Backslash first (it is the escape character), then ``%`` and ``_``.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validate_framing(framing: str | None) -> None:
    """Raise :class:`CaseWorkingFileInvalid` unless ``framing`` is valid or None."""
    if framing is not None and framing not in _VALID_FRAMINGS:
        raise CaseWorkingFileInvalid(
            f"Invalid framing {framing!r}; expected one of {sorted(_VALID_FRAMINGS)} or None.",
            field="framing",
        )


class CaseRepository(BaseRepository):
    """Open, read, append to, and close cases in the active tenant context."""

    async def _active_tenant(self) -> UUID:
        """Return the tenant bound to the active session (``app.tenant_id``)."""
        row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        return row.scalar_one()

    async def _insert_entry(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID,
        kind: str,
        actor: str,
        actor_user_id: UUID | None,
        payload: dict,
        now: datetime,
    ) -> CaseEntryDTO:
        """Insert one timeline entry and return it. Transaction-agnostic.

        Runs inside whatever transaction (or savepoint) the caller has open;
        it never commits or rolls back itself. Does no vocabulary validation
        — callers validate before reaching here.
        """
        stmt = (
            insert(CaseEntry)
            .values(
                tenant_id=tenant_id,
                case_id=case_id,
                kind=kind,
                actor=actor,
                actor_user_id=actor_user_id,
                payload=payload,
                created_at=now,
            )
            .returning(*_CASE_ENTRY_COLUMNS)
        )
        row = (await self._session.execute(stmt)).one()
        return CaseEntryDTO(**row._mapping)

    # ------------------------------------------------------------------
    # The milestone path (ADR-0138 §2, Object model; D-CS-B-7)
    # ------------------------------------------------------------------

    async def _insert_milestone(
        self,
        *,
        case: CaseDTO,
        kind: str,
        actor: str,
        actor_user_id: UUID | None,
        payload: dict,
        now: datetime,
    ) -> CaseEntryDTO:
        """Insert the milestone entry of a milestone pair. Transaction-agnostic."""
        assert kind in _MILESTONE_KINDS, f"{kind!r} is not a milestone kind"
        return await self._insert_entry(
            tenant_id=case.tenant_id,
            case_id=case.id,
            kind=kind,
            actor=actor,
            actor_user_id=actor_user_id,
            payload=payload,
            now=now,
        )

    async def _insert_frozen(
        self,
        *,
        case: CaseDTO,
        milestone: CaseEntryDTO,
        now: datetime,
        stand: dict | None,
        book_values: dict | None,
    ) -> CaseEntryDTO:
        """Build the whole-file snapshot and insert the ``frozen`` entry for ``milestone``.

        Built at call time, so it shows every write the transaction has made
        before it — the column transition and the milestone entry included.
        Written by ``system`` with no acting user, at the milestone's
        ``now``. Transaction-agnostic.
        """
        snapshot = await build_file_snapshot(
            self._session, case.id, stand=stand, book_values=book_values
        )
        return await self._insert_entry(
            tenant_id=case.tenant_id,
            case_id=case.id,
            kind="frozen",
            actor="system",
            actor_user_id=None,
            payload={
                "milestone_entry_id": str(milestone.id),
                "milestone_kind": milestone.kind,
                "snapshot_version": SNAPSHOT_VERSION,
                "frozen_at": now.isoformat(),
                "file": snapshot,
            },
            now=now,
        )

    async def _milestone(
        self,
        *,
        case: CaseDTO,
        kind: str,
        actor: str,
        actor_user_id: UUID | None,
        payload: dict,
        now: datetime,
        stand: dict | None,
        book_values: dict | None,
    ) -> tuple[CaseEntryDTO, CaseEntryDTO]:
        """Write a milestone pair: the milestone entry, then its ``frozen`` snapshot.

        Transaction-agnostic like :meth:`_insert_entry`: the caller's
        transaction carries both rows and any column write made before them,
        so the snapshot shows the state the milestone produced.

        Returns:
            ``(milestone, frozen)``.
        """
        milestone = await self._insert_milestone(
            case=case,
            kind=kind,
            actor=actor,
            actor_user_id=actor_user_id,
            payload=payload,
            now=now,
        )
        frozen = await self._insert_frozen(
            case=case, milestone=milestone, now=now, stand=stand, book_values=book_values
        )
        return milestone, frozen

    async def _attempt_create(
        self,
        *,
        active_tenant: UUID,
        title: str,
        opened_by: UUID,
        description: str | None,
        finding_id: UUID | None,
        opened_payload: dict | None,
        opened_actor: str,
        counterparty: str | None,
        due_on: date | None,
        framing: str | None,
        now: datetime,
        stand: dict | None = None,
    ) -> CaseDTO:
        """One case-creation attempt: the case row plus its ``opened`` pair.

        Wrapped in a SAVEPOINT so a case-number collision rolls back just
        this attempt, leaving the caller's transaction intact for the retry.
        ``case_number`` is allocated in-SQL as
        ``COALESCE(MAX(case_number), 0) + 1`` over the tenant's rows — the
        scalar subquery runs under RLS, so the MAX is tenant-scoped.
        """
        async with self._session.begin_nested():
            next_number = select(func.coalesce(func.max(Case.case_number), 0) + 1).scalar_subquery()
            case_stmt = (
                insert(Case)
                .values(
                    tenant_id=active_tenant,
                    case_number=next_number,
                    title=title,
                    description=description,
                    state="open",
                    finding_id=finding_id,
                    opened_by=opened_by,
                    opened_at=now,
                    owner_user_id=opened_by,
                    counterparty=counterparty,
                    due_on=due_on,
                    framing=framing,
                    updated_at=now,
                )
                .returning(*_CASE_COLUMNS)
            )
            row = (await self._session.execute(case_stmt)).one()
            case_dto = CaseDTO(**row._mapping)

            # The mandatory first milestone and its snapshot. The opened
            # entry is attributed to a user only when a human PM opened the
            # case; system / shirley openings carry no actor_user_id.
            opened_actor_user = opened_by if opened_actor == "pm" else None
            await self._milestone(
                case=case_dto,
                kind="opened",
                actor=opened_actor,
                actor_user_id=opened_actor_user,
                payload=opened_payload or {},
                now=now,
                stand=stand,
                book_values=None,
            )
        return case_dto

    async def create(
        self,
        *,
        title: str,
        opened_by: UUID,
        description: str | None = None,
        finding_id: UUID | None = None,
        opened_payload: dict | None = None,
        opened_actor: str = "system",
        counterparty: str | None = None,
        due_on: date | None = None,
        framing: str | None = None,
        now: datetime,
        stand: dict | None = None,
    ) -> CaseDTO:
        """Open a case — the one creation path for both entry points.

        Manual creation passes a ``description`` and no ``finding_id``;
        open-from-finding passes a ``finding_id`` (and may leave
        ``description`` null). Either way exactly one ``opened`` entry and
        its ``frozen`` snapshot are written in the same transaction (the
        milestone pair, ADR-0138 §2), the entry carrying ``opened_payload``
        (opaque JSONB — the route layer later freezes materiality-at-opening
        into it; this layer treats it as opaque). ADR-0107 §3: two entry
        points, one object.

        ``case_number`` is allocated tenant-sequentially. The
        ``uq_cases_tenant_case_number`` constraint is the guarantee; the
        recovery is a single retry: on a unique-violation against it the
        whole attempt (which re-reads ``MAX(case_number)``) is retried once,
        and if the retry also collides the error propagates.

        The opener becomes the case's owner (``owner_user_id``, ADR-0138
        §13) and ``updated_at`` starts at ``now``.

        This method does **not** touch the finding referenced by
        ``finding_id`` — resolving it as ``opened_case`` is the C4 route's
        concern, composed in the route transaction (ADR-0085).

        Args:
            title: The case title (required).
            opened_by: The user opening the case.
            description: Manual-creation description, or ``None`` for
                open-from-finding.
            finding_id: The originating Irene finding, or ``None`` for a
                manually opened case.
            opened_payload: Opaque JSONB for the ``opened`` entry; defaults
                to ``{}``.
            opened_actor: One of ``pm`` / ``shirley`` / ``system`` — who
                opened the case. Defaults to ``system``.
            counterparty: Free-text counterparty (ADR-0138 §5), or ``None``.
            due_on: The case's own due date, or ``None``.
            framing: ``question`` / ``task`` (ADR-0138 §3), or ``None``.
            now: The timestamp for ``opened_at``, ``updated_at`` and the
                ``opened`` pair.
            stand: Where we stand as rendered, for the snapshot, or
                ``None`` (D-CS-B-8).

        Returns:
            The newly created :class:`CaseDTO`.

        Raises:
            CaseActorInvalid: If ``opened_actor`` is outside the vocabulary.
            CaseWorkingFileInvalid: If ``framing`` is outside the vocabulary.
        """
        _validate_framing(framing)
        if opened_actor not in _VALID_ACTORS:
            raise CaseActorInvalid(
                f"Invalid actor {opened_actor!r}; expected one of {sorted(_VALID_ACTORS)}.",
                field="actor",
            )
        active_tenant = await self._active_tenant()
        for attempt in range(2):
            try:
                return await self._attempt_create(
                    active_tenant=active_tenant,
                    title=title,
                    opened_by=opened_by,
                    description=description,
                    finding_id=finding_id,
                    opened_payload=opened_payload,
                    opened_actor=opened_actor,
                    counterparty=counterparty,
                    due_on=due_on,
                    framing=framing,
                    now=now,
                    stand=stand,
                )
            except IntegrityError as exc:
                # Retry once on a case-number collision; propagate anything
                # else, and propagate a second collision too.
                if attempt == 0 and _is_case_number_conflict(exc):
                    continue
                raise
        raise AssertionError("unreachable: the loop returns or raises")

    async def get(self, case_id: UUID) -> CaseDTO | None:
        """Return one case by id in the active tenant, or ``None``."""
        result = await self._session.execute(select(Case).where(Case.id == case_id))
        model = result.scalar_one_or_none()
        return _case_to_dto(model) if model is not None else None

    async def get_by_number(self, case_number: int) -> CaseDTO | None:
        """Return one case by its number in the active tenant, or ``None``.

        The number is tenant-sequential and unique per tenant
        (``uq_cases_tenant_case_number``); RLS scopes the read, so another
        tenant's case of the same number reads ``None``. The read behind
        Shirley's ``get_case`` (ADR-0138 §10), which names a case as
        ``CASE-nnnn`` rather than by id.
        """
        result = await self._session.execute(select(Case).where(Case.case_number == case_number))
        model = result.scalar_one_or_none()
        return _case_to_dto(model) if model is not None else None

    async def get_by_finding(self, finding_id: UUID) -> CaseDTO | None:
        """Return the case opened from ``finding_id``, or ``None``.

        The reverse lookup the C4 Journal rendering needs (finding → its
        case). At most one case is expected per finding; if several ever
        exist the newest (by ``opened_at``) is returned.
        """
        result = await self._session.execute(
            select(Case)
            .where(Case.finding_id == finding_id)
            .order_by(Case.opened_at.desc())
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return _case_to_dto(model) if model is not None else None

    async def list_closed(self, *, limit: int = 100) -> list[CaseDTO]:
        """Return closed cases for the Watch Desk Journal source.

        The Journal is a render-time projection that gains closed cases as a
        second source (ADR-0107, C4 · Gate-C0 decision B); this is that source.
        Newest ``closed_at`` first, ``case_number`` as the stable tie-break,
        capped so a page of closed cases can interleave with resolved
        findings. Not a UI list; nothing renders it directly.

        Args:
            limit: Maximum number of closed cases to return (most recent).

        Returns:
            Closed cases in the active tenant context, newest first.
        """
        result = await self._session.execute(
            select(Case)
            .where(Case.state == "closed")
            .order_by(Case.closed_at.desc(), Case.case_number.desc())
            .limit(limit)
        )
        return [_case_to_dto(model) for model in result.scalars().all()]

    async def list_closed_before(
        self,
        *,
        before: datetime,
        limit: int,
        start: datetime | None = None,
        family: str | None = None,
    ) -> list[CaseDTO]:
        """Return closed cases closed before ``before``, newest first.

        The Journal's case source paged by instant (ADR-0133 §5), the
        sibling of :meth:`list_closed` that the day-by-day Journal reads
        instead of a fixed cap. ``family`` keeps the cases opened from a
        finding of that watchpoint family (the ``subject_key`` prefix of
        the origin finding); a case opened by hand carries no family and is
        absent from a family-filtered page.

        Args:
            before: Exclusive upper bound on ``closed_at``.
            limit: Maximum number of rows (the Journal's page size).
            start: Inclusive lower bound on ``closed_at`` — the period
                filter — or ``None`` for no bound.
            family: A watchpoint family, or ``None`` for every case.

        Returns:
            Closed cases in the active tenant context, newest ``closed_at``
            first, ``case_number`` descending as the stable tie-break.
        """
        query = (
            select(Case)
            .where(Case.state == "closed", Case.closed_at < before)
            .order_by(Case.closed_at.desc(), Case.case_number.desc())
            .limit(limit)
        )
        if start is not None:
            query = query.where(Case.closed_at >= start)
        if family is not None:
            query = query.join(IreneFinding, IreneFinding.id == Case.finding_id).where(
                func.split_part(IreneFinding.subject_key, ":", 1) == family
            )
        result = await self._session.execute(query)
        return [_case_to_dto(model) for model in result.scalars().all()]

    async def list_entries(
        self, case_id: UUID, *, include_frozen: bool = False
    ) -> list[CaseEntryDTO]:
        """Return a case's record, oldest ``created_at`` first.

        The ``frozen`` snapshots are left out by default (D-CS-B-6): a
        milestone and its snapshot render as one record row, and the
        snapshot is reached through :meth:`get_frozen_for` /
        :meth:`latest_frozen`. With ``include_frozen=True`` every row is
        returned and a snapshot follows its milestone. Rows that share an
        instant are ordered by :data:`_RECORD_ORDER` in both modes.

        Args:
            case_id: The case.
            include_frozen: Also return the ``frozen`` rows.
        """
        stmt = select(CaseEntry).where(CaseEntry.case_id == case_id)
        if not include_frozen:
            stmt = stmt.where(CaseEntry.kind != "frozen")
        stmt = stmt.order_by(*_RECORD_ORDER)
        result = await self._session.execute(stmt)
        return [_entry_to_dto(model) for model in result.scalars().all()]

    # ------------------------------------------------------------------
    # System lines (ADR-0138 §7, D-CS-B-16)
    # ------------------------------------------------------------------

    async def add_system_line(
        self,
        case_id: UUID,
        *,
        event: str,
        subject: str,
        text: str,
        payload: dict,
        now: datetime,
    ) -> CaseEntryDTO | None:
        """Write one system line to an open or waiting case, once per event and subject.

        The one writer of ``kind = 'system_line'`` rows on the system-line
        path (ADR-0138 §7): ``actor = 'system'``, no acting user, and a
        payload of the caller's extras plus ``event``, ``subject`` and
        ``text``. The composition of the text and the extras is the
        system-lines service's; this layer knows the kind and the dedup key.

        **Dedup.** A case carries at most one line per ``(event, subject)``:
        one ``SELECT 1`` on ``payload->>'event'`` / ``payload->>'subject'``
        runs before the insert, and an existing line makes this a no-op that
        returns ``None``. A restated NAV for the same investment and as-of
        is therefore written once.

        **No ``updated_at`` bump.** A system line is the program's, not the
        PM's; the list's "updated" column is the PM's work, so writing a line
        leaves the case row untouched.

        Args:
            case_id: The case to write to.
            event: One of the system-line events (ADR-0138 §7).
            subject: The dedup subject within ``event`` (non-empty).
            text: The fixed template text (non-empty).
            payload: The event's machine-readable extras.
            now: The entry's ``created_at``.

        Returns:
            The written :class:`CaseEntryDTO`, or ``None`` when the case
            already carries a line for ``(event, subject)``.

        Raises:
            CaseWorkingFileInvalid: If ``event`` is outside the vocabulary or
                ``subject`` / ``text`` is empty.
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed — nothing is ever written
                to a closed case.
        """
        if event not in _SYSTEM_LINE_EVENTS:
            raise CaseWorkingFileInvalid(
                f"Invalid system-line event {event!r}; expected one of "
                f"{sorted(_SYSTEM_LINE_EVENTS)}.",
                field="event",
            )
        if not subject.strip():
            raise CaseWorkingFileInvalid("A system line names its subject.", field="subject")
        if not text.strip():
            raise CaseWorkingFileInvalid("A system line carries its text.", field="text")
        case = await self._require_mutable(case_id, action="write a system line to")
        existing = await self._session.execute(
            select(CaseEntry.id)
            .where(
                CaseEntry.case_id == case_id,
                CaseEntry.kind == "system_line",
                CaseEntry.payload["event"].astext == event,
                CaseEntry.payload["subject"].astext == subject,
            )
            .limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return None
        return await self._insert_entry(
            tenant_id=case.tenant_id,
            case_id=case_id,
            kind="system_line",
            actor="system",
            actor_user_id=None,
            payload={**payload, "event": event, "subject": subject, "text": text},
            now=now,
        )

    async def list_system_lines(self, case_id: UUID) -> list[CaseEntryDTO]:
        """Return a case's system lines, oldest ``created_at`` first (:data:`_RECORD_ORDER`)."""
        result = await self._session.execute(
            select(CaseEntry)
            .where(CaseEntry.case_id == case_id, CaseEntry.kind == "system_line")
            .order_by(*_RECORD_ORDER)
        )
        return [_entry_to_dto(model) for model in result.scalars().all()]

    async def list_open_by_finding_subject(self, subject_key: str) -> list[CaseDTO]:
        """Return the open or waiting cases opened from a finding on ``subject_key``.

        The ``subject_within_ceiling`` writer's targets (ADR-0138 §7): the
        case's origin finding carries ``subject_key`` and was resolved
        ``opened_case``. A manual case has no finding and is never returned.

        Returns:
            Matching cases, ``case_number`` ascending.
        """
        query = (
            select(Case)
            .join(IreneFinding, IreneFinding.id == Case.finding_id)
            .where(
                IreneFinding.subject_key == subject_key,
                IreneFinding.resolution == "opened_case",
                Case.state.in_(sorted(_MUTABLE_STATES)),
            )
            .order_by(Case.case_number.asc())
        )
        result = await self._session.execute(query)
        return [_case_to_dto(model) for model in result.scalars().all()]

    async def get_frozen_for(self, milestone_entry_id: UUID) -> CaseEntryDTO | None:
        """Return the ``frozen`` snapshot written with a milestone, or ``None``.

        Matches ``payload->>'milestone_entry_id'`` against the id as text —
        unindexed, which is fine: a case has few milestones. ``None`` for an
        entry that is no milestone, and for another tenant's id (RLS).
        """
        result = await self._session.execute(
            select(CaseEntry)
            .where(
                CaseEntry.kind == "frozen",
                CaseEntry.payload["milestone_entry_id"].astext == str(milestone_entry_id),
            )
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return _entry_to_dto(model) if model is not None else None

    async def latest_frozen(self, case_id: UUID) -> CaseEntryDTO | None:
        """Return the case's newest ``frozen`` snapshot, or ``None``.

        Newest by ``created_at``. Two freezes never share an instant: one
        milestone writes one snapshot, and a transaction that writes two
        milestones (the lift + close of P-CS-3c2) passes two ``now`` values.
        """
        result = await self._session.execute(
            select(CaseEntry)
            .where(CaseEntry.case_id == case_id, CaseEntry.kind == "frozen")
            .order_by(CaseEntry.created_at.desc())
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return _entry_to_dto(model) if model is not None else None

    async def close(
        self,
        case_id: UUID,
        *,
        closed_by: UUID,
        closing_note: str,
        now: datetime,
        stand: dict | None = None,
        book_values: dict | None = None,
    ) -> CaseDTO:
        """Close an open case — the close trio, once (ADR-0107 §4, ADR-0138 §4).

        In one transaction: sets ``state='closed'``, ``closed_by``,
        ``closed_at=now``, the stripped ``closing_note`` and
        ``updated_at=now``, then writes the milestone pair — the ``closed``
        entry (``actor='pm'``, ``actor_user_id=closed_by``, payload carrying
        the note) and its ``frozen`` snapshot of the file as closed — the
        "At close" record (ADR-0138 §2). No Journal is written — the Journal is a
        projection that gains closed cases as a render-time source in C4
        (Gate-C0 decision B); no later prompt should reinvent a journal write
        here.

        A waiting case is not closed directly: the wait is lifted first
        (D-CS-B-2) — the close route of P-CS-3c2 does lift + close in one
        transaction, two milestones with two freezes.

        Args:
            case_id: The case to close.
            closed_by: The user closing the case.
            closing_note: The mandatory closing note (ADR-0107 §4).
            now: The ``closed_at`` timestamp and the ``closed`` pair's time.
            stand: Where we stand as rendered, for the snapshot, or ``None``.
            book_values: Resolved ``book`` figure values for the snapshot,
                or ``None`` (D-CS-B-8).

        Returns:
            The closed :class:`CaseDTO`.

        Raises:
            CaseClosingNoteMissing: If ``closing_note`` is empty/whitespace.
            CaseStateInvalid: If no such case exists in the active tenant, or
                the case is waiting.
            CaseClosedError: If the case is already closed.
        """
        note = (closing_note or "").strip()
        if not note:
            raise CaseClosingNoteMissing(
                "A closing note is mandatory when closing a case (ADR-0107 §4).",
                field="closing_note",
            )
        case = await self.get(case_id)
        if case is None:
            raise CaseStateInvalid(
                f"No case {case_id} in this tenant to close.",
                field="state",
            )
        if case.state == "closed":
            raise CaseClosedError(
                f"Case {case_id} is already closed; closed cases are immutable (ADR-0107 §4)."
            )
        if case.state == "waiting":
            raise CaseStateInvalid(
                f"Case {case_id} is waiting; lift the wait before closing.",
                field="state",
            )
        upd = (
            update(Case)
            .where(Case.id == case_id)
            .values(
                state="closed",
                closed_by=closed_by,
                closed_at=now,
                closing_note=note,
                updated_at=now,
            )
            .returning(*_CASE_COLUMNS)
        )
        row = (await self._session.execute(upd)).one()
        closed = CaseDTO(**row._mapping)

        await self._milestone(
            case=closed,
            kind="closed",
            actor="pm",
            actor_user_id=closed_by,
            payload={"closing_note": note},
            now=now,
            stand=stand,
            book_values=book_values,
        )
        return closed

    # ------------------------------------------------------------------
    # The working-file columns, the owner and the state transitions
    # (ADR-0138 §4, §13, Object model). The owner and the states move only
    # through the milestone methods below; their column writes are private.
    # ------------------------------------------------------------------

    async def _write_case(self, case_id: UUID, values: dict[str, Any]) -> CaseDTO:
        """UPDATE one case row with ``values`` and return the updated DTO."""
        upd = update(Case).where(Case.id == case_id).values(**values).returning(*_CASE_COLUMNS)
        row = (await self._session.execute(upd)).one()
        return CaseDTO(**row._mapping)

    async def _write_case_if(
        self, case_id: UUID, values: dict[str, Any], *, condition: ColumnElement[bool]
    ) -> CaseDTO | None:
        """UPDATE one case row with ``values`` where ``condition`` still holds.

        The conditional sibling of :meth:`_write_case` (D-CS-B-72): ``None``
        when the row no longer satisfies ``condition``, so nothing was written.
        """
        upd = (
            update(Case)
            .where(Case.id == case_id, condition)
            .values(**values)
            .returning(*_CASE_COLUMNS)
        )
        row = (await self._session.execute(upd)).one_or_none()
        return CaseDTO(**row._mapping) if row is not None else None

    async def _require_mutable(self, case_id: UUID, *, action: str) -> CaseDTO:
        """Return the case if it is open or waiting.

        Raises:
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        case = await self.get(case_id)
        if case is None:
            raise CaseStateInvalid(f"No case {case_id} in this tenant to {action}.", field="state")
        if case.state not in _MUTABLE_STATES:
            raise CaseClosedError(
                f"Case {case_id} is closed; closed cases are immutable (ADR-0138 §2)."
            )
        return case

    async def list_cases(
        self,
        *,
        state: str | None = None,
        owner_user_id: UUID | None = None,
        before: datetime | None = None,
        limit: int = 200,
    ) -> list[CaseDTO]:
        """Return cases for the list filter Open · Waiting · Closed · All (ADR-0138 §12).

        The Closed filter is loaded by page: pass ``before`` (exclusive) with
        the last ``closed_at`` of the previous page and ``limit`` as the page
        size; the next page is the closed cases with ``closed_at < before``,
        newest first. ``before`` pages the Closed filter only.

        Args:
            state: ``open`` / ``waiting`` / ``closed``, or ``None`` for all.
            owner_user_id: When given, restrict to cases this user owns —
                the *Mine* filter, a filter and never a data boundary.
            before: With ``state='closed'`` only — return cases closed
                strictly before this instant.
            limit: Maximum number of rows.

        Returns:
            Waiting cases by ``wait_until`` ascending; closed cases by
            ``closed_at`` descending; open cases and *all* by ``updated_at``
            descending — ``case_number`` descending as the tie-break.

        Raises:
            CaseStateInvalid: If ``state`` is outside the vocabulary.
            ValueError: If ``before`` is given with any ``state`` but
                ``closed``.
        """
        if state is not None and state not in _VALID_STATES:
            raise CaseStateInvalid(
                f"Invalid state {state!r}; expected one of {sorted(_VALID_STATES)} or None.",
                field="state",
            )
        if before is not None and state != "closed":
            raise ValueError("before pages the Closed filter only; pass state='closed'.")
        stmt = select(*_CASE_COLUMNS)
        if state is not None:
            stmt = stmt.where(Case.state == state)
        if before is not None:
            stmt = stmt.where(Case.closed_at < before)
        if owner_user_id is not None:
            stmt = stmt.where(Case.owner_user_id == owner_user_id)
        if state == "waiting":
            stmt = stmt.order_by(Case.wait_until.asc(), Case.case_number.desc())
        elif state == "closed":
            stmt = stmt.order_by(Case.closed_at.desc(), Case.case_number.desc())
        else:
            stmt = stmt.order_by(Case.updated_at.desc(), Case.case_number.desc())
        result = await self._session.execute(stmt.limit(limit))
        return [CaseDTO(**row._mapping) for row in result.all()]

    async def list_unclosed(self) -> list[CaseDTO]:
        """Return the cases that take writes: open, then waiting (D-CS-B-65, D-CS-B-92).

        Two state-filtered reads — :meth:`list_cases` with ``state="open"``
        (by ``updated_at`` descending), then ``state="waiting"`` (by
        ``wait_until`` ascending) — so the row cap applies per state, after
        the closed cases are left out. The one picker read of its three
        callers: the Transactions composer's Provenance picker and the two
        Keep pickers (the dock and the Planning Desk, ``web/case_keep.py``).

        Returns:
            The open cases, then the waiting ones.
        """
        return [*await self.list_cases(state="open"), *await self.list_cases(state="waiting")]

    async def count_by_state(self) -> dict[str, int]:
        """Return the number of cases in each state, every state present.

        One ``GROUP BY`` over ``cases``; a state with no case reads 0. The
        Cases list's counts slot (``{open} open · {waiting} waiting · {closed}
        closed``) is its consumer. RLS scopes it to the active tenant.

        Returns:
            ``{"closed": n, "open": n, "waiting": n}``.
        """
        result = await self._session.execute(select(Case.state, func.count()).group_by(Case.state))
        counts = {state: 0 for state in sorted(_VALID_STATES)}
        counts.update({state: int(n) for state, n in result.all()})
        return counts

    async def update_working_file(
        self,
        case_id: UUID,
        *,
        now: datetime,
        title: str = _UNSET,
        description: str | None = _UNSET,
        counterparty: str | None = _UNSET,
        due_on: date | None = _UNSET,
        framing: str | None = _UNSET,
    ) -> CaseDTO:
        """Write the working-file columns of an open or waiting case.

        Only the fields given are written; an explicit ``None`` clears a
        nullable column. ``updated_at`` is set to ``now`` (D-CS-B-5).
        ``description`` is the question's (or task's) prose.

        Args:
            case_id: The case to update.
            now: The new ``updated_at``.
            title: The title — stripped, must not be empty.
            description: The question's prose, or ``None``.
            counterparty: Free-text counterparty, or ``None``.
            due_on: The case's own due date, or ``None``.
            framing: ``question`` / ``task``, or ``None``.

        Returns:
            The updated :class:`CaseDTO`.

        Raises:
            CaseWorkingFileInvalid: If ``title`` is empty or ``framing`` is
                outside the vocabulary.
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        values: dict[str, Any] = {}
        if title is not _UNSET:
            stripped = (title or "").strip()
            if not stripped:
                raise CaseWorkingFileInvalid("A case title must not be empty.", field="title")
            values["title"] = stripped
        if framing is not _UNSET:
            _validate_framing(framing)
            values["framing"] = framing
        if description is not _UNSET:
            values["description"] = description
        if counterparty is not _UNSET:
            values["counterparty"] = counterparty
        if due_on is not _UNSET:
            values["due_on"] = due_on
        await self._require_mutable(case_id, action="update")
        values["updated_at"] = now
        return await self._write_case(case_id, values)

    async def _set_owner_column(
        self, case: CaseDTO, *, owner_user_id: UUID, now: datetime
    ) -> CaseDTO:
        """Set the owner of an open or waiting case (ADR-0138 §13).

        Column write; called by the milestone methods only (:meth:`hand_over`).
        ``opened_by`` is left as history.
        """
        assert case.state in _MUTABLE_STATES, f"cannot hand over a {case.state} case"
        return await self._write_case(case.id, {"owner_user_id": owner_user_id, "updated_at": now})

    async def _set_waiting_columns(
        self, case: CaseDTO, *, wait_until: date, now: datetime
    ) -> CaseDTO:
        """Move an open case to ``waiting`` until ``wait_until`` (ADR-0138 §4).

        Column write; called by the milestone methods only (:meth:`wait`).
        """
        assert case.state == "open", f"cannot set a {case.state} case waiting"
        return await self._write_case(
            case.id, {"state": "waiting", "wait_until": wait_until, "updated_at": now}
        )

    async def _lift_waiting_columns(self, case: CaseDTO, *, now: datetime) -> CaseDTO:
        """Return a waiting case to ``open`` and clear ``wait_until`` (ADR-0138 §4).

        Column write; called by the milestone methods only (:meth:`lift_wait`).
        **The race** (D-CS-B-72): :meth:`lift_wait` reads the case before
        this write, and another transaction — the tick runner's date wake, a
        system line's event wake, the PM's own lift — may lift it between the
        two. The UPDATE therefore holds ``state = 'waiting'`` in its WHERE:
        under READ COMMITTED a second writer waits for the first to commit,
        re-checks the condition on the new row and matches nothing, so the
        loser raises here and writes no second ``waiting_lifted`` milestone.

        Raises:
            CaseStateInvalid: If the case is no longer waiting at the write.
        """
        lifted = await self._write_case_if(
            case.id,
            {"state": "open", "wait_until": None, "updated_at": now},
            condition=Case.state == "waiting",
        )
        if lifted is None:
            raise CaseStateInvalid(
                f"Case {case.id} is no longer waiting — it was lifted by the program meanwhile.",
                field="state",
            )
        return lifted

    async def wait(
        self,
        case_id: UUID,
        *,
        wait_until: date,
        by: UUID,
        now: datetime,
        note: str | None = None,
        stand: dict | None = None,
        book_values: dict | None = None,
    ) -> CaseDTO:
        """Set an open case waiting until ``wait_until`` — the ``waiting_set`` milestone.

        In the caller's transaction: the state moves to ``waiting`` with
        ``wait_until`` (ADR-0138 §4), then the ``waiting_set`` entry
        (``actor='pm'``, payload ``{wait_until, note}``) and its ``frozen``
        snapshot are written. The repository does not judge the date; the
        route validates it.

        Args:
            case_id: The case.
            wait_until: The date the case returns to open.
            by: The PM setting the wait.
            now: ``updated_at`` and the pair's time.
            note: Optional note on what the case waits for.
            stand: Where we stand as rendered, for the snapshot, or ``None``.
            book_values: Resolved ``book`` figure values, or ``None``.

        Returns:
            The updated :class:`CaseDTO`.

        Raises:
            CaseStateInvalid: If no such case exists in the active tenant, or
                the case is not open.
            CaseClosedError: If the case is closed.
        """
        case = await self._require_mutable(case_id, action="set waiting")
        if case.state != "open":
            raise CaseStateInvalid(
                f"Case {case_id} is {case.state}; only an open case can be set waiting.",
                field="state",
            )
        waiting = await self._set_waiting_columns(case, wait_until=wait_until, now=now)
        await self._milestone(
            case=waiting,
            kind="waiting_set",
            actor="pm",
            actor_user_id=by,
            payload={"wait_until": wait_until.isoformat(), "note": note},
            now=now,
            stand=stand,
            book_values=book_values,
        )
        return waiting

    async def lift_wait(
        self,
        case_id: UUID,
        *,
        now: datetime,
        by: UUID | None = None,
        reason: str = "manual",
        event: dict | None = None,
        stand: dict | None = None,
        book_values: dict | None = None,
    ) -> CaseDTO:
        """Return a waiting case to open — the ``waiting_lifted`` milestone.

        The PM lifts by hand (``reason='manual'``, ``by`` given, ``actor='pm'``);
        the program lifts on the date or on an event (``reason='date'`` /
        ``'event'``, no ``by``, ``actor='system'``) — ADR-0138 §4, D-CS-M-4.
        The payload is ``{reason, wait_until, event}`` with the date that was
        set. The waking event's own system line is the caller's, not written
        here.

        Three reasons, three callers:

        * ``manual`` — the PM by hand (*Resume now*, and the lift before
          *Close case…* on a waiting case; P-CS-3c2).
        * ``date`` — the shared tick runner's wake-up step
          (:func:`services.scheduler.tick_runner.run_case_wake_tick`), on
          the first tick of the day the wait ends.
        * ``event`` — the system-line writer
          (``services.cases.system_lines._write_line``), right after the
          waking line, in the same transaction (D-CS-M-4); ``event``
          names that line's event, subject and entry id.

        Args:
            case_id: The case.
            now: ``updated_at`` and the pair's time.
            by: The PM lifting the wait (``manual`` only).
            reason: ``manual`` · ``date`` · ``event``.
            event: The waking event, opaque and non-empty (``event`` only).
            stand: Where we stand as rendered, for the snapshot, or ``None``.
            book_values: Resolved ``book`` figure values, or ``None``.

        Returns:
            The updated :class:`CaseDTO`.

        Raises:
            CaseWorkingFileInvalid: On a reason outside the vocabulary, or a
                ``by`` / ``event`` that does not fit the reason.
            CaseStateInvalid: If no such case exists in the active tenant, or
                the case is not waiting.
        """
        if reason not in _LIFT_REASONS:
            raise CaseWorkingFileInvalid(
                f"Invalid lift reason {reason!r}; expected one of {sorted(_LIFT_REASONS)}.",
                field="reason",
            )
        if reason == "manual" and by is None:
            raise CaseWorkingFileInvalid("A manual lift names the PM (by).", field="by")
        if reason != "manual" and by is not None:
            raise CaseWorkingFileInvalid(
                f"A {reason!r} lift is the program's; it names no PM (by).", field="by"
            )
        if reason == "event" and not event:
            raise CaseWorkingFileInvalid("An 'event' lift carries the event.", field="event")
        if reason != "event" and event is not None:
            raise CaseWorkingFileInvalid(f"A {reason!r} lift carries no event.", field="event")
        case = await self.get(case_id)
        if case is None or case.state != "waiting":
            state = "absent" if case is None else case.state
            raise CaseStateInvalid(
                f"Case {case_id} is {state}; only a waiting case can be lifted.",
                field="state",
            )
        lifted = await self._lift_waiting_columns(case, now=now)
        await self._milestone(
            case=lifted,
            kind="waiting_lifted",
            actor="pm" if reason == "manual" else "system",
            actor_user_id=by,
            payload={
                "reason": reason,
                "wait_until": case.wait_until.isoformat() if case.wait_until else None,
                "event": event,
            },
            now=now,
            stand=stand,
            book_values=book_values,
        )
        return lifted

    async def hand_over(
        self,
        case_id: UUID,
        *,
        to_user_id: UUID,
        by: UUID,
        now: datetime,
        note: str | None = None,
        stand: dict | None = None,
        book_values: dict | None = None,
    ) -> CaseDTO:
        """Hand an open or waiting case to another owner — the ``handed_over`` milestone.

        ADR-0138 §13: the owner changes, ``opened_by`` stays as history; the
        ``handed_over`` entry (``actor='pm'``, payload ``{from_user_id,
        to_user_id, note}``) and its ``frozen`` snapshot are written in the
        caller's transaction.

        Args:
            case_id: The case.
            to_user_id: The new owner.
            by: The PM handing over.
            now: ``updated_at`` and the pair's time.
            note: Optional hand-over note.
            stand: Where we stand as rendered, for the snapshot, or ``None``.
            book_values: Resolved ``book`` figure values, or ``None``.

        Returns:
            The updated :class:`CaseDTO`.

        Raises:
            CaseWorkingFileInvalid: If ``to_user_id`` is the current owner.
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        case = await self._require_mutable(case_id, action="hand over")
        if to_user_id == case.owner_user_id:
            raise CaseWorkingFileInvalid(
                f"User {to_user_id} already owns case {case_id}.", field="to_user_id"
            )
        handed = await self._set_owner_column(case, owner_user_id=to_user_id, now=now)
        await self._milestone(
            case=handed,
            kind="handed_over",
            actor="pm",
            actor_user_id=by,
            payload={
                "from_user_id": str(case.owner_user_id),
                "to_user_id": str(to_user_id),
                "note": note,
            },
            now=now,
            stand=stand,
            book_values=book_values,
        )
        return handed

    async def record_decision(
        self,
        case_id: UUID,
        *,
        recorded_by: UUID,
        now: datetime,
        stand: dict | None = None,
        book_values: dict | None = None,
    ) -> CaseEntryDTO:
        """Record the case's draft decision — the ``decision_record`` milestone.

        ADR-0138 §14: the draft needs a chosen option and a rationale
        (D-CS-B-10); ``decided_by`` / ``decided_at`` stay optional. In the
        caller's transaction, in this order: the ``decision_record`` entry
        (``actor='pm'``, the decision as payload), then the draft is marked
        recorded against that entry (immutable from then on, ``updated_at =
        now``), then the ``frozen`` snapshot — so the snapshot shows the
        decision under ``recorded`` and no draft. A case may record more
        than one decision; each is its own milestone pair (D-CS-M-3).

        Args:
            case_id: The case.
            recorded_by: The PM recording the decision.
            now: ``recorded_at``, ``updated_at`` and the pair's time.
            stand: Where we stand as rendered, for the snapshot, or ``None``.
            book_values: Resolved ``book`` figure values, or ``None``.

        Returns:
            The ``decision_record`` entry.

        Raises:
            CaseWorkingFileInvalid: If there is no draft, or it has no chosen
                option or an empty rationale.
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        case = await self._require_mutable(case_id, action="record a decision on")
        decisions = CaseDecisionRepository(self._session)
        draft = await decisions.get_draft(case_id)
        if draft is None:
            raise CaseWorkingFileInvalid(
                f"Case {case_id} has no draft decision to record.", field="decision"
            )
        if draft.chosen_key is None:
            raise CaseWorkingFileInvalid(
                "A decision is recorded with a chosen option.", field="chosen_key"
            )
        if not (draft.rationale or "").strip():
            raise CaseWorkingFileInvalid(
                "A decision is recorded with a rationale.", field="rationale"
            )
        chosen_text = next(
            (option["text"] for option in draft.options if option["key"] == draft.chosen_key),
            None,
        )
        if chosen_text is None:
            raise CaseWorkingFileInvalid(
                f"chosen_key {draft.chosen_key!r} names no option.", field="chosen_key"
            )
        milestone = await self._insert_milestone(
            case=case,
            kind="decision_record",
            actor="pm",
            actor_user_id=recorded_by,
            payload={
                "decision_id": str(draft.id),
                "options": draft.options,
                "chosen_key": draft.chosen_key,
                "chosen_text": chosen_text,
                "rationale": draft.rationale,
                "decided_by": draft.decided_by,
                "decided_at": draft.decided_at.isoformat() if draft.decided_at else None,
            },
            now=now,
        )
        await decisions.mark_recorded(
            draft.id, recorded_by=recorded_by, record_entry_id=milestone.id, now=now
        )
        await self._insert_frozen(
            case=case, milestone=milestone, now=now, stand=stand, book_values=book_values
        )
        return milestone

    async def list_due_for_wake(self, *, today: date) -> list[CaseDTO]:
        """Return waiting cases whose ``wait_until`` is on or before ``today``.

        The wake-up step's read (ADR-0138 §4, P-CS-2c), ``wait_until``
        ascending; served by the partial index ``ix_cases_wait_until``.
        """
        result = await self._session.execute(
            select(*_CASE_COLUMNS)
            .where(Case.state == "waiting", Case.wait_until <= today)
            .order_by(Case.wait_until.asc(), Case.case_number.asc())
        )
        return [CaseDTO(**row._mapping) for row in result.all()]

    async def search_working_file(self, query: str, *, state: str | None = None) -> list[CaseDTO]:
        """Search cases over what the PM wrote or kept as text (ADR-0138 §10/§11).

        Case-insensitive substring match (ILIKE) with ``%`` / ``_`` in the
        user input escaped so they match literally, over: the case's
        ``title``, ``description`` and ``closing_note``; its threads'
        ``title`` / ``prose`` / ``conclusion`` (removed threads excluded);
        its figures' ``label`` / ``value_text`` / ``note``; its
        conversations' ``with_whom`` / ``what_was_said`` /
        ``what_was_promised``; its decisions' ``rationale`` / ``decided_by``
        and option texts; its kept rows' ``comment``, their ``caption``
        except on a ``document`` row, and — for ``document_excerpt`` rows
        only — the quoted ``passage`` (removed kept rows excluded). Closed
        cases are searched too: the archive is the institution's memory.

        **The DMS boundary holds** (ADR-0138 §11): never an attachment's
        filename or contents, never any other kept payload field, never a
        record entry's payload. A ``document`` row's caption is left out
        because the attach route captions it with the attachment's filename
        (P-CS-5a). An empty or whitespace-only query returns an empty list
        rather than matching everything.

        Args:
            query: The search text.
            state: ``open`` / ``waiting`` / ``closed``, or ``None`` for all.

        Returns:
            Matching cases, ``updated_at`` descending, ``case_number``
            descending as the tie-break.

        Raises:
            CaseStateInvalid: If ``state`` is outside the vocabulary.
        """
        if state is not None and state not in _VALID_STATES:
            raise CaseStateInvalid(
                f"Invalid state {state!r}; expected one of {sorted(_VALID_STATES)} or None.",
                field="state",
            )
        stripped = query.strip()
        if not stripped:
            return []
        pattern = f"%{_escape_like(stripped)}%"

        def matches(*columns: Any) -> Any:
            return or_(*(column.ilike(pattern, escape="\\") for column in columns))

        thread_hit = exists().where(
            CaseThread.case_id == Case.id,
            CaseThread.removed_at.is_(None),
            matches(CaseThread.title, CaseThread.prose, CaseThread.conclusion),
        )
        figure_hit = exists().where(
            CaseFigure.case_id == Case.id,
            matches(CaseFigure.label, CaseFigure.value_text, CaseFigure.note),
        )
        conversation_hit = exists().where(
            CaseConversation.case_id == Case.id,
            matches(
                CaseConversation.with_whom,
                CaseConversation.what_was_said,
                CaseConversation.what_was_promised,
            ),
        )
        decision_hit = exists().where(
            CaseDecision.case_id == Case.id,
            matches(
                CaseDecision.rationale,
                CaseDecision.decided_by,
                cast(CaseDecision.options, Text),
            ),
        )
        kept_hit = exists().where(
            CaseKept.case_id == Case.id,
            CaseKept.removed_at.is_(None),
            or_(
                matches(CaseKept.comment),
                and_(CaseKept.artifact != "document", matches(CaseKept.caption)),
                and_(
                    CaseKept.artifact == "document_excerpt",
                    matches(CaseKept.payload["passage"].astext),
                ),
            ),
        )
        stmt = select(*_CASE_COLUMNS).where(
            or_(
                matches(Case.title, Case.description, Case.closing_note),
                thread_hit,
                figure_hit,
                conversation_hit,
                decision_hit,
                kept_hit,
            )
        )
        if state is not None:
            stmt = stmt.where(Case.state == state)
        stmt = stmt.order_by(Case.updated_at.desc(), Case.case_number.desc())
        result = await self._session.execute(stmt)
        return [CaseDTO(**row._mapping) for row in result.all()]


def _case_to_dto(model: Case) -> CaseDTO:
    return CaseDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        case_number=model.case_number,
        title=model.title,
        description=model.description,
        state=model.state,
        finding_id=model.finding_id,
        opened_by=model.opened_by,
        opened_at=model.opened_at,
        closed_by=model.closed_by,
        closed_at=model.closed_at,
        closing_note=model.closing_note,
        wait_until=model.wait_until,
        owner_user_id=model.owner_user_id,
        counterparty=model.counterparty,
        due_on=model.due_on,
        framing=model.framing,
        updated_at=model.updated_at,
    )


def _entry_to_dto(model: CaseEntry) -> CaseEntryDTO:
    return CaseEntryDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        case_id=model.case_id,
        kind=model.kind,
        actor=model.actor,
        actor_user_id=model.actor_user_id,
        payload=model.payload,
        created_at=model.created_at,
    )
