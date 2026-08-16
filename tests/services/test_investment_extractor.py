# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.data_normalization.InvestmentExtractor``.

Pure unit tests — the extractor has no DB or FastAPI dependency.
Test snapshots use the
``DataFrame.to_json(orient="split")`` shape that
:class:`core.repositories.DataUploadRepository` persists in
``data_upload_sheets.data``.

Coverage targets

* Round-trip happy path: 3 investments × {plan/actual NAV + plan/actual
  cashflows} → typed dataclasses with right counts and signs.
* Strict sign validation on Cash Flow Out (positive value → error).
* Empty Asset Class falls back to ``"unclassified"``.
* Unknown Investment Type → row-level :class:`ImportRowError`.
* NaN / blank cells are skipped silently.
* Vintage Year stored as ``"2020"`` string is coerced to int.
* Missing ``Attributes`` sheet raises :class:`ImportFormatError`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from services.data_normalization import (
    ImportFormatError,
    InvestmentExtractor,
)


# ---------------------------------------------------------------------------
# Fixtures — JSONB-shaped sheet payloads
# ---------------------------------------------------------------------------


def _attributes_payload(
    investment_names: list[str],
    *,
    types: list[str | None],
    sub_classes: list[str | None] | None = None,
    asset_classes: list[str | None] | None = None,
    managers: list[str | None] | None = None,
    regions: list[str | None] | None = None,
    vintage_years: list[object] | None = None,
    currencies: list[str | None] | None = None,
) -> dict:
    """Build an Attributes-sheet JSONB payload of the documented shape.

    All optional rows default to ``None``-padded lists matching
    ``investment_names`` length.
    """
    n = len(investment_names)

    def _pad(row: list | None) -> list:
        return list(row) if row is not None else [None] * n

    rows = {
        "Investment Type": _pad(types),
        "Investment Sub-Class": _pad(sub_classes),
        "Asset Class": _pad(asset_classes),
        "Manager / Fondsname": _pad(managers),
        "Region": _pad(regions),
        "Vintage Year": _pad(vintage_years),
        "Währung": _pad(currencies),
    }
    return {
        "columns": list(investment_names),
        "index": list(rows.keys()),
        "data": list(rows.values()),
    }


def _timeseries_payload(
    investment_names: list[str],
    rows: list[tuple[str, list[object]]],
) -> dict:
    """Build a date-indexed JSONB payload.

    ``rows`` is a list of ``(iso_date_string, [val_per_investment, ...])``.
    """
    return {
        "columns": list(investment_names),
        "index": [iso for iso, _ in rows],
        "data": [vals for _, vals in rows],
    }


# ---------------------------------------------------------------------------
# IE-01: round-trip happy path
# ---------------------------------------------------------------------------


def test_ie01_roundtrip_three_investments_navs_and_cashflows() -> None:
    names = ["Fund A", "Fund B", "Fund C"]
    sheets = {
        "attributes": _attributes_payload(
            names,
            types=["Aktien", "Private Equity", "Immobilien"],
            asset_classes=["listed_equity", "private_equity", "real_estate"],
            managers=["GP A", "GP B", None],
            regions=["Europa", "USA", "DACH"],
            vintage_years=[2020, "2021", None],
            currencies=["EUR", "USD", "EUR"],
        ),
        "navs_actual": _timeseries_payload(
            names,
            [
                ("2024-01-01T00:00:00.000", [100.0, 200.0, None]),
                ("2024-07-01T00:00:00.000", [110.0, 210.0, 50.0]),
            ],
        ),
        "navs_plan": _timeseries_payload(
            names,
            [
                ("2024-12-31T00:00:00.000", [120.0, 220.0, 60.0]),
            ],
        ),
        "cash_flow_out_actual": _timeseries_payload(
            names,
            [
                ("2024-01-01T00:00:00.000", [-100.0, -200.0, None]),
            ],
        ),
        "cash_flow_in_actual": _timeseries_payload(
            names,
            [
                ("2024-07-01T00:00:00.000", [10.0, 20.0, None]),
            ],
        ),
    }

    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)

    assert extractor.errors == []
    assert {i.name for i in investments} == set(names)

    by_name = {i.name: i for i in investments}
    assert by_name["Fund A"].investment_type == "listed_equity"
    assert by_name["Fund A"].asset_class_code == "listed_equity"
    assert by_name["Fund A"].vintage_year == 2020
    assert by_name["Fund A"].currency == "EUR"
    assert by_name["Fund A"].manager_name == "GP A"
    assert by_name["Fund A"].commitment_amount is None

    assert by_name["Fund B"].investment_type == "private_equity"
    assert by_name["Fund B"].vintage_year == 2021  # "2021" string → int
    assert by_name["Fund B"].currency == "USD"

    assert by_name["Fund C"].investment_type == "real_estate"
    assert by_name["Fund C"].manager_name is None  # blank passes through

    # NAV / cashflow counts.
    assert len(by_name["Fund A"].navs) == 3  # 2 actual + 1 plan
    assert {n.nav_kind for n in by_name["Fund A"].navs} == {"actual", "plan"}
    assert by_name["Fund A"].navs[0].nav_value == Decimal("100.0")

    assert len(by_name["Fund A"].cashflows) == 2  # 1 out + 1 in
    flow_signs = {cf.flow_type: cf.amount for cf in by_name["Fund A"].cashflows}
    assert flow_signs["capital_call"] < 0
    assert flow_signs["distribution"] > 0


