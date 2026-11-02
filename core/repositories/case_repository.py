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
  (:meth:`set_owner`) and its state transitions (:meth:`set_waiting`,
  :meth:`lift_waiting`, :meth:`close`) are the permitted mutations of a
  case row; each one bumps ``updated_at``. The eight working-file tables
  live in :mod:`core.repositories.case_working_file_repository`.
* **A closed case is read-only in both layers** (ADR-0107 §4, confirmed by
  ADR-0138 §4). There is deliberately no ``delete_case`` and no
  ``reopen``.

The milestone path — a milestone entry and its ``frozen`` snapshot written
with the state change in one transaction — lands in P-CS-1b; until then
the state transitions here are column transitions only, and
:meth:`append_entry` refuses the milestone-only kinds.

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

from sqlalchemy import Text, and_, cast, exists, func, insert, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    CaseActorInvalid,
    CaseClosedError,
    CaseClosingNoteMissing,
    CaseEntryKindInvalid,
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
from core.repositories.base import BaseRepository

# Canonical lowercase vocabularies (ADR-0107 §2, ADR-0138 §4 and Object
# model). The columns are plain TEXT; the vocabularies are enforced here,
# not as SQL enums, matching the codebase's TEXT-for-status convention.
_VALID_STATES: frozenset[str] = frozenset({"open", "waiting", "closed"})
# The states in which the working file (and the record's generic append
# path) accepts writes; a closed case is read-only in both layers.
_MUTABLE_STATES: frozenset[str] = frozenset({"open", "waiting"})
# ``note`` / ``pin`` / ``decision_record`` stay writable until their last
# route writer is replaced (D-CS-B-1, staged retirement).
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
# Written only by the milestone path (P-CS-1b); append_entry rejects them.
_MILESTONE_ONLY_KINDS: frozenset[str] = frozenset(
    {"closed", "handed_over", "waiting_set", "waiting_lifted", "frozen"}
)
_VALID_ACTORS: frozenset[str] = frozenset({"pm", "shirley", "system"})
_VALID_FRAMINGS: frozenset[str] = frozenset({"question", "task"})


