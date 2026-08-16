# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — investment Multiples (TVPI / DPI lines).

Migration of the QT ``_make_tvpi_dpi_chart`` widget. The Charts
module variant (``modules/front_office/charts.py``) plots only
**TVPI and DPI as lines** with a 1.0× breakeven reference line —
no IRR overlay and no stacked bars. The portfolio-review treemap
variant (stacked DPI/RVPI bars + IRR line on a secondary axis) is
deliberately deferred to sub-stream 5e and surfaces here as a
``style="stacked_bars"`` parameter that raises
``NotImplementedError``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

import pandas as pd

from services.chart_specs._theme import apply_theme
from services.chart_specs.base import apply_axis_end, get_chart_theme


def build_multiples_spec(
    rolling_multiples: pd.DataFrame,
    rolling_irr: pd.Series,
    investment_name: str,
    style: Literal["lines", "stacked_bars"] = "lines",
    *,
    axis_end: date | None = None,
) -> dict[str, Any]:
    """Build the Multiples Plotly spec for a single investment.

    Args:
        rolling_multiples: DataFrame with columns ``as_of_date``,
            ``tvpi``, ``dpi``, ``rvpi``. The ``rvpi`` column is
            unused in the ``lines`` variant but accepted for API
            symmetry with the future stacked-bars variant.
        rolling_irr: Pandas Series indexed by ``as_of_date``, IRR
            values as decimals. Unused in the ``lines`` variant; the
            argument exists so callers can compute it once per
            investment and pass it through to whichever style they
            render.
        investment_name: Display name of the investment.
        style: Either ``"lines"`` (Charts-module variant: TVPI and
            DPI lines) or ``"stacked_bars"`` (Portfolio-Review
            variant: stacked DPI / RVPI bars with IRR overlay,
            **deferred to sub-stream 5e**).
        axis_end: Optional shared x-axis end (the ADR-0113 §1 universe
            as-of). Extends the auto-range on the right only; the start
            stays data-driven. ``None`` leaves the tile on its own
            auto-range.

    Returns:
        Plotly figure spec dict ``{"data", "layout", "config"}``.

    Raises:
        NotImplementedError: When ``style="stacked_bars"`` is
            requested. The Portfolio-Review chart will be
            implemented under sub-stream 5e — see ADR-0045 §3.
    """
    if style == "stacked_bars":
        raise NotImplementedError(
            "Stacked-bar multiples chart (Portfolio-Review variant) "
            "is implemented in sub-stream 5e — see ADR-0045 §3. The "
            "Charts-module variant uses style='lines'."
        )
    if style != "lines":
        raise ValueError(f"Unknown multiples style {style!r}; expected 'lines' or 'stacked_bars'.")

    theme = get_chart_theme()
    colours = theme["colours"]

    if rolling_multiples.empty:
        x_values: list[str] = []
        tvpi_values: list[float] = []
        dpi_values: list[float] = []
    else:
        sorted_df = rolling_multiples.sort_values("as_of_date")
        x_values = [pd.Timestamp(d).isoformat() for d in sorted_df["as_of_date"]]
        tvpi_values = [float(v) for v in sorted_df["tvpi"]]
        dpi_values = [float(v) for v in sorted_df["dpi"]]

    tvpi_trace = {
        "type": "scatter",
        "mode": "lines",
        "x": x_values,
        "y": tvpi_values,
        "name": "TVPI",
        "line": {"color": colours["primary"], "width": 2},
        "hovertemplate": ("<b>TVPI</b><br>%{x|%Y-%m-%d}<br>%{y:.2f}x<extra></extra>"),
    }
    dpi_trace = {
        "type": "scatter",
        "mode": "lines",
        "x": x_values,
        "y": dpi_values,
        "name": "DPI",
        "line": {
            "color": colours.get("secondary", colours["primary"]),
            "width": 2,
            "dash": "dash",
        },
        "hovertemplate": ("<b>DPI</b><br>%{x|%Y-%m-%d}<br>%{y:.2f}x<extra></extra>"),
    }

    layout: dict[str, Any] = {
        "title": {
            "text": f"TVPI & DPI — {investment_name}",
            "x": 0.5,
        },
        "xaxis": {"type": "date", "tickformat": "%Y-%m-%d", "title": {"text": ""}},
        "yaxis": {
            "tickformat": ".2f",
            "ticksuffix": "x",
            "title": {"text": "Multiple"},
            "rangemode": "tozero",
        },
        "showlegend": True,
        "shapes": [
            {
                "type": "line",
                "xref": "paper",
                "x0": 0.0,
                "x1": 1.0,
                "yref": "y",
                "y0": 1.0,
                "y1": 1.0,
                "line": {
                    "color": colours["axis_line"],
                    "width": 1,
                    "dash": "dot",
                },
            },
        ],
    }
    apply_axis_end(layout, axis_end, has_data=bool(x_values))

    fig: dict[str, Any] = {
        "data": [tvpi_trace, dpi_trace],
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)
