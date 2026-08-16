# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — Portfolio Review tile 6/5 (sector split treemap).

Flat treemap (no hierarchy) over sector codes. Tile size is
NAV-weighted (``weight_pct``). Mirrors the QT
:class:`~services.reporting.chart_builders.TreemapBuilder` output for
the Portfolio Review report's sector tile.
"""

from __future__ import annotations

from typing import Any

from services.analytics.portfolio_aggregation import SectorBreakdown
from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme


def build_sector_treemap_spec(
    breakdown: SectorBreakdown,
    title: str = "Sector split",
) -> dict[str, Any]:
    """Build the sector-treemap Plotly spec.

    Args:
        breakdown: :class:`SectorBreakdown` produced by
            :func:`services.analytics.portfolio_aggregation
            .aggregate_sector_breakdown`.
        title: Tile title.

    Returns:
        Plotly figure spec dict.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    rows = breakdown.rows
    labels: list[str] = ["Total"]
    parents: list[str] = [""]
    values: list[float] = [0.0]
    customdata: list[list[float]] = [[0.0]]

    for r in rows:
        labels.append(r.sector_display_name or r.sector_code)
        parents.append("Total")
        values.append(r.weight_pct)
        customdata.append([r.weight_pct])

    if len(values) > 1:
        values[0] = sum(values[1:])
        customdata[0] = [values[0]]

    palette = colours.get("series_palette") or [
        colours["primary"],
        colours.get("secondary", colours["primary"]),
        colours.get("tertiary", colours["primary"]),
        colours.get("quaternary", colours["primary"]),
    ]
    cell_colors = [palette[i % len(palette)] for i in range(len(labels))]

    treemap_trace = {
        "type": "treemap",
        "labels": labels,
        "parents": parents,
        "values": values,
        "customdata": customdata,
        "branchvalues": "total",
        "marker": {
            "colors": cell_colors,
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
