# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""TicketService — the trade-ticket workflow seam (ADR-0128 §3, §4).

Where :class:`core.repositories.trade_ticket_repository.TradeTicketRepository`
is *mechanism* — it moves a ticket between stations and records what a
booking emitted — this service is **policy**: which transitions are legal,
what "complete enough to be proposed" means for each flow, and what the
composer should be warned about. The two are deliberately separate so the
rules live in exactly one place rather than being half-enforced by a schema
CHECK and half-restated in a route.

Scope so far (S1 + S2a)
-----------------------
``create_draft`` · ``update_draft`` · ``propose`` · ``cancel`` (S1) and
``book`` for ``order``-kind tickets (S2a). The emission itself lives in
:mod:`services.transactions.emission`; what stays here is the policy around
it — which statuses may book, the re-validation, and the lifecycle walk.
The remaining omissions are structural rather than accidental:

* **No ``approve`` gesture.** In v1 the "Book now" gesture traverses
  ``proposed → approved → booked`` implicitly, writing both actor columns
  (ADR-0128 Q-6, D-4 permits ``approved_by == proposed_by``). A distinct
  approval gesture arrives with four-eyes enforcement, which is a
  tenant-scoped setting — a rule change, not a migration, because the
  columns and transitions already exist.
* **No reported-kind emission.** R-COMMIT / R-SEC-BUY / R-SEC-SELL also
  write cashflow and NAV rows, and the creating flows write the
  ``investments`` row itself (MD-12). :meth:`book` refuses them loudly until
  **S2b** fills the kind dispatch.
* **No reversal.** Cancelling a ``booked`` ticket deletes its enumerated
  effects in one DB transaction (ADR-0128 §6) and is **S2c** — which is why
  :meth:`cancel` refuses ``booked`` outright rather than flipping a status
  and orphaning a ledger.
* **No routes, no templates, no module registration.** S3/S4.

The service holds no session of its own: the caller opens
``core.repositories.tenant_context(...)`` and hands in repositories built on
that session, exactly as :class:`services.investments.InvestmentService`
does. Every method therefore runs inside the caller's transaction, which is
what lets S2 wrap a propose-and-book in one atomic unit later.

Temporal grounding
------------------
``now`` and ``today`` are **parameters, never clock reads** (ADR-0127). A
service that reads the wall clock cannot be tested at a date boundary and
cannot be replayed; the caller — a route, a test, an importer — owns what
time it is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from core.exceptions import (
    CurrencyMismatchError,
    NonNegativeHoldingsError,
    TicketIncomplete,
    TicketNotFound,
    TicketStateInvalid,
)
from core.repositories.instrument_price_repository import InstrumentPriceRepository
from core.repositories.investment_repository import InvestmentDTO, InvestmentRepository
from core.repositories.position_transaction_repository import (
    PositionTransactionRepository,
)
from core.repositories.trade_ticket_repository import (
    TradeTicketDTO,
    TradeTicketRepository,
)
from services.investments.aum import CASH_TYPE
from services.investments.holdings import first_negative_holding_date, holdings_as_of
from services.investments.investment_service import InvestmentService
from services.transactions.constants import (
    BLOCK_MISSING_ANLV,
    BLOCK_MISSING_PRICE,
    BOOKABLE_STATUSES,
    CANCEL_REASON_REQUIRED_STATUSES,
    CANCELLABLE_STATUSES,
    DIRECTION_BUY,
    DIRECTION_SELL,
    DIRECTIONS,
    INCOMPLETE_COMMITMENT_SHAPE,
    INCOMPLETE_INACTIVE_CASH_POSITION,
    INCOMPLETE_MISSING_AMOUNT,
    INCOMPLETE_MISSING_CANCEL_REASON,
    INCOMPLETE_MISSING_CASH_POSITION,
    INCOMPLETE_MISSING_COMMITMENT_AMOUNT,
    INCOMPLETE_MISSING_INVESTMENT,
    INCOMPLETE_MISSING_MASTER_DATA,
    INCOMPLETE_MISSING_UNITS,
    KIND_COMMITMENT,
    KIND_ORDER,
    KIND_SECONDARY,
    KINDS,
    MD_ACQUIRED_NAV,
    MD_ANLV_CODE,
    MD_CURRENCY,
    MD_NAME,
    PRICE_DEVIATION_WARN_RATIO,
    STATUS_APPROVED,
    STATUS_BOOKED,
    STATUS_CANCELLED,
    STATUS_DRAFT,
    STATUS_PROPOSED,
    WARNING_FUTURE_TRADE_DATE,
    WARNING_NEGATIVE_CASH,
    WARNING_NET_NON_POSITIVE,
    WARNING_PRICE_DEVIATION,
)
from services.transactions.emission import emit_order
from services.transactions.validation import (
    TicketWarning,
    TicketWarnings,
    derive_cash_effect,
    is_cash_moving,
    is_investment_creating,
    nearest_price,
    price_deviation_ratio,
)


@dataclass(frozen=True)
class _HypotheticalSell:
    """The as-yet-unwritten sell leg, shaped for the holdings check.

    Structurally satisfies
    :class:`services.investments.holdings.LedgerTransaction`, so the
    candidate participates in :func:`first_negative_holding_date` alongside
    the persisted rows — the same idiom as ``InvestmentService``'s own
    ledger candidate. It is restated rather than imported because the shared
    seam is the *protocol*, not a private class in another service; the
    summation itself is never restated.

    ``created_at`` is the caller's ``now``, so the candidate sorts after
    existing same-day rows (it would be the newest write), and ``id`` is a
    fresh UUID purely to complete the total order.
    """

    txn_type: str
    trade_date: _date
    units: Decimal
    created_at: datetime
    id: UUID


