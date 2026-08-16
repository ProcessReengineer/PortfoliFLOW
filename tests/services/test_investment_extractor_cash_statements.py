# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The ``Cash`` statement sheet — loader registration and extraction.

Workbook v32 (ADR-0103 §3) adds a statement-style ``Cash`` sheet: one column
per cash position, one row per statement date, each cell the **balance** in
the position's currency. It is the book of record for cash balances and
takes precedence over the NAV sheets, whose ``Cash USD`` column moves here.

Coverage
--------
* **Loader** — ``Cash`` is a wide investment time-series sheet: it derives
  the key ``cash``, shares the investment-column namespace, and participates
  in the cross-sheet consistency check. A v31 workbook (no such sheet)
  parses byte-identically.
* **Extraction** — the series comes out ordered; an explicit ``0`` is a
  statement and a blank is not; a negative cell is an ``ImportRowError`` and
  is dropped, leaving the series self-correcting; a non-cash column on the
  sheet errors on every populated cell.
* **Precedence** — a cash column carrying both statements and NAV values
  keeps the statements and warns; a stray ``Units`` row on it likewise.
* **v31 regression** — with no ``Cash`` sheet, a cash position's NAV column
  still imports as ordinary NAV rows and no statement series appears.
* **Sign-guard copy (S1.1 nit)** — the corrective sentence on a cash column
  speaks of contributions / withdrawals, not calls / distributions.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from core.exceptions import ValidationError
from services.data_normalization import InvestmentExtractor
from services.data_normalization.excel_workbook_loader import (
    INVESTMENT_TIMESERIES_SHEETS,
    RECOGNIZED_SHEETS,
    _sheet_name_to_key,
    validate_workbook,
)


# ---------------------------------------------------------------------------
# Snapshot helpers — the JSONB shape the upload path persists
# ---------------------------------------------------------------------------


def _split(df: pd.DataFrame) -> dict:
    """Serialise a DataFrame the way the upload path does."""
    import json

    return json.loads(df.to_json(orient="split", date_format="iso"))


def _attributes(investments: dict[str, dict[str, object]]) -> dict:
    columns = list(investments)
    labels = ["Investment Type", "Asset Class", "Währung", "Units"]
    data = [[investments[c].get(label) for c in columns] for label in labels]
    return _split(pd.DataFrame(data, index=labels, columns=columns))


def _timeseries(
    by_investment: dict[str, list[tuple[str, float | None]]],
    names: list[str],
) -> dict:
    all_dates = sorted({d for series in by_investment.values() for d, _ in series})
    frame = pd.DataFrame(
        {name: [dict(by_investment.get(name, [])).get(d) for d in all_dates] for name in names},
        index=pd.to_datetime(all_dates),
        columns=names,
    )
    return _split(frame)


def _cash(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "Investment Type": "cash",
        "Asset Class": "cash",
        "Währung": "EUR",
    }
    base.update(extra)
    return base


def _equity(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "Investment Type": "listed_equity",
        "Asset Class": "listed_equity",
        "Währung": "EUR",
    }
    base.update(extra)
    return base


def _by_name(extracted):
    return {imp.name: imp for imp in extracted}


# ---------------------------------------------------------------------------
# Loader registration
# ---------------------------------------------------------------------------


def test_cash_sheet_is_a_recognised_wide_investment_sheet() -> None:
    """``Cash`` rides the wide idiom and derives the key ``cash``."""
    assert "Cash" in INVESTMENT_TIMESERIES_SHEETS
    assert "Cash" in RECOGNIZED_SHEETS
    assert _sheet_name_to_key("Cash") == "cash"


