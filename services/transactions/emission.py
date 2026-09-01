# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Emission engine for booked trade tickets (ADR-0128 §2).

Where :mod:`services.transactions.ticket_service` decides *whether* a ticket
may book, this module decides *what a booking writes*. The split matters
because the two answer to different pressures: the policy seam grows a rule
per flow, while the emission stays a small, mostly-pure derivation from the
ticket's columns to ledger rows — and a derivation that can be read in one
sitting is the only kind that can be audited against a statement.

What emission means here
------------------------
An ``order``-kind ticket (U-BUY / U-SELL) settles as **two ledger rows** —
the instrument leg and its cash leg — written **atomically** (ADR-0128 §2).
Atomicity is structural rather than defended: every write goes through the
caller's one context-scoped session, nothing in the chain commits, and this
module contains no ``try``/``except`` at all. A failure anywhere — a CHECK,
a guard, a completeness refusal raised *after* the first leg is written —
propagates out of :func:`emit_order`, and the ``tenant_context`` block the
caller opened rolls the partial write back. There is no half-booked state to
clean up because there is no state until the block exits.

The single sanctioned seam (D-A)
--------------------------------
Every ledger row is written through
:meth:`services.investments.investment_service.InvestmentService.add_position_transaction`
— never through :class:`~core.repositories.position_transaction_repository.PositionTransactionRepository`
directly. That seam carries two things this module must not restate: the
ADR-0130 non-negativity decision (the instrument leg is guarded, a cash
target is exempt — see below) and the ADR-0098 §3 computed-NAV
materialisation trigger, which fires once per unitised leg in the same
transaction. The repository is still passed in, for *reads* only: the
holdings check behind MD-7 needs the ledger as it stands after the
instrument leg.

ADR-0130 end to end
-------------------
A U-BUY may take the settlement position below zero and still book. Nothing
in this module implements that — it is the decision the write seam already
carries, per *target* rather than per *caller*, which is exactly why there
is no flag to pass and no branch here to get wrong. The composer is warned
(``negative_cash``) and the book records the overdraft, because a negative
cash balance is an economic fact rather than an impossible state. The
instrument leg keeps the guard unconditionally: you cannot sell units you do
not hold.

Sign conventions (working document §2.1 / §2.2, D-B / D-C)
----------------------------------------------------------
:func:`services.transactions.validation.derive_cash_effect` yields a
*magnitude*: what the cash position gives up on a buy and receives on a
sell. Direction is applied here, once, and the **cash leg's direction
follows the sign of the cash effect, not the ticket's** (D-B) — a sell whose
costs exceed its gross moves cash *out*, and the ledger must say so. The
signed effect is recorded as the instrument leg's ``consideration`` (D-C);
the cash leg carries none, because a cash row's cash effect is its units at
1.0000 and restating it would be a second place for the same number to be
wrong.

Both legs book at ``trade_date``. ``settlement_date`` is informational in v1
(MD-4) and reaches no ledger row.

Provenance (D-D)
----------------
Both rows carry ``ingest_origin='manual'`` (ADR-0128 Q-1: a ticket booking is
a human act, not an ingest) and ``source = "ticket #<n>"``, which is what
makes a ledger row traceable back to the decision that produced it without
the ledger knowing what a ticket is. ``ticket.source`` — the composer's
free-text provenance for the *ticket* — is deliberately not copied down.

Scope
-----
``order`` kinds only. The reported kinds (R-COMMIT / R-SEC-BUY /
R-SEC-SELL), whose emission also writes cashflow and NAV rows and, for the
creating flows, the ``investments`` row itself (MD-12), are S2b; reversal is
S2c and consumes :func:`investment_before_image` from here (D-H).
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import date as _date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from core.exceptions import TicketIncomplete
from core.repositories.investment_repository import InvestmentDTO, InvestmentRepository
from core.repositories.position_transaction_repository import (
    PositionTransactionRepository,
)
from core.repositories.trade_ticket_repository import EffectInput, TradeTicketDTO
from services.investments.holdings import holdings_as_of
from services.investments.investment_service import InvestmentService
from services.transactions.constants import (
    DIRECTION_BUY,
    DIRECTION_SELL,
    INCOMPLETE_SET_INACTIVE_NOT_FULL_DISPOSAL,
    KIND_ORDER,
)

#: The unit price of cash. Cash positions are unitised at 1.0000 (F-2,
#: ADR-0103), which is what makes a cash balance *be* its holdings and lets
#: the same pure derivation serve both kinds of position.
CASH_UNIT_PRICE: Decimal = Decimal("1.0000")

