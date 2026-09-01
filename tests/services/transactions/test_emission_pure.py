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
* TE-07: ``cash_leg`` is exactly the cash half of ``order_legs`` (S2b).
* TE-08: ``parse_master_data`` — the one interpretation of the JSONB
  payload, and every conversion failure naming its key (D-V).
* TE-09: ``reconcile_commitment`` per flow (D-U).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from core.exceptions import TicketIncomplete
from core.repositories.investment_repository import InvestmentDTO
from core.repositories.trade_ticket_repository import TradeTicketDTO
from services.transactions.constants import (
    INCOMPLETE_COMMITMENT_SHAPE,
    INCOMPLETE_MISSING_MASTER_DATA,
    MD_ACQUIRED_NAV,
    MD_ANLV_CODE,
    MD_ASSET_CLASS_ID,
    MD_ASSUMED_UNFUNDED,
    MD_COMMITMENT_AMOUNT,
    MD_CURRENCY,
    MD_FIGI,
    MD_IDENTIFIER_SCHEME,
    MD_IDENTIFIER_VALUE,
    MD_INVESTMENT_TYPE,
    MD_MANAGER,
    MD_NAME,
    MD_PURCHASE_PRICE,
    MD_REGION,
    MD_VINTAGE_YEAR,
)
from services.transactions.emission import (
    CASH_UNIT_PRICE,
    cash_leg,
    investment_before_image,
    order_legs,
    parse_master_data,
    provenance,
    reconcile_commitment,
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


# ---------------------------------------------------------------------------
# TE-07: cash_leg is the cash half of order_legs, exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("direction", "effect"),
    [
        pytest.param("buy", Decimal("105.00"), id="buy"),
        pytest.param("sell", Decimal("100.00"), id="sell"),
        pytest.param("sell", Decimal("-50.00"), id="sell-costing-money"),
        pytest.param("sell", Decimal("0"), id="no-cash-moved"),
    ],
)
def test_te07_cash_leg_matches_the_cash_half_of_order_legs(direction: str, effect: Decimal) -> None:
    """The extraction changed nothing: one derivation, two entry points.

    ``cash_leg`` exists so the reported kinds can settle without pretending
    to be orders. If it ever disagreed with ``order_legs`` about a sign, the
    same trade would settle differently depending on which flow booked it.
    """
    ticket = _ticket(direction=direction)

    _, from_order = order_legs(ticket, cash_effect=effect)
    standalone = cash_leg(ticket, cash_effect=effect)

    assert standalone == from_order


def test_te07_cash_leg_needs_a_settlement_position() -> None:
    with pytest.raises(ValueError):
        cash_leg(_ticket(cash_investment_id=None), cash_effect=Decimal("100"))


# ---------------------------------------------------------------------------
# TE-08: parse_master_data (D-V)
# ---------------------------------------------------------------------------

_ASSET_CLASS = UUID("44444444-4444-4444-4444-444444444444")


