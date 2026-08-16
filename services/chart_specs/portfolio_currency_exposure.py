# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — currency exposure by position currency (donut).

The fourth Overview chart tile (ADR-0101 §1, joining the ADR-0072 row as
``ov-chart-currency``). A donut over the portfolio's converted NAV, sliced
by the **denomination** of each position.

The subtitle is load-bearing, not decoration. What the donut shows is
*unhedged notional NAV share by denomination* — it does **not** look through
a fund to the currencies of that fund's underlying assets, and it does not
net out hedges (hedging is deferred, ADR-0099 §6). A reader who assumes
otherwise would draw the wrong conclusion from an otherwise correct chart,
so "by position currency (unhedged)" is rendered with the figure
(ADR-0101 §Consequences).

Pure function — dataclass in, plain dict out — and matplotlib-free, so it
stays importable from any non-GUI consumer (ADR-0042 §4). Colours come from
the theme's ``series_palette`` (crimson primary first), never a hardcoded
hex (ADR-0021).
"""

from __future__ import annotations

from typing import Any

from services.analytics.portfolio_aggregation import CurrencyExposure
from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme
from services.money_format import currency_prefix

#: Rendered under the title (or in its place, when the card header already
#: names the tile). The wording is fixed by ADR-0101 §Consequences.
EXPOSURE_SUBTITLE = "by position currency (unhedged)"

#: Donut, not pie: the hole keeps the slice-angle comparison honest and
#: leaves room for the centre label.
_HOLE = 0.55


def build_currency_exposure_spec(
    exposure: CurrencyExposure,
    functional_currency: str = "EUR",
    title: str = "Currency exposure",
) -> dict[str, Any]:
    """Build the currency-exposure donut Plotly spec.

    Args:
        exposure: A :class:`CurrencyExposure` from
            :func:`services.analytics.portfolio_aggregation.aggregate_currency_exposure`
            — rows sorted by amount descending, ``weight_pct`` summing to
            100. An empty exposure yields a valid, slice-free figure.
        functional_currency: The currency the row ``amount`` values are
            expressed in (the tenant's functional currency). Drives the
            hover label's money prefix only — the slice *labels* are the
            position currencies, which is the whole point of the chart.
        title: Tile title. Pass ``""`` when the card header already names
            the tile; the subtitle then moves up into the title's place.

    Returns:
        Plotly figure spec dict ``{"data", "layout", "config"}``.
    """
    theme = get_chart_theme()
    colours = theme["colours"]
    font = theme["font"]
    family = font["family"]
    family_str = ", ".join(family) if isinstance(family, list) else str(family)

    palette: list[str] = list(colours["series_palette"])
    rows = list(exposure.rows)
    labels = [r.currency for r in rows]
    values = [r.amount for r in rows]
    # Cycle the palette so an implausibly wide currency set still colours in
    # (the theme ships 12 entries; a book in 13 currencies wraps rather than
    # falling off the end).
    slice_colours = [palette[i % len(palette)] for i in range(len(rows))]

    prefix = currency_prefix(functional_currency)

    donut_trace = {
        "type": "pie",
        "labels": labels,
        "values": values,
        "hole": _HOLE,
        "sort": False,  # rows already arrive sorted by amount descending
        "direction": "clockwise",
        "marker": {
            "colors": slice_colours,
            "line": {"color": colours["background"], "width": 1},
        },
        "textinfo": "label+percent",
        "textposition": "auto",
        "hovertemplate": (
            f"<b>%{{label}}</b><br>{prefix}%{{value:,.0f}}<br>%{{percent}}<extra></extra>"
        ),
    }

    # The subtitle sits directly under the title when there is one, and in
    # the title's place when the card header carries the name (title="").
    subtitle_y = 0.93 if title else 0.98
    annotations: list[dict[str, Any]] = [
        {
            "xref": "paper",
            "yref": "container",
            "x": 0.5,
            "y": subtitle_y,
            "xanchor": "center",
            "yanchor": "top",
            "showarrow": False,
            "text": EXPOSURE_SUBTITLE,
            "font": {
                "family": family_str,
                "size": font["tick_label_size"],
                "color": colours["neutral"],
            },
        }
    ]

    layout: dict[str, Any] = {
        "title": {
            "text": title,
            "x": 0.5,
            "xanchor": "center",
            "y": 0.97,
            "yref": "container",
            "yanchor": "top",
        },
        "annotations": annotations,
        "showlegend": True,
        "legend": {"orientation": "h", "y": -0.08, "x": 0.0},
        # Pinned: apply_theme would otherwise default this to "x unified",
        # which has no meaning on a pie and suppresses the slice hover.
        "hovermode": "closest",
        "margin": {"l": 20, "r": 20, "t": 50, "b": 30},
    }

    fig: dict[str, Any] = {
        "data": [donut_trace],
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)
