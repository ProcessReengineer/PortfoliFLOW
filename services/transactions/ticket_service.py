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

Scope so far (S1 + S2)
----------------------
``create_draft`` · ``update_draft`` · ``propose`` · ``cancel`` (S1),
``book`` for **all six flows** — U-BUY / U-SELL (S2a) and U-NEW / R-COMMIT /
R-SEC-BUY / R-SEC-SELL (S2b) — and ``reverse`` (S2c). The emission and its
inverse live in :mod:`services.transactions.emission`; what stays here is
the policy around them — which statuses may book or reverse, which flow a
validated ticket routes to, the re-validation, and the lifecycle walk. The
remaining omissions are structural rather than accidental:

* **No ``approve`` gesture.** In v1 the "Book now" gesture traverses
  ``proposed → approved → booked`` implicitly, writing both actor columns
  (ADR-0128 Q-6, D-4 permits ``approved_by == proposed_by``). A distinct
  approval gesture arrives with four-eyes enforcement, which is a
  tenant-scoped setting — a rule change, not a migration, because the
  columns and transitions already exist.
* **No partial reversal.** :meth:`reverse` undoes a booking whole or refuses
  (ADR-0128 §6). There is no gesture for undoing one leg, because a
  half-undone settlement is exactly the state the atomic emission exists to
  make impossible.
* **No routes, no templates, no module registration.** S3/S4.

Two terminal endings, and they are not the same one
---------------------------------------------------
:meth:`cancel` refuses a ``booked`` ticket and :meth:`reverse` accepts
nothing else. Both land on ``cancelled``, and the split is the point: before
booking a ticket is intent, and abandoning intent writes nothing; after
booking it is a fact with a ledger behind it, and undoing a fact means
undoing the ledger in the same transaction. One method that did both would
have to decide, from a status, which of two very different guarantees the
caller was asking for.

Creating flows (S2b)
--------------------
Three of the six flows have no ``investments`` row until they book (MD-12):
the master data rides on the ticket as a JSONB payload and the row is an
*emission effect*. That moves work into this seam, because a payload has no
schema behind it: the blocks below re-ask, at both Propose and Book, the
questions a foreign key would otherwise have answered — is the payload
complete and convertible (D-J / D-V), does the name already exist (D-O), do
the ticket and its payload agree about the commitment (D-U). Everything they
pass is then interpreted exactly once, by
:func:`~services.transactions.emission.parse_master_data`, so a ticket that
validates is a ticket that books.

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

