# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural tests for ``services.chart_specs.investment_cashflows_nav``.

Crucial invariant: the spec is dual-axis with the NAV trace bound
to ``yaxis2`` (Plotly's overlaid right-hand axis) and the
``yaxis2.overlaying = "y"`` configuration.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd

from services.chart_specs import build_cashflows_nav_spec
from services.chart_specs.base import get_chart_theme


def _ts(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=timezone.utc)


def _sample_inputs() -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    cashflows = pd.DataFrame(
        {
            "flow_timestamp": [
                _ts(2024, 1, 31),
                _ts(2024, 7, 1),
                _ts(2025, 6, 30),
            ],
            "flow_type": ["capital_call", "capital_call", "distribution"],
            "amount": [-100.0, -50.0, 30.0],
        }
    )
    nav = pd.Series(
        [100.0, 160.0, 200.0],
        index=[date(2024, 1, 31), date(2024, 12, 31), date(2025, 6, 30)],
    )
    ncg = pd.Series(
        [0.0, 10.0, 80.0],
        index=[
            pd.Timestamp(date(2024, 1, 31), tz="UTC"),
            pd.Timestamp(date(2024, 12, 31), tz="UTC"),
            pd.Timestamp(date(2025, 6, 30), tz="UTC"),
        ],
    )
    return cashflows, nav, ncg


def test_top_level_keys() -> None:
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X")
    assert set(spec.keys()) == {"data", "layout", "config"}


def test_four_traces_calls_dist_ncg_nav() -> None:
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X")
    names = [t["name"] for t in spec["data"]]
    assert "Calls" in names
    assert "Distributions" in names
    assert "Net Capital Gain" in names
    assert "NAV" in names
    assert len(spec["data"]) == 4


def test_nav_trace_bound_to_secondary_axis() -> None:
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X")
    nav_trace = next(t for t in spec["data"] if t["name"] == "NAV")
    assert nav_trace["yaxis"] == "y2"


def test_layout_yaxis2_overlays_yaxis() -> None:
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X")
    assert spec["layout"]["yaxis2"]["overlaying"] == "y"
    assert spec["layout"]["yaxis2"]["side"] == "right"


def test_calls_and_distributions_are_bar_traces() -> None:
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X")
    calls = next(t for t in spec["data"] if t["name"] == "Calls")
    dist = next(t for t in spec["data"] if t["name"] == "Distributions")
    assert calls["type"] == "bar"
    assert dist["type"] == "bar"


def test_ncg_is_line_trace_on_left_axis() -> None:
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X")
    ncg_trace = next(t for t in spec["data"] if t["name"] == "Net Capital Gain")
    assert ncg_trace["type"] == "scatter"
    assert ncg_trace["mode"] == "lines"
    assert ncg_trace["yaxis"] == "y"


def test_no_distributions_renders_empty_distribution_trace() -> None:
    cashflows = pd.DataFrame(
        {
            "flow_timestamp": [_ts(2024, 1, 31)],
            "flow_type": ["capital_call"],
            "amount": [-100.0],
        }
    )
    nav = pd.Series([100.0], index=[date(2024, 1, 31)])
    ncg = pd.Series([0.0], index=[pd.Timestamp(date(2024, 1, 31), tz="UTC")])
    spec = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund Y")
    dist = next(t for t in spec["data"] if t["name"] == "Distributions")
    assert dist["x"] == []
    assert dist["y"] == []
    # The trace still exists for layout stability.
    assert len(spec["data"]) == 4


def test_theme_applied() -> None:
    theme = get_chart_theme()
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X")
    assert spec["layout"]["paper_bgcolor"] == theme["colours"]["background"]
    assert spec["layout"]["plot_bgcolor"] == theme["colours"]["plot_area"]


def test_title_includes_investment_name() -> None:
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(cashflows, nav, ncg, "My Fund")
    assert "My Fund" in spec["layout"]["title"]["text"]


def test_pure_function_deterministic() -> None:
    cashflows, nav, ncg = _sample_inputs()
    a = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X")
    b = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X")
    assert a == b


# ---------------------------------------------------------------------------
# ADR-0113 §1 — unified right axis end ("universe as-of")
# ---------------------------------------------------------------------------


def test_axis_end_omitted_leaves_spec_unchanged() -> None:
    """The default is byte-identical to the pre-ADR-0113 output."""
    cashflows, nav, ncg = _sample_inputs()
    baseline = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X")
    cashflows, nav, ncg = _sample_inputs()
    assert baseline == build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X", axis_end=None)
    assert "autorangeoptions" not in baseline["layout"]["xaxis"]


def test_axis_end_extends_the_x_axis_autorange() -> None:
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X", axis_end=date(2025, 12, 31))
    assert spec["layout"]["xaxis"]["autorangeoptions"] == {"include": "2025-12-31"}


def test_axis_end_leaves_the_data_untouched() -> None:
    cashflows, nav, ncg = _sample_inputs()
    without = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X")
    cashflows, nav, ncg = _sample_inputs()
    with_end = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X", axis_end=date(2025, 12, 31))
    assert with_end["data"] == without["data"]


def test_axis_end_not_applied_to_an_empty_figure() -> None:
    spec = build_cashflows_nav_spec(
        pd.DataFrame(columns=["flow_timestamp", "flow_type", "amount"]),
        pd.Series(dtype="float64"),
        pd.Series(dtype="float64"),
        "Empty Fund",
        axis_end=date(2025, 12, 31),
    )
    assert "autorangeoptions" not in spec["layout"]["xaxis"]


# ---------------------------------------------------------------------------
# ADR-0113 §2 — plan-tail display trace on the NAV line
# ---------------------------------------------------------------------------

_PLAN_TRACE_NAME = "NAV (Plan)"


def _nav_plan() -> pd.Series:
    """Plan NAVs straddling the last actual (2025-06-30) and the tail end."""
    return pd.Series(
        [155.0, 210.0, 240.0, 300.0],
        index=[
            date(2024, 12, 31),  # before the last actual — dropped
            date(2025, 9, 30),  # inside the tail window
            date(2025, 12, 31),  # inside the tail window
            date(2026, 6, 30),  # beyond the tail end — dropped
        ],
    )


def _plan_trace(spec: dict) -> dict:
    return next(t for t in spec["data"] if t["name"] == _PLAN_TRACE_NAME)


def test_plan_tail_defaults_leave_the_spec_unchanged() -> None:
    """Without both parameters the output is byte-identical to today."""
    cashflows, nav, ncg = _sample_inputs()
    baseline = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X")
    cashflows, nav, ncg = _sample_inputs()
    assert baseline == build_cashflows_nav_spec(
        cashflows, nav, ncg, "Fund X", nav_plan=None, plan_tail_end=None
    )
    # A plan series without a tail end (and vice versa) is inert too.
    cashflows, nav, ncg = _sample_inputs()
    assert baseline == build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X", nav_plan=_nav_plan())
    cashflows, nav, ncg = _sample_inputs()
    assert baseline == build_cashflows_nav_spec(
        cashflows, nav, ncg, "Fund X", plan_tail_end=date(2025, 12, 31)
    )
    assert len(baseline["data"]) == 4


def test_plan_tail_adds_one_trace_on_the_secondary_axis() -> None:
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(
        cashflows,
        nav,
        ncg,
        "Fund X",
        nav_plan=_nav_plan(),
        plan_tail_end=date(2025, 12, 31),
    )
    assert len(spec["data"]) == 5
    plan = _plan_trace(spec)
    assert plan["type"] == "scatter"
    assert plan["mode"] == "lines"
    assert plan["yaxis"] == "y2"


def test_plan_tail_window_and_anchor() -> None:
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(
        cashflows,
        nav,
        ncg,
        "Fund X",
        nav_plan=_nav_plan(),
        plan_tail_end=date(2025, 12, 31),
    )
    nav_trace = next(t for t in spec["data"] if t["name"] == "NAV")
    plan = _plan_trace(spec)
    # Anchor + the two rows inside the window, in the NAV trace's own
    # timestamp formatting so the dashed line joins the solid one.
    assert plan["x"][0] == nav_trace["x"][-1]
    assert plan["y"][0] == nav_trace["y"][-1]
    assert plan["y"] == [200.0, 210.0, 240.0]
    assert len(plan["x"]) == 3


def test_plan_tail_styling_is_dashed_muted_and_same_hue_as_the_nav_line() -> None:
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(
        cashflows,
        nav,
        ncg,
        "Fund X",
        nav_plan=_nav_plan(),
        plan_tail_end=date(2025, 12, 31),
    )
    nav_trace = next(t for t in spec["data"] if t["name"] == "NAV")
    plan = _plan_trace(spec)
    assert plan["line"]["dash"] == "dash"
    assert plan["line"]["color"] == nav_trace["line"]["color"]
    assert plan["opacity"] == 0.65
    assert plan["hovertemplate"].endswith("(Plan)<extra></extra>")


def test_plan_tail_leaves_bars_and_ncg_untouched() -> None:
    """Plan cashflows are out of scope — only the NAV line gains a tail."""
    cashflows, nav, ncg = _sample_inputs()
    without = build_cashflows_nav_spec(cashflows, nav, ncg, "Fund X")
    cashflows, nav, ncg = _sample_inputs()
    with_tail = build_cashflows_nav_spec(
        cashflows,
        nav,
        ncg,
        "Fund X",
        nav_plan=_nav_plan(),
        plan_tail_end=date(2025, 12, 31),
    )
    assert with_tail["data"][:4] == without["data"]


def test_plan_tail_empty_plan_series_adds_no_trace() -> None:
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(
        cashflows,
        nav,
        ncg,
        "Fund X",
        nav_plan=pd.Series(dtype="float64"),
        plan_tail_end=date(2025, 12, 31),
    )
    assert len(spec["data"]) == 4
    assert _PLAN_TRACE_NAME not in [t["name"] for t in spec["data"]]


def test_plan_tail_empty_after_filtering_renders_an_empty_trace() -> None:
    """Plan rows all inside the actual period: the line ends, nothing drawn."""
    cashflows, nav, ncg = _sample_inputs()
    spec = build_cashflows_nav_spec(
        cashflows,
        nav,
        ncg,
        "Fund X",
        nav_plan=_nav_plan(),
        plan_tail_end=date(2025, 6, 30),
    )
    plan = _plan_trace(spec)
    assert plan["x"] == []
    assert plan["y"] == []


def test_plan_tail_without_actual_navs_has_an_open_lower_bound() -> None:
    cashflows = pd.DataFrame(columns=["flow_timestamp", "flow_type", "amount"])
    spec = build_cashflows_nav_spec(
        cashflows,
        pd.Series(dtype="float64"),
        pd.Series(dtype="float64"),
        "Fresh Fund",
        nav_plan=_nav_plan(),
        plan_tail_end=date(2025, 12, 31),
    )
    plan = _plan_trace(spec)
    # No anchor to prepend; every plan row up to the tail end qualifies.
    assert plan["y"] == [155.0, 210.0, 240.0]
