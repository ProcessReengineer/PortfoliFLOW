# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — correlation heatmap (sub-stream 5c).

Diverging-colour heatmap of pairwise correlations across the
investment universe. The former Qt implementation (ADR-0094)
used a custom ``primary_alt`` → ``cell_bg_even`` → ``primary``
gradient with theme-driven endpoints; the Plotly counterpart
defines the same three stops on a normalised ``[-1, 1]`` colour
range so the visual language matches.

Pure function: takes a square :class:`pandas.DataFrame` of
correlations and returns a Plotly figure dict.

Cell colours are drawn as SVG ``layout.shapes`` (one ``rect`` per
finite cell, colour interpolated server-side over the same three
stops), not by the heatmap raster. Plotly renders a heatmap through
an off-screen ``<canvas>`` whose readback is randomised by browsers
with canvas anti-fingerprinting (LibreWolf, Firefox
``resistFingerprinting``), which garbled the cells while colorbar and
numbers stayed correct. The ``heatmap`` trace is kept at ``opacity``
0 so the colorbar and the hover template keep working; shapes and
annotations are plain SVG and render identically everywhere.
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
    theme switch (a test calling
    :meth:`ThemeService.set_active_chart_theme`) is reflected on the
    next request.

    Args:
        corr_df: Square DataFrame whose index and columns are the
            investment names. Values in ``[-1, 1]``; NaN cells are
            rendered with no annotation and no shape (the plot
            background shows through).

    Returns:
        Plotly figure spec dict ``{"data": [...], "layout": {...},
        "config": {...}}``. ``layout.shapes`` carries one ``rect``
        per finite cell. Empty input → empty trace and empty
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
            "opacity": 0.0,
        }
        empty_layout: dict[str, Any] = {
            "title": {"text": "Correlation Matrix", "x": 0.5},
            "xaxis": {"title": {"text": ""}, "tickangle": -45},
            "yaxis": {"title": {"text": ""}, "autorange": "reversed"},
            "annotations": [],
            "shapes": [],
            "margin": {"l": 120, "r": 30, "t": 60, "b": 120},
        }
        return apply_theme({"data": [empty_trace], "layout": empty_layout, "config": _config()})

    names = [str(n) for n in corr_df.index]
    z_values: list[list[float | None]] = []
    annotations: list[dict[str, Any]] = []
    shapes: list[dict[str, Any]] = []

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
            shapes.append(
                {
                    "type": "rect",
                    "xref": "x",
                    "yref": "y",
                    "x0": j - 0.5,
                    "x1": j + 0.5,
                    "y0": i - 0.5,
                    "y1": i + 0.5,
                    "fillcolor": _interpolate_colour(value, cold, neutral, hot),
                    "line": {"width": 0},
                    "layer": "below",
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
        "opacity": 0.0,
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
        "shapes": shapes,
        "margin": {"l": 120, "r": 30, "t": 60, "b": 120},
    }

    fig: dict[str, Any] = {
        "data": [trace],
        "layout": layout,
        "config": _config(),
    }
    return apply_theme(fig)


def _format_correlation(value: float) -> str:
    """Render a correlation coefficient like ``0.67`` / ``-0.04``; never ``-0.00``."""
    if abs(value) < 0.005:
        value = 0.0
    return f"{value:.2f}"


def _hex_to_rgb(colour: str) -> tuple[int, int, int]:
    """Parse ``#RRGGBB`` or ``#RRGGBBAA`` (alpha ignored) into an RGB triple."""
    digits = colour.lstrip("#")
    if len(digits) not in (6, 8):
        raise ValueError(f"Expected #RRGGBB or #RRGGBBAA, got {colour!r}.")
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def _interpolate_colour(value: float, cold: str, neutral: str, hot: str) -> str:
    """Colour for a correlation on the ``cold`` (-1) → ``neutral`` (0) → ``hot`` (+1) scale.

    Linear RGB interpolation between the two stops the value falls
    between, clamped to ``[-1, 1]`` — the same mapping Plotly applies
    to the trace's three-stop colorscale, so cells match the colorbar.
    Returns upper-case ``#RRGGBB``.
    """
    clamped = max(-1.0, min(1.0, value))
    if clamped < 0.0:
        start, end, t = _hex_to_rgb(cold), _hex_to_rgb(neutral), clamped + 1.0
    else:
        start, end, t = _hex_to_rgb(neutral), _hex_to_rgb(hot), clamped
    r, g, b = (round(s + (e - s) * t) for s, e in zip(start, end, strict=True))
    return f"#{r:02X}{g:02X}{b:02X}"


def _config() -> dict[str, Any]:
    return {
        "displayModeBar": True,
        "displaylogo": False,
        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
        "responsive": True,
    }
