# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Fixture-level extraction test on the real v31 workbook.

Loads ``PortfoliFLOW_Testdaten_v31.xlsx`` through the production parse
(:func:`load_excel`) and the upload serialisation shape
(``to_json(orient="split", date_format="iso")``), then runs the extractor
and asserts:

* the S4 units contract (ADR-0097 §7): the nine listed instruments each
  carry the approved unit count with ``units_as_of`` defaulted to their
  earliest actual NAV date, while the twelve private-markets / cash
  positions carry no units;
* the ADR-0100 Block-4 additions the v31 fixture introduces: the new
  ``Cash USD`` column lands as ``investment_type='cash'``, ``currency='USD'``,
  asset class ``cash``, with a blank (NULL) vintage; and Investment T —
  whose ``Typ`` label moved from ``Cash`` to ``Money Market`` — lands as
  ``listed_bonds`` with its Fixed-Income bond-analytics rows intact.

This validates the real workbook's structure end-to-end through the
extractor without the eight-minute cost of writing the full 73k-row
NAV/cashflow set; the ledger-write reconcile is covered by
``test_investment_service_transform_openings.py`` (synthetic, fast), and
the full transform on this file was verified manually once.

The workbook lives in the operator's working directory outside the tree;
the test skips when absent.
"""

from __future__ import annotations

import json
import pathlib
from datetime import date
from decimal import Decimal

import pytest

from services.data_normalization import InvestmentExtractor
from services.data_normalization.excel_workbook_loader import load_excel

_V31_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "excel_import_files"
    / "PortfoliFLOW_Testdaten_v31.xlsx"
)

# The nine listed instruments: (units, expected opening date = earliest
# actual NAV). Matches the v30/v31 generator and plan §G — the units rows
# are unchanged by the ADR-0100 derivation.
_EXPECTED_UNITS: dict[str, tuple[Decimal, date]] = {
    "Investment A": (Decimal("1190000"), date(2016, 5, 1)),
    "Investment B": (Decimal("2250000"), date(2016, 5, 1)),
    "Investment C": (Decimal("1360000"), date(2016, 5, 1)),
    "Investment H": (Decimal("1090000"), date(2018, 4, 1)),
    "Investment I": (Decimal("525000"), date(2016, 5, 1)),
    "Investment J": (Decimal("14000000"), date(2017, 6, 1)),
    "Investment K": (Decimal("458000"), date(2016, 5, 1)),
    "Investment L": (Decimal("524000"), date(2019, 1, 15)),
    "Investment M": (Decimal("280000"), date(2017, 9, 1)),
}
# The 20 lettered investments plus the ADR-0100 explicit cash position.
_ALL_INVESTMENTS = [f"Investment {c}" for c in "ABCDEFGHIJKLMNOPQRST"] + ["Cash USD"]


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
    payload = _payload(load_excel(_V31_PATH))
    investments = extractor.extract(payload)
    by_name = {i.name: i for i in investments}
    return extractor, by_name, payload


def test_v31_extracts_units_rows_with_cash_carrying_none() -> None:
    if not _V31_PATH.exists():
        pytest.skip(f"v31 workbook not at {_V31_PATH}; skipping.")

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
            # Private-markets, real-estate, money-market and cash positions
            # carry no units.
            assert inv.units is None, name
            assert inv.units_as_of is None, name


def test_v31_cash_usd_and_money_market_land_correctly() -> None:
    """ADR-0100 Block-4: the Cash USD column and the relabelled MMF."""
    if not _V31_PATH.exists():
        pytest.skip(f"v31 workbook not at {_V31_PATH}; skipping.")

    extractor, by_name, payload = _extract()
    assert extractor.errors == []

    # The new explicit foreign-currency cash position.
    cash = by_name["Cash USD"]
    assert cash.investment_type == "cash"
    assert cash.currency == "USD"
    assert cash.asset_class_code.strip().lower() == "cash"
    assert cash.vintage_year is None  # blank vintage → NULL (ADR-0100 §1)
    assert cash.navs, "Cash USD must carry its NAV-only balance series"

    # Investment T's Typ moved from 'Cash' to 'Money Market': it is a fund,
    # not a cash row (ADR-0100 §5), so it stays listed_bonds with its
    # Fixed-Income analytics intact.
    inv_t = by_name["Investment T"]
    assert inv_t.investment_type == "listed_bonds"
    bond_analytics = extractor.extract_bond_analytics(payload)
    assert bond_analytics.get("Investment T"), "Investment T must retain its bond-analytics rows"
    # The cash row, by contrast, carries no bond analytics.
    assert bond_analytics.get("Cash USD") == []
