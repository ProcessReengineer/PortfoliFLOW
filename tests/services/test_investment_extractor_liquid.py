# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the liquid-archetype extractor paths (ADR-0081).

Pure unit tests — the three tidy reference extractors and the income
path have no DB or FastAPI dependency. Snapshots use the
``DataFrame.to_json(orient="split")`` shape that
:class:`core.repositories.DataUploadRepository` persists.

Coverage

* Tidy parse for bond analytics / rating weights / maturity weights:
  happy path, unknown bucket → row error, out-of-range weight → row
  error, missing ``ytm`` → row error, ``oas`` / ``convexity`` ``None``
  allowed, unknown investment name → row error.
* Income ``flow_type`` derivation: ``listed_equity`` → dividend,
  ``listed_bonds`` → coupon, other type → no income, ``cash`` → no
  income (NAV-only, ADR-0100 §4).
* Negative income → row error.
* Alias mapping: ``Credit`` / ``Money Market`` normalise to
  ``listed_bonds``; ``Cash`` normalises to ``cash`` (ADR-0100 §5
  supersedes the ADR-0081 §3 ``Cash`` → ``listed_bonds`` mapping).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from services.data_normalization import (
    InvestmentExtractor,
)


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _attributes_payload(
    investment_names: list[str],
    *,
    types: list[str | None],
) -> dict:
    """Build a minimal Attributes-sheet JSONB payload (type row only)."""
    return {
        "columns": list(investment_names),
        "index": ["Investment Type"],
        "data": [list(types)],
    }


def _tidy_payload(columns: list[str], rows: list[list[object]]) -> dict:
    """Build a tidy/long reference-sheet payload (RangeIndex)."""
    return {
        "columns": list(columns),
        "index": list(range(len(rows))),
        "data": [list(r) for r in rows],
    }


def _timeseries_payload(
    investment_names: list[str],
    rows: list[tuple[str, list[object]]],
) -> dict:
    """Build a wide date-indexed payload (income / NAV / cashflow idiom)."""
    return {
        "columns": list(investment_names),
        "index": [iso for iso, _ in rows],
        "data": [vals for _, vals in rows],
    }


_BOND_COLS = ["as_of_date", "investment", "ytm", "eff_duration", "oas", "convexity"]
_RATING_COLS = ["as_of_date", "investment", "rating_bucket", "weight_pct"]
_MATURITY_COLS = ["as_of_date", "investment", "maturity_bucket", "weight_pct"]


# ---------------------------------------------------------------------------
# Bond analytics
# ---------------------------------------------------------------------------


