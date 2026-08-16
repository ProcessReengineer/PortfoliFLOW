# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — investment composition split (sector | region).

Equity Tile 3 of the liquid-archetype triplet (ADR-0082). Renders the
sector and region allocations of a listed-equity investment as two
side-by-side horizontal-bar panels in a single figure. Reading the two
distributions separately — rather than stacking them — keeps each
allocation's shape clean; the two panels are tinted in distinct theme
colours (primary for sector, secondary for region) to reinforce the
"separate" reading.

This codebase builds two-panel figures by hand via axis ``domain``
splits rather than ``make_subplots`` (see
``services/chart_specs/`` siblings). ``apply_theme`` only themes the
primary ``xaxis``/``yaxis``, so the right panel's ``xaxis2``/``yaxis2``
are themed explicitly via :func:`themed_secondary_axis`.

The function is pure — mappings in, a plain Plotly dict out — so it is
callable from any non-GUI consumer (ADR-0013 / ADR-0045).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.chart_specs._theme import apply_theme, themed_secondary_axis
from services.chart_specs.base import get_chart_theme

# Horizontal axis domains for the left (sector) and right (region)
# panels. The gap between them keeps the right panel's category
# labels off the left panel's value axis.
_LEFT_DOMAIN = [0.0, 0.46]
_RIGHT_DOMAIN = [0.54, 1.0]


def _sorted_percent_items(
    weights: Mapping[str, float],
) -> tuple[list[str], list[float]]:
    """Sort a weight mapping descending and scale the values to percent.

    Args:
        weights: Mapping of category label to decimal weight. Need not
            sum to 1 (per the country/sector non-summation rule).

    Returns:
        A ``(labels, percents)`` pair, ordered by descending weight.
        Both lists are empty when ``weights`` is empty.
    """
    items = sorted(weights.items(), key=lambda kv: float(kv[1]), reverse=True)
    labels = [str(label) for label, _ in items]
    percents = [float(weight) * 100.0 for _, weight in items]
    return labels, percents


def build_composition_split_spec(
    sector_weights: Mapping[str, float],
    region_weights: Mapping[str, float],
    investment_name: str,
) -> dict[str, Any]:
    """Build the two-panel sector | region composition Plotly spec.

    Two horizontal-bar panels share one figure via axis ``domain``
    splits: the sector panel on the left axes (``xaxis``/``yaxis``,
    primary colour), the region panel on the right axes
    (``xaxis2``/``yaxis2``, secondary colour). Each panel is sorted by
    descending weight, with the largest slice at the top.

    Either mapping may be empty — the corresponding panel then renders
    an empty trace while the other panel still draws. When both are
    empty the figure is valid but carries no bars.

    Args:
        sector_weights: Mapping of sector label to decimal weight.
        region_weights: Mapping of region label to decimal weight.
        investment_name: Display name of the investment (used in the
            chart title).

    Returns:
        Plotly figure spec dict ``{"data": [...], "layout": {...},
        "config": {...}}``. Serialise to JSON for ``Plotly.newPlot``.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    sector_labels, sector_pct = _sorted_percent_items(sector_weights)
    region_labels, region_pct = _sorted_percent_items(region_weights)

    sector_trace = {
        "type": "bar",
        "orientation": "h",
        "x": sector_pct,
        "y": sector_labels,
        "xaxis": "x",
        "yaxis": "y",
        "marker": {"color": colours["primary"]},
        "name": "Sector",
        "hovertemplate": "<b>%{y}</b><br>%{x:.1f}%<extra></extra>",
    }
    region_trace = {
        "type": "bar",
        "orientation": "h",
        "x": region_pct,
        "y": region_labels,
        "xaxis": "x2",
        "yaxis": "y2",
        "marker": {"color": colours["secondary"]},
        "name": "Region",
        "hovertemplate": "<b>%{y}</b><br>%{x:.1f}%<extra></extra>",
    }

    layout: dict[str, Any] = {
        "title": {
            "text": f"Composition — {investment_name}",
            "x": 0.5,
        },
        "xaxis": {
            "domain": _LEFT_DOMAIN,
            "ticksuffix": "%",
            "title": {"text": ""},
        },
        # Largest weight at the top of each panel.
        "yaxis": {"autorange": "reversed", "automargin": True},
        "xaxis2": {
            "domain": _RIGHT_DOMAIN,
            "ticksuffix": "%",
            "title": {"text": ""},
            **themed_secondary_axis(),
        },
        "yaxis2": {
            "anchor": "x2",
            "autorange": "reversed",
            "automargin": True,
            **themed_secondary_axis(),
        },
        # Domain splits have no per-panel titles; label via annotations.
        "annotations": [
            {
                "text": "Sector",
                "xref": "paper",
                "yref": "paper",
                "x": sum(_LEFT_DOMAIN) / 2.0,
                "y": 1.04,
                "xanchor": "center",
                "yanchor": "bottom",
                "showarrow": False,
            },
            {
                "text": "Region",
                "xref": "paper",
                "yref": "paper",
                "x": sum(_RIGHT_DOMAIN) / 2.0,
                "y": 1.04,
                "xanchor": "center",
                "yanchor": "bottom",
                "showarrow": False,
            },
        ],
        "showlegend": False,
        "hovermode": "closest",
        "bargap": 0.25,
    }

    fig: dict[str, Any] = {
        "data": [sector_trace, region_trace],
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)
