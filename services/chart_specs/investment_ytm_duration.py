# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — fixed-income YTM / OAS & duration (dual-axis).

Fixed-Income Tile 2 of the liquid-archetype triplet (ADR-0082).
Renders the yield and interest-rate-sensitivity history of a bond
investment on two y-axes: yield to maturity (and, where available,
option-adjusted spread) on the left in percent, and effective duration
in years on the right. The dual axis follows the established
``yaxis2`` / ``overlaying='y'`` pattern of
``services/chart_specs/investment_cashflows_nav.py``.

``apply_theme`` only themes the primary axes, so the right-hand
``yaxis2`` is themed explicitly via :func:`themed_secondary_axis`.

The function is pure — a DataFrame in, a plain Plotly dict out — so it
is callable from any non-GUI consumer (ADR-0013 / ADR-0045).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from services.chart_specs._theme import apply_theme, themed_secondary_axis
from services.chart_specs.base import apply_axis_end, get_chart_theme


def _nullable_percent(values: pd.Series) -> list[float | None]:
    """Scale a Series to percent, mapping NaN to ``None`` (a line gap).

    Args:
        values: Numeric Series of decimal rates (``0.045`` for 4.5 %).

    Returns:
        A list of percent floats with ``None`` wherever the input was
        NaN, so Plotly renders a gap rather than a spurious zero.
    """
    return [None if pd.isna(v) else float(v) * 100.0 for v in values]


def _nullable(values: pd.Series) -> list[float | None]:
    """Convert a Series to floats, mapping NaN to ``None`` (a line gap).

    Args:
        values: Numeric Series in native units (e.g. duration in years).

    Returns:
        A list of floats with ``None`` wherever the input was NaN.
    """
    return [None if pd.isna(v) else float(v) for v in values]


def build_ytm_duration_spec(
    analytics: pd.DataFrame,
    investment_name: str,
    *,
    axis_end: date | None = None,
) -> dict[str, Any]:
    """Build the dual-axis YTM / OAS & duration Plotly spec.

    Trace layout:

    - **YTM** — line on ``yaxis`` (left), percent-scaled, primary
      colour.
    - **OAS** — line on ``yaxis`` (left), percent-scaled, in a muted
      theme colour. Emitted **only** when the ``oas`` column exists and
      is not entirely NaN (government bonds carry no spread).
    - **Effective Duration** — line on ``yaxis2`` (right), in years,
      secondary colour.

    Args:
        analytics: Date-indexed DataFrame, sorted ascending, with
            decimal-rate columns ``ytm`` and ``oas`` (nullable) and a
            ``eff_duration`` column in years. May be empty.
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

    frame = analytics.sort_index() if not analytics.empty else analytics
    x_dates = [pd.Timestamp(idx).isoformat() for idx in frame.index] if not frame.empty else []

    ytm_trace = {
        "type": "scatter",
        "mode": "lines",
        "x": x_dates,
        "y": _nullable_percent(frame["ytm"]) if not frame.empty else [],
        "yaxis": "y",
        "line": {"color": colours["primary"], "width": 2},
        "name": "YTM",
        "hovertemplate": ("<b>YTM</b><br>%{x|%Y-%m-%d}<br>%{y:.2f}%<extra></extra>"),
    }

    data: list[dict[str, Any]] = [ytm_trace]

    # Govies have no OAS; only draw the spread line when the column is
    # present and carries at least one real value.
    has_oas = not frame.empty and "oas" in frame.columns and not frame["oas"].isna().all()
    if has_oas:
        data.append(
            {
                "type": "scatter",
                "mode": "lines",
                "x": x_dates,
                "y": _nullable_percent(frame["oas"]),
                "yaxis": "y",
                "line": {
                    "color": colours.get("muted", colours.get("tertiary", colours["primary"])),
                    "width": 2,
                },
                "name": "OAS",
                "hovertemplate": ("<b>OAS</b><br>%{x|%Y-%m-%d}<br>%{y:.2f}%<extra></extra>"),
            }
        )

    duration_trace = {
        "type": "scatter",
        "mode": "lines",
        "x": x_dates,
        "y": _nullable(frame["eff_duration"]) if not frame.empty else [],
        "yaxis": "y2",
        "line": {"color": colours["secondary"], "width": 2},
        "name": "Eff. Duration",
        "hovertemplate": ("<b>Eff. Duration</b><br>%{x|%Y-%m-%d}<br>%{y:.2f} yrs<extra></extra>"),
    }
    data.append(duration_trace)

    layout: dict[str, Any] = {
        "title": {
            "text": f"YTM & Duration — {investment_name}",
            "x": 0.5,
        },
        "xaxis": {"type": "date", "tickformat": "%Y-%m-%d", "title": {"text": ""}},
        "yaxis": {
            "title": {"text": "YTM / OAS"},
            "ticksuffix": "%",
            "tickformat": ".2f",
            "side": "left",
        },
        "yaxis2": {
            "title": {"text": "Duration (yrs)"},
            "overlaying": "y",
            "side": "right",
            "showgrid": False,
            "tickformat": ".1f",
            **themed_secondary_axis(),
        },
        "showlegend": True,
        "hovermode": "x unified",
    }
    apply_axis_end(layout, axis_end, has_data=bool(x_dates))

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
