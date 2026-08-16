# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — Portfolio Review tile 4 (region split treemap).

Flat treemap with one rectangle per tenant region. Tile size is
NAV-weighted (``weight_pct``). Per ADR-0046 the Excel import path
emits region weights directly; there is no longer a country hierarchy
in the bundle to render, so the historical region-parent / country-
child layout collapses to a single layer under a synthetic root.
"""

from __future__ import annotations

from typing import Any

from services.analytics.portfolio_aggregation import RegionBreakdown
from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme


def build_region_treemap_spec(
    breakdown: RegionBreakdown,
    title: str = "Region split",
) -> dict[str, Any]:
    """Build the region-treemap Plotly spec.

    Args:
        breakdown: :class:`RegionBreakdown` produced by
            :func:`services.analytics.portfolio_aggregation
            .aggregate_region_breakdown`.
        title: Tile title.

    Returns:
        Plotly figure spec dict ready for ``Plotly.newPlot``. The
        treemap is flat: every region sits directly under ``"Total"``.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    rows = breakdown.rows

    labels: list[str] = ["Total"]
    parents: list[str] = [""]
    values: list[float] = [0.0]  # placeholder for the root, filled below
    customdata: list[list[float]] = [[0.0]]

    for r in rows:
        labels.append(r.region_display_name)
        parents.append("Total")
        values.append(r.weight_pct)
        customdata.append([r.weight_pct])

    # Root size = sum of leaves.
    if len(values) > 1:
        values[0] = sum(values[1:])
        customdata[0] = [values[0]]

    treemap_trace = {
        "type": "treemap",
        "labels": labels,
        "parents": parents,
        "values": values,
        "customdata": customdata,
        "branchvalues": "total",
        "marker": {
            "colors": _palette_for(len(labels), colours),
            "line": {"color": colours["axis_line"], "width": 1},
        },
        "textinfo": "label+percent parent",
        "hovertemplate": ("<b>%{label}</b><br>%{customdata[0]:.1f}%<extra></extra>"),
    }

    layout: dict[str, Any] = {
        "title": {"text": title, "x": 0.5},
        "showlegend": False,
        "margin": {"l": 10, "r": 10, "t": 50, "b": 10},
    }

    fig: dict[str, Any] = {
        "data": [treemap_trace],
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)


def _palette_for(n: int, colours: dict[str, Any]) -> list[str]:
    """Cycle the canonical series palette to fill ``n`` cells."""
    palette = colours.get("series_palette") or [
        colours["primary"],
        colours.get("secondary", colours["primary"]),
        colours.get("tertiary", colours["primary"]),
        colours.get("quaternary", colours["primary"]),
    ]
    return [palette[i % len(palette)] for i in range(n)]
