# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the Scenario Analysis impact chart pair (ADR-0104 §5).

DB-free: the :class:`ScenarioResult` DTOs are constructed by hand, so the spec
is exercised as the pure projection it is — no book, no repository, no assembly.

Four things are pinned, each a §5 verification of the S34.5 brief:

* **The shared axes** — the range spans the union of *both* worlds and does not
  move between the two panels (the binding honest-comparison rule); computed
  once per axis, applied to both.
* **The ghost baseline** — the scenario panel repeats the baseline in the
  theme's neutral grey; the baseline panel does not.
* **The identical-history invariant** — left of the seam the drawn scenario NAV
  line and its baseline ghost are coincident (overlays never touch actuals).
* **The seam** — one amber dashed rule at t₀, in the *same* ``SEAM_COLOUR`` the
  cash-flow timeline draws (one formulation, imported not restated).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from services.chart_specs import cash_flow_timeline, scenario_impact
from services.chart_specs.scenario_impact import (
    SEAM_COLOUR,
    build_scenario_impact_pair,
)
from services.planning_desk.scenario_results import (
    CompositionPair,
    ScenarioResult,
    ScenarioSeriesPair,
)
from services.analytics.portfolio_aggregation import FundCompositionBreakdown

_GRID: tuple[date, ...] = (
    date(2025, 12, 31),
    date(2026, 6, 30),
    date(2026, 9, 30),
    date(2026, 12, 31),
)
_LABELS: list[str] = ["Q4 2025", "Q2 2026", "Q3 2026", "Q4 2026"]
_SEAM_INDEX: int = 2


def _D(*values: float | None) -> tuple[Decimal | None, ...]:
    return tuple(None if v is None else Decimal(str(v)) for v in values)


def _floats(*values: float | None) -> tuple[float | None, ...]:
    return tuple(values)


def _result(
    *,
    nav_baseline: tuple[Decimal | None, ...],
    nav_scenario: tuple[Decimal | None, ...],
    ret_baseline: tuple[float | None, ...],
    ret_scenario: tuple[float | None, ...],
) -> ScenarioResult:
    """Assemble a hand-built result over the four-period grid.

    The Σ-NAV pair carries Decimal money; the return-index pair carries rebased
    ``float`` — the two numeric kinds the DTO keeps apart.
    """
    empty = FundCompositionBreakdown(rows=[])
    return ScenarioResult(
        nav_path=ScenarioSeriesPair(
            period_ends=_GRID,
            seam_index=_SEAM_INDEX,
            baseline=nav_baseline,
            scenario=nav_scenario,
        ),
        return_index=ScenarioSeriesPair(
            period_ends=_GRID,
            seam_index=_SEAM_INDEX,
            baseline=ret_baseline,
            scenario=ret_scenario,
        ),
        kpis=(),
        headroom=(),
        composition=CompositionPair(baseline=empty, scenario=empty),
    )


def _adverse_result() -> ScenarioResult:
    """A scenario that dips below and recovers slower than the baseline.

    History (left of the seam, first two columns) is identical in both worlds —
    the invariant. After the seam the scenario NAV drops and the baseline climbs
    above it, so neither world brackets the other on its own and the shared
    range is testable rather than incidental.
    """
    return _result(
        nav_baseline=_D(200_000_000, 205_000_000, 214_000_000, 238_600_000),
        nav_scenario=_D(200_000_000, 205_000_000, 196_000_000, 219_200_000),
        ret_baseline=_floats(None, 100.0, 106.0, 114.8),
        ret_scenario=_floats(None, 100.0, 98.0, 107.9),
    )


def _traces(spec: dict[str, Any]) -> dict[str, list[Any]]:
    return {trace["name"]: trace["y"] for trace in spec["data"]}


# ---------------------------------------------------------------------------
# The shared axes — the honest-comparison rule
# ---------------------------------------------------------------------------


