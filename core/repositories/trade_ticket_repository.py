# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""TradeTicketRepository — persistence for the Transactions area (ADR-0128).

Backs the ``trade_tickets`` and ``trade_ticket_effects`` tables introduced
in migration b034. Shape mirrors the other tenant-scoped repositories: a
tenant-scoped :class:`AsyncSession` is passed in, methods return frozen
DTOs, and ``tenant_id`` is implicit in the session context (RLS derives it
from ``app.tenant_id``).

**This layer is mechanism, not policy.** It moves a ticket between the
stations of ADR-0128 §3 and records what a booking emitted; it does not
decide which transitions are legal, whether a ticket is complete enough to
be proposed, whether an AnlV code is set, or what a booking should emit.
All of that is the service seam's (ADR-0128 §4, decision record §2.8/§2.10)
— duplicating it here would fork one contract across two places. Two
guarantees *are* enforced here, because they are properties of the store
rather than of the workflow:

* **Only drafts are editable through :meth:`update_draft`.** A ticket that
  has left ``draft`` is a record, not a form. The method distinguishes
  "no such ticket" from "not a draft" rather than silently updating
  nothing — a no-op would let a caller believe it had written.
* **Vocabularies are validated before any SQL runs.** ``status`` and
  ``effect_type`` are plain TEXT with CHECK constraints behind them; the
  repository refuses an unknown value with a typed error instead of
  letting an ``IntegrityError`` surface from the driver.

``ticket_number`` allocation follows the ``case_number`` precedent
(:mod:`core.repositories.case_repository`) exactly: an in-SQL
``COALESCE(MAX(...), 0) + 1`` inside a SAVEPOINT, with a single retry on
the unique-constraint collision. The constraint is the guarantee; the
retry is the recovery.

The repository never touches the ledger. Emitting ``position_transactions``
/ ``investment_cashflows`` / ``investment_navs`` rows and updating
``investments`` is the booking service's concern; this layer only records
*that* they were emitted, through :meth:`add_effects` — the one-way
dependency of ADR-0128 §2.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from core.exceptions import TicketNotFound, TicketStateInvalid
from core.models.trade_ticket import TradeTicket, TradeTicketEffect
from core.repositories.base import BaseRepository

# Canonical lowercase vocabularies (ADR-0128 §1, §2, §3). The columns are
# plain TEXT with CHECK constraints behind them; the vocabularies are
# mirrored here so a bad value fails as a typed domain error rather than as
# a driver IntegrityError.
_VALID_STATUSES: frozenset[str] = frozenset(
    {
        "draft",
        "proposed",
        "approved",
        "sent",
        "acknowledged",
        "executed",
        "booked",
        "cancelled",
    }
)
_VALID_EFFECT_TYPES: frozenset[str] = frozenset(
    {"position_txn", "cashflow", "nav", "investment_update"}
)

# The unique constraint that guarantees tenant-sequential numbering; a
# concurrent allocation collides on it and the write is retried once.
_TICKET_NUMBER_CONSTRAINT = "uq_trade_tickets_tenant_ticket_number"

#: Columns :meth:`TradeTicketRepository.update_draft` may write.
#:
#: Everything the composer collects, and nothing else. Excluded: ``id`` and
#: ``tenant_id`` (identity), ``ticket_number`` (allocated once, at creation),
#: ``created_by`` / ``created_at`` (provenance), ``updated_at`` (set by the
#: method itself), and every column belonging to a lifecycle station —
#: ``status``, the three actor/timestamp pairs, ``cancelled_at`` and
#: ``cancel_reason``. Those stations are :meth:`set_status`'s alone; a draft
#: has no cancellation to describe, so exposing ``cancel_reason`` here would
#: create a second writer for a transition column.
_UPDATABLE_DRAFT_FIELDS: frozenset[str] = frozenset(
    {
        "kind",
        "direction",
        "investment_id",
        "cash_investment_id",
        "trade_date",
        "settlement_date",
        "units",
        "price_per_unit",
        "gross_amount",
        "fees",
        "taxes",
        "net_amount",
        "currency",
        "commitment_amount",
        "master_data",
        "set_inactive",
        "note",
        "source",
        "case_id",
    }
)

