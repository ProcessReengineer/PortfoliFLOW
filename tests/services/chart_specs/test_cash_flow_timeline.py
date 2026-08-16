# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the Cash Flow Planning timeline spec (ADR-0104 §4/§6).

DB-free: the DTOs are constructed by hand, so the spec is exercised as the
pure projection it is — no book, no repository, no clock.

Three things are pinned:

* **Series composition.** What the lens draws, per view: the currency lines
  of the *active* world, its total in the accent role, and — in scenario view
  only — the baseline total as a ghost. Empty cells survive as ``None``, never
  as ``0`` (the empty-is-not-zero rule the DTO carries).
* **The seam.** One amber dashed rule between the last actual column and the
  first plan one, annotated with the date it means.
* **The shared y-range** — the binding correctness rule: the range spans the
  union of *both* worlds, so the Baseline/Scenario toggle re-draws the picture
  without re-scaling the ruler, and the gap between the two totals is readable
  as a magnitude rather than as a shape.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from services.chart_specs.cash_flow_timeline import (
    SEAM_COLOUR,
    CurrencyView,
    WorldView,
    build_cash_flow_timeline_spec,
)
from services.investments.cash_flow_timeline import (
    CashFlowPlanningResult,
    CashFlowTimeline,
    CurrencyRow,
    Periodisation,
    TimelinePeriod,
)

_LABELS: tuple[str, ...] = ("Q1 2026", "Q2 2026", "Q3 2026", "Q4 2026")
_SEAM_INDEX: int = 2
_SEAM_DATE: date = date(2026, 6, 30)


def _periods() -> tuple[TimelinePeriod, ...]:
    """Two actual columns, then two plan columns — the seam at index 2."""
    return tuple(
        TimelinePeriod(
            end_date=date(2026, 3 * (index + 1), 28),
            label=label,
            is_actual=index < _SEAM_INDEX,
        )
        for index, label in enumerate(_LABELS)
    )


def _decimals(
    *values: float | None,
) -> tuple[Decimal | None, ...]:
    return tuple(None if value is None else Decimal(str(value)) for value in values)


def _timeline(
    *,
    eur: tuple[Decimal | None, ...],
    usd: tuple[Decimal | None, ...],
    total: tuple[Decimal | None, ...],
) -> CashFlowTimeline:
    return CashFlowTimeline(
        periods=_periods(),
        seam_index=_SEAM_INDEX,
        seam_date=_SEAM_DATE,
        currency_rows=(
            CurrencyRow(currency="EUR", balances=eur),
            CurrencyRow(currency="USD", balances=usd),
        ),
        total=total,
        functional_currency="EUR",
        periodisation=Periodisation.QUARTERLY,
    )


def _result() -> CashFlowPlanningResult:
    """Two worlds over one grid.

    The scenario spends cash after the seam, so its total dips *below* the
    baseline's; the baseline in turn climbs *above* the scenario's at the last
    column. Neither world therefore brackets the other on its own, which is
    what makes the shared-range rule testable rather than incidental.
    """
    baseline = _timeline(
        eur=_decimals(100, 110, 120, 200),
        usd=_decimals(None, 40, 40, 40),
        total=_decimals(100, 146, 156, 236),
    )
    scenario = _timeline(
        eur=_decimals(100, 110, 20, 90),
        usd=_decimals(None, 40, 40, 40),
        total=_decimals(100, 146, 56, 126),
    )
    return CashFlowPlanningResult(baseline=baseline, scenario=scenario)


def _traces(spec: dict[str, Any]) -> dict[str, list[Any]]:
    """Index a spec's traces by name."""
    return {trace["name"]: trace["y"] for trace in spec["data"]}


# ---------------------------------------------------------------------------
# Series composition
# ---------------------------------------------------------------------------


