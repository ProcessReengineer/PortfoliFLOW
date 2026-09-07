# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure tests for the ticket → overlay mapping (S6 P-6a, ADR-0128 Q-3).

:mod:`services.transactions.impact` is the whole of what the impact panel
*decides*; everything after it is the overlay's arithmetic and the panel's
formatting. Two rules make it worth its own module, and both are pinned here:

* **the sign flip.** The emission carries the consideration in the *cash*
  view (a buy is negative — D-B); the overlay wants the *value* view (a buy
  is positive). A second negation anywhere would render a buy as a disposal,
  and the failure would be silent — the panel would still draw, with the
  arrows the wrong way round. The executor anchor at the foot of this module
  is what catches it: it applies a real basis to real frames and asserts that
  a buy raises the value path and lowers the cash path.
* **OP-07 (a).** A ticket dated at or before the seam is previewed as if
  traded on ``t₀ + 1``, because the executor refuses a historic date outright
  and realised history is identical in every scenario (ADR-0104 §5).

Coverage
--------
* TI-01: sign and units, both directions.
* TI-02: costs are inside ``C``; a stated ``net_amount`` wins.
* TI-03: the OP-07 shift, on both sides of the seam.
* TI-04: the currency is uppercased for the frames' keying.
* TI-05: :func:`impact_scope` — the three refusals and the previewable case.
* TI-06: the programmer-error ``ValueError``s.
* TI-07: the executor anchor — value up, cash down, history untouched.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from core.repositories.trade_ticket_repository import TradeTicketDTO
from core.tenant_constants import SENTINEL_TENANT_ID
from services.overlay import apply_overlay
from services.transactions.constants import (
    DIRECTION_BUY,
    DIRECTION_SELL,
    KIND_COMMITMENT,
    KIND_ORDER,
    KIND_SECONDARY,
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_PROPOSED,
)
from services.transactions.impact import (
    IMPACT_SCOPE_CREATING_FLOW,
    IMPACT_SCOPE_DRAFT,
    IMPACT_SCOPE_REPORTED_KIND,
    build_impact_basis,
    impact_scope,
)
from services.transactions.validation import derive_cash_effect

# The overlay's own executor fixtures, reused rather than re-modelled: the
# anchor has to run against the frames the executors are pinned on, or it
# would be asserting the sign of a shape only this file believes in.
from tests.services.overlay.test_executors import _EQ, _T0, _frames

_D = Decimal
_INVESTMENT = _EQ
_TRADE_DATE = date(2026, 9, 30)
_NOW = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)


def _dto(**fields: Any) -> TradeTicketDTO:
    """A ticket DTO for the pure pins — ``test_transactions_blotter._dto``'s shape."""
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": SENTINEL_TENANT_ID,
        "ticket_number": 23,
        "kind": KIND_ORDER,
        "direction": DIRECTION_BUY,
        "status": STATUS_PROPOSED,
        "investment_id": _INVESTMENT,
        "cash_investment_id": uuid4(),
        "trade_date": _TRADE_DATE,
        "settlement_date": None,
        "units": _D("100"),
        "price_per_unit": _D("10"),
        "gross_amount": None,
        "fees": None,
        "taxes": None,
        "net_amount": None,
        "currency": "EUR",
        "commitment_amount": None,
        "master_data": None,
        "set_inactive": False,
        "note": None,
        "source": None,
        "cancel_reason": None,
        "case_id": None,
        "proposed_by": uuid4(),
        "proposed_at": _NOW,
        "approved_by": None,
        "approved_at": None,
        "booked_by": None,
        "booked_at": None,
        "cancelled_at": None,
        "created_by": uuid4(),
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return TradeTicketDTO(**{**defaults, **fields})


# ---------------------------------------------------------------------------
# TI-01 — the sign flip
# ---------------------------------------------------------------------------


def test_a_buy_is_positive_in_the_value_view() -> None:
    """The overlay's ``consideration`` is value in, not cash out (D-6a (i))."""
    basis = build_impact_basis(_dto(direction=DIRECTION_BUY), t0=_T0)

    assert basis.transformation.consideration == _D("1000")
    assert basis.transformation.units == _D("100")
    assert basis.cash_effect == _D("1000")


