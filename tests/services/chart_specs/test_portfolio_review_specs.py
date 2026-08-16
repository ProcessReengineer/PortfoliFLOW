# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural tests for the Portfolio Review chart-spec generators.

The Portfolio Review tile generators are pure functions that take a
typed dataclass (or pandas Series) and return a Plotly figure dict.
Full-fidelity dict diffs would be brittle; these tests assert the
trace counts, types, axis configurations, theme propagation, and
legend / hover wiring that the route consumers rely on.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from services.analytics.portfolio_aggregation import (
    InvestedCapitalNavSeries,
    PortfolioCashflowSeries,
    PortfolioMultiplesSeries,
    RegionBreakdown,
    RegionBreakdownRow,
    SectorBreakdown,
    SectorBreakdownRow,
    VintageDistribution,
)
from services.chart_specs import (
    build_invested_capital_nav_spec,
    build_multiples_stacked_spec,
    build_region_treemap_spec,
    build_sector_treemap_spec,
    build_total_return_index_spec,
    build_vintage_bar_spec,
    build_yearly_cashflows_spec,
)
from services.chart_specs.base import get_chart_theme


# ---------------------------------------------------------------------------
# Sample inputs
# ---------------------------------------------------------------------------


def _sample_invested_nav() -> InvestedCapitalNavSeries:
    return InvestedCapitalNavSeries(
        years=[2023, 2024, 2025],
        invested_capital=[100.0, 150.0, 150.0],
        nav=[50.0, 120.0, 200.0],
    )


def _sample_cashflows() -> PortfolioCashflowSeries:
    return PortfolioCashflowSeries(
        years=[2023, 2024],
        calls=[-100.0, -50.0],
        distributions=[0.0, 30.0],
        nav=[50.0, 200.0],
        ncg=[-50.0, 80.0],
    )


def _sample_multiples() -> PortfolioMultiplesSeries:
    return PortfolioMultiplesSeries(
        years=[2023, 2024, 2025],
        dpi=[0.0, 0.20, 0.30],
        rvpi=[1.20, 1.50, 1.40],
        tvpi=[1.20, 1.70, 1.70],
        irr=[0.05, 0.12, 0.10],
    )


def _sample_region() -> RegionBreakdown:
    return RegionBreakdown(
        rows=[
            RegionBreakdownRow(
                region_code="north_america_usa",
                region_display_name="North America — USA",
                nav_eur=300.0,
                weight_pct=75.0,
            ),
            RegionBreakdownRow(
                region_code="dach",
                region_display_name="DACH",
                nav_eur=100.0,
                weight_pct=25.0,
            ),
        ]
    )


def _sample_sector() -> SectorBreakdown:
    return SectorBreakdown(
        rows=[
            SectorBreakdownRow(
                sector_code="tech",
                sector_display_name="Technology",
                nav_eur=200.0,
                weight_pct=66.66,
            ),
            SectorBreakdownRow(
                sector_code="health",
                sector_display_name="Healthcare",
                nav_eur=100.0,
                weight_pct=33.34,
            ),
        ]
    )


def _sample_vintages() -> VintageDistribution:
    return VintageDistribution(
        vintages=[2018, 2020, 2022],
        weight_pct=[50.0, 30.0, 20.0],
        count=[3, 2, 1],
    )


# ---------------------------------------------------------------------------
# Invested Capital + NAV
# ---------------------------------------------------------------------------


class TestInvestedCapitalNavSpec:
    def test_top_level_keys(self) -> None:
        spec = build_invested_capital_nav_spec(_sample_invested_nav())
        assert set(spec.keys()) == {"data", "layout", "config"}

    def test_two_traces(self) -> None:
        spec = build_invested_capital_nav_spec(_sample_invested_nav())
        assert len(spec["data"]) == 2
        names = [t["name"] for t in spec["data"]]
        assert "Invested Capital" in names
        assert "NAV" in names

    def test_x_values_are_year_strings(self) -> None:
        spec = build_invested_capital_nav_spec(_sample_invested_nav())
        for trace in spec["data"]:
            assert trace["x"] == ["2023", "2024", "2025"]

    def test_invested_is_area_filled(self) -> None:
        spec = build_invested_capital_nav_spec(_sample_invested_nav())
        invested = next(t for t in spec["data"] if t["name"] == "Invested Capital")
        assert invested.get("fill") == "tozeroy"

    def test_theme_applied(self) -> None:
        theme = get_chart_theme()
        spec = build_invested_capital_nav_spec(_sample_invested_nav())
        assert spec["layout"]["paper_bgcolor"] == theme["colours"]["background"]


# ---------------------------------------------------------------------------
# Yearly Cashflows
# ---------------------------------------------------------------------------


class TestYearlyCashflowsSpec:
    def test_four_traces(self) -> None:
        spec = build_yearly_cashflows_spec(_sample_cashflows())
        assert len(spec["data"]) == 4
        names = [t["name"] for t in spec["data"]]
        assert "Calls" in names
        assert "Distributions" in names
        assert "NAV" in names
        assert "Net Capital Gain" in names

    def test_calls_negative(self) -> None:
        spec = build_yearly_cashflows_spec(_sample_cashflows())
        calls = next(t for t in spec["data"] if t["name"] == "Calls")
        assert all(v <= 0.0 for v in calls["y"])

    def test_distributions_non_negative(self) -> None:
        spec = build_yearly_cashflows_spec(_sample_cashflows())
        dists = next(t for t in spec["data"] if t["name"] == "Distributions")
        assert all(v >= 0.0 for v in dists["y"])

    def test_relative_barmode(self) -> None:
        spec = build_yearly_cashflows_spec(_sample_cashflows())
        assert spec["layout"]["barmode"] == "relative"


