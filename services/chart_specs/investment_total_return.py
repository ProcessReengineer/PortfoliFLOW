# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — investment Total Return bars.

Period-over-period Total Return rendered as bars on the canonical
dark theme. Pure function: takes a pandas Series of decimal returns
and returns a Plotly figure dict.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from services.chart_specs._theme import apply_theme
from services.chart_specs.base import apply_axis_end, get_chart_theme


def build_total_return_spec(
    return_series: pd.Series,
    investment_name: str,
    *,
    axis_end: date | None = None,
) -> dict[str, Any]:
    """Build a Plotly figure spec for an investment's periodic Total Return.

    The QT chart in ``gui/widgets/chart_widgets.py::_make_total_return_chart``
    uses a continuous line with a zero reference. The Phase-5 spec
    follows the migration prompt and renders bars instead — bars make
    the sign of each period unambiguous (positive vs. negative
    return) at the small chart-tile size used in the investment
    detail view. Theme colours are inherited from the canonical
    chart theme so the visual language matches the QT plots.

    Args:
        return_series: Pandas Series indexed by date, values are
            decimal returns (``0.05`` for +5 %). May be empty.
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

    cleaned = return_series.dropna() if not return_series.empty else return_series
    cleaned = cleaned.sort_index() if not cleaned.empty else cleaned

    x_values: list[str] = []
    y_values: list[float] = []
    bar_colors: list[str] = []
    if not cleaned.empty:
        for idx, value in cleaned.items():
            x_values.append(pd.Timestamp(idx).isoformat())
            y_values.append(float(value) * 100.0)
            bar_colors.append(
                colours["primary"]
                if float(value) >= 0.0
                else colours.get("secondary", colours["primary"])
            )

    bar_trace = {
        "type": "bar",
        "x": x_values,
        "y": y_values,
        "marker": {"color": bar_colors or colours["primary"]},
        "name": "Total Return",
        "hovertemplate": ("<b>Total Return</b><br>%{x|%Y-%m-%d}<br>%{y:.2f}%<extra></extra>"),
    }

    layout: dict[str, Any] = {
        "title": {
            "text": f"Total Return — {investment_name}",
            "x": 0.5,
        },
        "xaxis": {"type": "date", "tickformat": "%Y-%m-%d", "title": {"text": ""}},
        "yaxis": {
            "ticksuffix": "%",
            "tickformat": ".1f",
            "zeroline": True,
            "zerolinewidth": 1,
            "zerolinecolor": colours["axis_line"],
            "title": {"text": "Return"},
        },
        "showlegend": False,
        "bargap": 0.2,
    }
    apply_axis_end(layout, axis_end, has_data=bool(x_values))

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