def test_the_nav_range_spans_both_worlds() -> None:
    """The left (NAV) range brackets the union of baseline and scenario.

    The scenario's floor (196m) lies below the baseline's third column (214m)
    and the baseline's ceiling (238.6m) above the scenario's, so a range taken
    from either world alone would clip the other. Values are stated in millions.
    """
    baseline, scenario = build_scenario_impact_pair(
        _adverse_result(), functional_currency="EUR", labels=_LABELS
    )

    low, high = baseline["layout"]["yaxis"]["range"]
    assert low <= 196.0, "the scenario's floor must fit"
    assert high >= 238.6, "the baseline's ceiling must fit"
    # Both panels carry the very same NAV range object-for-value.
    assert scenario["layout"]["yaxis"]["range"] == [low, high]


def test_the_ranges_do_not_move_between_panels() -> None:
    """Baseline and scenario panels share one ruler on both axes.

    Auto-scaling each panel to its own world would make the two pictures
    incomparable — the gap would read as a shape rather than a magnitude
    (ADR-0104 §5).
    """
    baseline, scenario = build_scenario_impact_pair(
        _adverse_result(), functional_currency="EUR", labels=_LABELS
    )

    assert baseline["layout"]["yaxis"]["range"] == scenario["layout"]["yaxis"]["range"]
    assert baseline["layout"]["yaxis2"]["range"] == scenario["layout"]["yaxis2"]["range"]


# ---------------------------------------------------------------------------
# The ghost baseline
# ---------------------------------------------------------------------------


def test_the_scenario_panel_repeats_the_baseline_as_a_ghost() -> None:
    """The scenario panel carries a neutral-grey dashed baseline reference."""
    theme_neutral = cash_flow_timeline.get_chart_theme()["colours"]["neutral"]
    _baseline, scenario = build_scenario_impact_pair(
        _adverse_result(), functional_currency="EUR", labels=_LABELS
    )

    ghost = next(t for t in scenario["data"] if t["name"] == "NAV (baseline)")
    assert ghost["line"]["color"] == theme_neutral
    assert ghost["line"]["dash"] == "dash"
    # In millions, the baseline NAV ghost states the baseline world.
    assert ghost["y"] == [200.0, 205.0, 214.0, 238.6]


def test_the_baseline_panel_carries_no_scenario_series() -> None:
    """The baseline panel draws the unmodified plan world alone (D19)."""
    baseline, _scenario = build_scenario_impact_pair(
        _adverse_result(), functional_currency="EUR", labels=_LABELS
    )

    names = [t["name"] for t in baseline["data"]]
    assert "NAV (baseline)" not in names, "no ghost in the baseline panel"
    assert not any("baseline" in n and "Total" in n for n in names)


# ---------------------------------------------------------------------------
# The identical-history invariant
# ---------------------------------------------------------------------------


def test_the_history_is_coincident_left_of_the_seam() -> None:
    """The scenario NAV line and its baseline ghost agree left of t₀.

    Overlays never touch actuals, so the two worlds are one before the seam —
    the chart renders that equality, column for column.
    """
    _baseline, scenario = build_scenario_impact_pair(
        _adverse_result(), functional_currency="EUR", labels=_LABELS
    )

    traces = _traces(scenario)
    scenario_nav = traces["Portfolio NAV (Σ, EURm)"]
    ghost_nav = traces["NAV (baseline)"]
    for index in range(_SEAM_INDEX):
        assert scenario_nav[index] == ghost_nav[index]


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


def test_the_seam_is_one_amber_rule_at_t0() -> None:
    """Each panel carries one dashed amber vline half a step left of the seam."""
    for spec in build_scenario_impact_pair(
        _adverse_result(), functional_currency="EUR", labels=_LABELS
    ):
        shapes = spec["layout"]["shapes"]
        assert len(shapes) == 1
        seam = shapes[0]
        assert seam["x0"] == seam["x1"] == _SEAM_INDEX - 0.5
        assert seam["line"]["color"] == SEAM_COLOUR
        assert seam["line"]["dash"] == "dash"
        assert seam["yref"] == "paper"