# ---------------------------------------------------------------------------
# Multiples stacked
# ---------------------------------------------------------------------------


class TestMultiplesStackedSpec:
    def test_three_traces(self) -> None:
        spec = build_multiples_stacked_spec(_sample_multiples())
        assert len(spec["data"]) == 3

    def test_dpi_rvpi_are_bars(self) -> None:
        spec = build_multiples_stacked_spec(_sample_multiples())
        bar_traces = [t for t in spec["data"] if t["type"] == "bar"]
        assert {t["name"] for t in bar_traces} == {"DPI", "RVPI"}

    def test_irr_on_secondary_axis(self) -> None:
        spec = build_multiples_stacked_spec(_sample_multiples())
        irr = next(t for t in spec["data"] if t["name"] == "IRR")
        assert irr.get("yaxis") == "y2"

    def test_yaxis_uses_x_suffix(self) -> None:
        spec = build_multiples_stacked_spec(_sample_multiples())
        assert spec["layout"]["yaxis"]["ticksuffix"] == "x"

    def test_yaxis2_uses_percent_suffix(self) -> None:
        spec = build_multiples_stacked_spec(_sample_multiples())
        assert spec["layout"]["yaxis2"]["ticksuffix"] == "%"

    def test_stacked_barmode(self) -> None:
        spec = build_multiples_stacked_spec(_sample_multiples())
        assert spec["layout"]["barmode"] == "stack"

    def test_tvpi_text_labels_on_top_bar(self) -> None:
        spec = build_multiples_stacked_spec(_sample_multiples())
        rvpi = next(t for t in spec["data"] if t["name"] == "RVPI")
        assert rvpi.get("text") == ["1.20x", "1.70x", "1.70x"]


# ---------------------------------------------------------------------------
# Region / sector treemap
# ---------------------------------------------------------------------------


class TestRegionTreemapSpec:
    def test_treemap_trace_type(self) -> None:
        spec = build_region_treemap_spec(_sample_region())
        assert spec["data"][0]["type"] == "treemap"

    def test_includes_root_and_children(self) -> None:
        spec = build_region_treemap_spec(_sample_region())
        labels = spec["data"][0]["labels"]
        assert labels[0] == "Total"
        assert "DACH" in labels
        assert "North America — USA" in labels

    def test_flat_layout_under_total(self) -> None:
        spec = build_region_treemap_spec(_sample_region())
        parents = spec["data"][0]["parents"]
        # Every non-root cell sits directly under "Total".
        non_root_parents = {p for p in parents if p != ""}
        assert non_root_parents == {"Total"}


class TestSectorTreemapSpec:
    def test_treemap_trace_type(self) -> None:
        spec = build_sector_treemap_spec(_sample_sector())
        assert spec["data"][0]["type"] == "treemap"

    def test_uses_display_names(self) -> None:
        spec = build_sector_treemap_spec(_sample_sector())
        labels = spec["data"][0]["labels"]
        assert "Technology" in labels
        assert "Healthcare" in labels


# ---------------------------------------------------------------------------
# Vintage bar
# ---------------------------------------------------------------------------


class TestVintageBarSpec:
    def test_one_bar_trace(self) -> None:
        spec = build_vintage_bar_spec(_sample_vintages())
        assert len(spec["data"]) == 1
        assert spec["data"][0]["type"] == "bar"

    def test_x_values_are_year_strings(self) -> None:
        spec = build_vintage_bar_spec(_sample_vintages())
        assert spec["data"][0]["x"] == ["2018", "2020", "2022"]

    def test_count_annotations_text(self) -> None:
        spec = build_vintage_bar_spec(_sample_vintages())
        assert spec["data"][0]["text"] == ["n=3", "n=2", "n=1"]

    def test_yaxis_percent_suffix(self) -> None:
        spec = build_vintage_bar_spec(_sample_vintages())
        assert spec["layout"]["yaxis"]["ticksuffix"] == "%"


# ---------------------------------------------------------------------------
# Total Return Index (single-investment tile 4)
# ---------------------------------------------------------------------------


def _sample_index_series() -> pd.Series:
    return pd.Series(
        [100.0, 110.0, 105.0, 120.0],
        index=pd.to_datetime(
            [
                date(2024, 1, 1),
                date(2024, 4, 1),
                date(2024, 7, 1),
                date(2024, 10, 1),
            ]
        ),
    )


class TestTotalReturnIndexSpec:
    def test_single_line_trace(self) -> None:
        spec = build_total_return_index_spec(_sample_index_series())
        assert len(spec["data"]) == 1
        assert spec["data"][0]["type"] == "scatter"
        assert spec["data"][0]["mode"] == "lines"

    def test_baseline_at_100(self) -> None:
        spec = build_total_return_index_spec(_sample_index_series())
        shapes = spec["layout"]["shapes"]
        assert any(s["y0"] == 100.0 and s["y1"] == 100.0 for s in shapes)

    def test_x_axis_is_date_typed(self) -> None:
        spec = build_total_return_index_spec(_sample_index_series())
        assert spec["layout"]["xaxis"]["type"] == "date"

    def test_empty_series_renders_empty_trace(self) -> None:
        spec = build_total_return_index_spec(pd.Series(dtype="float64"))
        assert spec["data"][0]["x"] == []
        assert spec["data"][0]["y"] == []