#: The ``ingest_origin`` every ticket-emitted ledger row carries (ADR-0128
#: Q-1). A booking is a deliberate human write, so a later Excel re-import
#: overwrites it as book of record but a live market-data fetch does not.
EMISSION_INGEST_ORIGIN: str = "manual"

#: The ``effect_type`` values this module emits (ADR-0128 §2 vocabulary).
EFFECT_POSITION_TXN: str = "position_txn"
EFFECT_INVESTMENT_UPDATE: str = "investment_update"


@dataclass(frozen=True)
class LegSpec:
    """One ledger row a booking will write, fully derived and not yet written.

    The intermediate that makes the sign and provenance rules testable
    without a database: :func:`order_legs` produces these purely, and
    :func:`emit_order` does nothing but hand them to the write seam. A bug in
    the sign convention is therefore a failing unit test rather than a
    corrupted book.

    ``trade_date`` is deliberately **absent**. Both legs of an order book on
    the ticket's ``trade_date`` (working document §4.1) — carrying it per leg
    would invite a future caller to set them apart, and a two-leg settlement
    split across two dates is not a settlement.

    Attributes:
        investment_id: The position the row lands on — the traded instrument
            or the settlement cash position.
        txn_type: ``buy`` or ``sell``, in the ledger's vocabulary. On the
            cash leg this follows the sign of the cash effect (D-B), which is
            not always the ticket's direction.
        units: Signed, per ``ck_position_transactions_sign``.
        price_per_unit: The execution price; :data:`CASH_UNIT_PRICE` on a
            cash leg.
        consideration: The signed cash effect on the instrument leg, ``None``
            on the cash leg (D-C).
        currency: The ticket's currency, which validation has already proved
            equal to both positions' (F-3, ADR-0097 §5).
        note: The ticket's note, passed through unchanged.
        source: The ticket's provenance string, per :func:`provenance`.
    """

    investment_id: UUID
    txn_type: str
    units: Decimal
    price_per_unit: Decimal
    consideration: Decimal | None
    currency: str
    note: str | None
    source: str


def provenance(ticket: TradeTicketDTO) -> str:
    """Return the ``source`` string a ticket stamps on the rows it emits.

    The one place the format is written. ``position_transactions.source`` is
    free text that nothing parses — the existing producers write
    ``'excel-import'`` and ``'excel-import:cash-statement'`` — so this is a
    human-readable trace, not a join key. The machine-readable linkage is
    ``trade_ticket_effects`` (ADR-0128 §2), which is what a reversal walks.

    Args:
        ticket: The booking ticket.

    Returns:
        ``"ticket #<ticket_number>"``, using the tenant-sequential number the
        operator actually sees rather than the UUID they never do.
    """
    return f"ticket #{ticket.ticket_number}"


