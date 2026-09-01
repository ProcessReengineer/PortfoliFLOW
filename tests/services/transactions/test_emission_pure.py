# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure emission-derivation tests — no database (ADR-0128 §2, D-B…D-H).

The sign convention of the working document §2.1 / §2.2 is the thing most
worth pinning without a database in the way: a booking that gets a sign
wrong writes rows that pass every CHECK and every FK and are simply untrue.
These tests read :func:`~services.transactions.emission.order_legs` as the
table it is.

Coverage
--------
* TE-01: the direction table — instrument and cash legs, both directions.
* TE-02: D-B's real subject — a sell whose costs exceed its gross moves cash
  *out*, and a zero effect emits no cash leg at all.
* TE-03: ``consideration`` per D-C, and the shared provenance string.
* TE-04: ``investment_before_image`` — JSON-safe, total, and lossless.
* TE-05: the ``ValueError`` preconditions.
* TE-06: the module stays out of ``services/analytics/``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from core.repositories.investment_repository import InvestmentDTO
from core.repositories.trade_ticket_repository import TradeTicketDTO
from services.transactions.emission import (
    CASH_UNIT_PRICE,
    investment_before_image,
    order_legs,
    provenance,
)

_TRADE_DATE = date(2026, 8, 31)
_NOW = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)
_INSTRUMENT = UUID("11111111-1111-1111-1111-111111111111")
_CASH = UUID("22222222-2222-2222-2222-222222222222")
_ACTOR = UUID("33333333-3333-3333-3333-333333333333")


