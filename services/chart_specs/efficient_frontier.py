# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec builder for SAA efficient-frontier charts.

The PyQt6 reference is ``gui/widgets/saa_widget.py::_render_chart``.
This module mirrors its visual choices — the same colours, the same
marker shapes, the same legend labels, the same axis formatters —
so that the side-by-side acceptance comparison in sub-stream 3d
shows visual identity, not divergence (per ADR-0042 §4).

Differences from the matplotlib path are limited to web-typical
interactivity that has no matplotlib analogue: hover tooltips with
return / volatility / Sharpe per frontier point, weight breakdowns
on tangency / min-variance markers, and the standard Plotly
zoom / pan / autoscale toolbar.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from services.analytics.portfolio_optimizer import PortfolioResult
from services.chart_specs.base import (
    color_palette,
    get_chart_theme,
    layout_from_theme,
)

_WEIGHT_DISPLAY_THRESHOLD = 0.001


def _format_weights_html(asset_names: list[str], weights: np.ndarray | list[float]) -> str:
    """Format a per-asset weight breakdown as an HTML fragment.

    Used inside Plotly hover templates. Near-zero weights (below
    0.1 %) are suppressed to keep the tooltip readable for
    configurations with weight bounds that pin one or more classes
    at the lower bound.

    Args:
        asset_names: Asset class display names.
        weights: Weights aligned with ``asset_names``.

    Returns:
        ``<br>``-joined ``"Name: 12.3%"`` lines, or ``"—"`` when
        every weight is below the display threshold.
    """
    parts: list[str] = []
    for name, weight in zip(asset_names, weights):
        if weight > _WEIGHT_DISPLAY_THRESHOLD:
            parts.append(f"{name}: {weight * 100:.1f}%")
    return "<br>".join(parts) if parts else "—"