def order_legs(
    ticket: TradeTicketDTO,
    *,
    cash_effect: Decimal,
) -> tuple[LegSpec, LegSpec | None]:
    """Derive the instrument leg and its cash leg from an order ticket.

    Pure: no I/O, no clock, no session. The whole sign convention of the
    working document §2.1 / §2.2 lives in these few lines and nowhere else.

    The instrument leg is the ticket read literally — ``units`` signed by the
    direction, at the execution price. The cash leg is the *consequence*, and
    its direction is decided by the sign of the cash effect rather than by
    the ticket's direction (D-B): a sale whose fees exceed its gross proceeds
    takes money out of the settlement position, and a ledger that recorded
    that as an inflow would be lying about a real, if unusual, trade.

    A cash effect of exactly zero produces **no cash leg**. This is not a
    rounding accommodation but a schema fact: ``ck_position_transactions_sign``
    admits no zero-unit ``buy`` or ``sell``, and an event that moves no cash
    is not an event. The ``net_non_positive`` warning has already fired.

    Args:
        ticket: A complete order ticket. Completeness is the service's
            responsibility and has already been established.
        cash_effect: The magnitude from
            :func:`services.transactions.validation.derive_cash_effect` —
            what the cash position gives up on a buy, receives on a sell.

    Returns:
        ``(instrument_leg, cash_leg)``; ``cash_leg`` is ``None`` when the
        ticket moves no cash.

    Raises:
        ValueError: If the ticket is not a complete order ticket. These are
            programmer errors, not user errors: every one of them is
            unreachable behind ``_block_completeness``, and reporting them as
            domain errors would invite a caller to handle what it should
            have prevented.
    """
    if ticket.kind != KIND_ORDER:
        raise ValueError(f"order_legs is for {KIND_ORDER!r} tickets, not {ticket.kind!r}.")
    if ticket.investment_id is None:
        raise ValueError("An order ticket reaching emission must name an investment.")
    if ticket.cash_investment_id is None:
        raise ValueError("An order ticket reaching emission must name a settlement position.")
    if ticket.units is None or ticket.price_per_unit is None:
        raise ValueError("An order ticket reaching emission must carry units and a price.")

    selling = ticket.direction == DIRECTION_SELL
    # One signed number serves both legs: it is the instrument leg's
    # `consideration` and the cash leg's `units`, because the cash the trade
    # moves and the cash the position receives are the same fact seen from
    # two rows.
    signed_effect = cash_effect if selling else -cash_effect
    source = provenance(ticket)

    instrument = LegSpec(
        investment_id=ticket.investment_id,
        txn_type=ticket.direction,
        units=-ticket.units if selling else ticket.units,
        price_per_unit=ticket.price_per_unit,
        consideration=signed_effect,
        currency=ticket.currency,
        note=ticket.note,
        source=source,
    )

    if signed_effect == 0:
        return instrument, None

    cash = LegSpec(
        investment_id=ticket.cash_investment_id,
        txn_type=DIRECTION_BUY if signed_effect > 0 else DIRECTION_SELL,
        units=signed_effect,
        price_per_unit=CASH_UNIT_PRICE,
        # D-C: a cash row's cash effect *is* its units at 1.0000. Restating
        # it here would be a second place for one number to go wrong.
        consideration=None,
        currency=ticket.currency,
        note=ticket.note,
        source=source,
    )
    return instrument, cash


def investment_before_image(dto: InvestmentDTO) -> dict[str, Any]:
    """Return a JSON-safe before-image of an investment row (D-H).

    The ``prior_state`` an ``investment_update`` effect carries, and the only
    thing a reversal has to restore from — so it records the **whole** row
    rather than the field the booking happened to touch. Recording only the
    changed field would make the effect table's usefulness depend on the
    emission remembering to widen it, which is precisely the coupling
    ADR-0128 §2 avoids by enumerating effects rather than describing them.

    The transform is total and lossless-for-restoration: ``UUID``,
    ``Decimal``, ``date`` and ``datetime`` become strings (ISO-8601 for the
    temporal pair), containers are converted recursively, and ``None`` stays
    ``None``. Everything survives ``json.dumps`` because the destination is a
    JSONB column, and ``Decimal`` in particular goes to *string* rather than
    float so a restored amount is the amount that was stored.

    ``datetime`` is tested before ``date`` deliberately — it is a subclass,
    and the reverse order would silently truncate every timestamp to a day.

    Args:
        dto: The investment as it stands *before* the booking's update.

    Returns:
        A plain dict keyed by the DTO's field names.
    """
    return {field.name: _json_safe(getattr(dto, field.name)) for field in fields(dto)}


