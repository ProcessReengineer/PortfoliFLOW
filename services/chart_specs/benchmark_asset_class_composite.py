# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — Stage-b asset-class composite small-multiples.

One subplot per asset class. Each subplot contains two cumulative
return lines: composite (primary) and benchmark (secondary).
Subplot title: ``asset_class_display_name``. Per-tile footer
annotation under the chart: ``"Excess +X.X% p.a. | IR Y.YY"``.
A top-right badge per populated tile carries the annualised excess
return in green (positive) or red (negative). Empty tiles (no own
investments) get their benchmark line and title dimmed to alpha
0.45 / 0.55 so the eye is drawn to populated tiles first; per
ADR-0062 §3.

Populated tiles are sorted by annualised excess descending; empty
tiles sink to the end of the grid.

Layout: 3 columns, automatic row count. Shared X-axis range (union
of all tile periods), per-subplot Y-axis (each composite/benchmark
pair scales independently).

Pure dict-emitting helper per ADR-0045; no DB / FastAPI / Qt imports.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme

_N_COLS: int = 3
_H_GAP: float = 0.05
_V_GAP: float = 0.12
_HEADROOM: float = 1.15

# Dim factors for empty-tile rendering per ADR-0062 §3.
_EMPTY_TILE_LINE_ALPHA: float = 0.45
_EMPTY_TILE_TITLE_ALPHA: float = 0.55

_MINUS_SIGN: str = "−"  # U+2212 true minus, not a hyphen