def test_the_seam_colour_is_the_one_shared_formulation() -> None:
    """The impact spec draws the *same* seam the cash-flow spec does.

    One formulation, imported not restated (closure §8.5 / decision 4.13). If
    the two diverged the paired lens would draw two different ambers at the same
    t₀ — the failure §2 prevents.
    """
    assert scenario_impact.SEAM_COLOUR is cash_flow_timeline.SEAM_COLOUR


# ---------------------------------------------------------------------------
# Dual axis, empty overlay, empty grid, theme
# ---------------------------------------------------------------------------


def test_the_return_index_lives_on_the_right_axis() -> None:
    """Σ-NAV draws on the left axis, the return index on the right."""
    baseline, _scenario = build_scenario_impact_pair(
        _adverse_result(), functional_currency="EUR", labels=_LABELS
    )

    nav = next(t for t in baseline["data"] if t["name"].startswith("Portfolio NAV"))
    ret = next(t for t in baseline["data"] if t["name"] == "Total return (idx)")
    assert nav.get("yaxis", "y") == "y"
    assert ret["yaxis"] == "y2"
    assert baseline["layout"]["yaxis2"]["overlaying"] == "y"
    assert baseline["layout"]["yaxis2"]["side"] == "right"


def test_an_empty_overlay_renders_coincident_lines_and_no_gap() -> None:
    """Baseline ≡ scenario: the scenario NAV line equals its ghost everywhere."""
    nav = _D(200_000_000, 205_000_000, 214_000_000, 238_600_000)
    ret = _floats(None, 100.0, 106.0, 114.8)
    result = _result(
        nav_baseline=nav,
        nav_scenario=nav,
        ret_baseline=ret,
        ret_scenario=ret,
    )

    _baseline, scenario = build_scenario_impact_pair(
        result, functional_currency="EUR", labels=_LABELS
    )

    traces = _traces(scenario)
    assert traces["Portfolio NAV (Σ, EURm)"] == traces["NAV (baseline)"]
    assert traces["Total return (idx)"] == traces["Total return (baseline)"]


def test_a_none_hole_stays_a_gap_never_a_zero() -> None:
    """A ``None`` return value is a gap, not a fabricated zero."""
    baseline, _scenario = build_scenario_impact_pair(
        _adverse_result(), functional_currency="EUR", labels=_LABELS
    )

    ret = next(t for t in baseline["data"] if t["name"] == "Total return (idx)")
    assert ret["y"][0] is None
    assert ret["connectgaps"] is False


def test_an_empty_grid_yields_two_themed_empty_states() -> None:
    """A result with no periods states so, rather than drawing empty axes."""
    empty = FundCompositionBreakdown(rows=[])
    result = ScenarioResult(
        nav_path=ScenarioSeriesPair(period_ends=(), seam_index=0, baseline=(), scenario=()),
        return_index=ScenarioSeriesPair(period_ends=(), seam_index=0, baseline=(), scenario=()),
        kpis=(),
        headroom=(),
        composition=CompositionPair(baseline=empty, scenario=empty),
    )

    baseline, scenario = build_scenario_impact_pair(result)

    for spec in (baseline, scenario):
        assert spec["data"] == []
        assert "No projection" in spec["layout"]["annotations"][0]["text"]


def test_the_theme_is_applied_to_both_panels() -> None:
    """Both panels carry the house dark canvas, not Plotly's default."""
    for spec in build_scenario_impact_pair(
        _adverse_result(), functional_currency="EUR", labels=_LABELS
    ):
        assert spec["layout"]["paper_bgcolor"]
        assert spec["layout"]["font"]["family"]
        # The secondary axis is themed too (else it falls back to a light grid).
        assert spec["layout"]["yaxis2"]["gridcolor"]
