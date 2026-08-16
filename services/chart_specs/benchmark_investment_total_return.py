# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — Stage-a per-investment cumulative-return chart.

Three traces:
  - Investment cumulative total return (primary line).
  - Benchmark cumulative total return (secondary line).
  - Excess (filled area between investment and benchmark; positive
    tinted green via ``colours.positive_bar``, negative red via
    ``colours.negative_bar``).

X-axis: time. Y-axis: cumulative decimal return.

Pure dict-emitting helper per ADR-0045 §Chart-Spec convention; no
DB / FastAPI / Qt imports. Decimal→float conversion happens at the
spec boundary.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from services.chart_specs._theme import apply_theme
from services.chart_specs.base import apply_axis_end, get_chart_theme


def build_benchmark_investment_total_return_spec(
    investment_name: str,
    benchmark_display_name: str,
    investment_cumulative: pd.Series,
    benchmark_cumulative: pd.Series,
    excess_cumulative: pd.Series,
    *,
    axis_end: date | None = None,
) -> dict[str, Any]:
    """Build the Stage-a per-investment cumulative-return Plotly spec.

    Args:
        investment_name: Display name of the investment (chart
            subtitle + first trace name).
        benchmark_display_name: Display name of the mapped benchmark
            (second trace name).
        investment_cumulative: Cumulative decimal-return series of
            the investment, indexed by month-end ``pd.Timestamp``.
        benchmark_cumulative: Cumulative decimal-return series of
            the benchmark on the same monthly grid.
        excess_cumulative: ``investment_cumulative -
            benchmark_cumulative``. Pre-computed so the spec stays
            free of arithmetic.
        axis_end: Optional shared x-axis end (the ADR-0113 §1 universe
            as-of). Extends the auto-range on the right only; the start
            stays data-driven. ``None`` leaves the tile on its own
            auto-range. The hero runs on a month-end grid, so the drawn
            line ends at the last month-end at or before ``axis_end``
            (ADR-0113 §6 — an accepted property, not worked around).

    Returns:
        Plotly figure spec dict (themed). Empty input series → a
        themed figure with a single centred annotation.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    if investment_cumulative.empty and benchmark_cumulative.empty and excess_cumulative.empty:
        return _empty_spec(investment_name, colours)

    inv_x, inv_y = _series_to_plotly_xy(investment_cumulative)
    bench_x, bench_y = _series_to_plotly_xy(benchmark_cumulative)
    excess_x, excess_y = _series_to_plotly_xy(excess_cumulative)

    # Positive vs. negative tint on the excess fill is achieved with
    # the zero-baseline reference: a single trace with `fill="tozeroy"`
    # and a translucent neutral colour reads as positive when y > 0
    # and negative when y < 0. The polish round (Phase 1b) can switch
    # to a split-fill if asymmetric tinting becomes a real need.
    excess_positive_fill = _hex_to_rgba(colours["positive_bar"], 0.18)

    traces: list[dict[str, Any]] = [
        {
            "type": "scatter",
            "mode": "lines",
            "x": inv_x,
            "y": inv_y,
            "name": investment_name,
            "line": {"color": colours["primary"], "width": 2},
            "hovertemplate": (
                f"<b>{investment_name}</b><br>%{{x|%Y-%m-%d}}<br>%{{y:.2%}}<extra></extra>"
            ),
        },
        {
            "type": "scatter",
            "mode": "lines",
            "x": bench_x,
            "y": bench_y,
            "name": benchmark_display_name,
            "line": {"color": colours["secondary"], "width": 2},
            "hovertemplate": (
                f"<b>{benchmark_display_name}</b><br>%{{x|%Y-%m-%d}}<br>%{{y:.2%}}<extra></extra>"
            ),
        },
        {
            "type": "scatter",
            "mode": "lines",
            "x": excess_x,
            "y": excess_y,
            "name": "Excess",
            "line": {"color": colours["accent_line"], "width": 1.2, "dash": "dot"},
            "fill": "tozeroy",
            "fillcolor": excess_positive_fill,
            "hovertemplate": ("<b>Excess</b><br>%{x|%Y-%m-%d}<br>%{y:.2%}<extra></extra>"),
        },
    ]

    layout: dict[str, Any] = {
        "title": {
            "text": f"{investment_name} vs. {benchmark_display_name}",
            "x": 0.5,
        },
        "xaxis": {
            "type": "date",
            "tickformat": "%Y-%m-%d",
            "title": {"text": ""},
        },
        "yaxis": {
            "tickformat": ".0%",
            "title": {"text": "Cumulative Return"},
        },
        "legend": {
            "orientation": "h",
            "y": 1.05,
            "x": 0.5,
            "xanchor": "center",
        },
        "showlegend": True,
        "hovermode": "x unified",
    }
    apply_axis_end(layout, axis_end, has_data=bool(inv_x or bench_x or excess_x))

    fig: dict[str, Any] = {
        "data": traces,
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)


def _empty_spec(investment_name: str, colours: dict[str, str]) -> dict[str, Any]:
    """Return a themed figure carrying a single empty-state annotation."""
    fig: dict[str, Any] = {
        "data": [],
        "layout": {
            "title": {"text": investment_name, "x": 0.5},
            "showlegend": False,
            "annotations": [
                {
                    "text": "No aligned observations",
                    "xref": "paper",
                    "yref": "paper",
                    "x": 0.5,
                    "y": 0.5,
                    "xanchor": "center",
                    "yanchor": "middle",
                    "showarrow": False,
                    "font": {"size": 13, "color": colours["text"]},
                }
            ],
            "xaxis": {"visible": False},
            "yaxis": {"visible": False},
        },
        "config": {
            "displayModeBar": False,
            "displaylogo": False,
            "responsive": True,
        },
    }
    return apply_theme(fig)


def _series_to_plotly_xy(series: pd.Series) -> tuple[list[str], list[float]]:
    """Convert a Pandas Series to parallel x / y lists for Plotly."""
    if series.empty:
        return [], []
    cleaned = series.dropna().sort_index()
    if cleaned.empty:
        return [], []
    x_values = [pd.Timestamp(idx).isoformat() for idx in cleaned.index]
    y_values = [float(v) for v in cleaned.values]
    return x_values, y_values


def _hex_to_rgba(hex_colour: str, alpha: float) -> str:
    h = hex_colour.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"Expected six-digit hex colour, got {hex_colour!r}")
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


__all__ = ["build_benchmark_investment_total_return_spec"]