def build_benchmark_asset_class_composite_spec(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the Stage-b small-multiples Plotly spec.

    Args:
        rows: List of dicts with keys:
            ``asset_class_display_name``,
            ``benchmark_display_name``,
            ``composite_cumulative`` (``pd.Series``),
            ``benchmark_cumulative`` (``pd.Series``),
            ``excess_return_annualised`` (float),
            ``information_ratio`` (float),
            ``n_investments`` (int).
            Asset classes with ``n_investments == 0`` get a "No
            investments yet" subplot annotation instead of lines.

    Returns:
        Plotly figure spec dict with theme applied. Empty ``rows``
        list → a themed figure with a single centre annotation.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    if not rows:
        return _empty_spec(colours)

    rows = sorted(rows, key=_excess_descending_sort_key)
    n_rows = math.ceil(len(rows) / _N_COLS)
    cell_w = (1.0 - (_N_COLS - 1) * _H_GAP) / _N_COLS
    cell_h = (1.0 - (n_rows - 1) * _V_GAP) / n_rows

    from_ts, to_ts = _resolve_shared_x_range(rows)

    data: list[dict[str, Any]] = []
    layout: dict[str, Any] = {
        "title": {
            "text": "Asset-Class Composites vs. Benchmarks",
            "x": 0.5,
        },
        "margin": {"l": 50, "r": 30, "t": 60, "b": 50},
        "showlegend": False,
        "annotations": [],
        "height": max(450, n_rows * 240),
    }

    for index, row in enumerate(rows):
        grid_row = index // _N_COLS
        col = index % _N_COLS
        x0 = col * (cell_w + _H_GAP)
        x1 = x0 + cell_w
        y1_paper = 1.0 - grid_row * (cell_h + _V_GAP)
        y0_paper = y1_paper - cell_h

        axis_id = index + 1
        x_axis_name = "x" if axis_id == 1 else f"x{axis_id}"
        y_axis_name = "y" if axis_id == 1 else f"y{axis_id}"
        x_axis_key = "xaxis" if axis_id == 1 else f"xaxis{axis_id}"
        y_axis_key = "yaxis" if axis_id == 1 else f"yaxis{axis_id}"

        composite_series: pd.Series = row["composite_cumulative"]
        benchmark_series: pd.Series = row["benchmark_cumulative"]
        ac_label = str(row["asset_class_display_name"])
        bm_label = str(row["benchmark_display_name"])
        n_invest = int(row.get("n_investments", 0))

        if n_invest == 0 or (composite_series is None or composite_series.empty):
            # Render benchmark line alone (if available) so the tile
            # still shows the market context, plus the empty-state
            # subtitle. Both line and title are dimmed per
            # ADR-0062 §3 so the eye is drawn to populated tiles.
            if benchmark_series is not None and not benchmark_series.empty:
                bench_x, bench_y = _series_to_plotly_xy(benchmark_series)
                data.append(
                    {
                        "type": "scatter",
                        "mode": "lines",
                        "x": bench_x,
                        "y": bench_y,
                        "name": bm_label,
                        "line": {
                            "color": _hex_to_rgba(
                                colours["secondary"],
                                _EMPTY_TILE_LINE_ALPHA,
                            ),
                            "width": 1.6,
                        },
                        "xaxis": x_axis_name,
                        "yaxis": y_axis_name,
                        "showlegend": False,
                        "hovertemplate": (
                            f"<b>{bm_label}</b><br>%{{x|%Y-%m-%d}}<br>%{{y:.2%}}<extra></extra>"
                        ),
                    }
                )
                _, bench_y_floats = bench_x, bench_y
                y_top = _resolve_y_top([], bench_y_floats)
                y_bot = _resolve_y_bottom([], bench_y_floats)
            else:
                y_top = 0.05
                y_bot = -0.05

            layout[x_axis_key] = {
                "domain": [x0, x1],
                "anchor": y_axis_name,
                "type": "date",
                "tickformat": "%Y-%m",
                "range": [from_ts, to_ts],
            }
            layout[y_axis_key] = {
                "domain": [y0_paper, y1_paper],
                "anchor": x_axis_name,
                "range": [y_bot, y_top],
                "tickformat": ".0%",
            }
            layout["annotations"].append(
                _subplot_title_annotation(ac_label, x0, x1, y1_paper, colours, dimmed=True)
            )
            layout["annotations"].append(
                _subplot_footer_annotation("No investments yet", x0, x1, y0_paper, colours)
            )
            continue

        comp_x, comp_y = _series_to_plotly_xy(composite_series)
        bench_x, bench_y = _series_to_plotly_xy(benchmark_series)

        data.append(
            {
                "type": "scatter",
                "mode": "lines",
                "x": comp_x,
                "y": comp_y,
                "name": ac_label,
                "line": {"color": colours["primary"], "width": 1.8},
                "xaxis": x_axis_name,
                "yaxis": y_axis_name,
                "showlegend": False,
                "hovertemplate": (
                    f"<b>{ac_label}</b><br>%{{x|%Y-%m-%d}}<br>%{{y:.2%}}<extra></extra>"
                ),
            }
        )
        data.append(
            {
                "type": "scatter",
                "mode": "lines",
                "x": bench_x,
                "y": bench_y,
                "name": bm_label,
                "line": {"color": colours["secondary"], "width": 1.6},
                "xaxis": x_axis_name,
                "yaxis": y_axis_name,
                "showlegend": False,
                "hovertemplate": (
                    f"<b>{bm_label}</b><br>%{{x|%Y-%m-%d}}<br>%{{y:.2%}}<extra></extra>"
                ),
            }
        )

        y_top = _resolve_y_top(comp_y, bench_y)
        y_bot = _resolve_y_bottom(comp_y, bench_y)
        layout[x_axis_key] = {
            "domain": [x0, x1],
            "anchor": y_axis_name,
            "type": "date",
            "tickformat": "%Y-%m",
            "range": [from_ts, to_ts],
        }
        layout[y_axis_key] = {
            "domain": [y0_paper, y1_paper],
            "anchor": x_axis_name,
            "range": [y_bot, y_top],
            "tickformat": ".0%",
        }

        layout["annotations"].append(
            _subplot_title_annotation(ac_label, x0, x1, y1_paper, colours, dimmed=False)
        )
        badge = _subplot_excess_badge_annotation(
            row.get("excess_return_annualised"), x0, x1, y1_paper, colours
        )
        if badge is not None:
            layout["annotations"].append(badge)
        footer_text = _format_footer(
            row.get("excess_return_annualised"),
            row.get("information_ratio"),
        )
        layout["annotations"].append(
            _subplot_footer_annotation(footer_text, x0, x1, y0_paper, colours)
        )

    fig: dict[str, Any] = {
        "data": data,
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)


def _empty_spec(colours: dict[str, str]) -> dict[str, Any]:
    fig: dict[str, Any] = {
        "data": [],
        "layout": {
            "title": {
                "text": "Asset-Class Composites vs. Benchmarks",
                "x": 0.5,
            },
            "showlegend": False,
            "annotations": [
                {
                    "text": "No asset classes with benchmark mappings",
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
    if series is None or series.empty:
        return [], []
    cleaned = series.dropna().sort_index()
    if cleaned.empty:
        return [], []
    x_values = [pd.Timestamp(idx).isoformat() for idx in cleaned.index]
    y_values = [float(v) for v in cleaned.values]
    return x_values, y_values


def _resolve_shared_x_range(rows: list[dict[str, Any]]) -> tuple[str, str]:
    """Union of all (composite + benchmark) timestamps for the shared X axis."""
    timestamps: list[pd.Timestamp] = []
    for row in rows:
        for key in ("composite_cumulative", "benchmark_cumulative"):
            series = row.get(key)
            if series is None or series.empty:
                continue
            timestamps.append(pd.Timestamp(series.index.min()))
            timestamps.append(pd.Timestamp(series.index.max()))
    if not timestamps:
        return "", ""
    return min(timestamps).isoformat(), max(timestamps).isoformat()


def _resolve_y_top(composite_values: list[float], benchmark_values: list[float]) -> float:
    candidates = [v for v in [*composite_values, *benchmark_values] if v is not None]
    if not candidates:
        return 0.05
    high = max(candidates)
    if high <= 0.0:
        return 0.05
    return high * _HEADROOM


def _resolve_y_bottom(composite_values: list[float], benchmark_values: list[float]) -> float:
    candidates = [v for v in [*composite_values, *benchmark_values] if v is not None]
    if not candidates:
        return -0.05
    low = min(candidates)
    if low >= 0.0:
        return 0.0
    return low * _HEADROOM


def _subplot_title_annotation(
    text: str,
    x0: float,
    x1: float,
    y_top: float,
    colours: dict[str, str],
    *,
    dimmed: bool = False,
) -> dict[str, Any]:
    title_colour = (
        _hex_to_rgba(colours["text"], _EMPTY_TILE_TITLE_ALPHA) if dimmed else colours["text"]
    )
    return {
        "text": text,
        "xref": "paper",
        "yref": "paper",
        "x": (x0 + x1) / 2.0,
        "y": min(y_top + 0.02, 1.0),
        "xanchor": "center",
        "yanchor": "bottom",
        "showarrow": False,
        "font": {"size": 13, "color": title_colour},
    }


def _subplot_footer_annotation(
    text: str,
    x0: float,
    x1: float,
    y_bot: float,
    colours: dict[str, str],
) -> dict[str, Any]:
    return {
        "text": text,
        "xref": "paper",
        "yref": "paper",
        "x": (x0 + x1) / 2.0,
        "y": max(y_bot - 0.04, 0.0),
        "xanchor": "center",
        "yanchor": "top",
        "showarrow": False,
        "font": {"size": 11, "color": colours["text"]},
    }


def _subplot_excess_badge_annotation(
    excess_annualised: float | None,
    x0: float,
    x1: float,
    y_top: float,
    colours: dict[str, str],
) -> dict[str, Any] | None:
    """Top-right per-tile badge with the annualised excess return.

    Green for positive excess, red for negative, default text colour
    for exactly zero. ``None`` short-circuits the badge for tiles
    where the metric is unavailable (NaN / missing).
    """
    if excess_annualised is None or excess_annualised != excess_annualised:
        return None
    if excess_annualised > 0:
        colour = colours["positive_bar"]
        sign = "+"
        magnitude = excess_annualised
    elif excess_annualised < 0:
        colour = colours["negative_bar"]
        sign = _MINUS_SIGN
        magnitude = abs(excess_annualised)
    else:
        colour = colours["text"]
        sign = ""
        magnitude = 0.0
    text = f"{sign}{magnitude * 100:.1f}%"
    return {
        "text": f"<b>{text}</b>",
        "xref": "paper",
        "yref": "paper",
        "x": x1 - 0.005,
        "y": min(y_top + 0.005, 1.0),
        "xanchor": "right",
        "yanchor": "bottom",
        "showarrow": False,
        "font": {"size": 12, "color": colour},
    }


def _excess_descending_sort_key(row: dict[str, Any]) -> tuple[int, float]:
    """Sort key: populated tiles first (by excess desc); empty tiles last.

    The first element is a bucket (0 = populated, 1 = empty) so empty
    tiles sink to the end regardless of their excess. Within the
    populated bucket the negated excess sorts descending.
    """
    n_invest = int(row.get("n_investments", 0) or 0)
    composite = row.get("composite_cumulative")
    has_data = composite is not None and hasattr(composite, "empty") and not composite.empty
    if n_invest == 0 or not has_data:
        return (1, 0.0)
    excess = row.get("excess_return_annualised")
    if excess is None or excess != excess:
        return (0, 0.0)
    return (0, -float(excess))


def _hex_to_rgba(hex_colour: str, alpha: float) -> str:
    """Convert a six-digit hex colour to an rgba() string.

    Mirrors the helper in benchmark_investment_total_return.py and
    limits_coverage_small_multiples.py. Promoting this to a shared
    location is a separate refactor outside the scope of A12 Phase 1b.
    """
    h = hex_colour.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"Expected six-digit hex colour, got {hex_colour!r}")
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


def _format_footer(excess_ann: float | None, info_ratio: float | None) -> str:
    parts: list[str] = []
    if excess_ann is not None and excess_ann == excess_ann:
        sign = "+" if excess_ann >= 0 else ""
        parts.append(f"Excess {sign}{excess_ann * 100:.1f}% p.a.")
    if info_ratio is not None and info_ratio == info_ratio:
        parts.append(f"IR {info_ratio:.2f}")
    return " | ".join(parts) if parts else "—"


__all__ = ["build_benchmark_asset_class_composite_spec"]