def build_efficient_frontier_spec(
    *,
    frontier: list[PortfolioResult],
    tangency: PortfolioResult,
    min_var: PortfolioResult,
    cloud: list[PortfolioResult],
    cml: list[tuple[float, float]],
    asset_names: list[str],
    risk_free_rate: float,
) -> dict[str, Any]:
    """Build a Plotly figure spec for the SAA efficient frontier view.

    The figure has six traces (mirroring the PyQt6 chart from
    ``gui/widgets/saa_widget.py``):

    1. Random portfolio cloud (scatter, low alpha, hover off).
    2. Efficient frontier (line, hover with return / vol / Sharpe).
    3. Capital Market Line (dashed line from rf to tangency).
    4. Risk-free rate horizontal reference line.
    5. Tangency portfolio (single star marker, weight breakdown on hover).
    6. Minimum-variance portfolio (single diamond marker, weight breakdown on hover).

    Args:
        frontier: Efficient frontier points (sorted by volatility ascending).
        tangency: Tangency (max-Sharpe) portfolio.
        min_var: Minimum-variance portfolio.
        cloud: Random portfolios for visual context.
        cml: Capital market line points (volatility, return).
        asset_names: Asset class names aligned with the weight vectors.
        risk_free_rate: Annualised risk-free rate (decimal).

    Returns:
        A Plotly figure spec dict with keys ``data``, ``layout``,
        ``config``. Serialise to JSON and pass to
        ``Plotly.newPlot(target, fig.data, fig.layout, fig.config)``.
    """
    theme = get_chart_theme()
    palette = color_palette()
    optimization = theme["optimization"]

    # 1 — Random portfolio cloud. SVG scatter, not scattergl: the cloud is
    # the only WebGL-dependent element in the application, and browsers with
    # a blocked WebGL context render Plotly's unsupported banner instead.
    cloud_trace = {
        "type": "scatter",
        "x": [p.volatility for p in cloud],
        "y": [p.expected_return for p in cloud],
        "mode": "markers",
        "marker": {
            "size": optimization["cloud_marker_size"] + 2,
            "color": palette["cloud"],
            "opacity": max(optimization["cloud_alpha"], 0.18),
        },
        "name": "Random Portfolios",
        "hoverinfo": "skip",
        "showlegend": False,
    }

    # 2 — Efficient frontier line.
    frontier_sharpes = [p.sharpe_ratio if p.sharpe_ratio is not None else 0.0 for p in frontier]
    frontier_trace = {
        "type": "scatter",
        "x": [p.volatility for p in frontier],
        "y": [p.expected_return for p in frontier],
        "mode": "lines",
        "line": {
            "color": palette["frontier"],
            "width": optimization["frontier_linewidth"],
        },
        "name": "Efficient Frontier",
        "customdata": [[s] for s in frontier_sharpes],
        "hovertemplate": (
            "Vol: %{x:.2%}<br>Return: %{y:.2%}<br>Sharpe: %{customdata[0]:.2f}<extra></extra>"
        ),
    }

    # 3 — Capital Market Line.
    cml_trace = {
        "type": "scatter",
        "x": [point[0] for point in cml],
        "y": [point[1] for point in cml],
        "mode": "lines",
        "line": {
            "color": palette["cml"],
            "width": optimization["cml_linewidth"],
            "dash": "dash",
        },
        "name": "Capital Market Line",
        "hoverinfo": "skip",
    }

    # 4 — Risk-free rate horizontal reference line. Drawn as a trace
    # rather than a layout shape so it appears in the legend like the
    # PyQt6 version does.
    if frontier or cml:
        x_max_candidates = [p.volatility for p in frontier] + [point[0] for point in cml]
        x_max = max(x_max_candidates) * 1.05 if x_max_candidates else 0.5
    else:
        x_max = 0.5
    rf_trace = {
        "type": "scatter",
        "x": [0.0, x_max],
        "y": [risk_free_rate, risk_free_rate],
        "mode": "lines",
        "line": {
            "color": palette["rf_line"],
            "width": 1.0,
            "dash": "dot",
        },
        "name": f"Risk-Free Rate ({risk_free_rate * 100:.1f}%)",
        "opacity": optimization["rf_line_alpha"],
        "hoverinfo": "skip",
    }

    # 5 — Tangency portfolio marker.
    tangency_weights_text = _format_weights_html(asset_names, tangency.weights)
    tangency_trace = {
        "type": "scatter",
        "x": [tangency.volatility],
        "y": [tangency.expected_return],
        "mode": "markers",
        "marker": {
            "size": 18,
            "color": palette["tangency"],
            "symbol": "star",
            "line": {"color": theme["colours"]["background"], "width": 1},
        },
        "name": "Tangency Portfolio",
        "hovertemplate": (
            "<b>Tangency Portfolio</b><br>"
            "Vol: %{x:.2%}<br>"
            "Return: %{y:.2%}<br>"
            f"Sharpe: {tangency.sharpe_ratio:.2f}<br><br>"
            f"<b>Weights</b><br>{tangency_weights_text}<extra></extra>"
        ),
    }

    # 6 — Minimum-variance portfolio marker.
    min_var_weights_text = _format_weights_html(asset_names, min_var.weights)
    min_var_trace = {
        "type": "scatter",
        "x": [min_var.volatility],
        "y": [min_var.expected_return],
        "mode": "markers",
        "marker": {
            "size": 12,
            "color": palette["min_var"],
            "symbol": "diamond",
            "line": {"color": theme["colours"]["background"], "width": 1},
        },
        "name": "Min Variance",
        "hovertemplate": (
            "<b>Min-Variance Portfolio</b><br>"
            "Vol: %{x:.2%}<br>"
            "Return: %{y:.2%}<br><br>"
            f"<b>Weights</b><br>{min_var_weights_text}<extra></extra>"
        ),
    }

    layout = layout_from_theme(
        title="Strategic Asset Allocation — Efficient Frontier",
        xlabel="Volatility (annualised)",
        ylabel="Expected Return (annualised)",
        show_legend=True,
    )
    # Pin the lower y-axis bound the same way the PyQt6 chart does so
    # the visual baseline is identical.
    layout["yaxis"]["rangemode"] = "tozero"
    layout["xaxis"]["rangemode"] = "tozero"

    config = {
        "displayModeBar": True,
        "displaylogo": False,
        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
        "responsive": True,
    }

    return {
        "data": [
            cloud_trace,
            frontier_trace,
            cml_trace,
            rf_trace,
            tangency_trace,
            min_var_trace,
        ],
        "layout": layout,
        "config": config,
    }