def test_a_sell_is_negative_in_the_value_view() -> None:
    """Both legs invert together: value leaves, cash arrives."""
    basis = build_impact_basis(_dto(direction=DIRECTION_SELL), t0=_T0)

    assert basis.transformation.consideration == _D("-1000")
    assert basis.transformation.units == _D("-100")
    # The magnitude the panel prints stays unsigned.
    assert basis.cash_effect == _D("1000")


def test_the_magnitude_is_the_validation_seam_s_own() -> None:
    """One derivation of what a ticket costs — never a second copy here."""
    ticket = _dto(fees=_D("5"), taxes=_D("2.50"))
    basis = build_impact_basis(ticket, t0=_T0)

    assert basis.cash_effect == derive_cash_effect(
        direction=ticket.direction,
        net_amount=ticket.net_amount,
        gross_amount=ticket.gross_amount,
        units=ticket.units,
        price_per_unit=ticket.price_per_unit,
        fees=ticket.fees,
        taxes=ticket.taxes,
    )


# ---------------------------------------------------------------------------
# TI-02 — the costs, and the precedence of a stated net
# ---------------------------------------------------------------------------


def test_fees_and_taxes_sit_inside_the_consideration() -> None:
    """Buy 100 @ 10 with 5 of fees is ``C = 1005`` — the cash leg is exact."""
    basis = build_impact_basis(_dto(fees=_D("5")), t0=_T0)

    assert basis.transformation.consideration == _D("1005")
    assert basis.costs == _D("5")
    # The value leg the overstatement is measured against.
    assert basis.gross == _D("1000")


def test_taxes_are_costs_too() -> None:
    """``costs`` is ``fees + taxes``, either half absent reading as zero."""
    basis = build_impact_basis(_dto(fees=_D("5"), taxes=_D("2.50")), t0=_T0)

    assert basis.costs == _D("7.50")
    assert basis.transformation.consideration == _D("1007.50")


def test_a_stated_net_amount_wins_over_units_times_price() -> None:
    """The settlement figure is taken as given; costs are inside it already."""
    basis = build_impact_basis(_dto(net_amount=_D("999.42"), fees=_D("5")), t0=_T0)

    assert basis.transformation.consideration == _D("999.42")
    # `gross` stays the units × price figure — it is what the value leg would
    # be, not a restatement of the net.
    assert basis.gross == _D("1000")


# ---------------------------------------------------------------------------
# TI-03 — OP-07 (a)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trade_date", [_T0, date(2026, 1, 1)])
def test_a_ticket_at_or_before_the_seam_is_shifted_to_the_day_after(
    trade_date: date,
) -> None:
    """The executor never sees a date the plan world does not own."""
    basis = build_impact_basis(_dto(trade_date=trade_date), t0=_T0)

    assert basis.shifted is True
    assert basis.effective_date == date(2026, 7, 1)
    assert basis.transformation.trade_date == date(2026, 7, 1)


def test_a_ticket_after_the_seam_keeps_its_own_date() -> None:
    """No shift, and the panel says nothing about one."""
    basis = build_impact_basis(_dto(trade_date=_TRADE_DATE), t0=_T0)

    assert basis.shifted is False
    assert basis.effective_date == _TRADE_DATE
    assert basis.transformation.trade_date == _TRADE_DATE


# ---------------------------------------------------------------------------
# TI-04 — the currency
# ---------------------------------------------------------------------------


def test_the_currency_is_uppercased_for_the_frames() -> None:
    """The frames key cash paths and position currencies by uppercased code."""
    basis = build_impact_basis(_dto(currency="eur"), t0=_T0)

    assert basis.transformation.currency == "EUR"


# ---------------------------------------------------------------------------
# TI-05 — the scope gate
# ---------------------------------------------------------------------------


