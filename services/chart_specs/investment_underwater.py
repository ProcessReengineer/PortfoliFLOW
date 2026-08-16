# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — investment underwater (drawdown) profile.

Equity Tile 2 of the liquid-archetype triplet (ADR-0082). Renders the
underwater curve — the running drawdown of the total-return index
relative to its prior peak — as a filled area hugging the zero line
from below. The fill tints the area beneath the curve in the canonical
accent hue so the depth and duration of each drawdown read at a glance.

The function is pure — a pandas Series in, a plain Plotly dict out — so
it is callable from any non-GUI consumer (ADR-0013 / ADR-0045). The
Phase-3 regression guard
(``tests/regression/test_no_matplotlib_in_web.py``) keeps this module
matplotlib-free.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from services.chart_specs._theme import apply_theme
from services.chart_specs.base import apply_axis_end, get_chart_theme

# Opacity for the translucent drawdown fill. Low enough that the
# gridlines and zero reference stay legible through the tinted area.
_FILL_ALPHA = 0.25


def _hex_to_rgba(hex_colour: str, alpha: float) -> str:
    """Convert a ``#RRGGBB`` colour to an ``rgba(...)`` string at ``alpha``.

    Used for the translucent underwater fill so the area beneath the
    drawdown line is tinted in the same hue as the line itself, only
    fainter. Returns the input unchanged when it is not a six-digit hex
    string (defensive — every shipped theme uses six-digit hex).

    Args:
        hex_colour: A ``#RRGGBB`` colour string.
        alpha: Opacity in ``[0.0, 1.0]``.

    Returns:
        An ``rgba(r, g, b, alpha)`` string, or ``hex_colour`` unchanged
        if it cannot be parsed.
    """
    value = hex_colour.lstrip("#")
    if len(value) != 6:
        return hex_colour
    try:
        r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return hex_colour
    return f"rgba({r}, {g}, {b}, {alpha})"


def build_underwater_spec(
    underwater_series: pd.Series,
    investment_name: str,
    *,
    axis_end: date | None = None,
) -> dict[str, Any]:
    """Build a Plotly figure spec for an investment's underwater profile.

    The input is the output of
    :func:`services.analytics.investment_returns.compute_underwater_series`:
    a date-indexed Series of decimal drawdown levels, each ``<= 0``
    (``0.0`` at a new peak, ``-0.12`` at a 12 % drawdown). Values are
    scaled to percent and drawn as a single filled area anchored to the
    zero line, which sits at the top of the chart.

    Args:
        underwater_series: Date-indexed Series of decimal drawdown
            levels (``<= 0``). May be empty.
        investment_name: Display name of the investment (used in the
            chart title).
        axis_end: Optional shared x-axis end (the ADR-0113 §1 universe
            as-of). Extends the auto-range on the right only; the start
            stays data-driven. ``None`` leaves the tile on its own
            auto-range.

    Returns:
        Plotly figure spec dict ``{"data": [...], "layout": {...},
        "config": {...}}``. Serialise to JSON for ``Plotly.newPlot``.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    cleaned = (
        underwater_series.dropna().sort_index()
        if not underwater_series.empty
        else underwater_series
    )

    x_values: list[str] = []
    y_values: list[float] = []
    if not cleaned.empty:
        for idx, value in cleaned.items():
            x_values.append(pd.Timestamp(idx).isoformat())
            y_values.append(float(value) * 100.0)

    underwater_trace = {
        "type": "scatter",
        "mode": "lines",
        "x": x_values,
        "y": y_values,
        "fill": "tozeroy",
        "line": {"color": colours["primary"], "width": 2},
        "fillcolor": _hex_to_rgba(colours["primary"], _FILL_ALPHA),
        "name": "Drawdown",
        "hovertemplate": ("<b>Drawdown</b><br>%{x|%Y-%m-%d}<br>%{y:.1f}%<extra></extra>"),
    }

    layout: dict[str, Any] = {
        "title": {
            "text": f"Underwater — {investment_name}",
            "x": 0.5,
        },
        "xaxis": {"type": "date", "tickformat": "%Y-%m-%d", "title": {"text": ""}},
        "yaxis": {
            "ticksuffix": "%",
            "tickformat": ".1f",
            "rangemode": "tozero",
            "zeroline": True,
            "zerolinewidth": 1,
            "zerolinecolor": colours["axis_line"],
            "title": {"text": "Drawdown"},
        },
        "showlegend": False,
    }
    apply_axis_end(layout, axis_end, has_data=bool(x_values))

    fig: dict[str, Any] = {
        "data": [underwater_trace],
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)
