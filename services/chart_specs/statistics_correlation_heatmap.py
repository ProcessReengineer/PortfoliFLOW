# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — correlation heatmap (sub-stream 5c).

Diverging-colour heatmap of pairwise correlations across the
investment universe. The QT widget
(``gui/widgets/statistics_widgets.py::CorrelationMatrixWidget``)
uses a custom ``primary_alt`` → ``cell_bg_even`` → ``primary``
gradient with theme-driven endpoints; the Plotly counterpart
defines the same three stops on a normalised ``[-1, 1]`` colour
range so the visual language matches.

Pure function: takes a square :class:`pandas.DataFrame` of
correlations and returns a Plotly figure dict.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme


def build_correlation_heatmap_spec(corr_df: pd.DataFrame) -> dict[str, Any]:
    """Build a Plotly heatmap spec for a correlation matrix.

    Annotations carry the correlation value formatted to two
    decimals (``0.67``, ``-0.04``); the colour scale stops at
    ``-1.0``, ``0.0``, ``+1.0`` use theme tokens so a runtime
    theme switch (the GUI Phase-B picker calling
    :meth:`ThemeService.set_active_chart_theme`) is reflected on the
    next request.

    Args:
        corr_df: Square DataFrame whose index and columns are the
            investment names. Values in ``[-1, 1]``; NaN cells are
            rendered with no annotation and the neutral mid-stop
            colour (Plotly handles NaN natively for the colorscale).

    Returns:
        Plotly figure spec dict ``{"data": [...], "layout": {...},
        "config": {...}}``. Empty input → empty trace and empty
        layout annotations so the route can still serialise.
    """
    theme = get_chart_theme()
    colours = theme["colours"]
    table = theme.get("table", {})

    cold = colours.get("primary_alt", colours.get("secondary", "#5B8DEE"))
    neutral = table.get("cell_bg_even", colours.get("plot_area", "#1a1a1a"))
    hot = colours["primary"]

    if corr_df.empty:
        empty_trace = {
            "type": "heatmap",
            "x": [],
            "y": [],
            "z": [],
            "colorscale": [
                [0.0, cold],
                [0.5, neutral],
                [1.0, hot],
            ],
            "zmin": -1.0,
            "zmax": 1.0,
            "showscale": True,
        }
        empty_layout: dict[str, Any] = {
            "title": {"text": "Correlation Matrix", "x": 0.5},
            "xaxis": {"title": {"text": ""}, "tickangle": -45},
            "yaxis": {"title": {"text": ""}, "autorange": "reversed"},
            "annotations": [],
            "margin": {"l": 120, "r": 30, "t": 60, "b": 120},
        }
        return apply_theme({"data": [empty_trace], "layout": empty_layout, "config": _config()})

    names = [str(n) for n in corr_df.index]
    z_values: list[list[float | None]] = []
    annotations: list[dict[str, Any]] = []

    for i, row_name in enumerate(corr_df.index):
        row: list[float | None] = []
        for j, col_name in enumerate(corr_df.columns):
            raw = corr_df.iloc[i, j]
            if pd.isna(raw):
                row.append(None)
                continue
            value = float(raw)
            row.append(value)
            annotations.append(
                {
                    "x": str(col_name),
                    "y": str(row_name),
                    "text": _format_correlation(value),
                    "showarrow": False,
                    "font": {
                        "color": ("#FFFFFF" if abs(value) >= 0.5 else colours["text"]),
                        "size": theme.get("font", {}).get("tick_label_size", 11),
                    },
                }
            )
        z_values.append(row)

    trace = {
        "type": "heatmap",
        "x": names,
        "y": names,
        "z": z_values,
        "colorscale": [
            [0.0, cold],
            [0.5, neutral],
            [1.0, hot],
        ],
        "zmin": -1.0,
        "zmax": 1.0,
        "showscale": True,
        "hovertemplate": ("<b>%{y}</b> vs <b>%{x}</b><br>ρ = %{z:.4f}<extra></extra>"),
        "colorbar": {
            "title": {"text": "ρ"},
            "tickvals": [-1.0, -0.5, 0.0, 0.5, 1.0],
            "ticktext": ["−1", "−0.5", "0", "+0.5", "+1"],
        },
    }

    layout: dict[str, Any] = {
        "title": {"text": "Correlation Matrix", "x": 0.5},
        "xaxis": {
            "title": {"text": ""},
            "tickangle": -45,
            "automargin": True,
            "side": "bottom",
        },
        "yaxis": {
            "title": {"text": ""},
            "automargin": True,
            "autorange": "reversed",
        },
        "annotations": annotations,
        "margin": {"l": 120, "r": 30, "t": 60, "b": 120},
    }

    fig: dict[str, Any] = {
        "data": [trace],
        "layout": layout,
        "config": _config(),
    }
    return apply_theme(fig)


def _format_correlation(value: float) -> str:
    """Render a correlation coefficient like ``0.67`` / ``-0.04``."""
    return f"{value:.2f}"


def _config() -> dict[str, Any]:
    return {
        "displayModeBar": True,
        "displaylogo": False,
        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
        "responsive": True,
    }
