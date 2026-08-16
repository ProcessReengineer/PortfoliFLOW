# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — Portfolio Review tile 1 (Invested Capital & NAV).

Stacked-area trace for invested capital plus a NAV line overlay,
both indexed by year. Mirrors the QT
:class:`~services.reporting.chart_builders.StackedAreaWithLineBuilder`
output rendered for the Portfolio Review report's first tile.

Pure function — pandas / dataclass in, plain dict out — so it is
callable from any non-GUI consumer.
"""

from __future__ import annotations

from typing import Any

from services.analytics.portfolio_aggregation import InvestedCapitalNavSeries
from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme


def build_invested_capital_nav_spec(
    series: InvestedCapitalNavSeries,
    title: str = "Invested Capital & NAV",
    currency: str = "EUR",
) -> dict[str, Any]:
    """Build the Invested-Capital + NAV Plotly spec.

    Trace layout:

    - **Invested Capital** — area trace, theme accent (red).
    - **NAV** — line trace, theme orange / accent line.

    Args:
        series: :class:`InvestedCapitalNavSeries` produced by
            :func:`services.analytics.portfolio_aggregation
            .aggregate_invested_capital_and_nav`.
        title: Tile title.
        currency: The currency the series is denominated in — the
            tenant's functional currency on the portfolio surfaces, the
            investment's own currency on the single-investment surface
            (which does not convert). Rendered as the value-axis title.
            Defaults to ``"EUR"`` for the pre-ADR-0101 call shape.

    Returns:
        Plotly figure spec dict ``{"data", "layout", "config"}``.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    x_values = [str(y) for y in series.years]

    invested_trace = {
        "type": "scatter",
        "mode": "lines",
        "fill": "tozeroy",
        "x": x_values,
        "y": [float(v) for v in series.invested_capital],
        "name": "Invested Capital",
        "line": {"color": colours["primary"], "width": 2},
        "fillcolor": colours.get("primary_translucent", colours["primary"]),
        "hovertemplate": ("<b>Invested Capital</b><br>%{x}<br>%{y:,.0f}<extra></extra>"),
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

    layout: dict[str, Any] = {
        "title": {"text": title, "x": 0.5},
        "xaxis": {"type": "category", "title": {"text": ""}},
        "yaxis": {
            "tickformat": ",.0f",
            "title": {"text": currency},
            "rangemode": "tozero",
        },
        "showlegend": True,
        "hovermode": "x unified",
    }

    fig: dict[str, Any] = {
        "data": [invested_trace, nav_trace],
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)