def test_cash_sheet_participates_in_cross_sheet_consistency() -> None:
    """It shares the investment-column namespace, so a mismatch is caught."""
    dates = pd.to_datetime(["2024-01-31"])
    consistent = {
        "navs_actual": pd.DataFrame([[1.0, 2.0]], index=dates, columns=["Fund", "Cash EUR"]),
        "cash": pd.DataFrame([[None, 500.0]], index=dates, columns=["Fund", "Cash EUR"]),
    }
    validate_workbook(consistent)  # does not raise

    mismatched = {
        "navs_actual": pd.DataFrame([[1.0, 2.0]], index=dates, columns=["Fund", "Cash EUR"]),
        # The Cash sheet must carry the same investment columns as every
        # other investment sheet — the cash column alone is a format error.
        "cash": pd.DataFrame([[500.0]], index=dates, columns=["Cash EUR"]),
    }
    with pytest.raises(ValidationError, match="Investment column mismatch"):
        validate_workbook(mismatched)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_statement_series_is_extracted_in_chronological_order() -> None:
    names = ["Cash EUR"]
    sheets = {
        "attributes": _attributes({"Cash EUR": _cash()}),
        # Deliberately out of order in the payload: the extractor sorts.
        "cash": _timeseries(
            {
                "Cash EUR": [
                    ("2024-03-31", 1_200.0),
                    ("2024-01-31", 1_000.0),
                    ("2024-02-29", 900.0),
                ]
            },
            names,
        ),
    }
    extractor = InvestmentExtractor()
    imp = _by_name(extractor.extract(sheets))["Cash EUR"]

    assert extractor.errors == []
    assert [(s.statement_date, s.balance) for s in imp.cash_statements] == [
        (date(2024, 1, 31), Decimal("1000.0")),
        (date(2024, 2, 29), Decimal("900.0")),
        (date(2024, 3, 31), Decimal("1200.0")),
    ]


def test_explicit_zero_is_a_statement_and_blank_is_not() -> None:
    """An emptied account is a fact; a blank cell is the absence of one."""
    names = ["Cash EUR"]
    sheets = {
        "attributes": _attributes({"Cash EUR": _cash()}),
        "cash": _timeseries(
            {
                "Cash EUR": [
                    ("2024-01-31", 1_000.0),
                    ("2024-02-29", None),  # no statement this month
                    ("2024-03-31", 0.0),  # account emptied — a statement
                ]
            },
            names,
        ),
    }
    extractor = InvestmentExtractor()
    imp = _by_name(extractor.extract(sheets))["Cash EUR"]

    assert extractor.errors == []
    assert [s.statement_date for s in imp.cash_statements] == [
        date(2024, 1, 31),
        date(2024, 3, 31),
    ]
    assert imp.cash_statements[-1].balance == Decimal("0")


def test_negative_balance_is_an_error_and_the_series_self_corrects() -> None:
    """The date drops out; the next delta spans to the following statement."""
    names = ["Cash EUR"]
    sheets = {
        "attributes": _attributes({"Cash EUR": _cash()}),
        "cash": _timeseries(
            {
                "Cash EUR": [
                    ("2024-01-31", 1_000.0),
                    ("2024-02-29", -50.0),  # rejected
                    ("2024-03-31", 800.0),
                ]
            },
            names,
        ),
    }
    extractor = InvestmentExtractor()
    imp = _by_name(extractor.extract(sheets))["Cash EUR"]

    assert len(extractor.errors) == 1
    error = extractor.errors[0]
    assert error.sheet == "cash"
    assert error.row_index == "2024-02-29"
    assert "negative" in error.message

    # The offending date is simply not a statement; the series stays valid
    # and the March balance is still the true level (levels, not flows).
    assert [(s.statement_date, s.balance) for s in imp.cash_statements] == [
        (date(2024, 1, 31), Decimal("1000.0")),
        (date(2024, 3, 31), Decimal("800.0")),
    ]


def test_non_cash_column_on_the_cash_sheet_errors_on_every_cell() -> None:
    names = ["Fund"]
    sheets = {
        "attributes": _attributes({"Fund": _equity()}),
        "cash": _timeseries({"Fund": [("2024-01-31", 100.0), ("2024-02-29", 200.0)]}, names),
    }
    extractor = InvestmentExtractor()
    imp = _by_name(extractor.extract(sheets))["Fund"]

    assert imp.cash_statements == ()
    assert len(extractor.errors) == 2
    assert {e.row_index for e in extractor.errors} == {
        "2024-01-31",
        "2024-02-29",
    }
    assert all(e.sheet == "cash" for e in extractor.errors)
    assert all("book of record for cash positions only" in e.message for e in extractor.errors)


def test_cash_sheet_takes_precedence_over_nav_columns() -> None:
    """One book of record: the NAV column is dropped, with one warning."""
    names = ["Cash USD"]
    sheets = {
        "attributes": _attributes({"Cash USD": _cash(**{"Währung": "USD"})}),
        "navs_actual": _timeseries({"Cash USD": [("2024-01-31", 111.0)]}, names),
        "cash": _timeseries({"Cash USD": [("2024-01-31", 999.0)]}, names),
    }
    extractor = InvestmentExtractor()
    imp = _by_name(extractor.extract(sheets))["Cash USD"]

    assert imp.navs == ()
    assert imp.cash_statements[0].balance == Decimal("999.0")
    precedence = [w for w in extractor.warnings if w.action == "skipped_cash_sheet_precedence"]
    assert len(precedence) == 1
    assert precedence[0].field == "navs"