class _Unset:
    """Type of :data:`_UNSET` — "argument not given", distinct from ``None``."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "_UNSET"


#: Sentinel for partial updates: a field left at ``_UNSET`` is not written,
#: while an explicit ``None`` clears a nullable column.
_UNSET: Any = _Unset()

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
    ) -> CaseDTO:
        """One case-creation attempt: the case row plus its ``opened`` entry.

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

            # The mandatory first timeline entry. The opened entry is
            # attributed to a user only when a human PM opened the case;
            # system / shirley openings carry no actor_user_id.
            opened_actor_user = opened_by if opened_actor == "pm" else None
            await self._insert_entry(
                tenant_id=active_tenant,
                case_id=case_dto.id,
                kind="opened",
                actor=opened_actor,
                actor_user_id=opened_actor_user,
                payload=opened_payload or {},
                now=now,
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
    ) -> CaseDTO:
        """Open a case — the one creation path for both entry points.

        Manual creation passes a ``description`` and no ``finding_id``;
        open-from-finding passes a ``finding_id`` (and may leave
        ``description`` null). Either way exactly one ``opened`` timeline
        entry is written in the same transaction, carrying ``opened_payload``
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
                ``opened`` entry.

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

    async def list_open(self, *, opened_by: UUID | None = None) -> list[CaseDTO]:
        """Return open cases, newest ``opened_at`` first.

        Args:
            opened_by: When given, restrict to cases opened by this user —
                the "Mine" chip, a filter and never a data boundary
                (ADR-0107 §1). Omitted, every open case is returned.
        """
        stmt = select(Case).where(Case.state == "open")
        if opened_by is not None:
            stmt = stmt.where(Case.opened_by == opened_by)
        stmt = stmt.order_by(Case.opened_at.desc(), Case.case_number.desc())
        result = await self._session.execute(stmt)
        return [_case_to_dto(model) for model in result.scalars().all()]

    async def list_recently_closed(self, *, limit: int = 5) -> list[CaseDTO]:
        """Return closed cases, newest ``closed_at`` first, capped at ``limit``."""
        result = await self._session.execute(
            select(Case)
            .where(Case.state == "closed")
            .order_by(Case.closed_at.desc(), Case.case_number.desc())
            .limit(limit)
        )
        return [_case_to_dto(model) for model in result.scalars().all()]

    async def list_closed(self, *, limit: int = 100) -> list[CaseDTO]:
        """Return closed cases for the Watch Desk Journal source.

        The Journal is a render-time projection that gains closed cases as a
        second source (ADR-0107, C4 · Gate-C0 decision B); this is that source,
        deliberately distinct from :meth:`list_recently_closed` (the five-row
        *UI* list on the Cases page). Same ordering — newest ``closed_at``
        first, ``case_number`` as the stable tie-break — but a larger cap so a
        page of closed cases can interleave with resolved findings. Not a UI
        list; nothing renders it directly.

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

    async def search_archive(self, query: str) -> list[CaseDTO]:
        """Search closed cases by ``title`` or ``closing_note`` (ILIKE).

        Case-insensitive substring match with ``%`` / ``_`` in the user
        input escaped so they match literally. Titles and closing notes
        **only** — never entry payloads, never attachment content: the DMS
        boundary (ADR-0107 §7). An empty or whitespace-only query returns an
        empty list rather than matching everything.
        """
        stripped = query.strip()
        if not stripped:
            return []
        pattern = f"%{_escape_like(stripped)}%"
        result = await self._session.execute(
            select(Case)
            .where(
                Case.state == "closed",
                or_(
                    Case.title.ilike(pattern, escape="\\"),
                    Case.closing_note.ilike(pattern, escape="\\"),
                ),
            )
            .order_by(Case.closed_at.desc(), Case.case_number.desc())
        )
        return [_case_to_dto(model) for model in result.scalars().all()]

    async def append_entry(
        self,
        case_id: UUID,
        *,
        kind: str,
        actor: str,
        actor_user_id: UUID | None,
        payload: dict,
        now: datetime,
    ) -> CaseEntryDTO:
        """Append a record entry to an open or waiting case (append-only).

        Vocabulary is validated before any SQL runs. The milestone-only
        kinds — ``closed``, ``handed_over``, ``waiting_set``,
        ``waiting_lifted`` and ``frozen`` — are rejected here: they are
        written only by the milestone path (:meth:`close` today, the
        milestone + freeze transaction of P-CS-1b). ``note``, ``pin`` and
        ``decision_record`` stay writable until their last route writer is
        replaced (D-CS-B-1); ``system_line`` is the system lines' kind
        (ADR-0138 §7).

        Args:
            case_id: The case to append to.
            kind: One of ``opened`` / ``note`` / ``pin`` /
                ``decision_record`` / ``system_line`` (the milestone-only
                kinds are rejected).
            actor: One of ``pm`` / ``shirley`` / ``system``.
            actor_user_id: The acting user when a user acted, else ``None``.
            payload: Opaque JSONB for the entry.
            now: The entry's ``created_at``.

        Returns:
            The newly appended :class:`CaseEntryDTO`.

        Raises:
            CaseEntryKindInvalid: If ``kind`` is outside the vocabulary, or is
                a milestone-only kind (which only the milestone path writes).
            CaseActorInvalid: If ``actor`` is outside the vocabulary.
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is already closed.
        """
        if kind not in _VALID_ENTRY_KINDS:
            raise CaseEntryKindInvalid(
                f"Invalid entry kind {kind!r}; expected one of {sorted(_VALID_ENTRY_KINDS)}.",
                field="kind",
            )
        if kind in _MILESTONE_ONLY_KINDS:
            raise CaseEntryKindInvalid(
                f"The {kind!r} entry is written only by the milestone path "
                "(CaseRepository.close() and the milestone + freeze transaction); "
                f"append_entry does not accept kind={kind!r}.",
                field="kind",
            )
        if actor not in _VALID_ACTORS:
            raise CaseActorInvalid(
                f"Invalid actor {actor!r}; expected one of {sorted(_VALID_ACTORS)}.",
                field="actor",
            )
        case = await self.get(case_id)
        if case is None:
            raise CaseStateInvalid(
                f"No case {case_id} in this tenant to append to.",
                field="state",
            )
        if case.state not in _MUTABLE_STATES:
            raise CaseClosedError(
                f"Case {case_id} is closed; closed cases are immutable (ADR-0107 §4)."
            )
        return await self._insert_entry(
            tenant_id=case.tenant_id,
            case_id=case_id,
            kind=kind,
            actor=actor,
            actor_user_id=actor_user_id,
            payload=payload,
            now=now,
        )

    async def list_entries(self, case_id: UUID) -> list[CaseEntryDTO]:
        """Return a case's timeline, oldest ``created_at`` first."""
        result = await self._session.execute(
            select(CaseEntry)
            .where(CaseEntry.case_id == case_id)
            .order_by(CaseEntry.created_at.asc())
        )
        return [_entry_to_dto(model) for model in result.scalars().all()]

    async def close(
        self,
        case_id: UUID,
        *,
        closed_by: UUID,
        closing_note: str,
        now: datetime,
    ) -> CaseDTO:
        """Close an open case — the close trio, once (ADR-0107 §4, ADR-0138 §4).

        In one transaction: sets ``state='closed'``, ``closed_by``,
        ``closed_at=now``, the stripped ``closing_note`` and
        ``updated_at=now``, then appends the
        ``closed`` timeline entry (``actor='pm'``, ``actor_user_id=closed_by``,
        payload carrying the note). No Journal is written — the Journal is a
        projection that gains closed cases as a render-time source in C4
        (Gate-C0 decision B); no later prompt should reinvent a journal write
        here.

        A waiting case is not closed directly: the wait is lifted first
        (D-CS-B-2) — the close route of P-CS-3c does lift + close in one
        transaction, two milestones with two freezes.

        Args:
            case_id: The case to close.
            closed_by: The user closing the case.
            closing_note: The mandatory closing note (ADR-0107 §4).
            now: The ``closed_at`` timestamp and the ``closed`` entry's time.

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

        await self._insert_entry(
            tenant_id=case.tenant_id,
            case_id=case_id,
            kind="closed",
            actor="pm",
            actor_user_id=closed_by,
            payload={"closing_note": note},
            now=now,
        )
        return closed

    # ------------------------------------------------------------------
    # The working-file columns, the owner and the state transitions
    # (ADR-0138 §4, §13, Object model). Column writes only: the milestone
    # entries and their frozen snapshots around them are P-CS-1b's.
    # ------------------------------------------------------------------

    async def _write_case(self, case_id: UUID, values: dict[str, Any]) -> CaseDTO:
        """UPDATE one case row with ``values`` and return the updated DTO."""
        upd = update(Case).where(Case.id == case_id).values(**values).returning(*_CASE_COLUMNS)
        row = (await self._session.execute(upd)).one()
        return CaseDTO(**row._mapping)

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
        limit: int = 200,
    ) -> list[CaseDTO]:
        """Return cases for the list filter Open · Waiting · Closed · All (ADR-0138 §12).

        Args:
            state: ``open`` / ``waiting`` / ``closed``, or ``None`` for all.
            owner_user_id: When given, restrict to cases this user owns —
                the *Mine* filter, a filter and never a data boundary.
            limit: Maximum number of rows.

        Returns:
            Waiting cases by ``wait_until`` ascending; closed cases by
            ``closed_at`` descending; open cases and *all* by ``updated_at``
            descending — ``case_number`` descending as the tie-break.

        Raises:
            CaseStateInvalid: If ``state`` is outside the vocabulary.
        """
        if state is not None and state not in _VALID_STATES:
            raise CaseStateInvalid(
                f"Invalid state {state!r}; expected one of {sorted(_VALID_STATES)} or None.",
                field="state",
            )
        stmt = select(*_CASE_COLUMNS)
        if state is not None:
            stmt = stmt.where(Case.state == state)
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

    async def set_owner(self, case_id: UUID, *, owner_user_id: UUID, now: datetime) -> CaseDTO:
        """Set the owner of an open or waiting case (ADR-0138 §13).

        A column write only — the ``handed_over`` milestone and its freeze
        are P-CS-1b's. ``opened_by`` is left as history.

        Raises:
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is closed.
        """
        await self._require_mutable(case_id, action="hand over")
        return await self._write_case(case_id, {"owner_user_id": owner_user_id, "updated_at": now})

    async def set_waiting(self, case_id: UUID, *, wait_until: date, now: datetime) -> CaseDTO:
        """Move an open case to ``waiting`` until ``wait_until`` (ADR-0138 §4).

        A column transition only — the ``waiting_set`` milestone and its
        freeze are P-CS-1b's.

        Raises:
            CaseStateInvalid: If no such case exists in the active tenant, or
                the case is not open.
        """
        case = await self.get(case_id)
        if case is None or case.state != "open":
            state = "absent" if case is None else case.state
            raise CaseStateInvalid(
                f"Case {case_id} is {state}; only an open case can be set waiting.",
                field="state",
            )
        return await self._write_case(
            case_id, {"state": "waiting", "wait_until": wait_until, "updated_at": now}
        )

    async def lift_waiting(self, case_id: UUID, *, now: datetime) -> CaseDTO:
        """Return a waiting case to ``open`` and clear ``wait_until`` (ADR-0138 §4).

        A column transition only — the ``waiting_lifted`` milestone and its
        freeze are P-CS-1b's.

        Raises:
            CaseStateInvalid: If no such case exists in the active tenant, or
                the case is not waiting.
        """
        case = await self.get(case_id)
        if case is None or case.state != "waiting":
            state = "absent" if case is None else case.state
            raise CaseStateInvalid(
                f"Case {case_id} is {state}; only a waiting case can be lifted.",
                field="state",
            )
        return await self._write_case(
            case_id, {"state": "open", "wait_until": None, "updated_at": now}
        )

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
        and option texts; its kept rows' ``caption`` / ``comment`` and —
        for ``document_excerpt`` rows only — the quoted ``passage``
        (removed kept rows excluded). Closed cases are searched too: the
        archive is the institution's memory.

        **The DMS boundary holds** (ADR-0138 §11): never an attachment's
        filename or contents, never any other kept payload field, never a
        record entry's payload. An empty or whitespace-only query returns
        an empty list rather than matching everything.

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
                matches(CaseKept.caption, CaseKept.comment),
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