#: The attribution columns each target status requires, per the b034 CHECKs
#: ``ck_trade_tickets_{proposed,approved,booked}_attribution``. A status
#: absent from this map needs no attribution — ``draft`` and the three
#: ADR-0129 channel states, which no v1 transition writes.
_STATUS_ATTRIBUTION: dict[str, tuple[str, str]] = {
    "proposed": ("proposed_by", "proposed_at"),
    "approved": ("approved_by", "approved_at"),
    "booked": ("booked_by", "booked_at"),
}

_TICKET_COLUMNS = (
    TradeTicket.id,
    TradeTicket.tenant_id,
    TradeTicket.ticket_number,
    TradeTicket.kind,
    TradeTicket.direction,
    TradeTicket.status,
    TradeTicket.investment_id,
    TradeTicket.cash_investment_id,
    TradeTicket.trade_date,
    TradeTicket.settlement_date,
    TradeTicket.units,
    TradeTicket.price_per_unit,
    TradeTicket.gross_amount,
    TradeTicket.fees,
    TradeTicket.taxes,
    TradeTicket.net_amount,
    TradeTicket.currency,
    TradeTicket.commitment_amount,
    TradeTicket.master_data,
    TradeTicket.set_inactive,
    TradeTicket.note,
    TradeTicket.source,
    TradeTicket.cancel_reason,
    TradeTicket.case_id,
    TradeTicket.proposed_by,
    TradeTicket.proposed_at,
    TradeTicket.approved_by,
    TradeTicket.approved_at,
    TradeTicket.booked_by,
    TradeTicket.booked_at,
    TradeTicket.cancelled_at,
    TradeTicket.created_by,
    TradeTicket.created_at,
    TradeTicket.updated_at,
)

_EFFECT_COLUMNS = (
    TradeTicketEffect.id,
    TradeTicketEffect.tenant_id,
    TradeTicketEffect.ticket_id,
    TradeTicketEffect.effect_type,
    TradeTicketEffect.effect_id,
    TradeTicketEffect.prior_state,
    TradeTicketEffect.emitted_at,
)


@dataclass(frozen=True)
class TradeTicketDTO:
    """Plain data-only view of a ``trade_tickets`` row.

    ``master_data`` is opaque JSONB — the U-NEW / R-COMMIT / R-SEC-BUY
    master-data inventory the ticket carries until booking emits the
    ``investments`` row (MD-12 / MD-15, decision record §2.5). ``units`` is
    unsigned; the direction's sign is applied at emission.
    """

    id: UUID
    tenant_id: UUID
    ticket_number: int
    kind: str
    direction: str
    status: str
    investment_id: UUID | None
    cash_investment_id: UUID | None
    trade_date: date
    settlement_date: date | None
    units: Decimal | None
    price_per_unit: Decimal | None
    gross_amount: Decimal | None
    fees: Decimal | None
    taxes: Decimal | None
    net_amount: Decimal | None
    currency: str
    commitment_amount: Decimal | None
    master_data: dict | None
    set_inactive: bool
    note: str | None
    source: str | None
    cancel_reason: str | None
    case_id: UUID | None
    proposed_by: UUID | None
    proposed_at: datetime | None
    approved_by: UUID | None
    approved_at: datetime | None
    booked_by: UUID | None
    booked_at: datetime | None
    cancelled_at: datetime | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TradeTicketEffectDTO:
    """Plain data-only view of a ``trade_ticket_effects`` row.

    ``effect_id`` is an unconstrained UUID: the ledger stays ignorant of the
    layer above it, and the referenced row may legitimately be gone
    (ADR-0128 §2). ``prior_state`` is the opaque before-image of an updated
    ``investments`` row, present only for ``effect_type='investment_update'``.
    """

    id: UUID
    tenant_id: UUID
    ticket_id: UUID
    effect_type: str
    effect_id: UUID
    prior_state: dict | None
    emitted_at: datetime


