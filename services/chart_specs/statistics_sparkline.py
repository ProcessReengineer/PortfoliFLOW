# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — KPI-card sparkline (sub-stream 5c).

A small, axis-less line chart that summarises an investment's
cumulative performance inside the Statistics page Key-Metrics
strip. The spec is intentionally minimal: no axes, no hover, no
legend, transparent canvas — visual focus is on the line shape, with
the surrounding KPI card carrying the labels.

Pure function: takes a list of cumulative-performance values and
returns a Plotly figure dict. The Statistics page renders one
sparkline per investment by serialising this spec to JSON and
calling ``Plotly.newPlot`` per target div.
"""

from __future__ import annotations

from typing import Any

from services.chart_specs.base import get_chart_theme


def build_sparkline_spec(values: list[float]) -> dict[str, Any]:
    """Build a minimal-chrome Plotly spec for a KPI-card sparkline.

    Args:
        values: Cumulative-performance values, typically
            ``(1 + r).cumprod()``. Empty list → empty trace (the
            Plotly target stays blank).

    Returns:
        Plotly figure spec dict ``{"data": [...], "layout": {...},
        "config": {...}}``.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    line_colour = (
        colours["primary"]
        if not values or values[-1] >= (values[0] if values else 0.0)
        else colours.get("secondary", colours["primary"])
    )

    trace = {
        "type": "scatter",
        "x": list(range(len(values))),
        "y": list(values),
        "mode": "lines",
        "line": {"color": line_colour, "width": 2},
        "hoverinfo": "skip",
        "showlegend": False,
    }

    layout: dict[str, Any] = {
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "margin": {"l": 0, "r": 0, "t": 0, "b": 0},
        "showlegend": False,
        "xaxis": {
            "visible": False,
            "showgrid": False,
            "zeroline": False,
            "showticklabels": False,
        },
        "yaxis": {
            "visible": False,
            "showgrid": False,
            "zeroline": False,
            "showticklabels": False,
        },
    }

    config = {
        "displayModeBar": False,
        "staticPlot": True,
        "responsive": True,
    }
    return {"data": [trace], "layout": layout, "config": config}
