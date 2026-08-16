# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — Portfolio Review tile 2 (yearly cashflows + NAV/NCG).

Yearly capital calls (negative, red) and distributions (positive,
green) drawn as bars, with NAV and Net Capital Gain rendered as line
overlays on the same axis. Mirrors the QT
:class:`~services.reporting.chart_builders.StackedBarWithLineBuilder`
output for the Portfolio Review report's second tile.

The QT report shows distributions as positive bars and calls as
negative bars; the y-axis straddles zero and the NAV / NCG lines
share the same numeric scale as the bars.
"""

from __future__ import annotations

from typing import Any

from services.analytics.portfolio_aggregation import PortfolioCashflowSeries
from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme


def build_yearly_cashflows_spec(
    series: PortfolioCashflowSeries,
    title: str = "Cashflows",
    currency: str = "EUR",
) -> dict[str, Any]:
    """Build the year-aggregated Cashflows + NAV / NCG overlay Plotly spec.

    Args:
        series: :class:`PortfolioCashflowSeries` produced by
            :func:`services.analytics.portfolio_aggregation
            .aggregate_portfolio_cashflows`.
        title: Tile title.
        currency: The currency the series is denominated in — the
            tenant's functional currency on the portfolio surfaces, the
            investment's own currency on the single-investment surface
            (which does not convert). Rendered as the value-axis title.
            Defaults to ``"EUR"`` for the pre-ADR-0101 call shape.

    Returns:
        Plotly figure spec dict.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    x_values = [str(y) for y in series.years]

    calls_trace = {
        "type": "bar",
        "x": x_values,
        "y": [float(v) for v in series.calls],
        "name": "Calls",
        "marker": {"color": colours.get("calls", colours["primary"])},
        "hovertemplate": ("<b>Calls</b><br>%{x}<br>%{y:,.0f}<extra></extra>"),
    }
    distributions_trace = {
        "type": "bar",
        "x": x_values,
        "y": [float(v) for v in series.distributions],
        "name": "Distributions",
        "marker": {
            "color": colours.get(
                "distributions",
                colours.get("tertiary", "#4CAF50"),
            )
        },
        "hovertemplate": ("<b>Distributions</b><br>%{x}<br>%{y:,.0f}<extra></extra>"),
    }
    nav_trace = {
        "type": "scatter",
        "mode": "lines",
        "x": x_values,
        "y": [float(v) for v in series.nav],
        "name": "NAV",
        "line": {
            "color": colours.get(
                "nav_line",
                colours.get("accent_line", colours["secondary"]),
            ),
            "width": 2,
        },
        "hovertemplate": ("<b>NAV</b><br>%{x}<br>%{y:,.0f}<extra></extra>"),
    }
    ncg_trace = {
        "type": "scatter",
        "mode": "lines",
        "x": x_values,
        "y": [float(v) for v in series.ncg],
        "name": "Net Capital Gain",
        "line": {
            "color": colours.get(
                "net_capital_gain_line",
                colours.get("ncg_line", colours.get("accent_line", "#FFA726")),
            ),
            "width": 2,
            "dash": "dot",
        },
        "hovertemplate": ("<b>NCG</b><br>%{x}<br>%{y:,.0f}<extra></extra>"),
    }

    layout: dict[str, Any] = {
        "title": {"text": title, "x": 0.5},
        "xaxis": {"type": "category", "title": {"text": ""}},
        "yaxis": {
            "tickformat": ",.0f",
            "title": {"text": currency},
            "zeroline": True,
            "zerolinewidth": 1,
            "zerolinecolor": colours["axis_line"],
        },
        "barmode": "relative",
        "showlegend": True,
        "hovermode": "x unified",
    }

    fig: dict[str, Any] = {
        "data": [calls_trace, distributions_trace, nav_trace, ncg_trace],
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)