def _ticket(**overrides) -> TradeTicketDTO:
    """A complete order ticket; override one field to vary the case."""
    values: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "ticket_number": 7,
        "kind": "order",
        "direction": "buy",
        "status": "proposed",
        "investment_id": _INSTRUMENT,
        "cash_investment_id": _CASH,
        "trade_date": _TRADE_DATE,
        "settlement_date": date(2026, 9, 2),
        "units": Decimal("10"),
        "price_per_unit": Decimal("10.00"),
        "gross_amount": None,
        "fees": None,
        "taxes": None,
        "net_amount": None,
        "currency": "EUR",
        "commitment_amount": None,
        "master_data": None,
        "set_inactive": False,
        "note": "quarterly rebalance",
        "source": "broker confirmation",
        "cancel_reason": None,
        "case_id": None,
        "proposed_by": _ACTOR,
        "proposed_at": _NOW,
        "approved_by": None,
        "approved_at": None,
        "booked_by": None,
        "booked_at": None,
        "cancelled_at": None,
        "created_by": _ACTOR,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(overrides)
    return TradeTicketDTO(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TE-01: the direction table
# ---------------------------------------------------------------------------


def test_te01_sell_signs_the_instrument_out_and_the_cash_in() -> None:
    """A sale removes units and adds cash (working document §2.1)."""
    instrument, cash = order_legs(_ticket(direction="sell"), cash_effect=Decimal("100.00"))

    assert (instrument.investment_id, instrument.txn_type) == (_INSTRUMENT, "sell")
    assert instrument.units == Decimal("-10")
    assert instrument.price_per_unit == Decimal("10.00")

    assert cash is not None
    assert (cash.investment_id, cash.txn_type) == (_CASH, "buy")
    assert cash.units == Decimal("100.00")
    assert cash.price_per_unit == CASH_UNIT_PRICE == Decimal("1.0000")


def test_te01_buy_signs_the_instrument_in_and_the_cash_out() -> None:
    """A purchase adds units and removes cash (working document §2.2)."""
    instrument, cash = order_legs(_ticket(direction="buy"), cash_effect=Decimal("105.00"))

    assert (instrument.txn_type, instrument.units) == ("buy", Decimal("10"))
    assert cash is not None
    assert (cash.txn_type, cash.units) == ("sell", Decimal("-105.00"))


def test_te01_both_legs_carry_the_ticket_currency_note_and_provenance() -> None:
    """D-D: one provenance string and the ticket's note on both rows."""
    instrument, cash = order_legs(_ticket(), cash_effect=Decimal("100"))

    assert cash is not None
    for leg in (instrument, cash):
        assert leg.currency == "EUR"
        assert leg.note == "quarterly rebalance"
        assert leg.source == "ticket #7"


# ---------------------------------------------------------------------------
# TE-02: the cash leg follows the *effect's* sign, not the ticket's (D-B)
# ---------------------------------------------------------------------------


def test_te02_sell_with_costs_above_gross_moves_cash_out() -> None:
    """The case D-B exists for: a sale that costs money to make.

    The ticket says ``sell`` and the cash position still *loses* money. A
    cash leg keyed off the ticket's direction would book an inflow here and
    the ledger would be quietly, permanently wrong.
    """
    instrument, cash = order_legs(_ticket(direction="sell"), cash_effect=Decimal("-50.00"))

    assert instrument.txn_type == "sell"
    assert cash is not None
    assert cash.txn_type == "sell"
    assert cash.units == Decimal("-50.00")


def test_te02_zero_effect_emits_no_cash_leg() -> None:
    """A schema fact, not a rounding accommodation.

    ``ck_position_transactions_sign`` admits no zero-unit buy or sell, so an
    event that moves no cash is not an event.
    """
    instrument, cash = order_legs(_ticket(direction="sell"), cash_effect=Decimal("0"))

    assert cash is None
    assert instrument.units == Decimal("-10")


# ---------------------------------------------------------------------------
# TE-03: consideration (D-C)
# ---------------------------------------------------------------------------


def test_te03_consideration_is_the_signed_effect_on_the_instrument_only() -> None:
    sell_leg, sell_cash = order_legs(_ticket(direction="sell"), cash_effect=Decimal("100.00"))
    buy_leg, buy_cash = order_legs(_ticket(direction="buy"), cash_effect=Decimal("105.00"))

    assert sell_leg.consideration == Decimal("100.00")
    assert buy_leg.consideration == Decimal("-105.00")
    # A cash row's cash effect *is* its units at 1.0000; restating it would
    # be a second place for one number to go wrong.
    assert sell_cash is not None and sell_cash.consideration is None
    assert buy_cash is not None and buy_cash.consideration is None


def test_te03_provenance_names_the_number_the_operator_sees() -> None:
    assert provenance(_ticket(ticket_number=42)) == "ticket #42"


# ---------------------------------------------------------------------------
# TE-04: the before-image (D-H)
# ---------------------------------------------------------------------------


def _investment(**overrides) -> InvestmentDTO:
    values: dict[str, object] = {
        "id": _INSTRUMENT,
        "tenant_id": uuid4(),
        "name": "Listed Fund",
        "investment_type": "listed_equity",
        "asset_class_id": uuid4(),
        "manager_name": None,
        "region": "DACH",
        "currency": "EUR",
        "vintage_year": 2021,
        "commitment_amount": Decimal("5000000.0000"),
        "is_active": True,
        "type_specific_data": {"isin": "DE0001", "nested": {"n": 1}},
        "created_by": _ACTOR,
        "created_at": _NOW,
        "updated_at": _NOW,
        "anlv_code": "anlv_13",
        "valuation_mode": "unitised",
    }
    values.update(overrides)
    return InvestmentDTO(**values)  # type: ignore[arg-type]


def test_te04_before_image_is_total_and_json_serialisable() -> None:
    """Every field, and it survives the JSONB column it is bound for."""
    dto = _investment()
    image = investment_before_image(dto)

    assert set(image) == {field for field in vars(dto)}
    json.dumps(image)  # would raise on any surviving UUID / Decimal / datetime


def test_te04_before_image_converts_by_type_and_keeps_none() -> None:
    image = investment_before_image(_investment())

    assert image["id"] == str(_INSTRUMENT)
    assert image["commitment_amount"] == "5000000.0000"
    assert image["created_at"] == _NOW.isoformat()
    assert image["is_active"] is True
    assert image["vintage_year"] == 2021
    assert image["manager_name"] is None
    # Containers are converted recursively rather than assumed flat.
    assert image["type_specific_data"] == {"isin": "DE0001", "nested": {"n": 1}}


def test_te04_before_image_keeps_decimals_exact_as_strings() -> None:
    """Decimal → str, never float: a restored amount is the amount stored."""
    image = investment_before_image(
        _investment(commitment_amount=Decimal("0.1000000000000000055511151231"))
    )

    assert image["commitment_amount"] == "0.1000000000000000055511151231"
    assert Decimal(image["commitment_amount"]) == Decimal("0.1000000000000000055511151231")


def test_te04_before_image_dates_do_not_truncate_timestamps() -> None:
    """``datetime`` is a ``date`` subclass; the wrong order would lose the time."""
    image = investment_before_image(_investment())

    assert image["updated_at"].startswith("2026-08-31T09:00")


# ---------------------------------------------------------------------------
# TE-05: preconditions are programmer errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"kind": "commitment"}, id="wrong-kind"),
        pytest.param({"investment_id": None}, id="no-investment"),
        pytest.param({"cash_investment_id": None}, id="no-settlement-position"),
        pytest.param({"units": None}, id="no-units"),
        pytest.param({"price_per_unit": None}, id="no-price"),
    ],
)
def test_te05_incomplete_ticket_is_a_value_error(overrides: dict) -> None:
    """Not a domain error: completeness has already been established."""
    with pytest.raises(ValueError):
        order_legs(_ticket(**overrides), cash_effect=Decimal("100"))


# ---------------------------------------------------------------------------
# TE-06: layering
# ---------------------------------------------------------------------------


def test_te06_emission_does_not_reach_into_analytics() -> None:
    """The emission derives from ticket columns, never from a calculation engine."""
    source = (
        Path(__file__).resolve().parents[3] / "services" / "transactions" / "emission.py"
    ).read_text()

    assert "services.analytics" not in source
    assert "services import analytics" not in source
