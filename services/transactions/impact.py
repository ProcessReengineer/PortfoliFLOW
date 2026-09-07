# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Ticket → overlay mapping for the pre-trade impact panel (S6, ADR-0128 Q-3).

Pure: no repository, no session, no clock. The overlay is the only
computation engine (ADR-0104 §2, F-4); this module only says *what* to hand
it. Two rules live here and nowhere else:

* the sign flip — the emission carries the consideration in the cash view
  (buy negative, D-B); the overlay wants the value view (buy positive), so the
  panel negates, once, here;
* OP-07 (a) — a ticket dated at or before the seam is previewed as if traded
  the day after it. The executor never sees a date ≤ t0.

The artefact is the **impact panel**, never "preview" unqualified:
:meth:`services.transactions.ticket_service.TicketService.preview` is the
composition-time warning set (S4a) and is a different thing, computed at a
different moment, from different inputs. Nothing here calls it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from core.repositories.trade_ticket_repository import TradeTicketDTO
from services.overlay import InsertTransaction
from services.transactions.constants import (
    DIRECTION_BUY,
    KIND_ORDER,
    STATUS_DRAFT,
)
from services.transactions.validation import derive_cash_effect

#: The ticket is still private workspace — propose it first (decision 2).
IMPACT_SCOPE_DRAFT: Final[str] = "draft"

#: A reported flow restates NAV and cash rather than inserting a trade, so
#: ``insert_transaction`` is the wrong transformation for it. A named
#: fast-follow, not a gap the panel should paper over.
IMPACT_SCOPE_REPORTED_KIND: Final[str] = "reported_kind"

#: A creating flow has no ``investments`` row until booking (MD-12), so the
#: plan world does not contain the thing the trade would land on.
IMPACT_SCOPE_CREATING_FLOW: Final[str] = "creating_flow"


def impact_scope(ticket: TradeTicketDTO) -> str | None:
    """Return why this ticket has no impact panel, or ``None`` if it has one.

    The scope gate, stated once so the button, the endpoint and the template
    cannot come to disagree about which tickets are previewable. It returns an
    *identifier* rather than a sentence: the copy is the template's (MD-9), and
    a service string here would be a second place to edit it.

    The three checks in order — status, kind, creating flow — because that is
    the order in which they stop being interesting: a draft's kind does not
    matter yet, and a reported flow's creating-ness does not either.

    A status outside ``{proposed, approved, draft}`` never reaches here: the
    route's ``_in_flight`` gate 404s a booked or cancelled ticket before the
    scope is asked for. So there is no fourth branch and no assertion — the
    gate above is the guarantee.

    Args:
        ticket: The in-flight ticket the panel was asked for.

    Returns:
        One of :data:`IMPACT_SCOPE_DRAFT`, :data:`IMPACT_SCOPE_REPORTED_KIND`,
        :data:`IMPACT_SCOPE_CREATING_FLOW`, or ``None`` when the ticket maps
        onto an ``insert_transaction``.
    """
    if ticket.status == STATUS_DRAFT:
        return IMPACT_SCOPE_DRAFT
    if ticket.kind != KIND_ORDER:
        return IMPACT_SCOPE_REPORTED_KIND
    if ticket.investment_id is None:
        return IMPACT_SCOPE_CREATING_FLOW
    return None


@dataclass(frozen=True)
class ImpactBasis:
    """What the panel feeds the overlay, plus what it must state about it.

    The transformation alone would not be enough to render the panel honestly:
    the reader has to be able to see *that* the date was shifted and *that* the
    costs sit inside ``C``, and both facts are lost once the
    :class:`~services.overlay.contract.InsertTransaction` is built. So they
    travel beside it rather than being re-derived by the route.

    Attributes:
        transformation: The one-element overlay the panel applies.
        effective_date: The date the transformation actually carries — the
            ticket's own trade date, or ``t₀ + 1`` where OP-07 (a) shifted it.
        shifted: Whether OP-07 (a) moved the date. Drives the panel's info
            block; never inferred from a date comparison downstream.
        cash_effect: The **unsigned** magnitude
            :func:`~services.transactions.validation.derive_cash_effect`
            returned — what the panel prints as ``C``.
        costs: ``fees + taxes``, absent halves read as zero. Zero is the
            ordinary case and renders as "none".
        gross: ``units × price_per_unit`` — the value the trade would move if
            the costs sat outside ``C``. Stated so the overstatement the
            "Costs" line names is a figure the reader can check, not a claim.
    """

    transformation: InsertTransaction
    effective_date: date
    shifted: bool
    cash_effect: Decimal
    costs: Decimal
    gross: Decimal


