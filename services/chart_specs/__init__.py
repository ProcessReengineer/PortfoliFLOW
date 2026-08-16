# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Server-side Plotly figure specs built from the canonical chart theme.

This package is the bridge between PortfoliFLOW's canonical chart
theme (``config/chart_theme.json``) and Plotly.js. Each spec module
exports a pure function that returns a Plotly figure dict
(``{"data": [...], "layout": {...}, "config": {...}}``) with the
theme already applied. Routes serialise the dict to JSON; the
browser calls
``Plotly.newPlot(target, fig.data, fig.layout, fig.config)``.

The package is deliberately Qt-free, FastAPI-free, and matplotlib-
free per ADR-0042 §4 / §5. It is importable from any non-GUI
consumer (web routes today, Shirley tool calls in Phase 5+) without
dragging a UI toolkit into the import graph.
"""

from services.chart_specs._theme import (
    DARK_LAYOUT_TEMPLATE,
    apply_theme,
    dark_layout_template,
)
from services.chart_specs.base import color_palette, layout_from_theme
from services.chart_specs.benchmark_asset_class_composite import (
    build_benchmark_asset_class_composite_spec,
)
from services.chart_specs.benchmark_investment_total_return import (
    build_benchmark_investment_total_return_spec,
)
from services.chart_specs.benchmark_saa_hypothetical import (
    build_benchmark_saa_hypothetical_spec,
)
from services.chart_specs.cash_flow_timeline import (
    SEAM_COLOUR,
    CurrencyView,
    WorldView,
    build_cash_flow_timeline_spec,
)
from services.chart_specs.efficient_frontier import (
    build_efficient_frontier_spec,
)
from services.chart_specs.investment_cashflows_nav import (
    build_cashflows_nav_spec,
)
from services.chart_specs.investment_composition_split import (
    build_composition_split_spec,
)
from services.chart_specs.investment_multiples import build_multiples_spec
from services.chart_specs.investment_nav_timeseries import (
    build_nav_timeseries_spec,
)
from services.chart_specs.investment_rating_maturity_split import (
    build_rating_maturity_split_spec,
)
from services.chart_specs.investment_total_return import (
    build_total_return_spec,
)
from services.chart_specs.investment_underwater import (
    build_underwater_spec,
)
from services.chart_specs.investment_ytm_duration import (
    build_ytm_duration_spec,
)
from services.chart_specs.limits_coverage_small_multiples import (
    build_limits_coverage_spec,
)
from services.chart_specs.portfolio_analysis_frontier import (
    build_frontier_spec as build_portfolio_analysis_frontier_spec,
)
from services.chart_specs.portfolio_currency_exposure import (
    build_currency_exposure_spec,
)
from services.chart_specs.portfolio_fund_composition import (
    build_fund_composition_spec,
)
from services.chart_specs.portfolio_review_cashflows import (
    build_yearly_cashflows_spec,
)
from services.chart_specs.portfolio_review_invested_capital_nav import (
    build_invested_capital_nav_spec,
)
from services.chart_specs.portfolio_review_multiples import (
    build_multiples_stacked_spec,
)
from services.chart_specs.portfolio_review_region_treemap import (
    build_region_treemap_spec,
)
from services.chart_specs.portfolio_review_sector_treemap import (
    build_sector_treemap_spec,
)
from services.chart_specs.portfolio_review_total_return_index import (
    build_total_return_index_spec,
)
from services.chart_specs.portfolio_review_vintage_bar import (
    build_vintage_bar_spec,
)
from services.chart_specs.scenario_impact import build_scenario_impact_pair
from services.chart_specs.statistics_correlation_heatmap import (
    build_correlation_heatmap_spec,
)
from services.chart_specs.statistics_sparkline import build_sparkline_spec

__all__ = [
    "DARK_LAYOUT_TEMPLATE",
    "SEAM_COLOUR",
    "CurrencyView",
    "WorldView",
    "apply_theme",
    "build_benchmark_asset_class_composite_spec",
    "build_benchmark_investment_total_return_spec",
    "build_benchmark_saa_hypothetical_spec",
    "build_cash_flow_timeline_spec",
    "build_cashflows_nav_spec",
    "build_composition_split_spec",
    "build_correlation_heatmap_spec",
    "build_currency_exposure_spec",
    "build_efficient_frontier_spec",
    "build_fund_composition_spec",
    "build_invested_capital_nav_spec",
    "build_limits_coverage_spec",
    "build_multiples_spec",
    "build_multiples_stacked_spec",
    "build_nav_timeseries_spec",
    "build_portfolio_analysis_frontier_spec",
    "build_rating_maturity_split_spec",
    "build_region_treemap_spec",
    "build_scenario_impact_pair",
    "build_sector_treemap_spec",
    "build_sparkline_spec",
    "build_total_return_index_spec",
    "build_total_return_spec",
    "build_underwater_spec",
    "build_vintage_bar_spec",
    "build_yearly_cashflows_spec",
    "build_ytm_duration_spec",
    "color_palette",
    "dark_layout_template",
    "layout_from_theme",
]
