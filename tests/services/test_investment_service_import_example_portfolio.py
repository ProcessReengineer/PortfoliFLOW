# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Fixture-level extraction test on the example-portfolio workbook.

Loads ``sample_data/PortfoliFLOW_example_portfolio.xlsx`` through the
production parse (:func:`load_excel`) and the upload serialisation shape
(``to_json(orient="split", date_format="iso")``), then runs the extractor
and asserts:

* the S4 units contract (ADR-0097 §7): the ten unitised instruments each
  carry the approved unit count with ``units_as_of`` defaulted to their
  earliest actual NAV date, while the eleven private-markets positions
  and the two cash positions carry no units;
* the ADR-0100 Block-4 cash additions: ``Cash USD`` and ``Cash EUR`` each
  land as ``investment_type='cash'`` in their own currency, asset class
  ``cash``, with a blank (NULL) vintage;
* the ADR-0103 §3 book of record: because this workbook carries a
  ``Cash`` sheet, every cash position's balances arrive as
  ``cash_statements`` and its ``navs`` stay empty — the mutually
  exclusive pair. The predecessor v31 workbook had no ``Cash`` sheet, so
  this test used to exercise the NAV-column branch instead; that branch
  stays alive for Cash-sheet-less workbooks and is covered by the
  synthetic extractor tests.
* Investment T — a Money Market *fund*, not a cash row (ADR-0100 §5) —
  lands as ``listed_bonds`` with its Fixed-Income bond-analytics rows
  intact.

This validates the real workbook's structure end-to-end through the
extractor without the cost of writing the full NAV/cashflow set; the
ledger-write reconcile is covered by
``test_investment_service_transform_openings.py`` (synthetic, fast).

The workbook is tracked in the repository, so the test is unconditional.
"""

from __future__ import annotations

import json
import pathlib
from datetime import date
from decimal import Decimal

from services.data_normalization import InvestmentExtractor
from services.data_normalization.excel_workbook_loader import load_excel

_WORKBOOK_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "sample_data"
    / "PortfoliFLOW_example_portfolio.xlsx"
)

# The ten unitised instruments: (units, expected opening date = earliest
# actual NAV). Read off the workbook's ``Units`` / ``Units As Of`` rows,
# which agree with the earliest actual NAV date for every one of them.
_EXPECTED_UNITS: dict[str, tuple[Decimal, date]] = {
    "Investment A": (Decimal("1315000"), date(2016, 5, 2)),
    "Investment B": (Decimal("189000"), date(2016, 5, 3)),
    "Investment C": (Decimal("1845000"), date(2016, 5, 3)),
    "Investment H": (Decimal("4802000"), date(2018, 4, 3)),
    "Investment I": (Decimal("577000"), date(2016, 5, 2)),
    "Investment J": (Decimal("16394000"), date(2017, 6, 1)),
    "Investment K": (Decimal("450000"), date(2016, 5, 3)),
    "Investment L": (Decimal("266000"), date(2019, 1, 15)),
    "Investment M": (Decimal("265000"), date(2017, 9, 1)),
    "Investment T": (Decimal("196000"), date(2016, 5, 2)),
}
# The 21 lettered investments plus the two ADR-0100 explicit cash positions.
_ALL_INVESTMENTS = [f"Investment {c}" for c in "ABCDEFGHIJKLMNOPQRSTU"] + [
    "Cash USD",
    "Cash EUR",
]
#: The explicit cash positions and the currency each is denominated in.
_CASH_POSITIONS: dict[str, str] = {"Cash USD": "USD", "Cash EUR": "EUR"}


def _payload(sheets) -> dict:
    """Serialise loaded DataFrames to the extractor's JSONB input shape.

    Mirrors ``DataUploadRepository._df_to_jsonb_payload`` — the exact
    round-trip the production upload path performs before extraction.
    """
    return {
        name: json.loads(df.to_json(orient="split", date_format="iso"))
        for name, df in sheets.items()
    }


def _extract() -> tuple[InvestmentExtractor, dict, dict]:
    extractor = InvestmentExtractor()
    payload = _payload(load_excel(_WORKBOOK_PATH))
    investments = extractor.extract(payload)
    by_name = {i.name: i for i in investments}
    return extractor, by_name, payload


def test_example_portfolio_extracts_units_rows_with_cash_carrying_none() -> None:
    extractor, by_name, _ = _extract()
    assert extractor.errors == []
    assert set(by_name) == set(_ALL_INVESTMENTS)

    for name in _ALL_INVESTMENTS:
        inv = by_name[name]
        if name in _EXPECTED_UNITS:
            units, as_of = _EXPECTED_UNITS[name]
            assert inv.units == units, name
            assert inv.units_as_of == as_of, name
        else:
            # Private-markets, real-estate and cash positions carry no units.
            assert inv.units is None, name
            assert inv.units_as_of is None, name


def test_example_portfolio_cash_positions_and_money_market_land_correctly() -> None:
    """ADR-0100 Block-4 and ADR-0103 §3: the cash columns and the MMF."""
    extractor, by_name, payload = _extract()
    assert extractor.errors == []

    # The explicit foreign-currency cash positions.
    for name, currency in _CASH_POSITIONS.items():
        cash = by_name[name]
        assert cash.investment_type == "cash", name
        assert cash.currency == currency, name
        assert cash.asset_class_code.strip().lower() == "cash", name
        assert cash.vintage_year is None, name  # blank vintage → NULL (ADR-0100 §1)

        # The Cash sheet is the book of record for balances, so the
        # statement series is populated and the NAV column stays empty —
        # the two are mutually exclusive (ADR-0103 §3).
        statements = cash.cash_statements
        assert statements, f"{name} must carry its Cash-sheet statement series"
        assert not cash.navs, f"{name} must not also carry NAV rows"
        dates = [s.statement_date for s in statements]
        assert dates == sorted(dates), f"{name} statements must be date-ordered"

    # The opening statement anchors the balance path to real workbook data.
    usd_opening = by_name["Cash USD"].cash_statements[0]
    assert usd_opening.statement_date == date(2024, 11, 1)
    assert usd_opening.balance == Decimal("2400000")

    # Investment T is a Money Market fund, not a cash row (ADR-0100 §5),
    # so it stays listed_bonds with its Fixed-Income analytics intact.
    inv_t = by_name["Investment T"]
    assert inv_t.investment_type == "listed_bonds"
    bond_analytics = extractor.extract_bond_analytics(payload)
    assert bond_analytics.get("Investment T"), "Investment T must retain its bond-analytics rows"
    # The cash rows, by contrast, carry no bond analytics.
    for name in _CASH_POSITIONS:
        assert not bond_analytics.get(name), name