def test_scenario_view_draws_currency_lines_and_both_totals() -> None:
    """Per-currency scenario view: one line per currency, two totals."""
    spec = build_cash_flow_timeline_spec(_result())

    names = [trace["name"] for trace in spec["data"]]
    assert names == ["EUR", "USD", "Total (scenario)", "Total (baseline)"]

    traces = _traces(spec)
    # The currency lines are the *scenario*'s, in position currency.
    assert traces["EUR"] == [100.0, 110.0, 20.0, 90.0]
    assert traces["Total (scenario)"] == [100.0, 146.0, 56.0, 126.0]
    assert traces["Total (baseline)"] == [100.0, 146.0, 156.0, 236.0]


def test_empty_cell_stays_empty_and_never_becomes_zero() -> None:
    """A currency with no observation contributes ``None``, not ``0.0``."""
    spec = build_cash_flow_timeline_spec(_result())

    usd = _traces(spec)["USD"]
    assert usd[0] is None, "an unopened currency must not read as a zero balance"
    assert usd[1:] == [40.0, 40.0, 40.0]
    usd_trace = next(t for t in spec["data"] if t["name"] == "USD")
    assert usd_trace["connectgaps"] is False


def test_the_x_axis_is_the_periods_of_the_grid() -> None:
    """Every trace runs over the DTO's period labels, in grid order."""
    spec = build_cash_flow_timeline_spec(_result())

    assert spec["layout"]["xaxis"]["categoryarray"] == list(_LABELS)
    for trace in spec["data"]:
        assert trace["x"] == list(_LABELS)


def test_functional_only_view_drops_the_currency_lines() -> None:
    """The converted totals remain; the position-currency rows go."""
    spec = build_cash_flow_timeline_spec(_result(), currency_view=CurrencyView.FUNCTIONAL_ONLY)

    names = [trace["name"] for trace in spec["data"]]
    assert names == ["Total (scenario)", "Total (baseline)"]


def test_baseline_view_states_the_baseline_and_nothing_else() -> None:
    """Baseline view draws the book's own plan world — no scenario series."""
    spec = build_cash_flow_timeline_spec(_result(), view=WorldView.BASELINE)

    names = [trace["name"] for trace in spec["data"]]
    assert names == ["EUR", "USD", "Total (baseline)"]
    assert "Total (scenario)" not in names

    traces = _traces(spec)
    assert traces["EUR"] == [100.0, 110.0, 120.0, 200.0]
    assert traces["Total (baseline)"] == [100.0, 146.0, 156.0, 236.0]


def test_the_accent_belongs_to_the_total_alone() -> None:
    """No currency line wears the accent — it is the total's colour."""
    spec = build_cash_flow_timeline_spec(_result())

    total = next(t for t in spec["data"] if t["name"] == "Total (scenario)")
    accent = total["line"]["color"]
    for trace in spec["data"]:
        if trace["name"] in ("EUR", "USD"):
            assert trace["line"]["color"] != accent


def test_the_baseline_total_is_a_dashed_ghost() -> None:
    """The baseline reads as a reference line, not as a second series."""
    spec = build_cash_flow_timeline_spec(_result())

    ghost = next(t for t in spec["data"] if t["name"] == "Total (baseline)")
    scenario = next(t for t in spec["data"] if t["name"] == "Total (scenario)")
    assert ghost["line"]["dash"] == "dash"
    assert "dash" not in scenario["line"]


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


def test_the_seam_is_one_amber_rule_between_actuals_and_plan() -> None:
    """One dashed amber vline, half a step left of the first plan column."""
    spec = build_cash_flow_timeline_spec(_result())

    shapes = spec["layout"]["shapes"]
    assert len(shapes) == 1
    seam = shapes[0]
    assert seam["x0"] == seam["x1"] == _SEAM_INDEX - 0.5
    assert seam["line"]["color"] == SEAM_COLOUR
    assert seam["line"]["dash"] == "dash"
    assert seam["yref"] == "paper"