def _payload(**overrides) -> dict[str, object]:
    """A full creating payload as JSONB returns it — every value a string."""
    values: dict[str, object] = {
        MD_NAME: "  New Fund IV  ",
        MD_INVESTMENT_TYPE: "private_equity",
        MD_ASSET_CLASS_ID: str(_ASSET_CLASS),
        MD_CURRENCY: "EUR",
        MD_ANLV_CODE: "anlv_13",
        MD_IDENTIFIER_SCHEME: "preqin",
        MD_IDENTIFIER_VALUE: "PQ-991",
        MD_FIGI: "BBG000BLNNH6",
        MD_MANAGER: "Example Partners",
        MD_REGION: "DACH",
        MD_VINTAGE_YEAR: "2021",
        MD_COMMITMENT_AMOUNT: "5000000",
        MD_PURCHASE_PRICE: "750000.50",
        MD_ACQUIRED_NAV: "800000",
        MD_ASSUMED_UNFUNDED: "250000",
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def test_te08_parses_a_string_typed_payload_into_domain_types() -> None:
    """JSONB hands back strings; the row needs Decimals, an int and a UUID."""
    master = parse_master_data(_payload())

    assert master.name == "New Fund IV"  # trimmed
    assert master.investment_type == "private_equity"
    assert master.asset_class_id == _ASSET_CLASS
    assert master.currency == "EUR"
    assert master.anlv_code == "anlv_13"
    assert (master.identifier_scheme, master.identifier_value) == ("preqin", "PQ-991")
    assert master.figi == "BBG000BLNNH6"
    assert (master.manager, master.region) == ("Example Partners", "DACH")
    assert master.vintage_year == 2021
    assert master.commitment_amount == Decimal("5000000")
    assert master.purchase_price == Decimal("750000.50")
    assert master.acquired_nav == Decimal("800000")
    assert master.assumed_unfunded == Decimal("250000")


def test_te08_absent_optionals_are_none_not_defaults() -> None:
    """A composer that skipped a field said nothing, not zero."""
    master = parse_master_data(
        {
            MD_NAME: "Minimal",
            MD_INVESTMENT_TYPE: "cash",
            MD_ASSET_CLASS_ID: str(_ASSET_CLASS),
            MD_CURRENCY: "EUR",
        }
    )

    assert master.anlv_code is None
    assert master.vintage_year is None
    assert master.commitment_amount is None
    assert master.acquired_nav is None
    assert master.assumed_unfunded is None


def test_te08_blank_strings_read_as_absent() -> None:
    """An untouched form field posts ``""``; that is not a value."""
    master = parse_master_data(_payload(**{MD_VINTAGE_YEAR: "", MD_ACQUIRED_NAV: "  "}))

    assert master.vintage_year is None
    assert master.acquired_nav is None


@pytest.mark.parametrize(
    ("overrides", "key"),
    [
        pytest.param({MD_NAME: None}, MD_NAME, id="name-absent"),
        pytest.param({MD_NAME: "   "}, MD_NAME, id="name-blank"),
        pytest.param({MD_INVESTMENT_TYPE: None}, MD_INVESTMENT_TYPE, id="type-absent"),
        pytest.param({MD_ASSET_CLASS_ID: None}, MD_ASSET_CLASS_ID, id="asset-class-absent"),
        pytest.param({MD_ASSET_CLASS_ID: "not-a-uuid"}, MD_ASSET_CLASS_ID, id="asset-class-shape"),
        pytest.param({MD_ASSET_CLASS_ID: 7}, MD_ASSET_CLASS_ID, id="asset-class-not-text"),
        pytest.param({MD_CURRENCY: None}, MD_CURRENCY, id="currency-absent"),
        pytest.param({MD_VINTAGE_YEAR: "twenty"}, MD_VINTAGE_YEAR, id="year-not-a-number"),
        pytest.param({MD_COMMITMENT_AMOUNT: "5.000,00"}, MD_COMMITMENT_AMOUNT, id="amount-locale"),
        pytest.param({MD_ACQUIRED_NAV: "n/a"}, MD_ACQUIRED_NAV, id="amount-not-a-number"),
        pytest.param({MD_MANAGER: 12}, MD_MANAGER, id="text-not-a-string"),
    ],
)
def test_te08_every_conversion_failure_names_its_key(overrides: dict, key: str) -> None:
    """One identifier for the surface, the key in the message for the human."""
    with pytest.raises(TicketIncomplete) as excinfo:
        parse_master_data(_payload(**overrides))

    assert excinfo.value.identifier == INCOMPLETE_MISSING_MASTER_DATA
    assert excinfo.value.field == "master_data"
    assert key in str(excinfo.value)


def test_te08_a_float_amount_converts_through_its_repr() -> None:
    """``Decimal(str(v))``, so 0.1 is 0.1 rather than its binary expansion."""
    master = parse_master_data(_payload(**{MD_ACQUIRED_NAV: 0.1}))

    assert master.acquired_nav == Decimal("0.1")


# ---------------------------------------------------------------------------
# TE-09: reconcile_commitment (D-U)
# ---------------------------------------------------------------------------


def test_te09_commitment_takes_the_ticket_column() -> None:
    ticket = _ticket(kind="commitment", commitment_amount=Decimal("5000000.0000"))
    master = parse_master_data(_payload())

    assert reconcile_commitment(ticket, master=master) == Decimal("5000000.0000")


def test_te09_commitment_refuses_a_payload_that_disagrees() -> None:
    ticket = _ticket(kind="commitment", commitment_amount=Decimal("4000000.0000"))
    master = parse_master_data(_payload())

    with pytest.raises(TicketIncomplete) as excinfo:
        reconcile_commitment(ticket, master=master)

    assert excinfo.value.identifier == INCOMPLETE_COMMITMENT_SHAPE
    assert excinfo.value.field == "commitment_amount"


def test_te09_secondary_takes_the_assumed_unfunded_from_the_payload() -> None:
    """MD-15: the commitment a secondary buyer assumes is the unfunded part."""
    ticket = _ticket(kind="secondary", commitment_amount=None)
    master = parse_master_data(_payload())

    assert reconcile_commitment(ticket, master=master) == Decimal("250000")


def test_te09_secondary_refuses_a_column_that_disagrees() -> None:
    ticket = _ticket(kind="secondary", commitment_amount=Decimal("999.0000"))
    master = parse_master_data(_payload())

    with pytest.raises(TicketIncomplete) as excinfo:
        reconcile_commitment(ticket, master=master)

    assert excinfo.value.identifier == INCOMPLETE_COMMITMENT_SHAPE


def test_te09_a_new_order_records_no_commitment() -> None:
    """A listed instrument has none, whatever the payload happens to carry."""
    ticket = _ticket(kind="order", commitment_amount=None)

    assert reconcile_commitment(ticket, master=parse_master_data(_payload())) is None