@dataclass(frozen=True)
class EffectInput:
    """One effect to record against a ticket (input to :meth:`add_effects`).

    Args:
        effect_type: One of ``position_txn`` / ``cashflow`` / ``nav`` /
            ``investment_update``.
        effect_id: The id of the emitted row. Unconstrained by design.
        prior_state: The before-image, for ``investment_update`` only.
    """

    effect_type: str
    effect_id: UUID
    prior_state: dict | None = None


def _is_ticket_number_conflict(exc: IntegrityError) -> bool:
    """Return True when ``exc`` is the tenant ticket-number unique violation.

    Reads asyncpg's ``constraint_name`` off the wrapped error, falling back
    to a substring match so a hand-built test error is recognised too.
    """
    constraint = getattr(getattr(exc, "orig", None), "constraint_name", None)
    if constraint == _TICKET_NUMBER_CONSTRAINT:
        return True
    return _TICKET_NUMBER_CONSTRAINT in str(exc)


def _validate_status(status: str) -> None:
    """Raise :class:`TicketStateInvalid` if ``status`` is outside ADR-0128 §3."""
    if status not in _VALID_STATUSES:
        raise TicketStateInvalid(
            f"Invalid ticket status {status!r}; expected one of {sorted(_VALID_STATUSES)}.",
            field="status",
        )


def _validate_effect_type(effect_type: str) -> None:
    """Raise :class:`TicketStateInvalid` if ``effect_type`` is outside ADR-0128 §2."""
    if effect_type not in _VALID_EFFECT_TYPES:
        raise TicketStateInvalid(
            f"Invalid effect type {effect_type!r}; expected one of {sorted(_VALID_EFFECT_TYPES)}.",
            field="effect_type",
        )