def test_bond_analytics_happy_path_oas_convexity_optional() -> None:
    names = ["FI Fund"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Anleihen"]),
        "bond_analytics": _tidy_payload(
            _BOND_COLS,
            [
                ["2024-01-31", "FI Fund", 0.045, 3.2, 0.012, 0.4],
                # oas / convexity blank → None allowed.
                ["2024-02-29", "FI Fund", -0.001, 0.1, None, None],
            ],
        ),
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_bond_analytics(sheets)

    assert extractor.errors == []
    rows = out["FI Fund"]
    assert len(rows) == 2
    assert rows[0].as_of_date == date(2024, 1, 31)
    assert rows[0].ytm == Decimal("0.045")
    assert rows[0].eff_duration == Decimal("3.2")
    assert rows[0].oas == Decimal("0.012")
    assert rows[0].convexity == Decimal("0.4")
    # Negative YTM is valid; oas/convexity may be None.
    assert rows[1].ytm == Decimal("-0.001")
    assert rows[1].oas is None
    assert rows[1].convexity is None


def test_bond_analytics_missing_ytm_emits_error_and_drops_row() -> None:
    names = ["FI Fund"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Anleihen"]),
        "bond_analytics": _tidy_payload(
            _BOND_COLS,
            [
                ["2024-01-31", "FI Fund", None, 3.2, None, None],
                ["2024-02-29", "FI Fund", 0.05, 3.1, None, None],
            ],
        ),
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_bond_analytics(sheets)

    assert len(out["FI Fund"]) == 1
    assert out["FI Fund"][0].as_of_date == date(2024, 2, 29)
    assert len(extractor.errors) == 1
    assert extractor.errors[0].column == "ytm"


def test_bond_analytics_missing_eff_duration_emits_error() -> None:
    names = ["FI Fund"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Anleihen"]),
        "bond_analytics": _tidy_payload(
            _BOND_COLS,
            [["2024-01-31", "FI Fund", 0.05, None, None, None]],
        ),
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_bond_analytics(sheets)

    assert out["FI Fund"] == []
    assert len(extractor.errors) == 1
    assert extractor.errors[0].column == "eff_duration"


def test_bond_analytics_unknown_investment_name_emits_error() -> None:
    names = ["FI Fund"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Anleihen"]),
        "bond_analytics": _tidy_payload(
            _BOND_COLS,
            [["2024-01-31", "Ghost Fund", 0.05, 3.0, None, None]],
        ),
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_bond_analytics(sheets)

    assert out["FI Fund"] == []
    assert len(extractor.errors) == 1
    assert extractor.errors[0].investment_name == "Ghost Fund"
    assert extractor.errors[0].column == "investment"


def test_bond_analytics_absent_sheet_returns_empty_lists() -> None:
    names = ["FI Fund"]
    sheets = {"attributes": _attributes_payload(names, types=["Anleihen"])}
    extractor = InvestmentExtractor()
    out = extractor.extract_bond_analytics(sheets)
    assert out == {"FI Fund": []}
    assert extractor.errors == []


# ---------------------------------------------------------------------------
# Rating weights
# ---------------------------------------------------------------------------


def test_rating_weights_happy_path() -> None:
    names = ["FI Fund"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Anleihen"]),
        "rating_weights": _tidy_payload(
            _RATING_COLS,
            [
                ["2024-03-31", "FI Fund", "AAA", 40.0],
                ["2024-03-31", "FI Fund", "CCC_and_below", 5.0],
                ["2024-03-31", "FI Fund", "NR", 2.5],
            ],
        ),
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_rating_weights(sheets)

    assert extractor.errors == []
    rows = out["FI Fund"]
    assert {r.rating_bucket for r in rows} == {"AAA", "CCC_and_below", "NR"}
    by_bucket = {r.rating_bucket: r.weight_pct for r in rows}
    assert by_bucket["AAA"] == Decimal("40.0")
    assert by_bucket["CCC_and_below"] == Decimal("5.0")


def test_rating_weights_unknown_bucket_emits_error_and_drops_row() -> None:
    names = ["FI Fund"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Anleihen"]),
        "rating_weights": _tidy_payload(
            _RATING_COLS,
            [
                ["2024-03-31", "FI Fund", "AA", 60.0],
                ["2024-03-31", "FI Fund", "ZZZ", 40.0],
            ],
        ),
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_rating_weights(sheets)

    assert [r.rating_bucket for r in out["FI Fund"]] == ["AA"]
    assert len(extractor.errors) == 1
    assert extractor.errors[0].column == "rating_bucket"


def test_rating_weights_out_of_range_emits_error_and_drops_row() -> None:
    names = ["FI Fund"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Anleihen"]),
        "rating_weights": _tidy_payload(
            _RATING_COLS,
            [
                ["2024-03-31", "FI Fund", "AAA", 150.0],
                ["2024-03-31", "FI Fund", "BBB", -1.0],
                ["2024-03-31", "FI Fund", "BB", 12.5],
            ],
        ),
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_rating_weights(sheets)

    assert [r.rating_bucket for r in out["FI Fund"]] == ["BB"]
    assert len(extractor.errors) == 2
    assert all(e.column == "weight_pct" for e in extractor.errors)


# ---------------------------------------------------------------------------
# Maturity weights
# ---------------------------------------------------------------------------


def test_maturity_weights_happy_and_unknown_bucket() -> None:
    names = ["FI Fund"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Anleihen"]),
        "maturity_weights": _tidy_payload(
            _MATURITY_COLS,
            [
                ["2024-03-31", "FI Fund", "0-1y", 100.0],
                ["2024-03-31", "FI Fund", "30y+", 0.0],
            ],
        ),
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_maturity_weights(sheets)

    assert [r.maturity_bucket for r in out["FI Fund"]] == ["0-1y"]
    assert out["FI Fund"][0].weight_pct == Decimal("100.0")
    assert len(extractor.errors) == 1
    assert extractor.errors[0].column == "maturity_bucket"


# ---------------------------------------------------------------------------
# Income flow-type derivation
# ---------------------------------------------------------------------------


def test_income_flow_type_derivation_by_investment_type() -> None:
    names = ["Equity Fund", "Bond Fund", "RE Fund", "Cash Fund"]
    sheets = {
        "attributes": _attributes_payload(
            names,
            # listed_equity → dividend; Anleihen (listed_bonds) → coupon;
            # Immobilien (real_estate) → no income; Cash → cash, which is
            # NAV-only and has *no* income mapping (ADR-0100 §4): the
            # income cell is silently skipped, exactly as for real_estate.
            types=["listed_equity", "Anleihen", "Immobilien", "Cash"],
        ),
        "cash_flow_income_actual": _timeseries_payload(
            names,
            [
                ("2024-03-31T00:00:00.000", [100.0, 200.0, 300.0, 5.0]),
            ],
        ),
        "cash_flow_income_plan": _timeseries_payload(
            names,
            [
                ("2024-06-30T00:00:00.000", [110.0, 210.0, 310.0, 6.0]),
            ],
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    by_name = {inv.name: inv for inv in investments}

    # Equity → dividend (one actual + one plan).
    eq_flows = by_name["Equity Fund"].cashflows
    assert {f.flow_type for f in eq_flows} == {"dividend"}
    assert {f.flow_kind for f in eq_flows} == {"actual", "plan"}
    assert sorted(f.amount for f in eq_flows) == [Decimal("100.0"), Decimal("110.0")]

    # Bond → coupon.
    bond_flows = by_name["Bond Fund"].cashflows
    assert {f.flow_type for f in bond_flows} == {"coupon"}

    # Real estate → no income emitted, and no error.
    assert by_name["RE Fund"].cashflows == ()

    # Cash → 'cash' (ADR-0100 §5). Cash is NAV-only with no
    # _INCOME_FLOW_TYPE_BY_TYPE entry (§4), so its income cell is a silent
    # mapping-miss skip — no coupon, no error — mirroring real_estate.
    assert by_name["Cash Fund"].cashflows == ()
    assert by_name["Cash Fund"].investment_type == "cash"

    assert extractor.errors == []


def test_negative_income_emits_row_error_and_drops() -> None:
    names = ["Bond Fund"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Anleihen"]),
        "cash_flow_income_actual": _timeseries_payload(
            names,
            [
                ("2024-03-31T00:00:00.000", [-50.0]),
                ("2024-06-30T00:00:00.000", [75.0]),
            ],
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    flows = investments[0].cashflows

    assert [f.amount for f in flows] == [Decimal("75.0")]
    assert len(extractor.errors) == 1
    assert extractor.errors[0].sheet == "cash_flow_income_actual"


def test_income_absent_sheets_leave_cashflows_unchanged() -> None:
    """Regression: no income sheets ⇒ a listed fund has no income flows."""
    names = ["Bond Fund"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Anleihen"]),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    assert investments[0].cashflows == ()
    assert extractor.errors == []


# ---------------------------------------------------------------------------
# Alias mapping — Credit / Money Market → listed_bonds; Cash → cash
#
# ADR-0081 §3 pointed both ``Credit`` and ``Cash`` at ``listed_bonds``.
# ADR-0100 §5 supersedes the ``Cash`` half: a money-market fund is a
# *fund* (``listed_bonds``, now labelled ``Money Market`` in the sample
# workbook), while ``Cash`` names a currency balance and resolves to the
# new ``cash`` type. ``Credit`` is unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["Credit", "credit", "Money Market", "money market", "Geldmarkt"])
def test_credit_and_money_market_aliases_resolve_to_listed_bonds(
    label: str,
) -> None:
    names = ["Inv"]
    sheets = {"attributes": _attributes_payload(names, types=[label])}
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    assert extractor.errors == []
    assert investments[0].investment_type == "listed_bonds"


@pytest.mark.parametrize("label", ["Cash", "cash", "CASH", "Kasse", "Liquidität"])
def test_cash_aliases_resolve_to_cash(label: str) -> None:
    names = ["Inv"]
    sheets = {"attributes": _attributes_payload(names, types=[label])}
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    assert extractor.errors == []
    assert investments[0].investment_type == "cash"