def _cleanup_new_instrument_shell(ticket: TradeTicketDTO) -> None:
    """No-op seam for the U-NEW shell clean-up (ADR-0128 §6).

    Under MD-12 no investment row exists before booking, so v1 drafts
    have no shell to remove and this seam intentionally does nothing.
    S2c fills it for the reversal path, where ADR-0128 §6's clean-up
    clause becomes reachable.
    """


class TicketService:
    """Compose, validate and advance trade tickets in the active tenant.

    All repositories must be tenant-scoped: the caller constructs them from
    a session obtained via :func:`core.repositories.tenant_context`. The
    service neither sets nor reads ``app.tenant_id`` — that lives on the
    session, and RLS does the rest.

    Only :attr:`tickets` is always required. The other three are wired
    per-flow and guarded by ``_require_*`` helpers that fail loudly: a
    caller that reaches a check without having wired its repository is a
    programming error, not a user error, and a silent fallback there would
    turn a missing dependency into a *passed* validation — the one failure
    mode a validation seam must never have. A commitment propose, for
    instance, legitimately needs nothing but ``tickets``.

    Args:
        tickets: The trade-ticket repository. Always required.
        investments: Needed to resolve the traded investment and the
            settlement cash position.
        position_transactions: Needed for the oversell block and the
            negative-cash warning — both read the ledger.
        instrument_prices: Needed for the price-deviation warning.
        investment_service: Needed to **book** (S2a). The emission writes
            every ledger row through this one seam and never through a
            repository — see :meth:`book`. Validation and cancellation need
            none of it, so it stays optional like the rest.
    """

    def __init__(
        self,
        tickets: TradeTicketRepository,
        investments: InvestmentRepository | None = None,
        position_transactions: PositionTransactionRepository | None = None,
        instrument_prices: InstrumentPriceRepository | None = None,
        investment_service: InvestmentService | None = None,
    ) -> None:
        self._tickets = tickets
        self._investments = investments
        self._position_transactions = position_transactions
        self._instrument_prices = instrument_prices
        self._investment_service = investment_service

    # -- dependency guards --------------------------------------------------

    def _require_investments(self) -> InvestmentRepository:
        """Return the wired investment repository or fail loudly."""
        if self._investments is None:
            raise RuntimeError(
                "TicketService was constructed without an investment "
                "repository; investment and settlement-position checks are "
                "unavailable."
            )
        return self._investments

    def _require_position_transactions(self) -> PositionTransactionRepository:
        """Return the wired position-transaction repository or fail loudly."""
        if self._position_transactions is None:
            raise RuntimeError(
                "TicketService was constructed without a position-transaction "
                "repository; the oversell block and the negative-cash warning "
                "are unavailable."
            )
        return self._position_transactions

    def _require_instrument_prices(self) -> InstrumentPriceRepository:
        """Return the wired instrument-price repository or fail loudly."""
        if self._instrument_prices is None:
            raise RuntimeError(
                "TicketService was constructed without an instrument-price "
                "repository; the price-deviation warning is unavailable."
            )
        return self._instrument_prices

    def _require_investment_service(self) -> InvestmentService:
        """Return the wired investment service or fail loudly.

        Booking has no repository-level fallback by design (D-A): the ledger
        write seam carries the ADR-0130 non-negativity decision and the
        ADR-0098 materialisation trigger, and a booking that quietly went
        around it would emit rows that look right and leave the NAV series
        stale. Refusing loudly is the only safe behaviour.
        """
        if self._investment_service is None:
            raise RuntimeError(
                "TicketService was constructed without an investment service; "
                "booking is unavailable. The emission writes every ledger row "
                "through InvestmentService.add_position_transaction and has no "
                "repository-level fallback (ADR-0128 §2, ADR-0130)."
            )
        return self._investment_service

    # -- vocabulary and shape ----------------------------------------------

    @staticmethod
    def _validate_kind(kind: str) -> None:
        """Raise :class:`TicketStateInvalid` for a kind outside ADR-0128 §1."""
        if kind not in KINDS:
            raise TicketStateInvalid(
                f"Invalid ticket kind {kind!r}; expected one of {sorted(KINDS)}.",
                field="kind",
            )

    @staticmethod
    def _validate_direction(direction: str) -> None:
        """Raise :class:`TicketStateInvalid` for a direction outside ADR-0128 §1."""
        if direction not in DIRECTIONS:
            raise TicketStateInvalid(
                f"Invalid ticket direction {direction!r}; expected one of {sorted(DIRECTIONS)}.",
                field="direction",
            )

    @staticmethod
    def _validate_commitment_shape(
        *,
        kind: str | None,
        direction: str | None,
        cash_investment_id: UUID | None,
    ) -> None:
        """Enforce R-3 / MD-19 before the row reaches the CHECK.

        A commitment is always a ``buy`` and books no cash leg: money moves
        with the capital calls, which stay ordinary cashflows outside the
        ticket object. ``ck_trade_tickets_commitment_shape`` says the same
        thing in SQL, but a driver ``IntegrityError`` names a constraint
        rather than a rule, so the service refuses first.

        Args:
            kind: The ticket kind, or ``None`` when not being set.
            direction: The direction, or ``None`` when not being set.
            cash_investment_id: The settlement position, or ``None``.

        Raises:
            TicketIncomplete: If a commitment is a sell, or names a
                settlement position.
        """
        if kind != KIND_COMMITMENT:
            return
        if direction is not None and direction != DIRECTION_BUY:
            raise TicketIncomplete(
                "A commitment ticket is always a 'buy' (R-3 / MD-19); "
                f"got direction {direction!r}.",
                identifier=INCOMPLETE_COMMITMENT_SHAPE,
                field="direction",
            )
        if cash_investment_id is not None:
            raise TicketIncomplete(
                "A commitment ticket books no cash leg and must name no "
                "settlement position (R-3 / MD-19); capital calls remain "
                "ordinary cashflows outside the ticket.",
                identifier=INCOMPLETE_COMMITMENT_SHAPE,
                field="cash_investment_id",
            )

    # -- draft composition --------------------------------------------------

    async def create_draft(
        self,
        *,
        kind: str,
        direction: str,
        currency: str,
        trade_date: _date,
        created_by: UUID,
        now: datetime,
        investment_id: UUID | None = None,
        cash_investment_id: UUID | None = None,
        settlement_date: _date | None = None,
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
    ) -> TradeTicketDTO:
        """Create a ticket in ``draft`` — the one creation path.

        **A draft may be arbitrarily sparse.** The row is written on the
        first explicit user gesture (Continue, Save as draft, Propose, Book
        now — never on opening a composer, MD-2), which may be very early
        indeed: a U-NEW wizard's step 1 creates a draft with no investment,
        no units and no price. Completeness is :meth:`propose`'s business,
        because ``proposed`` is what means "complete and validated"
        (ADR-0128 §3).

        What *is* enforced here is vocabulary and one structural rule — the
        commitment shape — because both are true of the row regardless of
        how far the composer has got, and both would otherwise surface as a
        driver ``IntegrityError`` naming a constraint instead of a rule.

        Args:
            kind: One of ``order`` / ``commitment`` / ``secondary``.
            direction: ``buy`` or ``sell``.
            currency: The ticket currency (the investment's, F-3).
            trade_date: The booking date of both legs.
            created_by: The user whose gesture created the draft.
            now: The ``created_at`` / ``updated_at`` timestamp.
            investment_id: The traded investment, or ``None`` for a
                creating flow (MD-12).
            cash_investment_id: The confirmed settlement position (MD-3).
            settlement_date: Recorded only; informational in v1 (MD-4).
            units: Unsigned; the sign is applied at emission.
            price_per_unit: Execution price in ``currency``.
            gross_amount: Gross consideration.
            fees: Transaction costs.
            taxes: Optional split out of ``fees``.
            net_amount: The settlement cash effect.
            commitment_amount: ``commitment`` kind only.
            master_data: The master-data payload carried until booking.
            set_inactive: The U-SELL full-disposal choice (MD-7).
            note: Free text.
            source: Free text, mirroring the ledger field.
            case_id: Optional Watch Desk → Case → Transactions provenance.

        Returns:
            The newly created ticket, in ``draft``.

        Raises:
            TicketStateInvalid: If ``kind`` or ``direction`` is outside its
                vocabulary.
            TicketIncomplete: If a commitment is not shaped like one.
        """
        self._validate_kind(kind)
        self._validate_direction(direction)
        self._validate_commitment_shape(
            kind=kind,
            direction=direction,
            cash_investment_id=cash_investment_id,
        )
        return await self._tickets.create_draft(
            kind=kind,
            direction=direction,
            currency=currency,
            trade_date=trade_date,
            created_by=created_by,
            investment_id=investment_id,
            cash_investment_id=cash_investment_id,
            settlement_date=settlement_date,
            units=units,
            price_per_unit=price_per_unit,
            gross_amount=gross_amount,
            fees=fees,
            taxes=taxes,
            net_amount=net_amount,
            commitment_amount=commitment_amount,
            master_data=master_data,
            set_inactive=set_inactive,
            note=note,
            source=source,
            case_id=case_id,
            now=now,
        )

    async def update_draft(self, ticket_id: UUID, **fields: object) -> TradeTicketDTO:
        """Update a ticket that is still in ``draft``.

        The composer's save path, with the same vocabulary and shape checks
        :meth:`create_draft` applies — run against **the fields provided**,
        which is what the caller is asserting about the row. Draft-only
        enforcement and the updatable-field whitelist stay the repository's
        (a ticket that has left ``draft`` is a record, not a form).

        A combination that spans stored and provided values — setting
        ``kind='commitment'`` on a draft that already names a settlement
        position — is caught by ``ck_trade_tickets_commitment_shape``, and
        in any case by :meth:`propose`, which re-validates the whole ticket
        before any status moves. Re-reading the row here to pre-empt that
        would buy a nicer message for an unreachable composer state at the
        price of a second source of truth for the same rule.

        Args:
            ticket_id: The draft to update.
            **fields: Columns to write; the repository's whitelist applies.

        Returns:
            The updated ticket.

        Raises:
            TicketStateInvalid: If a provided ``kind`` / ``direction`` is
                outside its vocabulary, or the ticket has left ``draft``.
            TicketIncomplete: If the provided fields make a malformed
                commitment.
            TicketNotFound: If no such ticket exists in the active tenant.
            ValueError: If a field name is outside the repository's
                whitelist — a programming error at the call site.
        """
        kind = fields.get("kind")
        direction = fields.get("direction")
        if kind is not None:
            self._validate_kind(str(kind))
        if direction is not None:
            self._validate_direction(str(direction))
        cash_investment_id = fields.get("cash_investment_id")
        self._validate_commitment_shape(
            kind=str(kind) if kind is not None else None,
            direction=str(direction) if direction is not None else None,
            cash_investment_id=(
                cash_investment_id if isinstance(cash_investment_id, UUID) else None
            ),
        )
        return await self._tickets.update_draft(ticket_id, **fields)

    # -- propose ------------------------------------------------------------

    async def propose(
        self,
        ticket_id: UUID,
        *,
        proposed_by: UUID,
        now: datetime,
        today: _date,
    ) -> tuple[TradeTicketDTO, TicketWarnings]:
        """Advance a draft to ``proposed``, blocking on gaps and warning on risks.

        The transition that gives ``proposed`` its meaning: "complete and
        validated" (ADR-0128 §3). The split between refusing and warning is
        D-2's — blocks are reserved for inputs that would corrupt the book's
        invariants; everything merely *probably unwise* warns and books
        anyway, because the users are professional portfolio managers and
        the contents of a transaction are theirs.

        Order of operations, and it matters: load, check the status, run
        **all** block checks, collect **all** warnings, and only then flip
        the status. Nothing is written when a block fires — the ticket is
        still a ``draft`` afterwards and the composer can be re-shown. The
        warnings are collected exhaustively rather than short-circuited so a
        composer can show them together.

        The blocks (working document §3 under the MD refinements):

        1. **Completeness per kind**, including the settlement position a
           cash-moving flow needs (MD-3: explicitly confirmed, never
           defaulted) and the master-data payload a creating flow carries in
           place of an ``investments`` row (MD-12).
        2. **Currency mismatch** against the traded investment (F-3). No
           silent conversion — that lives at the ADR-0099 §4 reporting seam.
           Has no UI state (MD-8); this guard is the whole enforcement.
        3. **Oversell** of the instrument leg (ADR-0097 §4), guarded
           unconditionally (ADR-0128 Q-2 relaxes the guard for *cash*
           positions at emission, never for the instrument).
        4. **The AnlV gate**, on investment-creating flows only (MD-21).

        Args:
            ticket_id: The draft to propose.
            proposed_by: The proposing user; written to ``proposed_by``.
            now: The ``proposed_at`` timestamp and the new ``updated_at``.
            today: The current date, injected by the caller (ADR-0127) — the
                reference for the future-trade-date warning.

        Returns:
            A tuple of the updated ticket and the collected warnings.

        Raises:
            TicketNotFound: If no such ticket exists in the active tenant.
            TicketStateInvalid: If the ticket is not a ``draft``.
            TicketIncomplete: If a required field or payload entry is
                missing for the ticket's flow.
            CurrencyMismatchError: If the ticket, its investment and its
                settlement position do not agree on a currency.
            NonNegativeHoldingsError: If the sell would drive holdings below
                zero on any date.
        """
        ticket = await self._tickets.get(ticket_id)
        if ticket is None:
            raise TicketNotFound(f"No trade ticket {ticket_id} in this tenant.")
        if ticket.status != STATUS_DRAFT:
            raise TicketStateInvalid(
                f"Trade ticket {ticket.ticket_number} is {ticket.status!r}, not "
                f"{STATUS_DRAFT!r}; only a draft can be proposed.",
                field="status",
            )

        await self._run_blocks(ticket, now=now)
        warnings = await self._collect_warnings(ticket, today=today)

        updated = await self._tickets.set_status(
            ticket_id,
            status=STATUS_PROPOSED,
            actor_user_id=proposed_by,
            now=now,
        )
        return updated, warnings

    # -- book ---------------------------------------------------------------

    async def book(
        self,
        ticket_id: UUID,
        *,
        booked_by: UUID,
        now: datetime,
        today: _date,
    ) -> tuple[TradeTicketDTO, TicketWarnings]:
        """Emit a ticket's ledger effects and land it in ``booked``.

        The transition that makes a ticket a fact. Everything before it is
        intent; this is where the book changes.

        Order of operations, and every step of it is load-bearing:

        1. Load the ticket and check its status — ``draft`` / ``proposed`` /
           ``approved`` may book, nothing else. A ``booked`` ticket is
           already a fact and a ``cancelled`` one is a decision reversed.
        2. Dispatch on kind. Only ``order`` emits here; the reported kinds
           are S2b.
        3. Run the **full** propose-time block set again. This is not
           belt-and-braces: an approved ticket may have been sitting for a
           week while its holdings moved, and MD-11 / MD-21 put the gates at
           Propose *and* Book precisely so that the second station re-asks
           the question against the book as it stands now. A stale ticket is
           refused here, with nothing written.
        4. Collect warnings — exhaustively, never blocking (D-2). A U-BUY
           that overdraws its cash position books and warns (ADR-0130).
        5. Emit. Both legs go through the one sanctioned write seam.
        6. Record the effects, then traverse the lifecycle to ``booked``.

        **Atomicity (ADR-0128 §2).** Steps 5 to 6 run on the caller's one
        context-scoped session and nothing in the chain commits, so the
        emitted ledger rows, the ``trade_ticket_effects`` linkage and the
        status flip land together or not at all. There is deliberately no
        ``try``/``except`` anywhere in the path: a failure propagates, the
        caller's ``tenant_context`` block rolls back, and the book is
        exactly as it was. Catching and compensating would be strictly worse
        — it would have to reproduce, in application code, the guarantee the
        transaction already gives for free.

        The lifecycle traversal is implicit (ADR-0128 Q-6): a draft booked
        directly passes through ``proposed`` and ``approved`` on the way, so
        the b034 attribution CHECKs — which require every earlier station's
        columns on a booked row — are satisfied by construction. Stations
        that already carry an actor keep it: a ticket proposed by A and
        booked by B records exactly that, because overwriting A would erase
        the only evidence that two people were involved.

        Args:
            ticket_id: The ticket to book.
            booked_by: The booking user. Written to ``booked_by``, to any
                unattributed earlier station, and to ``created_by`` on every
                emitted ledger row.
            now: The station timestamps and the new ``updated_at``.
            today: The current date, injected by the caller (ADR-0127) — the
                reference for the future-trade-date warning.

        Returns:
            A tuple of the booked ticket and the warnings the booking
            carried. The warnings are informational: by the time they are
            returned the booking has happened.

        Raises:
            TicketNotFound: If no such ticket exists in the active tenant.
            TicketStateInvalid: If the ticket is already booked or cancelled.
            TicketIncomplete: If a required field is missing, the settlement
                position is unusable, or ``set_inactive`` was asked for on
                something that is not a full disposal (MD-7).
            CurrencyMismatchError: If the ticket, its investment and its
                settlement position do not agree on a currency.
            NonNegativeHoldingsError: If the sell would drive the
                instrument's holdings below zero. Never raised for the cash
                leg — ADR-0130 exempts cash on every write path.
            NotImplementedError: For a non-``order`` kind, until S2b.
        """
        ticket = await self._tickets.get(ticket_id)
        if ticket is None:
            raise TicketNotFound(f"No trade ticket {ticket_id} in this tenant.")
        if ticket.status not in BOOKABLE_STATUSES:
            raise TicketStateInvalid(
                f"Trade ticket {ticket.ticket_number} is {ticket.status!r} and "
                f"cannot be booked; only {list(BOOKABLE_STATUSES)} can. A booked "
                "ticket is reversed, not re-booked (ADR-0128 §6).",
                field="status",
            )
        if ticket.kind != KIND_ORDER:
            # A loud placeholder, not a domain error: the ticket is fine and
            # the user did nothing wrong — the code is simply not written yet.
            raise NotImplementedError("Reported-kind emission lands in S2b")

        await self._run_blocks(ticket, now=now)
        warnings = await self._collect_warnings(ticket, today=today)

        effect = self._cash_effect(ticket)
        if effect is None:
            # Unreachable behind _block_completeness, which has just proved
            # units and price are present. Reported rather than assumed away.
            raise RuntimeError(
                f"Trade ticket {ticket.ticket_number} passed completeness but "
                "states no derivable cash effect; this is a bug in the "
                "completeness rules, not a user error."
            )

        effects = await emit_order(
            ticket,
            investment_service=self._require_investment_service(),
            position_transactions=self._require_position_transactions(),
            investments=self._require_investments(),
            booked_by=booked_by,
            cash_effect=effect,
        )
        await self._tickets.add_effects(ticket.id, effects)

        # Q-6: one set_status call per station, in order, writing the booking
        # actor only where a station has none. `set_status` writes exactly one
        # station's attribution per call, so the walk cannot be collapsed.
        if ticket.proposed_by is None:
            await self._tickets.set_status(
                ticket_id, status=STATUS_PROPOSED, actor_user_id=booked_by, now=now
            )
        if ticket.approved_by is None:
            await self._tickets.set_status(
                ticket_id, status=STATUS_APPROVED, actor_user_id=booked_by, now=now
            )
        updated = await self._tickets.set_status(
            ticket_id, status=STATUS_BOOKED, actor_user_id=booked_by, now=now
        )
        return updated, warnings

    # -- blocks -------------------------------------------------------------

    async def _run_blocks(self, ticket: TradeTicketDTO, *, now: datetime) -> None:
        """Run every propose-time block, raising on the first failure.

        ``trade_date`` and ``currency`` are ``NOT NULL`` in the schema and
        so are asserted by the DTO's own types rather than re-validated
        here: a check that cannot fail is noise that reads like a guarantee.
        """
        creating = is_investment_creating(
            kind=ticket.kind,
            direction=ticket.direction,
            investment_id=ticket.investment_id,
            master_data=ticket.master_data,
        )
        cash_moving = is_cash_moving(kind=ticket.kind)

        await self._block_completeness(ticket, creating=creating, cash_moving=cash_moving)
        if not creating:
            await self._block_currency_mismatch(ticket)
        await self._block_oversell(ticket, now=now)
        self._block_missing_anlv(ticket, creating=creating)

    async def _block_completeness(
        self,
        ticket: TradeTicketDTO,
        *,
        creating: bool,
        cash_moving: bool,
    ) -> None:
        """Refuse a ticket whose flow is missing a field it cannot book without."""
        if creating:
            self._require_master_data(ticket)
        elif ticket.investment_id is None:
            raise TicketIncomplete(
                "This flow books against an existing investment but the ticket names none.",
                identifier=INCOMPLETE_MISSING_INVESTMENT,
                field="investment_id",
            )

        if ticket.kind == KIND_ORDER:
            if ticket.units is None:
                raise TicketIncomplete(
                    "An order ticket needs a unit quantity.",
                    identifier=INCOMPLETE_MISSING_UNITS,
                    field="units",
                )
            if ticket.price_per_unit is None:
                raise TicketIncomplete(
                    "An order ticket needs an execution price.",
                    identifier=BLOCK_MISSING_PRICE,
                    field="price_per_unit",
                )
        elif ticket.kind == KIND_SECONDARY:
            self._require_secondary_amounts(ticket)
        elif ticket.kind == KIND_COMMITMENT:
            if ticket.commitment_amount is None or ticket.commitment_amount <= 0:
                raise TicketIncomplete(
                    "A commitment ticket needs a positive commitment amount.",
                    identifier=INCOMPLETE_MISSING_COMMITMENT_AMOUNT,
                    field="commitment_amount",
                )

        if cash_moving:
            await self._require_settlement_position(ticket)

    def _require_master_data(self, ticket: TradeTicketDTO) -> None:
        """Refuse a creating flow whose master-data payload cannot build a row.

        Name and currency are the minimum an ``investments`` row cannot be
        emitted without (decision record §2.5); the payload's currency must
        also *be* the ticket's, since the investment's currency is what the
        ticket's has to equal (F-3) and there is no conversion in a write
        path.
        """
        payload: Mapping[str, object] = ticket.master_data or {}
        if not payload.get(MD_NAME) or not payload.get(MD_CURRENCY):
            raise TicketIncomplete(
                "This flow creates the investment at booking, so its master "
                f"data must carry at least {MD_NAME!r} and {MD_CURRENCY!r} "
                "(MD-12, decision record §2.5).",
                identifier=INCOMPLETE_MISSING_MASTER_DATA,
                field="master_data",
            )
        payload_currency = payload[MD_CURRENCY]
        if payload_currency != ticket.currency:
            raise CurrencyMismatchError(
                f"Ticket currency {ticket.currency!r} differs from the master "
                f"data's {payload_currency!r}; the ticket currency is the "
                "investment's (F-3) and nothing converts in a write path.",
                field="currency",
            )

    def _require_secondary_amounts(self, ticket: TradeTicketDTO) -> None:
        """Refuse a secondary ticket that does not state what changed hands.

        A buy states its purchase price (``gross_amount``) and the stake's
        value at transfer (``acquired_nav`` in the payload); the two
        legitimately differ, and that difference is exactly what post-trade
        analysis wants (MD-20 — context, never judgement). A sell states its
        proceeds, net or gross.
        """
        if ticket.direction == DIRECTION_BUY:
            if ticket.gross_amount is None:
                raise TicketIncomplete(
                    "A secondary purchase needs its purchase price.",
                    identifier=INCOMPLETE_MISSING_AMOUNT,
                    field="gross_amount",
                )
            payload: Mapping[str, object] = ticket.master_data or {}
            if payload.get(MD_ACQUIRED_NAV) is None:
                raise TicketIncomplete(
                    "A secondary purchase needs the acquired NAV — the stake's "
                    f"value at transfer ({MD_ACQUIRED_NAV!r} in the master data).",
                    identifier=INCOMPLETE_MISSING_MASTER_DATA,
                    field="master_data",
                )
        elif ticket.net_amount is None and ticket.gross_amount is None:
            raise TicketIncomplete(
                "A secondary sale needs its proceeds, net or gross.",
                identifier=INCOMPLETE_MISSING_AMOUNT,
                field="net_amount",
            )

    async def _require_settlement_position(self, ticket: TradeTicketDTO) -> None:
        """Refuse a cash-moving flow without a usable settlement position.

        The settlement position is **always an explicit, user-confirmed
        choice** (D-1 / MD-3, decision record §2.2): NULL means "not yet
        confirmed", never "pick one for me", and no default-selection logic
        exists anywhere in the platform. It must be an actual cash position
        (ADR-0100) in the ticket's currency — a mismatch would make the cash
        leg a silent FX conversion, which is precisely what ADR-0099 keeps
        out of write paths — and it must be **active** (D-F): a retired
        position is not somewhere a trade can settle, and reviving it by
        writing to it would undo a deliberate gesture.

        The inactive case is a *distinct* identifier rather than a second
        ``missing_cash_position``, because that identifier is the structured
        signal the S4 surface turns into an inline "create a cash position"
        offer — and creating a second position is the wrong remedy when the
        right one exists and is merely retired.

        Shared by propose and book (MD-11 / MD-21), so every one of these
        refusals applies at both stations.
        """
        if ticket.cash_investment_id is None:
            raise TicketIncomplete(
                "This flow settles against a cash position but none is "
                "confirmed on the ticket (MD-3: no default is ever picked).",
                identifier=INCOMPLETE_MISSING_CASH_POSITION,
                field="cash_investment_id",
            )
        cash = await self._require_investments().get_by_id(ticket.cash_investment_id)
        if cash is None or cash.investment_type != CASH_TYPE:
            raise TicketIncomplete(
                f"Settlement position {ticket.cash_investment_id} is not a cash "
                f"position (investment_type={CASH_TYPE!r}).",
                identifier=INCOMPLETE_MISSING_CASH_POSITION,
                field="cash_investment_id",
            )
        if not cash.is_active:
            raise TicketIncomplete(
                f"Settlement position {cash.name!r} has been deactivated and "
                "cannot settle a trade; pick a live cash position.",
                identifier=INCOMPLETE_INACTIVE_CASH_POSITION,
                field="cash_investment_id",
            )
        if cash.currency != ticket.currency:
            raise CurrencyMismatchError(
                f"Settlement position {cash.name!r} is in {cash.currency!r} but "
                f"the ticket is in {ticket.currency!r}; PortfoliFLOW never "
                "converts on your behalf.",
                field="currency",
            )

    async def _block_currency_mismatch(self, ticket: TradeTicketDTO) -> None:
        """Refuse a ticket whose currency differs from its investment's (F-3).

        ADR-0097 §5: a ledger row's currency **must equal** the
        investment's, and the write path fails loudly rather than
        converting — a silent FX conversion point inside a write path is an
        audit hazard. This is a service-layer guard with no reachable UI
        state (MD-8): the composer derives the currency from the selected
        investment and shows it read-only.
        """
        investment = await self._load_investment(ticket)
        if investment.currency != ticket.currency:
            raise CurrencyMismatchError(
                f"Ticket currency {ticket.currency!r} differs from investment "
                f"{investment.name!r}'s {investment.currency!r}; no silent "
                "conversion happens in a write path (F-3, ADR-0097 §5).",
                field="currency",
            )

    async def _load_investment(self, ticket: TradeTicketDTO) -> InvestmentDTO:
        """Load the traded investment, which completeness has already required.

        A ``None`` here means the ``investment_id`` FK points at a row that
        is not visible in the active tenant — which RLS plus the FK make
        unreachable in practice. It is reported rather than assumed away,
        because assuming it away is how a validation seam silently passes.
        """
        assert ticket.investment_id is not None  # guaranteed by _block_completeness
        investment = await self._require_investments().get_by_id(ticket.investment_id)
        if investment is None:
            raise TicketIncomplete(
                f"Investment {ticket.investment_id} is not visible in this tenant.",
                identifier=INCOMPLETE_MISSING_INVESTMENT,
                field="investment_id",
            )
        return investment

    async def _block_oversell(self, ticket: TradeTicketDTO, *, now: datetime) -> None:
        """Refuse a unit sale that would drive holdings below zero (ADR-0097 §4).

        The candidate sell is appended to the persisted ledger and the whole
        thing handed to
        :func:`services.investments.holdings.first_negative_holding_date` —
        the same pure derivation the per-investment CRUD uses, so the two
        write paths cannot disagree about what a holding is. The scan is
        per-transaction rather than per-day, so an intra-day overdraw is
        caught even when a later same-day buy would restore it.

        The instrument leg keeps this guard **unconditionally** (ADR-0128
        Q-2): the relaxation Q-2 decides applies to *cash* positions at
        emission time, never here.
        """
        if ticket.kind != KIND_ORDER or ticket.direction != DIRECTION_SELL:
            return
        assert ticket.investment_id is not None  # guaranteed by _block_completeness
        assert ticket.units is not None  # guaranteed by _block_completeness

        ledger = await self._require_position_transactions().list_for_investment(
            ticket.investment_id
        )
        candidate = _HypotheticalSell(
            txn_type="sell",
            trade_date=ticket.trade_date,
            units=-ticket.units,
            created_at=now,
            id=uuid4(),
        )
        offending = first_negative_holding_date([*ledger, candidate])
        if offending is not None:
            raise NonNegativeHoldingsError(
                f"Selling {ticket.units} units on {ticket.trade_date} would "
                f"drive holdings below zero on {offending} for investment "
                f"{ticket.investment_id}; short positions are out of scope "
                "(ADR-0097 §4).",
                field="units",
            )

    @staticmethod
    def _block_missing_anlv(ticket: TradeTicketDTO, *, creating: bool) -> None:
        """Refuse an investment-creating flow with no AnlV category (MD-11, MD-21).

        Regulatory correctness over convenience: the gate applies to U-NEW,
        R-COMMIT and R-SEC-BUY on both Propose and Book now, because those
        create the investment row and this is the moment its classification
        is decided. U-BUY / U-SELL / R-SEC-SELL touch existing investments
        and carry **no** gate — an existing investment with a NULL
        ``anlv_code`` proposes fine, since the gate is not retroactive
        housekeeping.

        It is a transition guard, never a schema constraint (decision record
        §2.8): ``investments.anlv_code`` stays nullable, and ``Save as
        draft`` is untouched by it (MD-11) — only a *dangling* draft is
        permitted, because ``proposed`` means complete.
        """
        if not creating:
            return
        payload: Mapping[str, object] = ticket.master_data or {}
        if not payload.get(MD_ANLV_CODE):
            raise TicketIncomplete(
                "This flow creates an investment, so its AnlV category must be "
                "set before the ticket leaves 'draft' (MD-11, MD-21).",
                identifier=BLOCK_MISSING_ANLV,
                field="master_data",
            )

    # -- warnings -----------------------------------------------------------

    async def _collect_warnings(
        self,
        ticket: TradeTicketDTO,
        *,
        today: _date,
    ) -> TicketWarnings:
        """Collect every applicable warning. **Never** raises for a warning.

        Exhaustive by construction: one ticket can be several kinds of
        unwise at once, and a composer shows them together.
        """
        collected: list[TicketWarning] = []
        effect = self._cash_effect(ticket)

        negative_cash = await self._warn_negative_cash(ticket, effect=effect)
        if negative_cash is not None:
            collected.append(negative_cash)

        deviation = await self._warn_price_deviation(ticket)
        if deviation is not None:
            collected.append(deviation)

        if ticket.direction == DIRECTION_SELL and effect is not None and effect <= 0:
            collected.append(
                TicketWarning(
                    identifier=WARNING_NET_NON_POSITIVE,
                    data={"net_amount": effect, "currency": ticket.currency},
                )
            )

        if ticket.trade_date > today:
            collected.append(
                TicketWarning(
                    identifier=WARNING_FUTURE_TRADE_DATE,
                    data={"trade_date": ticket.trade_date, "today": today},
                )
            )

        return TicketWarnings(warnings=tuple(collected))

    @staticmethod
    def _cash_effect(ticket: TradeTicketDTO) -> Decimal | None:
        """The cash this ticket moves, via the one shared derivation."""
        return derive_cash_effect(
            direction=ticket.direction,
            net_amount=ticket.net_amount,
            gross_amount=ticket.gross_amount,
            units=ticket.units,
            price_per_unit=ticket.price_per_unit,
            fees=ticket.fees,
            taxes=ticket.taxes,
        )

    async def _warn_negative_cash(
        self,
        ticket: TradeTicketDTO,
        *,
        effect: Decimal | None,
    ) -> TicketWarning | None:
        """Warn when the purchase takes the settlement position below zero.

        **Booking is never refused for this** (D-2, OP-06 struck by MD-5):
        the warning states the resulting balance and steps aside. The
        persistent indicator that follows is S5's, and it derives from the
        live balance at read time — there is no stored flag anywhere
        (decision record §2.4), so the state clears itself when the balance
        returns to ≥ 0 without any acknowledgement gesture.

        Cash is unitised at price 1.0000 (F-2, ADR-0103), so the position's
        balance *is* its holdings and the same pure derivation applies.
        """
        if not is_cash_moving(kind=ticket.kind) or ticket.direction != DIRECTION_BUY:
            return None
        if effect is None or ticket.cash_investment_id is None:
            return None

        ledger = await self._require_position_transactions().list_for_investment(
            ticket.cash_investment_id
        )
        balance = holdings_as_of(ledger, ticket.trade_date)
        resulting = balance - effect
        if resulting >= 0:
            return None
        return TicketWarning(
            identifier=WARNING_NEGATIVE_CASH,
            data={"resulting_balance": resulting, "currency": ticket.currency},
        )

    async def _warn_price_deviation(self, ticket: TradeTicketDTO) -> TicketWarning | None:
        """Warn when the execution price is far from the nearest stored price.

        A fixed 5 % threshold (ADR-0128 Q-4), deliberately uncoupled from
        the watchpoint machinery, and never a block: the stored price may
        simply be stale and the user's execution price is the better fact.
        An empty series produces no warning — a missing price is not
        suspicious, it is just missing.
        """
        if ticket.kind != KIND_ORDER:
            return None
        if ticket.price_per_unit is None or ticket.investment_id is None:
            return None

        points = await self._require_instrument_prices().list_by_investment(ticket.investment_id)
        reference = nearest_price(points, ticket.trade_date)
        if reference is None:
            return None
        ratio = price_deviation_ratio(
            price=ticket.price_per_unit,
            reference=reference.price,
        )
        if ratio is None or ratio <= PRICE_DEVIATION_WARN_RATIO:
            return None
        return TicketWarning(
            identifier=WARNING_PRICE_DEVIATION,
            data={
                "reference_price": reference.price,
                "reference_date": reference.as_of_date,
                "deviation_ratio": ratio,
            },
        )

    # -- cancel -------------------------------------------------------------

    async def cancel(
        self,
        ticket_id: UUID,
        *,
        cancelled_by: UUID,
        now: datetime,
        reason: str | None = None,
    ) -> TradeTicketDTO:
        """Cancel a ticket that has not been booked. Terminal.

        Only ``draft`` / ``proposed`` / ``approved`` cancel this way. A
        ``booked`` ticket is **reversed**, not cancelled: its enumerated
        effects are deleted in one DB transaction and the ledger is undone
        (ADR-0128 §6) — that is S2c, and refusing it here is what keeps a
        status flip from orphaning emitted rows.

        A reason is required from ``proposed`` and ``approved`` and optional
        from ``draft``: a draft is private workspace, while a proposal is a
        decision others may have seen, so withdrawing it is explained.

        Args:
            ticket_id: The ticket to cancel.
            cancelled_by: The cancelling user. The b034 schema records no
                ``cancelled_by`` column — attribution for this station comes
                from the audit engine via the session's ``app.user_id`` — so
                this is passed through to ``set_status`` and makes the
                caller's actor explicit rather than implicit.
            now: The ``cancelled_at`` timestamp and the new ``updated_at``.
            reason: Why; stored in ``cancel_reason``.

        Returns:
            The cancelled ticket.

        Raises:
            TicketNotFound: If no such ticket exists in the active tenant.
            TicketStateInvalid: If the ticket's status is not cancellable.
            TicketIncomplete: If a reason is required and none was given.
        """
        ticket = await self._tickets.get(ticket_id)
        if ticket is None:
            raise TicketNotFound(f"No trade ticket {ticket_id} in this tenant.")
        if ticket.status not in CANCELLABLE_STATUSES:
            raise TicketStateInvalid(
                f"Trade ticket {ticket.ticket_number} is {ticket.status!r} and "
                f"cannot be cancelled; only {list(CANCELLABLE_STATUSES)} can. "
                "A booked ticket is reversed, not cancelled (ADR-0128 §6).",
                field="status",
            )
        if ticket.status in CANCEL_REASON_REQUIRED_STATUSES and not reason:
            raise TicketIncomplete(
                f"Cancelling a {ticket.status!r} ticket requires a reason.",
                identifier=INCOMPLETE_MISSING_CANCEL_REASON,
                field="cancel_reason",
            )

        _cleanup_new_instrument_shell(ticket)

        return await self._tickets.set_status(
            ticket_id,
            status=STATUS_CANCELLED,
            actor_user_id=cancelled_by,
            now=now,
            cancel_reason=reason,
        )


__all__ = ["TicketService"]
