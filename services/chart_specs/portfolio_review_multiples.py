# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — Portfolio Review tile 3 (Multiples stacked bars + IRR line).

Per-year DPI (red bottom) + RVPI (red top stacked) = TVPI annotation
above each bar; IRR line on a secondary y-axis (right). Implements
the variant deferred from
:func:`services.chart_specs.investment_multiples.build_multiples_spec`
(``style="stacked_bars"``) — see the ``NotImplementedError`` raised
there.

Mirrors the QT
:class:`~services.reporting.chart_builders.StackedBarWithLineBuilder`
configuration in
``services.reporting.report_engine._MULTIPLES_TS_CONFIG``.
"""

from __future__ import annotations

from typing import Any

from services.analytics.portfolio_aggregation import PortfolioMultiplesSeries
from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme


def build_multiples_stacked_spec(
    series: PortfolioMultiplesSeries,
    title: str = "Multiples (TVPI / DPI / IRR)",
) -> dict[str, Any]:
    """Build the stacked Multiples + IRR Plotly spec.

    Args:
        series: :class:`PortfolioMultiplesSeries` produced by
            :func:`services.analytics.portfolio_aggregation
            .aggregate_portfolio_multiples`.
        title: Tile title.

    Returns:
        Plotly figure spec dict.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    x_values = [str(y) for y in series.years]

    def _none_for_nan(v: float) -> float | None:
        return None if v != v else float(v)

    dpi_y = [_none_for_nan(v) for v in series.dpi]
    rvpi_y = [_none_for_nan(v) for v in series.rvpi]
    tvpi_labels = [f"{v:.2f}x" if v == v else "" for v in series.tvpi]
    irr_y = [_none_for_nan(v) for v in series.irr]
    irr_x = [x for x, v in zip(x_values, irr_y) if v is not None]
    irr_y_filtered = [v for v in irr_y if v is not None]

    dpi_trace = {
        "type": "bar",
        "x": x_values,
        "y": dpi_y,
        "name": "DPI",
        "marker": {"color": colours["primary"]},
        "hovertemplate": ("<b>DPI</b><br>%{x}<br>%{y:.2f}x<extra></extra>"),
    }
    rvpi_trace = {
        "type": "bar",
        "x": x_values,
        "y": rvpi_y,
        "name": "RVPI",
        "marker": {
            "color": colours.get(
                "primary_translucent",
                colours.get("secondary", colours["primary"]),
            )
        },
        "text": tvpi_labels,
        "textposition": "outside",
        "hovertemplate": ("<b>RVPI</b><br>%{x}<br>%{y:.2f}x<extra></extra>"),
    }
    irr_trace = {
        "type": "scatter",
        "mode": "lines",
        "x": irr_x,
        "y": [v * 100.0 for v in irr_y_filtered],
        "name": "IRR",
        "yaxis": "y2",
        "line": {
            "color": colours.get(
                "accent_line",
                colours.get("nav_line", colours["secondary"]),
            ),
            "width": 2,
        },
        "hovertemplate": ("<b>IRR</b><br>%{x}<br>%{y:.1f}%<extra></extra>"),
    }

    layout: dict[str, Any] = {
        "title": {"text": title, "x": 0.5},
        "xaxis": {"type": "category", "title": {"text": ""}},
        "yaxis": {
            "tickformat": ".2f",
            "ticksuffix": "x",
            "title": {"text": "Multiple"},
            "rangemode": "tozero",
        },
        "yaxis2": {
            "tickformat": ".1f",
            "ticksuffix": "%",
            "title": {"text": "IRR"},
            "overlaying": "y",
            "side": "right",
            "showgrid": False,
        },
        "barmode": "stack",
        "showlegend": True,
        "hovermode": "x unified",
    }

    fig: dict[str, Any] = {
        "data": [dpi_trace, rvpi_trace, irr_trace],
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)