def build_impact_basis(ticket: TradeTicketDTO, *, t0: date) -> ImpactBasis:
    """Map one order ticket onto the ``insert_transaction`` that previews it.

    **The sign flip (D-6a (i)).**
    :func:`~services.transactions.validation.derive_cash_effect` returns a
    magnitude, and :func:`services.transactions.emission._signed_cash_effect`
    applies the direction in the **cash** view (D-B: a buy spends, so it is
    negative). The overlay's ``consideration`` is the **value** view —
    :func:`services.overlay.executors.execute_insert_transaction` adds ``+C``
    to the investment's value path and ``−C`` to the cash path — so the two
    conventions are exact opposites and the panel negates once, here. A second
    negation anywhere downstream would show a buy as a disposal.

    **The costs live inside ``C`` (D-6a (i)).** ``derive_cash_effect`` folds
    fees and taxes into the magnitude, and the overlay carries one ``C`` for
    both legs. The cash leg is therefore exact and the value leg is overstated
    by the costs. That is the deliberate choice — the liquidity lens is where a
    few hundred units of currency can flip a sign, and a quota is insensitive
    to them — and the panel states it rather than hiding it.

    **OP-07 (a).** A ticket dated at or before the seam is previewed as if
    traded on ``t₀ + 1``. The executor refuses ``trade_date <= frames.t0``
    outright (:class:`~services.overlay.errors.HistoricTradeDateError`), and
    rightly: realised history is identical in every scenario (ADR-0104 §5).
    Shifting here means the executor never sees such a date, and the ticket
    itself is untouched — only the transformation is re-dated.

    Args:
        ticket: A previewable order ticket — one :func:`impact_scope` returned
            ``None`` for.
        t0: The plan/actual seam of the frames the transformation will be
            applied to (``frames.t0``). Never a clock read: the seam is the
            book's last statement date, and a panel anchored to "today" would
            move on a day the book did not.

    Returns:
        The :class:`ImpactBasis`.

    Raises:
        ValueError: If the ticket lacks the units, price or investment an order
            needs. Unreachable behind :func:`impact_scope` and the propose-time
            completeness gate, so it is a programmer error rather than an
            operator-facing refusal — the same idiom
            :func:`services.transactions.emission.order_legs` uses.
    """
    if ticket.investment_id is None or ticket.units is None or ticket.price_per_unit is None:
        raise ValueError(
            f"ticket {ticket.id} cannot be mapped onto an insert_transaction: "
            "an order needs an investment, units and a price per unit"
        )

    magnitude = derive_cash_effect(
        direction=ticket.direction,
        net_amount=ticket.net_amount,
        gross_amount=ticket.gross_amount,
        units=ticket.units,
        price_per_unit=ticket.price_per_unit,
        fees=ticket.fees,
        taxes=ticket.taxes,
    )
    if magnitude is None:
        raise ValueError(
            f"ticket {ticket.id} states no derivable cash effect, so there is "
            "no consideration to insert into the plan world"
        )

    buying = ticket.direction == DIRECTION_BUY
    # The value view, not the cash view — see the docstring's sign-flip note.
    signed = magnitude if buying else -magnitude
    units_signed = ticket.units if buying else -ticket.units

    shifted = ticket.trade_date <= t0
    effective_date = t0 + timedelta(days=1) if shifted else ticket.trade_date

    transformation = InsertTransaction(
        investment_id=ticket.investment_id,
        txn_type=ticket.direction,
        trade_date=effective_date,
        units=units_signed,
        price_per_unit=ticket.price_per_unit,
        consideration=signed,
        # The frames key both cash paths and position currencies by uppercased
        # code (``plan_world._cash_positions``, ``PlanInvestment.currency``), so
        # a lower-case ticket currency would miss the path it settles against
        # and be refused for a book that holds it.
        currency=ticket.currency.upper(),
    )
    return ImpactBasis(
        transformation=transformation,
        effective_date=effective_date,
        shifted=shifted,
        cash_effect=magnitude,
        costs=(ticket.fees or Decimal(0)) + (ticket.taxes or Decimal(0)),
        gross=ticket.units * ticket.price_per_unit,
    )


__all__ = [
    "IMPACT_SCOPE_CREATING_FLOW",
    "IMPACT_SCOPE_DRAFT",
    "IMPACT_SCOPE_REPORTED_KIND",
    "ImpactBasis",
    "build_impact_basis",
    "impact_scope",
]