def _json_safe(value: Any) -> Any:
    """Convert one value to its JSON-safe form; see :func:`investment_before_image`."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_safe(getattr(value, field.name)) for field in fields(value)}
    return str(value)


async def emit_order(
    ticket: TradeTicketDTO,
    *,
    investment_service: InvestmentService,
    position_transactions: PositionTransactionRepository,
    investments: InvestmentRepository,
    booked_by: UUID,
    cash_effect: Decimal,
) -> list[EffectInput]:
    """Write an order ticket's ledger rows and return the effects they are.

    The order of the writes is load-bearing. The **instrument leg goes
    first** so that its ADR-0097 §4 guard fires before any cash row exists:
    were the cash leg written first, an oversell would still be refused, but
    the rollback would be doing work the ordering makes unnecessary. In
    practice ``_block_oversell`` has already refused it — this ordering is
    what keeps that a redundancy rather than the only line of defence.

    Nothing here commits and nothing here catches. The caller's
    ``tenant_context`` block is the transaction boundary, so a raise from any
    step — including the MD-7 refusal below, which fires *after* both legs
    are written — leaves the book exactly as it was (ADR-0128 §2).

    MD-7 (``set_inactive``, D-E) is honoured for a full disposal only. The
    check is deliberately made against the ledger **as it stands after the
    sell**, not against a computed prediction: the question "does this
    investment still hold units?" is answered by the book, and answering it
    any other way would let the two disagree. A partial sale that asks for
    deactivation is refused rather than silently ignored — an inactive
    investment holding units is a corrupted book, and D-2 reserves blocks for
    exactly that.

    Args:
        ticket: The order ticket to emit. Already validated and complete.
        investment_service: The single sanctioned ledger write seam (D-A).
        position_transactions: Read-only here — the ledger the MD-7 holdings
            check reads back.
        investments: Read-only here — the before-image source for D-H.
        booked_by: The booking user; the ``created_by`` of every emitted row.
        cash_effect: The cash magnitude, from the one shared derivation.

    Returns:
        The effects in emission order: the instrument leg, the cash leg if
        one was written, then the ``investment_update`` if MD-7 applied.

    Raises:
        TicketIncomplete: If ``set_inactive`` is set on a ticket that is not
            a full disposal — a buy, or a sale leaving units behind.
        NonNegativeHoldingsError: If the instrument leg would drive holdings
            below zero (ADR-0097 §4, via the write seam).
        CurrencyMismatchError: If a leg's currency differs from its
            position's (ADR-0097 §5, via the write seam).
    """
    if ticket.set_inactive and ticket.direction != DIRECTION_SELL:
        # Refused before any write: a buy is never a disposal, and no ledger
        # read can change that. (D-E)
        raise TicketIncomplete(
            f"Trade ticket {ticket.ticket_number} asks to deactivate its "
            "investment, but a purchase is never a disposal (MD-7).",
            identifier=INCOMPLETE_SET_INACTIVE_NOT_FULL_DISPOSAL,
            field="set_inactive",
        )

    instrument_leg, cash_leg = order_legs(ticket, cash_effect=cash_effect)
    effects: list[EffectInput] = []

    for leg in (instrument_leg, cash_leg):
        if leg is None:
            continue
        created = await investment_service.add_position_transaction(
            investment_id=leg.investment_id,
            txn_type=leg.txn_type,
            trade_date=ticket.trade_date,
            units=leg.units,
            currency=leg.currency,
            ingest_origin=EMISSION_INGEST_ORIGIN,
            created_by=booked_by,
            price_per_unit=leg.price_per_unit,
            consideration=leg.consideration,
            note=leg.note,
            source=leg.source,
        )
        effects.append(EffectInput(effect_type=EFFECT_POSITION_TXN, effect_id=created.id))

    if ticket.set_inactive:
        effects.append(
            await _deactivate_on_full_disposal(
                ticket,
                investment_service=investment_service,
                position_transactions=position_transactions,
                investments=investments,
            )
        )

    return effects


async def _deactivate_on_full_disposal(
    ticket: TradeTicketDTO,
    *,
    investment_service: InvestmentService,
    position_transactions: PositionTransactionRepository,
    investments: InvestmentRepository,
) -> EffectInput:
    """Deactivate the traded investment, refusing anything but a full disposal.

    Split out so :func:`emit_order` reads as the three-step emission it is.
    See that function's docstring for why the holdings check reads the
    written ledger rather than predicting it.
    """
    investment_id = ticket.investment_id
    assert investment_id is not None  # guaranteed by order_legs' preconditions

    ledger = await position_transactions.list_for_investment(investment_id)
    remaining = holdings_as_of(ledger, ticket.trade_date)
    if remaining != 0:
        raise TicketIncomplete(
            f"Trade ticket {ticket.ticket_number} asks to deactivate investment "
            f"{investment_id}, but {remaining} units remain on "
            f"{ticket.trade_date}; an inactive investment holding units is a "
            "corrupted book (MD-7).",
            identifier=INCOMPLETE_SET_INACTIVE_NOT_FULL_DISPOSAL,
            field="set_inactive",
        )

    before = await investments.get_by_id(investment_id)
    if before is None:  # pragma: no cover — the legs above just wrote to it
        raise TicketIncomplete(
            f"Investment {investment_id} is not visible in this tenant.",
            identifier=INCOMPLETE_SET_INACTIVE_NOT_FULL_DISPOSAL,
            field="set_inactive",
        )
    await investment_service.set_investment_active(investment_id, False)
    return EffectInput(
        effect_type=EFFECT_INVESTMENT_UPDATE,
        effect_id=investment_id,
        prior_state=investment_before_image(before),
    )


__all__ = [
    "CASH_UNIT_PRICE",
    "EFFECT_INVESTMENT_UPDATE",
    "EFFECT_POSITION_TXN",
    "EMISSION_INGEST_ORIGIN",
    "LegSpec",
    "emit_order",
    "investment_before_image",
    "order_legs",
    "provenance",
]