# ---------------------------------------------------------------------------
# IE-02: cashflow type/kind matrix
# ---------------------------------------------------------------------------


def test_ie02_cashflow_matrix_routes_correctly() -> None:
    names = ["Inv"]
    sheets = {
        "attributes": _attributes_payload(names, types=["private_equity"]),
        "cash_flow_in_actual": _timeseries_payload(names, [("2024-01-01", [10.0])]),
        "cash_flow_in_plan": _timeseries_payload(names, [("2024-02-01", [11.0])]),
        "cash_flow_out_actual": _timeseries_payload(names, [("2024-03-01", [-12.0])]),
        "cash_flow_out_plan": _timeseries_payload(names, [("2024-04-01", [-13.0])]),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    assert extractor.errors == []
    cashflows = investments[0].cashflows
    assert len(cashflows) == 4
    by_kind_type = {(cf.flow_kind, cf.flow_type): cf for cf in cashflows}
    assert ("actual", "distribution") in by_kind_type
    assert ("plan", "distribution") in by_kind_type
    assert ("actual", "capital_call") in by_kind_type
    assert ("plan", "capital_call") in by_kind_type
    # Timestamps are 12:00 UTC by convention.
    for cf in cashflows:
        assert cf.flow_timestamp.hour == 12
        assert cf.flow_timestamp.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# IE-03: Cash Flow Out positive value → ImportRowError, no row emitted
# ---------------------------------------------------------------------------


def test_ie03_cashflow_out_positive_value_emits_error() -> None:
    names = ["Inv"]
    sheets = {
        "attributes": _attributes_payload(names, types=["private_equity"]),
        "cash_flow_out_actual": _timeseries_payload(
            names,
            [("2024-01-01", [100.0])],  # POSITIVE → invalid
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)

    assert investments[0].cashflows == ()
    assert len(extractor.errors) == 1
    err = extractor.errors[0]
    assert err.investment_name == "Inv"
    assert err.sheet == "cash_flow_out_actual"
    assert "positive value" in err.message


def test_ie03b_cashflow_in_negative_value_emits_error() -> None:
    names = ["Inv"]
    sheets = {
        "attributes": _attributes_payload(names, types=["private_equity"]),
        "cash_flow_in_actual": _timeseries_payload(
            names,
            [("2024-01-01", [-50.0])],  # NEGATIVE in IN → invalid
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)

    assert investments[0].cashflows == ()
    assert len(extractor.errors) == 1
    assert "negative value" in extractor.errors[0].message


# ---------------------------------------------------------------------------
# IE-04: empty Asset Class → "unclassified"
# ---------------------------------------------------------------------------


def test_ie04_empty_asset_class_falls_back_to_unclassified() -> None:
    names = ["Inv"]
    sheets = {
        "attributes": _attributes_payload(
            names,
            types=["private_equity"],
            asset_classes=[None],
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)

    assert extractor.errors == []
    assert investments[0].asset_class_code == "unclassified"


# ---------------------------------------------------------------------------
# IE-05: unknown Investment Type → row-level error, investment skipped
# ---------------------------------------------------------------------------


def test_ie05_unknown_investment_type_emits_error_and_skips() -> None:
    names = ["Bad", "Good"]
    sheets = {
        "attributes": _attributes_payload(names, types=["NotARealType", "Private Equity"]),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)

    assert {i.name for i in investments} == {"Good"}
    assert len(extractor.errors) == 1
    err = extractor.errors[0]
    assert err.investment_name == "Bad"
    assert "Unknown" in err.message or "unknown" in err.message.lower()


# ---------------------------------------------------------------------------
# IE-06: NaN / None / blank string NAV cells are skipped silently
# ---------------------------------------------------------------------------


def test_ie06_nan_and_blank_navs_skipped_silently() -> None:
    import math

    names = ["Inv"]
    sheets = {
        "attributes": _attributes_payload(names, types=["private_equity"]),
        "navs_actual": _timeseries_payload(
            names,
            [
                ("2024-01-01", [None]),
                ("2024-02-01", [math.nan]),
                ("2024-03-01", [""]),
                ("2024-04-01", [42.0]),
            ],
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)

    assert extractor.errors == []
    assert len(investments[0].navs) == 1
    assert investments[0].navs[0].as_of_date == date(2024, 4, 1)
    assert investments[0].navs[0].nav_value == Decimal("42.0")


# ---------------------------------------------------------------------------
# IE-07: vintage year as string ("2020") becomes int 2020
# ---------------------------------------------------------------------------


def test_ie07_vintage_year_string_coerces_to_int() -> None:
    names = ["Inv"]
    sheets = {
        "attributes": _attributes_payload(
            names,
            types=["private_equity"],
            vintage_years=["2020"],
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    assert investments[0].vintage_year == 2020


def test_ie07b_vintage_year_unparseable_emits_warning_and_nulls() -> None:
    names = ["Inv"]
    sheets = {
        "attributes": _attributes_payload(
            names,
            types=["private_equity"],
            vintage_years=["abracadabra"],
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    assert investments[0].vintage_year is None
    assert len(extractor.errors) == 1
    assert "Vintage Year" in extractor.errors[0].message


# ---------------------------------------------------------------------------
# IE-08: missing Attributes sheet raises ImportFormatError
# ---------------------------------------------------------------------------


def test_ie08_missing_attributes_sheet_raises_format_error() -> None:
    sheets = {"navs_actual": _timeseries_payload(["Inv"], [("2024-01-01", [1.0])])}
    extractor = InvestmentExtractor()
    with pytest.raises(ImportFormatError):
        extractor.extract(sheets)


def test_ie08b_attributes_with_no_columns_raises_format_error() -> None:
    sheets = {
        "attributes": {
            "columns": [],
            "index": ["Investment Type"],
            "data": [[]],
        }
    }
    extractor = InvestmentExtractor()
    with pytest.raises(ImportFormatError):
        extractor.extract(sheets)


# ---------------------------------------------------------------------------
# IE-09: re-run resets the errors buffer
# ---------------------------------------------------------------------------


def test_ie09_extractor_resets_errors_between_calls() -> None:
    names = ["Inv"]
    bad = {
        "attributes": _attributes_payload(names, types=["private_equity"]),
        "cash_flow_out_actual": _timeseries_payload(names, [("2024-01-01", [100.0])]),
    }
    good = {
        "attributes": _attributes_payload(names, types=["private_equity"]),
    }
    extractor = InvestmentExtractor()
    extractor.extract(bad)
    assert len(extractor.errors) == 1
    extractor.extract(good)
    assert extractor.errors == []


# ---------------------------------------------------------------------------
# IE-10: zero cashflow in CF Out / CF In is dropped (no row, no error)
# ---------------------------------------------------------------------------


def test_ie10_zero_cashflow_dropped_silently() -> None:
    names = ["Inv"]
    sheets = {
        "attributes": _attributes_payload(names, types=["private_equity"]),
        "cash_flow_out_actual": _timeseries_payload(names, [("2024-01-01", [0.0])]),
        "cash_flow_in_actual": _timeseries_payload(names, [("2024-02-01", [0.0])]),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    assert extractor.errors == []
    assert investments[0].cashflows == ()


# ---------------------------------------------------------------------------
# IE-11: canonical investment-type values pass through unchanged
# ---------------------------------------------------------------------------


def test_ie11_canonical_types_pass_through() -> None:
    names = [
        "PE",
        "PD",
        "RE",
        "Infra",
        "LE",
        "LB",
        "Other",
    ]
    sheets = {
        "attributes": _attributes_payload(
            names,
            types=[
                "private_equity",
                "private_debt",
                "real_estate",
                "infra_equity",
                "listed_equity",
                "listed_bonds",
                "other",
            ],
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    assert extractor.errors == []
    by_name = {i.name: i.investment_type for i in investments}
    assert by_name == {
        "PE": "private_equity",
        "PD": "private_debt",
        "RE": "real_estate",
        "Infra": "infra_equity",
        "LE": "listed_equity",
        "LB": "listed_bonds",
        "Other": "other",
    }


# ---------------------------------------------------------------------------
# IE-12: cashflow_timestamp default is 12:00 UTC on the as_of_date
# ---------------------------------------------------------------------------


def test_ie12_cashflow_timestamp_default_noon_utc() -> None:
    names = ["Inv"]
    sheets = {
        "attributes": _attributes_payload(names, types=["private_equity"]),
        "cash_flow_out_actual": _timeseries_payload(names, [("2024-06-15T00:00:00.000", [-100.0])]),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    cf = investments[0].cashflows[0]
    assert cf.flow_timestamp == datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)
