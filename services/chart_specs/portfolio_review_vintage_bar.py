# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — Portfolio Review tile 5 (Vintages distribution).

Bar chart with vintage years on the x-axis (categorical, dense /
unsorted-by-default in Plotly so we sort ascending) and NAV-weighted
share on the y-axis. ``n=N`` annotation above each bar (number of
investments in that vintage).

Mirrors the QT
:class:`~services.reporting.chart_builders.VerticalBarBuilder` output
for the Portfolio Review report's vintage tile.
"""

from __future__ import annotations

from typing import Any

from services.analytics.portfolio_aggregation import VintageDistribution
from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme


def build_vintage_bar_spec(
    distribution: VintageDistribution,
    title: str = "Vintages",
) -> dict[str, Any]:
    """Build the vintage-distribution Plotly spec.

    Args:
        distribution: :class:`VintageDistribution` produced by
            :func:`services.analytics.portfolio_aggregation
            .aggregate_vintage_distribution`.
        title: Tile title.

    Returns:
        Plotly figure spec dict.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    x_values = [str(y) for y in distribution.vintages]
    y_values = [float(v) for v in distribution.weight_pct]
    text_labels = [f"n={n}" for n in distribution.count]

    bar_trace = {
        "type": "bar",
        "x": x_values,
        "y": y_values,
        "name": "Vintage Share",
        "text": text_labels,
        "textposition": "outside",
        "marker": {"color": colours["primary"]},
        "hovertemplate": ("<b>Vintage %{x}</b><br>%{y:.1f}%<br>%{text}<extra></extra>"),
    }

    layout: dict[str, Any] = {
        "title": {"text": title, "x": 0.5},
        "xaxis": {"type": "category", "title": {"text": "Vintage"}},
        "yaxis": {
            "ticksuffix": "%",
            "tickformat": ".1f",
            "title": {"text": "NAV Share"},
            "rangemode": "tozero",
        },
        "showlegend": False,
        "bargap": 0.2,
    }

    fig: dict[str, Any] = {
        "data": [bar_trace],
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)