from collections.abc import Mapping, Sequence
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
    ValuationModeError,
)
from core.models.investment import INVESTMENT_TYPES
from core.repositories.audit_log_repository import AuditLogRepository
from core.repositories.instrument_price_repository import InstrumentPriceRepository
from core.repositories.investment_cashflow_repository import InvestmentCashflowRepository
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentDTO, InvestmentRepository
from core.repositories.position_transaction_repository import (
    PositionTransactionRepository,
)
from core.repositories.trade_ticket_repository import (
    EffectInput,
    TradeTicketDTO,
    TradeTicketEffectDTO,
    TradeTicketRepository,
)
from services.investments.aum import CASH_TYPE
from services.investments.holdings import first_negative_holding_date, holdings_as_of
from services.investments.investment_service import InvestmentService
from services.transactions.constants import (
    BLOCK_DUPLICATE_INVESTMENT_NAME,
    BLOCK_INVESTMENT_INACTIVE,
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
from services.transactions.emission import (
    EFFECT_INVESTMENT_UPDATE,
    VALUATION_MODE_REPORTED,
    VALUATION_MODE_UNITISED,
    ReversalReport,
    ShellOutcome,
    check_effects_untouched,
    cleanup_new_investment_shell,
    emit_commitment,
    emit_new_order,
    emit_order,
    emit_secondary_buy,
    emit_secondary_sell,
    parse_master_data,
    reconcile_commitment,
    undo_effects,
)
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


#: The ``valuation_mode`` an **existing** investment must be in for a ticket
#: of each kind to trade it (D-Q).
#:
#: The working document made this implicit and the schema cannot see it: §2.1
#: describes U-BUY / U-SELL as acting on a "unitised; active" position, and
#: F-5 rules out unit arithmetic on a reported one ("no units exist"). A
#: secondary sale is the mirror — a stake valued from GP statements, whose
#: disposal is a NAV write rather than a unit sale. Booking either against the
#: wrong mode would emit rows the investment's own valuation path cannot read.
#:
#: ``commitment`` is absent: it always creates its investment, so no existing
#: row is ever resolved for it. The creating flows set the mode themselves
#: (D-R) rather than checking one.
_REQUIRED_VALUATION_MODE: dict[str, str] = {
    KIND_ORDER: VALUATION_MODE_UNITISED,
    KIND_SECONDARY: VALUATION_MODE_REPORTED,
}


class TicketService:
    """Compose, validate and advance trade tickets in the active tenant.

    All repositories must be tenant-scoped: the caller constructs them from
    a session obtained via :func:`core.repositories.tenant_context`. The
    service neither sets nor reads ``app.tenant_id`` — that lives on the
    session, and RLS does the rest.

    Only :attr:`tickets` is always required. The others are wired per-flow
    and guarded by ``_require_*`` helpers that fail loudly: a caller that
    reaches a check without having wired its repository is a programming
    error, not a user error, and a silent fallback there would turn a
    missing dependency into a *passed* validation — the one failure mode a
    validation seam must never have. A cancellation, for instance,
    legitimately needs nothing but ``tickets``.

    Args:
        tickets: The trade-ticket repository. Always required.
        investments: Needed to resolve the traded investment and the
            settlement cash position, and — since S2b — to refuse a
            creating flow whose name is already taken (D-O).
        position_transactions: Needed for the oversell block and the
            negative-cash warning — both read the ledger.
        instrument_prices: Needed for the price-deviation warning.
        investment_service: Needed to **book**. The emission writes every
            ledger, cashflow, NAV and ``investments`` row through this one
            seam and never through a repository — see :meth:`book`. A
            creating flow whose payload carries an identifier additionally
            needs it wired with an identifier repository (D-L).
        navs: Needed to book a flow that writes a NAV — R-SEC-BUY and
            R-SEC-SELL. Read-only here: the emission writes NAVs through
            ``investment_service``, and this is the collision check that
            keeps the write reversible (D-N). :meth:`reverse` reads it too,
            to establish that an emitted NAV is still there.
        audit_log: Needed to **reverse**. The modification check of ADR-0128
            §6 asks the audit engine, not ``updated_at`` — see
            :func:`~services.transactions.emission.check_effects_untouched`
            for why that column cannot answer. Read-only.
        cashflows: Needed to reverse a flow that wrote a cashflow, and to
            probe a created shell for user rows. Read-only here: the deletes
            go through ``investment_service``.
    """

    def __init__(
        self,
        tickets: TradeTicketRepository,
        investments: InvestmentRepository | None = None,
        position_transactions: PositionTransactionRepository | None = None,
        instrument_prices: InstrumentPriceRepository | None = None,
        investment_service: InvestmentService | None = None,
        navs: InvestmentNavRepository | None = None,
        audit_log: AuditLogRepository | None = None,
        cashflows: InvestmentCashflowRepository | None = None,
    ) -> None:
        self._tickets = tickets
        self._investments = investments
        self._position_transactions = position_transactions
        self._instrument_prices = instrument_prices
        self._investment_service = investment_service
        self._navs = navs
        self._audit_log = audit_log
        self._cashflows = cashflows

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

    def _require_navs(self) -> InvestmentNavRepository:
        """Return the wired NAV repository or fail loudly.

        Used read-only, and only by the flows that write a NAV. The check it
        backs (D-N) is what keeps that write reversible, so a missing
        repository must refuse rather than skip: an emission that silently
        UPSERTed over an existing NAV would look identical and be
        unrecoverable.
        """
        if self._navs is None:
            raise RuntimeError(
                "TicketService was constructed without a NAV repository; the "
                "reported kinds cannot book, because the trade-date collision "
                "check that keeps their NAV write reversible is unavailable "
                "(D-N)."
            )
        return self._navs

    def _require_audit_log(self) -> AuditLogRepository:
        """Return the wired audit-log repository or fail loudly.

        Reversal's modification check has no fallback. Skipping it would let
        a reversal delete a row somebody had corrected since the booking —
        the exact failure ADR-0128 §6 refuses — and falling back to
        ``updated_at`` would be worse than skipping, because it would *look*
        like a check while reading a column two of the four target tables
        never write.
        """
        if self._audit_log is None:
            raise RuntimeError(
                "TicketService was constructed without an audit-log "
                "repository; reversal is unavailable. The check that an "
                "emitted row is unmodified reads the audit engine, because "
                "`updated_at` is not maintained on every target table (D-Y)."
            )
        return self._audit_log

    def _require_cashflows(self) -> InvestmentCashflowRepository:
        """Return the wired cashflow repository or fail loudly.

        Read-only, and needed by every reversal: the presence check runs over
        all four effect types before anything is deleted, and a created
        shell is probed for cashflows before it may go.
        """
        if self._cashflows is None:
            raise RuntimeError(
                "TicketService was constructed without a cashflow "
                "repository; reversal is unavailable. The emitted-row "
                "presence check and the created-shell probe both read it."
            )
        return self._cashflows

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
           place of an ``investments`` row (MD-12) — which since S2b must be
           complete, convertible, free of a name clash, and agreed with the
           ticket about the commitment (D-J / D-V / D-O / D-U).
        2. **The traded investment is a usable target**, for the flows that
           name one: live (D-P) and in the valuation mode the kind books
           against (D-Q), and in the ticket's currency (F-3). No silent
           conversion — that lives at the ADR-0099 §4 reporting seam. The
           currency case has no UI state (MD-8); this guard is the whole
           enforcement.
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
            ValuationModeError: If the traded investment's valuation mode is
                wrong for the ticket's kind (D-Q).
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
        2. Run the **full** propose-time block set again. This is not
           belt-and-braces: an approved ticket may have been sitting for a
           week while its holdings moved, and MD-11 / MD-21 put the gates at
           Propose *and* Book precisely so that the second station re-asks
           the question against the book as it stands now. A stale ticket is
           refused here, with nothing written.
        3. Collect warnings — exhaustively, never blocking (D-2). A U-BUY
           that overdraws its cash position books and warns (ADR-0130).
        4. Emit, dispatching on the flow (:meth:`_emit`). Every row goes
           through the one sanctioned write seam.
        5. Record the effects, then traverse the lifecycle to ``booked``.

        The dispatch is deliberately the *last* thing that happens rather
        than the first: refusing an incomplete ticket is the same refusal
        whichever flow it belongs to, and the blocks are what establish the
        preconditions each emission then assumes. Sorting the flow out first
        would put six variants of "is this bookable" in front of one
        emission each.

        **Atomicity (ADR-0128 §2).** Steps 4 to 5 run on the caller's one
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
            ValuationModeError: If the traded investment's valuation mode is
                wrong for the ticket's kind (D-Q).
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
        await self._run_blocks(ticket, now=now)
        warnings = await self._collect_warnings(ticket, today=today)

        effects = await self._emit(ticket, booked_by=booked_by, now=now)
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

    # -- emission dispatch --------------------------------------------------

    async def _emit(
        self,
        ticket: TradeTicketDTO,
        *,
        booked_by: UUID,
        now: datetime,
    ) -> list[EffectInput]:
        """Route a validated ticket to its flow's emission.

        The six flows of ADR-0128 §1 are ``(kind, direction, creating)``
        triples, and this is the one place the triple is resolved. The third
        element is not decoration: ``order``/``buy`` is U-BUY or U-NEW
        depending on whether an ``investments`` row exists yet (MD-12), and
        the two emit different things.

        A combination outside the six refuses rather than picking the nearest
        match. It is unreachable — ``ck_trade_tickets_kind`` and
        ``..._direction`` close the vocabulary, ``..._commitment_shape``
        closes the commitment's shape — but "unreachable" is a claim about
        today's callers, and the failure it guards against is silent: a
        ``secondary``/``buy`` ticket that already named an investment would,
        under a looser dispatch, create a *second* one.

        Args:
            ticket: The validated ticket. Every block has passed.
            booked_by: The booking user.
            now: The station timestamps and the new ``updated_at``.

        Returns:
            The effects the emission wrote, in emission order.

        Raises:
            TicketStateInvalid: For a ticket that is none of the six flows.
        """
        creating = is_investment_creating(
            kind=ticket.kind,
            direction=ticket.direction,
            investment_id=ticket.investment_id,
            master_data=ticket.master_data,
        )
        investment_service = self._require_investment_service()

        # R-COMMIT first: it is the one flow that moves no cash (MD-19), so
        # deriving a cash effect for it would be deriving a number that has
        # no meaning.
        if ticket.kind == KIND_COMMITMENT:
            if not creating:
                raise self._unroutable(ticket)
            return await emit_commitment(
                ticket,
                master=parse_master_data(ticket.master_data or {}),
                investment_service=investment_service,
                tickets=self._tickets,
                booked_by=booked_by,
                now=now,
            )

        cash_effect = self._required_cash_effect(ticket)

        if ticket.kind == KIND_ORDER:
            if creating:
                return await emit_new_order(
                    ticket,
                    master=parse_master_data(ticket.master_data or {}),
                    investment_service=investment_service,
                    position_transactions=self._require_position_transactions(),
                    investments=self._require_investments(),
                    tickets=self._tickets,
                    booked_by=booked_by,
                    now=now,
                    cash_effect=cash_effect,
                )
            return await emit_order(
                ticket,
                investment_service=investment_service,
                position_transactions=self._require_position_transactions(),
                investments=self._require_investments(),
                booked_by=booked_by,
                cash_effect=cash_effect,
            )

        if ticket.kind == KIND_SECONDARY:
            if ticket.direction == DIRECTION_SELL and not creating:
                return await emit_secondary_sell(
                    ticket,
                    investment_service=investment_service,
                    navs=self._require_navs(),
                    investments=self._require_investments(),
                    booked_by=booked_by,
                    cash_effect=cash_effect,
                )
            if ticket.direction == DIRECTION_BUY and creating:
                return await emit_secondary_buy(
                    ticket,
                    master=parse_master_data(ticket.master_data or {}),
                    investment_service=investment_service,
                    navs=self._require_navs(),
                    tickets=self._tickets,
                    booked_by=booked_by,
                    now=now,
                    cash_effect=cash_effect,
                )

        raise self._unroutable(ticket)

    def _required_cash_effect(self, ticket: TradeTicketDTO) -> Decimal:
        """The cash a cash-moving ticket moves, which completeness has proved derivable."""
        effect = self._cash_effect(ticket)
        if effect is None:
            # Unreachable behind _block_completeness, which has just proved
            # the amounts this flow needs are present. Reported rather than
            # assumed away.
            raise RuntimeError(
                f"Trade ticket {ticket.ticket_number} passed completeness but "
                "states no derivable cash effect; this is a bug in the "
                "completeness rules, not a user error."
            )
        return effect

    @staticmethod
    def _unroutable(ticket: TradeTicketDTO) -> TicketStateInvalid:
        """Build the refusal for a ticket that is none of the six defined flows."""
        return TicketStateInvalid(
            f"Trade ticket {ticket.ticket_number} is {ticket.kind!r} / "
            f"{ticket.direction!r} and names "
            f"{'no investment' if ticket.investment_id is None else 'an investment'}; "
            "that is none of the six flows ADR-0128 §1 defines, so there is "
            "nothing to emit. The b034 CHECKs should have made it unreachable.",
            field="kind",
        )

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
            # Resolves the traded investment and vets it as a target
            # (D-P / D-Q) on the way to the currency comparison — see
            # :meth:`_load_investment`. A creating flow has no target yet.
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
            await self._require_master_data(ticket)
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

    async def _require_master_data(self, ticket: TradeTicketDTO) -> None:
        """Refuse a creating flow whose master-data payload cannot build a row.

        Under MD-12 the payload *is* the ``investments`` row until booking,
        so it has to satisfy, here, everything the table would otherwise have
        enforced for free. Four checks, in cost order — the three that are
        pure run before the one that queries:

        1. **Parseable and complete** (D-J / D-V). The four ``NOT NULL``
           columns must be present and convertible:
           :func:`~services.transactions.emission.parse_master_data` does the
           conversion, and it is the same call the emission makes, so a
           payload that validates is a payload that books.
        2. **A real investment type.** The eight values are read from
           :data:`core.models.investment.INVESTMENT_TYPES` rather than
           restated, so this cannot drift from the CHECK it mirrors.
        3. **The ticket's currency.** The investment's currency is what the
           ticket's has to equal (F-3), and nothing converts in a write path.
        4. **A free name, and one commitment.** ``uq_investments_tenant_name``
           would refuse a duplicate at Book with an ``IntegrityError`` naming
           a constraint; refusing it at Propose names the *rule*, and does so
           while the composer can still act on it (D-O). The commitment
           reconciliation is D-U's.

        The asset class is checked for **shape only** — a UUID, not an
        existing row. This service has no catalogue repository, S4's picker
        offers only real rows, and a bad FK fails loudly at the database; a
        third read to pre-empt an unreachable error would buy a nicer message
        at the price of another dependency in the constructor.
        """
        master = parse_master_data(ticket.master_data or {})

        if master.investment_type not in INVESTMENT_TYPES:
            raise TicketIncomplete(
                f"Master data states investment type {master.investment_type!r}, "
                f"which is not one of the eight canonical values "
                f"{sorted(INVESTMENT_TYPES)}.",
                identifier=INCOMPLETE_MISSING_MASTER_DATA,
                field="master_data",
            )

        if master.currency != ticket.currency:
            raise CurrencyMismatchError(
                f"Ticket currency {ticket.currency!r} differs from the master "
                f"data's {master.currency!r}; the ticket currency is the "
                "investment's (F-3) and nothing converts in a write path.",
                field="currency",
            )

        reconcile_commitment(ticket, master=master)

        clash = await self._require_investments().get_by_name(master.name)
        if clash is not None:
            raise TicketIncomplete(
                f"An investment named {master.name!r} already exists in this "
                "tenant. This flow creates the investment at booking, and names "
                "are the natural key the Excel re-import resolves on, so a "
                "second row with this name cannot be created — pick another "
                "name, or book against the existing investment.",
                identifier=BLOCK_DUPLICATE_INVESTMENT_NAME,
                field="master_data",
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
        """Resolve the traded investment and vet it as a trading target.

        The one place an existing-investment flow's target is loaded, so the
        two properties that make a row tradeable at all are asserted here
        rather than per-caller:

        * **It is live** (D-P). Trading a deactivated investment would revive
          it by writing to it, undoing a deliberate gesture — the same rule
          D-F already applies to the settlement side, and a distinct
          identifier from "no such investment" because the remedies differ.
        * **Its valuation mode fits the kind** (D-Q,
          :data:`_REQUIRED_VALUATION_MODE`). A unit order against a
          statement-valued fund, or a secondary disposal of a unit-dealt one,
          emits rows the investment's own valuation path cannot read.

        A ``None`` means the ``investment_id`` FK points at a row invisible in
        the active tenant — which RLS plus the FK make unreachable in
        practice. It is reported rather than assumed away, because assuming
        it away is how a validation seam silently passes.
        """
        assert ticket.investment_id is not None  # guaranteed by _block_completeness
        investment = await self._require_investments().get_by_id(ticket.investment_id)
        if investment is None:
            raise TicketIncomplete(
                f"Investment {ticket.investment_id} is not visible in this tenant.",
                identifier=INCOMPLETE_MISSING_INVESTMENT,
                field="investment_id",
            )
        if not investment.is_active:
            raise TicketIncomplete(
                f"Investment {investment.name!r} has been deactivated and cannot "
                "be traded; reactivate it first if this trade is real (D-P).",
                identifier=BLOCK_INVESTMENT_INACTIVE,
                field="investment_id",
            )
        required_mode = _REQUIRED_VALUATION_MODE.get(ticket.kind)
        if required_mode is not None and investment.valuation_mode != required_mode:
            raise ValuationModeError(
                f"Investment {investment.name!r} is valued "
                f"{investment.valuation_mode!r}, but a {ticket.kind!r} ticket "
                f"books against a {required_mode!r} position (D-Q, ADR-0097 §1).",
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

    # -- reverse ------------------------------------------------------------

    async def reverse(
        self,
        ticket_id: UUID,
        *,
        cancelled_by: UUID,
        now: datetime,
        reason: str,
    ) -> ReversalReport:
        """Undo a booked ticket's effects and cancel it. Terminal.

        The mirror of :meth:`book`, and the reason :meth:`cancel` refuses a
        ``booked`` ticket outright: after booking there is a ledger, and a
        status flip that left it standing would be a cancellation the book
        had never heard of. ADR-0128 §6 is explicit — after ``booked`` it is
        **reversal, not mutation**, and a correction is a cancel plus a
        re-entry rather than an edit.

        Order of operations, and every step of it is load-bearing:

        1. Load, and refuse anything but ``booked`` (D-AE). A ``draft`` has
           nothing to reverse; a ``cancelled`` ticket has been here already.
        2. Require a reason (D-X). A reversed booking without a stated reason
           is not an audit trail — it is a hole in one. Unlike
           :meth:`cancel`, where a draft may be abandoned silently, there is
           no status this is optional from.
        3. Enumerate the effects. An empty list is a ``RuntimeError``, not a
           quiet success: every flow emits at least one effect, so a booked
           ticket with none is a corrupted book and the honest response is to
           say so rather than to cancel it and call the ledger clean.
        4. Check **all** of them untouched before touching any of them
           (D-Y / D-Z) — the same all-blocks-first order as :meth:`propose`.
        5. Undo: ledger rows, cashflows, NAVs, then the before-image
           restores (D-AA / D-AB).
        6. Clean up a created shell, if this booking made one (D-AC).
        7. Cancel the ticket, with the reason and the actor.

        **Atomicity.** Steps 5 to 7 run on the caller's one context-scoped
        session and nothing in the chain commits, so the ledger deletions,
        the shell decision and the status flip land together or not at all.
        There is no ``try``/``except`` on this path except the single D-AA
        translation inside the emission module; a failure propagates, the
        caller's ``tenant_context`` block rolls back, and the booking is
        exactly as it was.

        **The effects survive.** ``trade_ticket_effects`` rows are never
        deleted (D-AD, T-1 §3). They are FK-less by design, so after a
        reversal they are the record of what this ticket once did — which is
        what a history surface needs and what a deletion would destroy.

        Args:
            ticket_id: The booked ticket to reverse.
            cancelled_by: The reversing user. There is no ``cancelled_by``
                column (T-1 D-5) — the audit engine captures the actor from
                the session's ``app.user_id`` — so this is passed through to
                ``set_status`` and is the ``acting_user`` attributable for
                the materialisation and cash-plan writes the deletions
                trigger.
            now: The ``cancelled_at`` timestamp and the new ``updated_at``.
            reason: Why the booking is being undone. Required, non-blank.

        Returns:
            A :class:`~services.transactions.emission.ReversalReport`: the
            cancelled ticket, the effects that were undone, and — for a
            creating flow — what became of the ``investments`` row it made.

        Raises:
            TicketNotFound: If no such ticket exists in the active tenant.
            TicketStateInvalid: If the ticket is not ``booked``.
            TicketIncomplete: With ``identifier='missing_cancel_reason'`` if
                no reason was given.
            TicketReversalBlocked: If any emitted row has been modified,
                deleted or consumed since the booking. Nothing is written.
            RuntimeError: If a booked ticket enumerates no effects, or more
                than one created investment.
        """
        ticket = await self._tickets.get(ticket_id)
        if ticket is None:
            raise TicketNotFound(f"No trade ticket {ticket_id} in this tenant.")
        if ticket.status != STATUS_BOOKED:
            raise TicketStateInvalid(
                f"Trade ticket {ticket.ticket_number} is {ticket.status!r} and "
                f"cannot be reversed; only {STATUS_BOOKED!r} tickets have "
                "effects to undo. An unbooked ticket is cancelled instead "
                "(ADR-0128 §6).",
                field="status",
            )
        if not reason.strip():
            raise TicketIncomplete(
                f"Reversing booked ticket {ticket.ticket_number} requires a "
                "reason; a booking undone without one leaves no audit trail.",
                identifier=INCOMPLETE_MISSING_CANCEL_REASON,
                field="reason",
            )

        effects = await self._tickets.list_effects(ticket_id)
        if not effects:
            raise RuntimeError(
                f"Booked trade ticket {ticket.ticket_number} enumerates no "
                "effects. Every flow emits at least one, so this is a "
                "corrupted book rather than an empty reversal (ADR-0128 §2)."
            )

        investment_service = self._require_investment_service()
        investments = self._require_investments()
        position_transactions = self._require_position_transactions()
        cashflows = self._require_cashflows()
        navs = self._require_navs()

        await check_effects_untouched(
            effects,
            audit_log=self._require_audit_log(),
            position_transactions=position_transactions,
            cashflows=cashflows,
            navs=navs,
            investments=investments,
        )
        await undo_effects(
            ticket,
            effects,
            investment_service=investment_service,
            investments=investments,
            position_transactions=position_transactions,
            acting_user=cancelled_by,
        )

        shell: ShellOutcome | None = None
        for effect in self._created_investments(ticket, effects):
            shell = await cleanup_new_investment_shell(
                ticket,
                effect.effect_id,
                tickets=self._tickets,
                investment_service=investment_service,
                position_transactions=position_transactions,
                cashflows=cashflows,
                navs=navs,
                investments=investments,
                now=now,
            )

        cancelled = await self._tickets.set_status(
            ticket_id,
            status=STATUS_CANCELLED,
            actor_user_id=cancelled_by,
            now=now,
            cancel_reason=reason,
        )
        return ReversalReport(ticket=cancelled, reversed=tuple(effects), shell=shell)

    @staticmethod
    def _created_investments(
        ticket: TradeTicketDTO,
        effects: Sequence[TradeTicketEffectDTO],
    ) -> list[TradeTicketEffectDTO]:
        """Return the effects marking an ``investments`` row this booking created.

        The D-I encoding, read off the row the reversal is already holding:
        ``investment_update`` with ``prior_state IS NULL`` means *created*,
        with a dict means *updated*. There is at most one per ticket by
        construction — the three creating flows call
        :func:`~services.transactions.emission.create_investment_from_ticket`
        exactly once and the other three call it not at all — so a second is
        a corrupted book and refuses rather than picking one.
        """
        created = [
            effect
            for effect in effects
            if effect.effect_type == EFFECT_INVESTMENT_UPDATE and effect.prior_state is None
        ]
        if len(created) > 1:
            raise RuntimeError(
                f"Trade ticket {ticket.ticket_number} records {len(created)} "
                "created investments; a ticket creates at most one (MD-12)."
            )
        return created

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

        return await self._tickets.set_status(
            ticket_id,
            status=STATUS_CANCELLED,
            actor_user_id=cancelled_by,
            now=now,
            cancel_reason=reason,
        )


__all__ = ["TicketService"]