def test_the_seam_annotation_names_the_date_it_means() -> None:
    """The rule is labelled with t₀ — the book's last actual statement."""
    spec = build_cash_flow_timeline_spec(_result())

    annotations = spec["layout"]["annotations"]
    assert len(annotations) == 1
    annotation = annotations[0]
    assert "last actual" in annotation["text"]
    assert _SEAM_DATE.isoformat() in annotation["text"]
    assert annotation["x"] == _SEAM_INDEX - 0.5


# ---------------------------------------------------------------------------
# The shared y-range — the honest-comparison rule
# ---------------------------------------------------------------------------


def test_the_y_range_spans_both_worlds() -> None:
    """The range brackets the union of baseline and scenario, not one world.

    The scenario's floor (20) lies below every baseline value and the
    baseline's ceiling (236) above every scenario value, so a range taken from
    either world alone would clip the other.
    """
    spec = build_cash_flow_timeline_spec(_result())

    low, high = spec["layout"]["yaxis"]["range"]
    assert low <= 20.0, "the scenario's floor must fit"
    assert high >= 236.0, "the baseline's ceiling must fit"


def test_the_y_range_does_not_move_when_the_toggle_does() -> None:
    """Baseline and scenario views share one ruler (ADR-0104 §5).

    Auto-scaling each world to its own data would make the two pictures
    incomparable — the gap between them would read as a shape rather than a
    magnitude.
    """
    scenario = build_cash_flow_timeline_spec(_result(), view=WorldView.SCENARIO)
    baseline = build_cash_flow_timeline_spec(_result(), view=WorldView.BASELINE)

    assert scenario["layout"]["yaxis"]["range"] == baseline["layout"]["yaxis"]["range"]


def test_the_functional_only_range_ignores_the_undrawn_currency_rows() -> None:
    """A row nobody can see must not scale the chart.

    Under ``functional-only`` the position-currency rows are not drawn in
    either world, so a large balance in one of them may not push the totals
    into a corner of the plot.
    """
    baseline = _timeline(
        eur=_decimals(100, 110, 120, 9_000_000),
        usd=_decimals(None, 40, 40, 40),
        total=_decimals(100, 146, 156, 236),
    )
    result = CashFlowPlanningResult(baseline=baseline, scenario=baseline)

    spec = build_cash_flow_timeline_spec(result, currency_view=CurrencyView.FUNCTIONAL_ONLY)

    _low, high = spec["layout"]["yaxis"]["range"]
    assert high < 1_000.0


def test_a_grid_of_empty_cells_leaves_the_range_to_plotly() -> None:
    """No balance in either world: no range is asserted."""
    empty = _timeline(
        eur=_decimals(None, None, None, None),
        usd=_decimals(None, None, None, None),
        total=_decimals(None, None, None, None),
    )
    spec = build_cash_flow_timeline_spec(CashFlowPlanningResult(baseline=empty, scenario=empty))

    assert "range" not in spec["layout"]["yaxis"]


# ---------------------------------------------------------------------------
# Degenerate grid
# ---------------------------------------------------------------------------


def test_an_empty_grid_yields_the_themed_empty_state() -> None:
    """A result with no periods states so, rather than drawing an empty axis."""
    empty = CashFlowTimeline(
        periods=(),
        seam_index=0,
        seam_date=_SEAM_DATE,
        currency_rows=(),
        total=(),
        functional_currency="EUR",
        periodisation=Periodisation.QUARTERLY,
    )
    spec = build_cash_flow_timeline_spec(CashFlowPlanningResult(baseline=empty, scenario=empty))

    assert spec["data"] == []
    assert "No cash-flow periods" in spec["layout"]["annotations"][0]["text"]


def test_the_theme_is_applied() -> None:
    """The spec carries the house dark canvas, not Plotly's default."""
    spec = build_cash_flow_timeline_spec(_result())

    assert spec["layout"]["paper_bgcolor"]
    assert spec["layout"]["font"]["family"]
