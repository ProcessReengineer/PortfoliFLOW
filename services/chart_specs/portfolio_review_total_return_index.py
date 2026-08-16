# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — Single-Investment Review tile 4 (Total Return Index).

Daily cumulative-return index ``cumprod(1 + r) * 100`` rendered as a
smooth line. The Single-Investment Review tile 4 shows this index
since inception. Tile is omitted in the Portfolio Overview where
NAV-weighted compounding is not in scope (per ADR-0045 §3 and the
QT precedent in
:class:`services.reporting.data_providers.TotalReturnTimeseriesProvider`).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme


def build_total_return_index_spec(
    index_series: pd.Series,
    title: str = "Total Return (since inception)",
) -> dict[str, Any]:
    """Build the rebased-to-100 Total Return Plotly spec.

    Args:
        index_series: Pandas Series indexed by date, values are the
            rebased index (``100`` at inception). Empty series →
            chart with empty trace (still themed).
        title: Tile title.

    Returns:
        Plotly figure spec dict.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    cleaned = index_series.dropna().sort_index() if not index_series.empty else index_series
    x_values: list[str] = []
    y_values: list[float] = []
    if not cleaned.empty:
        x_values = [pd.Timestamp(idx).isoformat() for idx in cleaned.index]
        y_values = [float(v) for v in cleaned.values]

    line_trace = {
        "type": "scatter",
        "mode": "lines",
        "x": x_values,
        "y": y_values,
        "name": "Index",
        "line": {"color": colours["primary"], "width": 2},
        "hovertemplate": ("<b>Index</b><br>%{x|%Y-%m-%d}<br>%{y:.2f}<extra></extra>"),
    }

    layout: dict[str, Any] = {
        "title": {"text": title, "x": 0.5},
        "xaxis": {"type": "date", "tickformat": "%Y-%m-%d", "title": {"text": ""}},
        "yaxis": {
            "tickformat": ".0f",
            "title": {"text": "Rebased (Inception = 100)"},
        },
        "shapes": [
            {
                "type": "line",
                "xref": "paper",
                "x0": 0.0,
                "x1": 1.0,
                "yref": "y",
                "y0": 100.0,
                "y1": 100.0,
                "line": {
                    "color": colours["axis_line"],
                    "width": 1,
                    "dash": "dot",
                },
            },
        ],
        "showlegend": False,
    }

    fig: dict[str, Any] = {
        "data": [line_trace],
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)