def test_impact_scope_names_each_refusal_and_passes_a_proposed_order() -> None:
    """The four answers, in the order the gate asks its questions."""
    assert impact_scope(_dto(status=STATUS_DRAFT)) == IMPACT_SCOPE_DRAFT
    assert impact_scope(_dto(kind=KIND_SECONDARY)) == IMPACT_SCOPE_REPORTED_KIND
    assert impact_scope(_dto(kind=KIND_COMMITMENT)) == IMPACT_SCOPE_REPORTED_KIND
    assert impact_scope(_dto(investment_id=None)) == IMPACT_SCOPE_CREATING_FLOW

    assert impact_scope(_dto(status=STATUS_PROPOSED)) is None
    assert impact_scope(_dto(status=STATUS_APPROVED)) is None


def test_a_draft_is_answered_as_a_draft_whatever_else_it_is() -> None:
    """Status first: a draft's kind is not yet an interesting fact."""
    assert impact_scope(_dto(status=STATUS_DRAFT, kind=KIND_SECONDARY)) == IMPACT_SCOPE_DRAFT


# ---------------------------------------------------------------------------
# TI-06 — the programmer errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["units", "price_per_unit", "investment_id"])
def test_an_incomplete_order_is_a_programmer_error(missing: str) -> None:
    """Unreachable behind the scope gate and the propose-time completeness gate."""
    with pytest.raises(ValueError):
        build_impact_basis(_dto(**{missing: None}), t0=_T0)


# ---------------------------------------------------------------------------
# TI-07 — the executor anchor (read-only over the overlay)
# ---------------------------------------------------------------------------


def _levels(path: Any) -> dict[str, Decimal]:
    """A balance path as ``{iso date: level}``, for readable assertions."""
    return {str(stamp.date()): value for stamp, value in path.items()}


def test_a_buy_raises_the_value_path_and_lowers_the_cash_path() -> None:
    """The sign flip, end to end — the one test that catches an inverted panel.

    ``_frames`` opens the listed holding at 500 and EUR cash at 1,000 on the
    seam. A buy of 100 @ 10 inserts ``C = 1000`` on 2026-09-30, so the value
    path steps **up** and the cash path **down** by the same amount from that
    date onward — and both are untouched at ``t₀``, which is the
    identical-history invariant (ADR-0104 §5) seen from the panel's side.
    """
    basis = build_impact_basis(_dto(direction=DIRECTION_BUY), t0=_T0)

    frames = _frames()
    scenario = apply_overlay(frames, (basis.transformation,))

    value = _levels(scenario.value_paths[_INVESTMENT])
    cash = _levels(scenario.cash_paths["EUR"])

    assert value["2026-06-30"] == _D("500")  # history untouched
    assert value["2026-09-30"] == _D("1500")  # 500 carried forward, +1000
    assert value["2026-12-31"] == _D("1550")  # 550, +1000

    assert cash["2026-06-30"] == _D("1000")  # history untouched
    assert cash["2026-09-30"] == _D("0")  # 1000 carried forward, −1000
    assert cash["2026-12-31"] == _D("-100")  # 900, −1000


def test_a_sell_lowers_the_value_path_and_raises_the_cash_path() -> None:
    """The mirror. Together with the buy this pins the sign, not just its size."""
    basis = build_impact_basis(_dto(direction=DIRECTION_SELL), t0=_T0)

    scenario = apply_overlay(_frames(), (basis.transformation,))

    value = _levels(scenario.value_paths[_INVESTMENT])
    cash = _levels(scenario.cash_paths["EUR"])

    assert value["2026-06-30"] == _D("500")
    assert value["2026-09-30"] == _D("-500")
    assert cash["2026-06-30"] == _D("1000")
    assert cash["2026-09-30"] == _D("2000")


def test_the_shifted_basis_is_accepted_by_the_executor() -> None:
    """OP-07 (a) exists so this call does not raise ``HistoricTradeDateError``."""
    basis = build_impact_basis(_dto(trade_date=_T0), t0=_T0)

    scenario = apply_overlay(_frames(), (basis.transformation,))

    cash = _levels(scenario.cash_paths["EUR"])
    assert cash["2026-06-30"] == _D("1000")  # the seam itself is untouched
    assert cash["2026-07-01"] == _D("0")  # the step lands the day after