def test_units_row_loses_to_the_statement_series() -> None:
    names = ["Cash EUR"]
    sheets = {
        "attributes": _attributes({"Cash EUR": _cash(Units=123)}),
        "cash": _timeseries({"Cash EUR": [("2024-01-31", 1_000.0)]}, names),
    }
    extractor = InvestmentExtractor()
    imp = _by_name(extractor.extract(sheets))["Cash EUR"]

    assert imp.units is None
    assert imp.units_as_of is None
    assert imp.cash_statements[0].balance == Decimal("1000.0")
    units_warning = [
        w
        for w in extractor.warnings
        if w.field == "units" and w.action == "skipped_cash_sheet_precedence"
    ]
    assert len(units_warning) == 1


def test_no_precedence_warning_when_the_nav_column_is_empty() -> None:
    """The clean v32 case: the cash column has left the NAV sheets."""
    names = ["Cash EUR", "Fund"]
    sheets = {
        "attributes": _attributes({"Cash EUR": _cash(), "Fund": _equity()}),
        "navs_actual": _timeseries({"Fund": [("2024-01-31", 5_000.0)]}, names),
        "cash": _timeseries({"Cash EUR": [("2024-01-31", 1_000.0)]}, names),
    }
    extractor = InvestmentExtractor()
    extracted = _by_name(extractor.extract(sheets))

    assert extractor.errors == []
    assert extractor.warnings == []
    assert extracted["Cash EUR"].cash_statements
    assert extracted["Fund"].cash_statements == ()
    assert len(extracted["Fund"].navs) == 1


# ---------------------------------------------------------------------------
# v31 regression — no Cash sheet
# ---------------------------------------------------------------------------


def test_v31_cash_position_without_a_cash_sheet_is_unchanged() -> None:
    """A cash NAV column still imports as ordinary NAV rows."""
    names = ["Cash USD"]
    sheets = {
        "attributes": _attributes({"Cash USD": _cash(**{"Währung": "USD"})}),
        "navs_actual": _timeseries(
            {
                "Cash USD": [
                    ("2024-01-31", 1_000.0),
                    ("2024-02-29", 1_100.0),
                ]
            },
            names,
        ),
    }
    extractor = InvestmentExtractor()
    imp = _by_name(extractor.extract(sheets))["Cash USD"]

    assert extractor.errors == []
    assert extractor.warnings == []
    assert imp.cash_statements == ()
    assert [n.nav_value for n in imp.navs] == [
        Decimal("1000.0"),
        Decimal("1100.0"),
    ]
    assert all(n.nav_kind == "actual" for n in imp.navs)


# ---------------------------------------------------------------------------
# S1.1 nit — sign-guard copy is flow-type aware
# ---------------------------------------------------------------------------


def test_sign_guard_copy_names_investor_flows_on_a_cash_column() -> None:
    names = ["Cash EUR", "Fund"]
    sheets = {
        "attributes": _attributes({"Cash EUR": _cash(), "Fund": _equity()}),
        # Both columns violate their sheet's sign convention.
        "cash_flow_in_actual": _timeseries(
            {
                "Cash EUR": [("2024-01-31", -10.0)],
                "Fund": [("2024-01-31", -10.0)],
            },
            names,
        ),
        "cash_flow_out_actual": _timeseries(
            {
                "Cash EUR": [("2024-02-29", 10.0)],
                "Fund": [("2024-02-29", 10.0)],
            },
            names,
        ),
    }
    extractor = InvestmentExtractor()
    extractor.extract(sheets)

    by_column = {(e.column, e.sheet): e.message for e in extractor.errors}
    # Cash column → investor-flow vocabulary.
    assert "withdrawals should live" in by_column[("Cash EUR", "cash_flow_in_actual")]
    assert "contributions should live" in by_column[("Cash EUR", "cash_flow_out_actual")]
    # Non-cash column → the original copy, verbatim.
    assert "calls should live" in by_column[("Fund", "cash_flow_in_actual")]
    assert "distributions should live" in by_column[("Fund", "cash_flow_out_actual")]
