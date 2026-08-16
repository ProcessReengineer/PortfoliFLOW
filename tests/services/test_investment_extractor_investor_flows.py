# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Extractor tests for the investor-flow derivation (ADR-0103 §5).

Pure unit tests — the cashflow extraction path has no DB or FastAPI
dependency. Snapshots use the ``DataFrame.to_json(orient="split")`` shape
that :class:`core.repositories.DataUploadRepository` persists.

ADR-0103 §5 carries plan and actual investor flows in the **existing**
flow-sheet convention: no new sheet, no format delta. The four Cash Flow
In/Out sheets already encode direction (In / Out) and kind (actual / plan),
so a **cash** column on them yields ``investor_flow`` rows instead of the
per-sheet fixed ``distribution`` / ``capital_call`` — the same
derive-from-resolved-type idiom the ADR-0081 income path uses one method
below.

Coverage

* Cash column, ``Cash Flow In plan`` → ``investor_flow`` / ``plan`` /
  positive (a planned contribution).
* Cash column, ``Cash Flow Out actual`` → ``investor_flow`` / ``actual`` /
  negative (a realised withdrawal).
* Sign guards survive the override: an In cell that is negative, and an Out
  cell that is positive, still produce an ``ImportRowError`` on a cash
  column and are dropped.
* **Regression:** a non-cash column on the very same sheets still yields the
  per-sheet fixed types, byte-identically to pre-ADR-0103 behaviour.
* Zero cells stay dropped; the 12:00-UTC stamp is unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from services.data_normalization import InvestmentExtractor


# ---------------------------------------------------------------------------
# Payload builders (shared idiom with the sibling extractor suites)
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


def _timeseries_payload(
    investment_names: list[str],
    rows: list[tuple[str, list[object]]],
) -> dict:
    """Build a wide date-indexed payload (the cashflow-sheet idiom)."""
    return {
        "columns": list(investment_names),
        "index": [iso for iso, _ in rows],
        "data": [vals for _, vals in rows],
    }


# ---------------------------------------------------------------------------
# The derivation itself
# ---------------------------------------------------------------------------


def test_cash_column_yields_investor_flows_on_all_four_sheets() -> None:
    """A cash column's In/Out × actual/plan cells are all investor flows."""
    names = ["Cash EUR"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Cash"]),
        "cash_flow_in_actual": _timeseries_payload(names, [("2024-03-31T00:00:00.000", [5_000.0])]),
        "cash_flow_in_plan": _timeseries_payload(names, [("2024-09-30T00:00:00.000", [7_000.0])]),
        "cash_flow_out_actual": _timeseries_payload(
            names, [("2024-06-30T00:00:00.000", [-2_000.0])]
        ),
        "cash_flow_out_plan": _timeseries_payload(names, [("2024-12-31T00:00:00.000", [-3_000.0])]),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)

    cash = investments[0]
    assert cash.investment_type == "cash"
    flows = cash.cashflows

    # Every one of the four cells is an investor flow — no calls, no
    # distributions: a cash position makes neither.
    assert {f.flow_type for f in flows} == {"investor_flow"}
    assert len(flows) == 4

    by_kind_sign = {(f.flow_kind, "in" if f.amount > 0 else "out"): f for f in flows}
    # Cash Flow In plan → a planned contribution: positive, plan.
    contribution_plan = by_kind_sign[("plan", "in")]
    assert contribution_plan.amount == Decimal("7000.0")
    assert contribution_plan.flow_timestamp == datetime(2024, 9, 30, 12, 0, tzinfo=timezone.utc)
    # Cash Flow Out actual → a realised withdrawal: negative, actual.
    withdrawal_actual = by_kind_sign[("actual", "out")]
    assert withdrawal_actual.amount == Decimal("-2000.0")
    assert withdrawal_actual.flow_timestamp == datetime(2024, 6, 30, 12, 0, tzinfo=timezone.utc)
    # And the other two corners of the 2×2.
    assert by_kind_sign[("actual", "in")].amount == Decimal("5000.0")
    assert by_kind_sign[("plan", "out")].amount == Decimal("-3000.0")

    assert extractor.errors == []


def test_non_cash_column_on_the_same_sheets_is_unchanged() -> None:
    """Regression: the override is a lookup miss for every non-cash type.

    The whole ADR-0103 §5 extractor obligation in one assertion — a private
    fund's columns on the four flow sheets behave byte-identically to their
    pre-ADR-0103 behaviour, whatever a cash column beside them now does.
    """
    names = ["PE Fund", "Cash EUR"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Private Equity", "Cash"]),
        "cash_flow_in_actual": _timeseries_payload(
            names, [("2024-03-31T00:00:00.000", [1_000.0, 5_000.0])]
        ),
        "cash_flow_out_plan": _timeseries_payload(
            names, [("2024-12-31T00:00:00.000", [-800.0, -3_000.0])]
        ),
    }
    extractor = InvestmentExtractor()
    by_name = {inv.name: inv for inv in extractor.extract(sheets)}

    pe_flows = by_name["PE Fund"].cashflows
    assert {(f.flow_type, f.flow_kind) for f in pe_flows} == {
        ("distribution", "actual"),
        ("capital_call", "plan"),
    }
    assert sorted(f.amount for f in pe_flows) == [
        Decimal("-800.0"),
        Decimal("1000.0"),
    ]

    # ...while the cash column beside it derives investor flows.
    assert {f.flow_type for f in by_name["Cash EUR"].cashflows} == {"investor_flow"}
    assert extractor.errors == []


# ---------------------------------------------------------------------------
# The sign guards survive the override
# ---------------------------------------------------------------------------


def test_negative_cell_on_cash_flow_in_still_errors_for_a_cash_column() -> None:
    """The In-sheet sign guard is unchanged by the investor-flow override."""
    names = ["Cash EUR"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Cash"]),
        "cash_flow_in_actual": _timeseries_payload(
            names, [("2024-03-31T00:00:00.000", [-5_000.0])]
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)

    # The sign-violating row is dropped, not sign-coerced (ADR-0043 §3).
    assert investments[0].cashflows == ()
    assert len(extractor.errors) == 1
    assert extractor.errors[0].sheet == "cash_flow_in_actual"
    assert extractor.errors[0].investment_name == "Cash EUR"


def test_positive_cell_on_cash_flow_out_still_errors_for_a_cash_column() -> None:
    """The Out-sheet sign guard is unchanged by the investor-flow override."""
    names = ["Cash EUR"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Cash"]),
        "cash_flow_out_plan": _timeseries_payload(names, [("2024-12-31T00:00:00.000", [3_000.0])]),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)

    assert investments[0].cashflows == ()
    assert len(extractor.errors) == 1
    assert extractor.errors[0].sheet == "cash_flow_out_plan"


def test_zero_cell_on_a_cash_column_is_dropped_without_error() -> None:
    """A literal zero stays dropped — no fabricated zero-value investor flow."""
    names = ["Cash EUR"]
    sheets = {
        "attributes": _attributes_payload(names, types=["Cash"]),
        "cash_flow_in_actual": _timeseries_payload(
            names,
            [
                ("2024-03-31T00:00:00.000", [0.0]),
                ("2024-06-30T00:00:00.000", [4_000.0]),
            ],
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)

    flows = investments[0].cashflows
    assert len(flows) == 1
    assert flows[0].amount == Decimal("4000.0")
    assert flows[0].flow_type == "investor_flow"
    assert extractor.errors == []
