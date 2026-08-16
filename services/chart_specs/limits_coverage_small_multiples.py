# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — Investment Limits small-multiples coverage chart.

One spec function parameterised over family (``"SAA"`` / ``"AnlV"``).
Renders one subplot per class_key with the coverage line plus the
step-function limit line overlay. ``NO_LIMIT`` and ``UNALLOCATED``
classes are intentionally not charted — they carry no ``max_pct``
reference so a coverage line without a ceiling is not meaningful in
this surface.

The coverage line takes the canonical ``colours.primary`` accent;
the limit line uses ``colours.text`` as a dashed step-function
overlay. The Y-axis is per-subplot (each class gets its own scale),
the X-axis range is shared and clamped to the evaluation range.

Decimal values are converted to ``float`` at the spec boundary;
``Decimal`` values do not survive JSON serialisation.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd

from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme

_N_COLS: int = 3
_H_GAP: float = 0.05
_V_GAP: float = 0.10
_HEADROOM: float = 1.2


def build_limits_coverage_spec(
    coverage_df: pd.DataFrame,
    limit_step_lines: dict[str, list[tuple[date, Decimal | None]]],
    family_label: str,
    *,
    to_date: date,
) -> dict[str, Any]:
    """Build a Plotly small-multiples spec for limit coverage.

    Args:
        coverage_df: Long-format coverage DataFrame from the engine
            (columns: ``as_of_date``, ``class_key``, ``max_pct``,
            ``nav_sum_eur``, ``coverage_pct``, ``headroom_eur``,
            ``status``). May be empty.
        limit_step_lines: Per-class step-line series from
            :meth:`services.limits.LimitsCoverageService
            ._build_limit_step_lines`. Keys are ``class_key`` values;
            entries are ordered ``(effective_from, max_pct | None)``
            transitions across set boundaries.
        family_label: Human-readable family name for the chart title
            (e.g. ``"SAA"`` or ``"AnlV"``).
        to_date: Upper bound of the evaluation range; extends the
            final step segment of each class to the chart edge.

    Returns:
        Plotly figure spec dict with theme applied. Empty-input path
        emits a themed figure with a single centre annotation.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    if coverage_df.empty:
        return _empty_spec(family_label, colours)

    # Limited classes: those with at least one row carrying a
    # max_pct. NO_LIMIT and UNALLOCATED rows have max_pct = None and
    # are intentionally excluded from the chart.
    limited_rows = coverage_df[coverage_df["max_pct"].notna()]
    class_keys = sorted(limited_rows["class_key"].unique().tolist())

    if not class_keys:
        return _empty_spec(family_label, colours)

    from_ts, to_ts = _resolve_x_range(coverage_df, to_date)

    n_rows = math.ceil(len(class_keys) / _N_COLS)
    cell_w = (1.0 - (_N_COLS - 1) * _H_GAP) / _N_COLS
    cell_h = (1.0 - (n_rows - 1) * _V_GAP) / n_rows

    data: list[dict[str, Any]] = []
    layout: dict[str, Any] = {
        "title": {"text": f"{family_label} Coverage", "x": 0.5},
        "margin": {"l": 50, "r": 30, "t": 60, "b": 40},
        "showlegend": False,
        "annotations": [],
        "height": max(450, n_rows * 220),
    }

    for index, class_key in enumerate(class_keys):
        row = index // _N_COLS
        col = index % _N_COLS
        x0 = col * (cell_w + _H_GAP)
        x1 = x0 + cell_w
        # Plotly's paper coords have y=0 at the bottom; the first
        # grid row sits at the top, so invert the row index.
        y1 = 1.0 - row * (cell_h + _V_GAP)
        y0 = y1 - cell_h

        axis_id = index + 1
        x_axis_name = "x" if axis_id == 1 else f"x{axis_id}"
        y_axis_name = "y" if axis_id == 1 else f"y{axis_id}"
        x_axis_key = "xaxis" if axis_id == 1 else f"xaxis{axis_id}"
        y_axis_key = "yaxis" if axis_id == 1 else f"yaxis{axis_id}"

        class_coverage = coverage_df[coverage_df["class_key"] == class_key]
        coverage_dates = [pd.Timestamp(d).date().isoformat() for d in class_coverage["as_of_date"]]
        coverage_values = [float(v) for v in class_coverage["coverage_pct"].tolist()]

        step_xs, step_ys = _materialise_step_line(limit_step_lines.get(class_key, []), to_date)

        y_top = _resolve_y_top(coverage_values, step_ys)

        data.append(
            {
                "type": "scatter",
                "mode": "lines",
                "x": coverage_dates,
                "y": coverage_values,
                "line": {"color": colours["primary"], "width": 1.8},
                "fill": "tozeroy",
                "fillcolor": _hex_to_rgba(colours["primary"], 0.18),
                "xaxis": x_axis_name,
                "yaxis": y_axis_name,
                "name": "Coverage",
                "showlegend": False,
                "hovertemplate": (
                    f"<b>{class_key}</b><br>%{{x|%Y-%m-%d}}<br>Coverage: %{{y:.2f}}%<extra></extra>"
                ),
            }
        )
        data.append(
            {
                "type": "scatter",
                "mode": "lines",
                "x": step_xs,
                "y": step_ys,
                "line": {
                    "color": colours["text"],
                    "width": 1.2,
                    "dash": "dash",
                    "shape": "hv",
                },
                "xaxis": x_axis_name,
                "yaxis": y_axis_name,
                "name": "Limit",
                "showlegend": False,
                "hovertemplate": ("<b>Limit</b><br>%{x|%Y-%m-%d}<br>%{y:.2f}%<extra></extra>"),
            }
        )

        layout[x_axis_key] = {
            "domain": [x0, x1],
            "anchor": y_axis_name,
            "type": "date",
            "tickformat": "%Y-%m",
            "range": [from_ts.isoformat(), to_ts.isoformat()],
        }
        layout[y_axis_key] = {
            "domain": [y0, y1],
            "anchor": x_axis_name,
            "range": [0.0, y_top],
            "ticksuffix": "%",
            "tickformat": ".0f",
        }
        layout["annotations"].append(
            {
                "text": class_key,
                "xref": "paper",
                "yref": "paper",
                "x": (x0 + x1) / 2.0,
                "y": min(y1 + 0.02, 1.0),
                "xanchor": "center",
                "yanchor": "bottom",
                "showarrow": False,
                "font": {"size": 11, "color": colours["text"]},
            }
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


def _empty_spec(family_label: str, colours: dict[str, str]) -> dict[str, Any]:
    """Return a themed figure carrying a single empty-state annotation."""
    fig: dict[str, Any] = {
        "data": [],
        "layout": {
            "title": {"text": f"{family_label} Coverage", "x": 0.5},
            "margin": {"l": 50, "r": 30, "t": 60, "b": 40},
            "showlegend": False,
            "annotations": [
                {
                    "text": "No coverage data in range",
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


def _resolve_x_range(
    coverage_df: pd.DataFrame,
    to_date: date,
) -> tuple[date, date]:
    """Resolve the shared X-axis range from the coverage DataFrame."""
    coverage_dates = pd.to_datetime(coverage_df["as_of_date"])
    from_ts = coverage_dates.min().date()
    to_ts = max(coverage_dates.max().date(), to_date)
    return from_ts, to_ts


def _materialise_step_line(
    transitions: list[tuple[date, Decimal | None]],
    to_date: date,
) -> tuple[list[str | None], list[float | None]]:
    """Flatten a class's step-line transitions into ``(xs, ys)`` lists.

    ``None`` values in the source mark removal gaps and are passed
    through to Plotly, which renders them as breaks in the line.
    The final segment is extended to ``to_date`` only when the last
    transition carried a value — a trailing ``None`` (class removed
    in the most-recent set) leaves the chart edge open.
    """
    xs: list[str | None] = []
    ys: list[float | None] = []
    for eff_date, max_pct in transitions:
        xs.append(eff_date.isoformat())
        ys.append(float(max_pct) if max_pct is not None else None)
    if xs and ys[-1] is not None:
        xs.append(to_date.isoformat())
        ys.append(ys[-1])
    return xs, ys


def _resolve_y_top(
    coverage_values: list[float],
    step_values: list[float | None],
) -> float:
    """Per-subplot Y-axis upper bound with ``_HEADROOM`` extra space."""
    candidates: list[float] = []
    if coverage_values:
        candidates.append(max(coverage_values))
    step_value_candidates = [v for v in step_values if v is not None]
    if step_value_candidates:
        candidates.append(max(step_value_candidates))
    if not candidates:
        return 1.0
    return max(candidates) * _HEADROOM


def _hex_to_rgba(hex_colour: str, alpha: float) -> str:
    """Convert a ``#RRGGBB`` hex string into ``rgba(r, g, b, alpha)``.

    Args:
        hex_colour: A six-digit hex string with leading ``#``
            (e.g. ``"#E8304A"``).
        alpha: Opacity in the range ``[0.0, 1.0]``.

    Returns:
        A Plotly-compatible ``rgba(...)`` string.

    Raises:
        ValueError: When ``hex_colour`` is not a six-digit hex string.
    """
    h = hex_colour.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"Expected six-digit hex colour, got {hex_colour!r}")
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


__all__ = ["build_limits_coverage_spec"]
