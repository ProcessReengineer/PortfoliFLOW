# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""CaseRepository — persistence for the Cases area workflow (ADR-0107).

Backs the ``cases`` and ``case_entries`` tables introduced in migration
b031. Shape mirrors the other tenant-scoped repositories: a tenant-scoped
:class:`AsyncSession` is passed in, methods return frozen DTOs, and
``tenant_id`` is implicit in the session context (RLS derives it from
``app.tenant_id``).

Two invariants are enforced by the *absence* of code paths as much as by
the guards below:

* **Append-only timeline.** Entries are never updated or deleted; a new
  situation is a new entry (ADR-0107 §2). There is deliberately no
  ``update_entry`` / ``delete_entry``.
* **Closed-case immutability.** A closed case is a read-only record in its
  entirety (ADR-0107 §4). The close transition — via :meth:`close` — is the
  single permitted mutation of a case row (ADR-0107 §2). There is
  deliberately no ``update_case``, no ``delete_case``, and no ``reopen``.

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
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, insert, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    CaseActorInvalid,
    CaseClosedError,
    CaseClosingNoteMissing,
    CaseEntryKindInvalid,
    CaseStateInvalid,
)
from core.models.case import Case, CaseEntry
from core.repositories.base import BaseRepository

# Canonical lowercase vocabularies (ADR-0107 §2). The columns are plain
# TEXT; the vocabularies are enforced here, not as SQL enums, matching the
# codebase's TEXT-for-status convention.
_VALID_STATES: frozenset[str] = frozenset({"open", "closed"})
_VALID_ENTRY_KINDS: frozenset[str] = frozenset(
    {"opened", "note", "pin", "decision_record", "closed"}
)
_VALID_ACTORS: frozenset[str] = frozenset({"pm", "shirley", "system"})

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
            now: The timestamp for ``opened_at`` and the ``opened`` entry.

        Returns:
            The newly created :class:`CaseDTO`.

        Raises:
            CaseActorInvalid: If ``opened_actor`` is outside the vocabulary.
        """
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
        """Append a timeline entry to an open case (append-only, ADR-0107 §2).

        Vocabulary is validated before any SQL runs. ``kind='closed'`` is
        rejected here — the ``closed`` entry is written only by
        :meth:`close`.

        Args:
            case_id: The case to append to.
            kind: One of ``opened`` / ``note`` / ``pin`` / ``decision_record``
                (``closed`` is rejected).
            actor: One of ``pm`` / ``shirley`` / ``system``.
            actor_user_id: The acting user when a user acted, else ``None``.
            payload: Opaque JSONB for the entry.
            now: The entry's ``created_at``.

        Returns:
            The newly appended :class:`CaseEntryDTO`.

        Raises:
            CaseEntryKindInvalid: If ``kind`` is outside the vocabulary, or is
                ``closed`` (which only :meth:`close` may write).
            CaseActorInvalid: If ``actor`` is outside the vocabulary.
            CaseStateInvalid: If no such case exists in the active tenant.
            CaseClosedError: If the case is already closed.
        """
        if kind not in _VALID_ENTRY_KINDS:
            raise CaseEntryKindInvalid(
                f"Invalid entry kind {kind!r}; expected one of {sorted(_VALID_ENTRY_KINDS)}.",
                field="kind",
            )
        if kind == "closed":
            raise CaseEntryKindInvalid(
                "The 'closed' entry is written only by CaseRepository.close(); "
                "append_entry does not accept kind='closed'.",
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
        if case.state != "open":
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
        """Close a case — the single permitted case-row mutation (ADR-0107 §2).

        In one transaction: sets ``state='closed'``, ``closed_by``,
        ``closed_at=now`` and the stripped ``closing_note``, then appends the
        ``closed`` timeline entry (``actor='pm'``, ``actor_user_id=closed_by``,
        payload carrying the note). No Journal is written — the Journal is a
        projection that gains closed cases as a render-time source in C4
        (Gate-C0 decision B); no later prompt should reinvent a journal write
        here.

        Args:
            case_id: The case to close.
            closed_by: The user closing the case.
            closing_note: The mandatory closing note (ADR-0107 §4).
            now: The ``closed_at`` timestamp and the ``closed`` entry's time.

        Returns:
            The closed :class:`CaseDTO`.

        Raises:
            CaseClosingNoteMissing: If ``closing_note`` is empty/whitespace.
            CaseStateInvalid: If no such case exists in the active tenant.
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
        if case.state != "open":
            raise CaseClosedError(
                f"Case {case_id} is already closed; closed cases are immutable (ADR-0107 §4)."
            )
        upd = (
            update(Case)
            .where(Case.id == case_id)
            .values(
                state="closed",
                closed_by=closed_by,
                closed_at=now,
                closing_note=note,
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
