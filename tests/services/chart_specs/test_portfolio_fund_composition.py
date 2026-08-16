# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural tests for the NAV-by-fund chart spec.

The generator is a pure function: a :class:`FundCompositionBreakdown`
(plus an optional :class:`ConcentrationStats`) in, a Plotly figure dict
out. Full dict diffs would be brittle; these tests assert the trace
counts / types, the two-axis wiring (EUR bars vs. a per-fund IRR marker
series on the secondary top axis), the neutral ``"Other"`` bar colour,
the concentration strip, the theme propagation, and the empty-input
behaviour the route consumer relies on. Top-N grouping and the
concentration computation are the caller's job (tested in the analytics
suite), so this spec renders whatever rows and stats it is given.
"""

from __future__ import annotations

from uuid import uuid4


from services.analytics.portfolio_aggregation import (
    ConcentrationStats,
    FundCompositionBreakdown,
    FundCompositionRow,
)
from services.chart_specs import build_fund_composition_spec
from services.chart_specs.base import get_chart_theme


# ---------------------------------------------------------------------------
# Sample inputs
# ---------------------------------------------------------------------------


def _row(
    name: str,
    nav: float,
    weight: float,
    cumulative: float,
    irr: float | None = 0.1,
) -> FundCompositionRow:
    return FundCompositionRow(
        investment_id=uuid4(),
        name=name,
        nav_eur=nav,
        weight_pct=weight,
        cumulative_pct=cumulative,
        irr=irr,
    )


def _sample_breakdown() -> FundCompositionBreakdown:
    return FundCompositionBreakdown(
        rows=[
            _row("Fund B", 300.0, 60.0, 60.0, irr=0.15),
            _row("Fund A", 100.0, 20.0, 80.0, irr=0.05),
            _row("Fund C", 100.0, 20.0, 100.0, irr=None),
        ]
    )


def _sample_concentration() -> ConcentrationStats:
    return ConcentrationStats(
        top1_pct=60.0,
        top3_pct=100.0,
        top5_pct=100.0,
        top10_pct=100.0,
        hhi=0.44,
        fund_count=3,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFundCompositionSpec:
    def test_top_level_keys(self) -> None:
        spec = build_fund_composition_spec(_sample_breakdown())
        assert set(spec.keys()) == {"data", "layout", "config"}

    def test_two_traces_bar_and_scatter(self) -> None:
        spec = build_fund_composition_spec(_sample_breakdown())
        assert len(spec["data"]) == 2
        bar = next(t for t in spec["data"] if t["type"] == "bar")
        scatter = next(t for t in spec["data"] if t["type"] == "scatter")
        assert bar["orientation"] == "h"
        assert scatter["xaxis"] == "x2"

    def test_bars_carry_absolute_nav(self) -> None:
        spec = build_fund_composition_spec(_sample_breakdown())
        bar = next(t for t in spec["data"] if t["type"] == "bar")
        # EUR bars, not shares — the largest fund's NAV is 300.
        assert bar["x"] == [300.0, 100.0, 100.0]

    def test_yaxis_reversed_for_largest_on_top(self) -> None:
        spec = build_fund_composition_spec(_sample_breakdown())
        assert spec["layout"]["yaxis"]["autorange"] == "reversed"

    def test_xaxis_is_eur_not_percent(self) -> None:
        spec = build_fund_composition_spec(_sample_breakdown())
        # Bottom axis is the absolute-EUR axis (SI tick format, no %).
        assert spec["layout"]["xaxis"]["tickformat"] == ".2s"
        assert "ticksuffix" not in spec["layout"]["xaxis"]

    def test_xaxis2_percent_suffix(self) -> None:
        spec = build_fund_composition_spec(_sample_breakdown())
        assert spec["layout"]["xaxis2"]["ticksuffix"] == "%"

    def test_xaxis2_title_is_irr(self) -> None:
        spec = build_fund_composition_spec(_sample_breakdown())
        assert spec["layout"]["xaxis2"]["title"]["text"] == "IRR"

    def test_hovermode_y_unified(self) -> None:
        spec = build_fund_composition_spec(_sample_breakdown())
        assert spec["layout"]["hovermode"] == "y unified"

    def test_second_trace_is_irr_markers(self) -> None:
        spec = build_fund_composition_spec(_sample_breakdown())
        scatter = next(t for t in spec["data"] if t["type"] == "scatter")
        # Markers only — never a line (the funds are ordered by NAV, not
        # by IRR, so joining the dots would imply a false ordering).
        assert scatter["mode"] == "markers"
        assert "lines" not in scatter["mode"]
        assert scatter["name"] == "IRR"
        # x is per-row irr * 100, with None where the IRR did not converge
        # (Fund C carries irr=None in the sample).
        assert scatter["x"] == [15.0, 5.0, None]

    def test_other_bar_uses_neutral_colour(self) -> None:
        theme = get_chart_theme()
        colours = theme["colours"]
        breakdown = FundCompositionBreakdown(
            rows=[
                _row("Fund B", 300.0, 60.0, 60.0, irr=0.15),
                FundCompositionRow(
                    investment_id=None,
                    name="Other (2 funds)",
                    nav_eur=200.0,
                    weight_pct=40.0,
                    cumulative_pct=100.0,
                    irr=0.08,
                ),
            ]
        )
        spec = build_fund_composition_spec(breakdown)
        bar = next(t for t in spec["data"] if t["type"] == "bar")
        colour_list = bar["marker"]["color"]
        assert isinstance(colour_list, list)
        # The "Other" residual (last row, investment_id=None) is neutral;
        # the genuine per-fund bar keeps the accent.
        assert colour_list[-1] == colours["neutral"]
        assert colour_list[0] == colours["primary"]

    def test_concentration_strip_present_when_supplied(self) -> None:
        spec = build_fund_composition_spec(
            _sample_breakdown(), concentration=_sample_concentration()
        )
        annotations = spec["layout"]["annotations"]
        assert len(annotations) == 1
        text = annotations[0]["text"]
        assert "Top 3" in text
        assert "Top 5" in text
        assert "Top 10" in text
        assert "HHI" in text

    def test_concentration_strip_absent_when_none(self) -> None:
        spec = build_fund_composition_spec(_sample_breakdown(), concentration=None)
        assert spec["layout"]["annotations"] == []

    def test_theme_applied(self) -> None:
        theme = get_chart_theme()
        spec = build_fund_composition_spec(_sample_breakdown())
        assert spec["layout"]["paper_bgcolor"] == theme["colours"]["background"]

    def test_empty_breakdown_returns_valid_empty_spec(self) -> None:
        spec = build_fund_composition_spec(FundCompositionBreakdown(rows=[]))
        assert set(spec.keys()) == {"data", "layout", "config"}
        bar = next(t for t in spec["data"] if t["type"] == "bar")
        assert bar["x"] == []
        assert bar["y"] == []
        # No IRR header annotation any more; concentration omitted → no
        # strip annotation either.
        assert spec["layout"]["annotations"] == []