class TradeTicketRepository(BaseRepository):
    """Create, read, edit and advance trade tickets in the active tenant."""

    async def _active_tenant(self) -> UUID:
        """Return the tenant bound to the active session (``app.tenant_id``)."""
        row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        return row.scalar_one()

    async def _attempt_create_draft(
        self,
        *,
        active_tenant: UUID,
        values: dict[str, Any],
    ) -> TradeTicketDTO:
        """One draft-creation attempt, with the ticket-number allocation.

        Wrapped in a SAVEPOINT so a ticket-number collision rolls back just
        this attempt, leaving the caller's transaction intact for the retry.
        ``ticket_number`` is allocated in-SQL as
        ``COALESCE(MAX(ticket_number), 0) + 1`` over the tenant's rows — the
        scalar subquery runs under RLS, so the MAX is tenant-scoped.
        """
        async with self._session.begin_nested():
            next_number = select(
                func.coalesce(func.max(TradeTicket.ticket_number), 0) + 1
            ).scalar_subquery()
            stmt = (
                insert(TradeTicket)
                .values(
                    tenant_id=active_tenant,
                    ticket_number=next_number,
                    status="draft",
                    **values,
                )
                .returning(*_TICKET_COLUMNS)
            )
            row = (await self._session.execute(stmt)).one()
            ticket = TradeTicketDTO(**row._mapping)
        return ticket

    async def create_draft(
        self,
        *,
        kind: str,
        direction: str,
        currency: str,
        trade_date: date,
        created_by: UUID,
        investment_id: UUID | None = None,
        cash_investment_id: UUID | None = None,
        settlement_date: date | None = None,
        units: Decimal | None = None,
        price_per_unit: Decimal | None = None,
        gross_amount: Decimal | None = None,
        fees: Decimal | None = None,
        taxes: Decimal | None = None,
        net_amount: Decimal | None = None,
        commitment_amount: Decimal | None = None,
        master_data: dict | None = None,
        set_inactive: bool = False,
        note: str | None = None,
        source: str | None = None,
        case_id: UUID | None = None,
        now: datetime,
    ) -> TradeTicketDTO:
        """Create a ticket in ``draft`` — the one creation path.

        A ticket is always born a draft: no other status is creatable, so
        every ticket that ever reached ``booked`` has a draft row's history
        behind it (ADR-0128 §3). The row is written on the first explicit
        user gesture — Continue, Save as draft, Propose or Book now — never
        on opening a composer, so there are no orphaned drafts and no burnt
        numbers (MD-2, decision record §2.1).

        ``ticket_number`` is allocated tenant-sequentially. The
        ``uq_trade_tickets_tenant_ticket_number`` constraint is the
        guarantee; the recovery is a single retry: on a unique violation
        against it the whole attempt (which re-reads ``MAX(ticket_number)``)
        is retried once, and if the retry also collides the error
        propagates. This is the ``case_number`` precedent, verbatim.

        Vocabulary for ``kind`` and ``direction`` is left to the schema
        CHECKs and the service seam — this method neither completes nor
        validates a composer's work.

        Args:
            kind: One of ``order`` / ``commitment`` / ``secondary``.
            direction: ``buy`` or ``sell``.
            currency: The ticket currency (the investment's, F-3).
            trade_date: The booking date of both legs.
            created_by: The user whose gesture created the draft.
            investment_id: The traded investment, or ``None`` while a
                new-instrument draft is mid-wizard (MD-12).
            cash_investment_id: The confirmed settlement position, or
                ``None`` until the user confirms one (MD-3). Must be
                ``None`` for ``kind='commitment'`` (R-3 / MD-19).
            settlement_date: Recorded only; informational in v1 (MD-4).
            units: Unsigned; the sign is applied at emission.
            price_per_unit: Execution price in ``currency``.
            gross_amount: Units × price, or the stated gross for the
                reported kinds that carry no units.
            fees: Transaction costs.
            taxes: Optional split out of ``fees``.
            net_amount: The settlement cash effect.
            commitment_amount: ``commitment`` kind only.
            master_data: The master-data inventory carried until booking
                (MD-12 / MD-15); opaque JSONB.
            set_inactive: The U-SELL full-disposal choice (MD-7).
            note: Free text.
            source: Free text, mirroring the ledger field.
            case_id: The optional Watch Desk → Case → Transactions
                provenance link.
            now: The ``created_at`` / ``updated_at`` timestamp.

        Returns:
            The newly created :class:`TradeTicketDTO`, in ``draft``.
        """
        values: dict[str, Any] = {
            "kind": kind,
            "direction": direction,
            "currency": currency,
            "trade_date": trade_date,
            "created_by": created_by,
            "investment_id": investment_id,
            "cash_investment_id": cash_investment_id,
            "settlement_date": settlement_date,
            "units": units,
            "price_per_unit": price_per_unit,
            "gross_amount": gross_amount,
            "fees": fees,
            "taxes": taxes,
            "net_amount": net_amount,
            "commitment_amount": commitment_amount,
            "master_data": master_data,
            "set_inactive": set_inactive,
            "note": note,
            "source": source,
            "case_id": case_id,
            "created_at": now,
            "updated_at": now,
        }
        active_tenant = await self._active_tenant()
        for attempt in range(2):
            try:
                return await self._attempt_create_draft(
                    active_tenant=active_tenant,
                    values=values,
                )
            except IntegrityError as exc:
                # Retry once on a ticket-number collision; propagate anything
                # else, and propagate a second collision too.
                if attempt == 0 and _is_ticket_number_conflict(exc):
                    continue
                raise
        raise AssertionError("unreachable: the loop returns or raises")

    async def get(self, ticket_id: UUID) -> TradeTicketDTO | None:
        """Return one ticket by id in the active tenant, or ``None``."""
        result = await self._session.execute(select(TradeTicket).where(TradeTicket.id == ticket_id))
        model = result.scalar_one_or_none()
        return _ticket_to_dto(model) if model is not None else None

    async def list_by_status(self, statuses: Sequence[str]) -> list[TradeTicketDTO]:
        """Return the tenant's tickets in ``statuses``, newest number first.

        Ordered by ``ticket_number`` descending — the blotter's order, and
        the only ordering that is stable regardless of how a ticket's
        timestamps were later filled in.

        Args:
            statuses: The statuses to include. An empty sequence returns an
                empty list rather than matching everything.

        Returns:
            Matching tickets, highest ``ticket_number`` first.

        Raises:
            TicketStateInvalid: If any entry is outside the ADR-0128 §3
                vocabulary. Validated before any SQL runs.
        """
        for status in statuses:
            _validate_status(status)
        if not statuses:
            return []
        result = await self._session.execute(
            select(TradeTicket)
            .where(TradeTicket.status.in_(list(statuses)))
            .order_by(TradeTicket.ticket_number.desc())
        )
        return [_ticket_to_dto(model) for model in result.scalars().all()]

    async def update_draft(self, ticket_id: UUID, **fields: Any) -> TradeTicketDTO:
        """Update a ticket that is still in ``draft``.

        The composer's save path. Only ``draft`` rows are touched: a ticket
        that has left ``draft`` is a record, not a form. The two failure
        modes are reported distinctly — a caller must be able to tell "no
        such ticket" from "that ticket is no longer editable", and a silent
        no-op would conflate them.

        ``updated_at`` is set to the database's ``NOW()`` in the same
        statement, so the stamp cannot drift from the write.

        Args:
            ticket_id: The draft to update.
            **fields: Columns to write; see :data:`_UPDATABLE_DRAFT_FIELDS`.
                Lifecycle-station columns are not updatable here — they
                belong to :meth:`set_status`.

        Returns:
            The updated :class:`TradeTicketDTO`.

        Raises:
            ValueError: If a field name is outside the whitelist. That is a
                programming error at the call site, not a domain condition.
            TicketNotFound: If no such ticket exists in the active tenant.
            TicketStateInvalid: If the ticket exists but has left ``draft``.
        """
        unknown = sorted(set(fields) - _UPDATABLE_DRAFT_FIELDS)
        if unknown:
            raise ValueError(
                f"Not updatable on a draft trade ticket: {unknown}. "
                f"Updatable fields are {sorted(_UPDATABLE_DRAFT_FIELDS)}."
            )
        stmt = (
            update(TradeTicket)
            .where(TradeTicket.id == ticket_id, TradeTicket.status == "draft")
            .values(**fields, updated_at=func.now())
            .returning(*_TICKET_COLUMNS)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is not None:
            return TradeTicketDTO(**row._mapping)

        # Nothing was updated: either the row is invisible (absent, or
        # another tenant's — RLS makes those one case) or it is no longer a
        # draft. Distinguish, rather than returning quietly.
        existing = await self.get(ticket_id)
        if existing is None:
            raise TicketNotFound(f"No trade ticket {ticket_id} in this tenant.")
        raise TicketStateInvalid(
            f"Trade ticket {ticket_id} is {existing.status!r}, not 'draft'; "
            "only drafts are editable.",
            field="status",
        )

    async def link_investment(
        self,
        ticket_id: UUID,
        *,
        investment_id: UUID,
        now: datetime,
    ) -> TradeTicketDTO:
        """Record the investment a booking created for this ticket (ADR-0128 §2).

        **Mechanism, not policy.** The method neither decides that a ticket
        should create an investment nor knows which flows do; it writes one
        column, once. The only caller is the booking emission of an
        investment-creating flow (U-NEW / R-COMMIT / R-SEC-BUY), where the
        ``investments`` row is an emission effect rather than a precondition
        (MD-12) and the ticket therefore learns its ``investment_id`` only at
        booking.

        :meth:`update_draft` cannot serve, for two independent reasons: it is
        draft-only, and a ticket being booked is commonly ``proposed`` or
        ``approved``; and it is the *composer's* save path, where a
        whitelisted field may be written repeatedly. This link is written at
        most once.

        Writing twice is refused rather than allowed to overwrite. A ticket
        whose ``investment_id`` moved would leave the first investment with
        no trace of what created it while claiming the second was booked by a
        ticket that never touched it — and ``trade_tickets.investment_id`` is
        ``ON DELETE RESTRICT``, so the orphan could not even be cleaned up.

        Args:
            ticket_id: The ticket to link.
            investment_id: The investment the booking just created.
            now: The new ``updated_at``.

        Returns:
            The updated :class:`TradeTicketDTO`.

        Raises:
            TicketNotFound: If no such ticket exists in the active tenant.
            TicketStateInvalid: If the ticket already names an investment.
        """
        existing = await self.get(ticket_id)
        if existing is None:
            raise TicketNotFound(f"No trade ticket {ticket_id} in this tenant.")
        if existing.investment_id is not None:
            raise TicketStateInvalid(
                f"Trade ticket {existing.ticket_number} already names investment "
                f"{existing.investment_id}; the link a creating booking writes is "
                "written once and never moved.",
                field="investment_id",
            )

        stmt = (
            update(TradeTicket)
            .where(TradeTicket.id == ticket_id)
            .values(investment_id=investment_id, updated_at=now)
            .returning(*_TICKET_COLUMNS)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:  # pragma: no cover — :meth:`get` just saw the row
            raise TicketNotFound(f"No trade ticket {ticket_id} in this tenant.")
        return TradeTicketDTO(**row._mapping)

    async def set_status(
        self,
        ticket_id: UUID,
        *,
        status: str,
        actor_user_id: UUID | None = None,
        now: datetime,
        cancel_reason: str | None = None,
    ) -> TradeTicketDTO:
        """Move a ticket to ``status``, writing that station's attribution.

        **Mechanism, not policy.** The method does not restrict the source
        state and does not decide which transitions are legal — that is the
        service seam's concern (ADR-0128 §3, P-2). What it guarantees is
        that a station is never reached without its attribution: reaching
        ``proposed`` writes ``proposed_by`` / ``proposed_at``, ``approved``
        writes ``approved_by`` / ``approved_at``, ``booked`` writes
        ``booked_by`` / ``booked_at``, and ``cancelled`` writes
        ``cancelled_at`` plus any ``cancel_reason``. The matching b034
        CHECKs are the backstop; if ``actor_user_id`` is omitted for a
        station that needs one, the database refuses the row.

        The three ADR-0129 channel states (``sent`` / ``acknowledged`` /
        ``executed``) are accepted by the vocabulary and need no
        attribution of their own — no v1 transition reaches them.

        Args:
            ticket_id: The ticket to advance.
            status: The target status (ADR-0128 §3 vocabulary).
            actor_user_id: The acting user, for a station that records one.
            now: The station timestamp and the new ``updated_at``.
            cancel_reason: Recorded when cancelling.

        Returns:
            The updated :class:`TradeTicketDTO`.

        Raises:
            TicketStateInvalid: If ``status`` is outside the vocabulary.
            TicketNotFound: If no such ticket exists in the active tenant.
        """
        _validate_status(status)
        values: dict[str, Any] = {"status": status, "updated_at": now}
        attribution = _STATUS_ATTRIBUTION.get(status)
        if attribution is not None:
            actor_column, timestamp_column = attribution
            values[actor_column] = actor_user_id
            values[timestamp_column] = now
        elif status == "cancelled":
            values["cancelled_at"] = now
            if cancel_reason is not None:
                values["cancel_reason"] = cancel_reason

        stmt = (
            update(TradeTicket)
            .where(TradeTicket.id == ticket_id)
            .values(**values)
            .returning(*_TICKET_COLUMNS)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            raise TicketNotFound(f"No trade ticket {ticket_id} in this tenant.")
        return TradeTicketDTO(**row._mapping)

    async def add_effects(
        self,
        ticket_id: UUID,
        effects: Sequence[EffectInput],
    ) -> list[TradeTicketEffectDTO]:
        """Record the ledger rows a ticket emitted (ADR-0128 §2).

        Called inside the booking service's transaction, alongside the
        status flip, so a booked ticket and its effect list are written
        together or not at all. The rows themselves are the ledger's; this
        only enumerates them, which is what makes them reversible.

        ``tenant_id`` is copied from the ticket rather than re-derived from
        the session, so an effect can never be attributed to a different
        tenant than the ticket it belongs to.

        Args:
            ticket_id: The ticket the effects belong to.
            effects: The emitted rows. An empty sequence is a no-op.

        Returns:
            The newly written effect rows, in the order given.

        Raises:
            TicketStateInvalid: If any ``effect_type`` is outside the
                ADR-0128 §2 vocabulary. Validated before any SQL runs.
            TicketNotFound: If no such ticket exists in the active tenant.
        """
        for effect in effects:
            _validate_effect_type(effect.effect_type)
        ticket = await self.get(ticket_id)
        if ticket is None:
            raise TicketNotFound(f"No trade ticket {ticket_id} in this tenant.")
        if not effects:
            return []

        written: list[TradeTicketEffectDTO] = []
        for effect in effects:
            stmt = (
                insert(TradeTicketEffect)
                .values(
                    tenant_id=ticket.tenant_id,
                    ticket_id=ticket_id,
                    effect_type=effect.effect_type,
                    effect_id=effect.effect_id,
                    prior_state=effect.prior_state,
                )
                .returning(*_EFFECT_COLUMNS)
            )
            row = (await self._session.execute(stmt)).one()
            written.append(TradeTicketEffectDTO(**row._mapping))
        return written

    async def list_effects(self, ticket_id: UUID) -> list[TradeTicketEffectDTO]:
        """Return a ticket's effects, oldest ``emitted_at`` first.

        ``id`` is the tie-break: a booking writes every effect in one
        transaction, so the timestamps are commonly identical and the
        ordering would otherwise be arbitrary.
        """
        result = await self._session.execute(
            select(TradeTicketEffect)
            .where(TradeTicketEffect.ticket_id == ticket_id)
            .order_by(TradeTicketEffect.emitted_at.asc(), TradeTicketEffect.id.asc())
        )
        return [_effect_to_dto(model) for model in result.scalars().all()]

    async def delete_effects_for_ticket(self, ticket_id: UUID) -> int:
        """Delete a ticket's effect rows and return how many were removed.

        The bookkeeping half of a reversal: once the emitted ledger rows are
        gone the linkage records a booking that no longer exists. Undoing
        the ledger itself is the service's concern — this layer does not
        reach into it (ADR-0128 §2).
        """
        result = await self._session.execute(
            delete(TradeTicketEffect).where(TradeTicketEffect.ticket_id == ticket_id)
        )
        return result.rowcount or 0


def _ticket_to_dto(model: TradeTicket) -> TradeTicketDTO:
    return TradeTicketDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        ticket_number=model.ticket_number,
        kind=model.kind,
        direction=model.direction,
        status=model.status,
        investment_id=model.investment_id,
        cash_investment_id=model.cash_investment_id,
        trade_date=model.trade_date,
        settlement_date=model.settlement_date,
        units=model.units,
        price_per_unit=model.price_per_unit,
        gross_amount=model.gross_amount,
        fees=model.fees,
        taxes=model.taxes,
        net_amount=model.net_amount,
        currency=model.currency,
        commitment_amount=model.commitment_amount,
        master_data=model.master_data,
        set_inactive=model.set_inactive,
        note=model.note,
        source=model.source,
        cancel_reason=model.cancel_reason,
        case_id=model.case_id,
        proposed_by=model.proposed_by,
        proposed_at=model.proposed_at,
        approved_by=model.approved_by,
        approved_at=model.approved_at,
        booked_by=model.booked_by,
        booked_at=model.booked_at,
        cancelled_at=model.cancelled_at,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _effect_to_dto(model: TradeTicketEffect) -> TradeTicketEffectDTO:
    return TradeTicketEffectDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        ticket_id=model.ticket_id,
        effect_type=model.effect_type,
        effect_id=model.effect_id,
        prior_state=model.prior_state,
        emitted_at=model.emitted_at,
    )
