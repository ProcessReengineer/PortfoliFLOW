# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — portfolio composition by fund (NAV bars + IRR dots).

Sorted horizontal **absolute-NAV** bars (largest fund at the top) with a
**per-fund IRR-since-inception marker series** on a secondary top
x-axis (markers only — a fund whose IRR did not converge carries no dot)
and an optional **concentration statistics strip**
(``Top 3 · X%  |  Top 5 · Y%  |  Top 10 · Z%  |  HHI h``) rendered as a
``neutral``-coloured strip in the top margin, above the IRR axis. The
two x-axes carry
different dimensions — position size (money) vs. performance (IRR %); see
ADR-0072 §1.3. The residual ``"Other"`` bar is filled in the theme
``neutral`` colour; individual-fund bars stay ``primary``.

Money figures are in the tenant's functional currency (ADR-0099 §4); the
``currency`` argument selects the label prefix (ADR-0101 §3).

Pure function — dataclass in, plain dict out — and matplotlib-free, so
it stays importable from any non-GUI consumer (ADR-0042 §4). Grouping
into a top-N + "Other" set is the caller's responsibility via
``services.analytics.portfolio_aggregation.group_fund_composition``;
this spec renders whatever rows it is given. The concentration strip is
computed by the caller via ``compute_concentration`` on the **full,
ungrouped** breakdown and passed in.
"""

from __future__ import annotations

from typing import Any

from services.analytics.portfolio_aggregation import (
    ConcentrationStats,
    FundCompositionBreakdown,
)
from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme
from services.money_format import currency_prefix


def _format_irr(irr: float | None) -> str:
    """Render an IRR decimal as a compact percent string, or an em-dash."""
    if irr is None or irr != irr:  # None or NaN
        return "—"
    return f"{irr * 100.0:.1f}%"


def build_fund_composition_spec(
    breakdown: FundCompositionBreakdown,
    concentration: ConcentrationStats | None = None,
    title: str = "NAV by fund",
    currency: str = "EUR",
) -> dict[str, Any]:
    """Build the NAV-by-fund Plotly spec (money bars + IRR dots + strip).

    Args:
        breakdown: A (typically already top-N grouped)
            :class:`FundCompositionBreakdown` — rows sorted by NAV
            descending with running ``cumulative_pct`` and per-row
            ``irr``. The synthetic ``"Other"`` row (``investment_id is
            None``) is drawn in the theme ``neutral`` colour.
        concentration: Optional :class:`ConcentrationStats` computed on
            the **full, ungrouped** breakdown. When supplied and
            non-empty (``fund_count > 0``) a concentration strip is
            rendered beneath the title; when ``None`` the spec stays
            valid and strip-free, so non-route callers are unaffected.
        title: Tile title.
        currency: The currency the row NAVs are denominated in (the
            tenant's functional currency — the rows arrive converted at
            the ADR-0099 §4 boundary). Drives the bar-label prefix and
            the hover unit. Defaults to ``"EUR"``, under which every
            emitted string is byte-identical to the pre-ADR-0101 spec.

    Returns:
        Plotly figure spec dict ``{"data", "layout", "config"}``.
    """
    theme = get_chart_theme()
    colours = theme["colours"]
    font = theme["font"]
    family = font["family"]
    family_str = ", ".join(family) if isinstance(family, list) else str(family)

    prefix = currency_prefix(currency)

    rows = list(breakdown.rows)
    names = [r.name for r in rows]
    navs = [r.nav_eur for r in rows]
    shares = [r.weight_pct for r in rows]
    irr_texts = [_format_irr(r.irr) for r in rows]

    # Per-bar colour: the residual "Other" bar (investment_id is None) is
    # the theme neutral; genuine per-fund positions keep the accent.
    bar_colours = [
        colours["neutral"] if r.investment_id is None else colours["primary"] for r in rows
    ]
    # IRR markers, in percent. A fund whose IRR did not converge carries
    # no dot (a ``None`` x makes Plotly draw no marker for that point).
    irr_x: list[float | None] = [
        (r.irr * 100.0) if (r.irr is not None and r.irr == r.irr) else None for r in rows
    ]

    bar_trace = {
        "type": "bar",
        "orientation": "h",
        "x": navs,
        "y": names,
        "name": "NAV",
        "marker": {"color": bar_colours},
        "text": [f"{prefix}{v:,.0f}" for v in navs],
        "texttemplate": f"{prefix}%{{x:.3s}}",
        "textposition": "auto",
        "customdata": [[shares[i], irr_texts[i]] for i in range(len(rows))],
        "hovertemplate": (
            "<b>%{y}</b><br>"
            f"NAV %{{x:,.0f}} {currency}<br>"
            "Share %{customdata[0]:.1f}%<br>"
            "IRR %{customdata[1]}<extra></extra>"
        ),
    }
    irr_trace = {
        "type": "scatter",
        "mode": "markers",
        "x": irr_x,
        "y": names,
        "xaxis": "x2",
        "name": "IRR",
        "marker": {
            "size": 7,
            "color": colours.get("secondary", colours["primary"]),
            "line": {"width": 1, "color": colours["background"]},
        },
        "customdata": irr_texts,
        "hovertemplate": "<b>%{y}</b><br>IRR %{customdata}<extra></extra>",
    }

    # IRR-axis range: always include zero, with ~10 % headroom.
    irr_vals = [v for v in irr_x if v is not None]
    if not irr_vals:
        x2_range: list[float] = [0.0, 10.0]
    else:
        lo = min(0.0, min(irr_vals))
        hi = max(irr_vals)
        x2_range = [
            lo * 1.1 if lo < 0 else 0.0,
            hi * 1.1 if hi > 0 else 1.0,
        ]

    # Concentration strip — a single neutral-coloured line sitting snugly
    # at the top of the (trimmed) top margin, just above the IRR (x2) axis.
    # Computed on the full ungrouped distribution by the caller. The paper
    # y is re-tuned for the trimmed margin.t (no title band any more).
    annotations: list[dict[str, Any]] = []
    if concentration is not None and concentration.fund_count > 0:
        c = concentration
        annotations.append(
            {
                "xref": "paper",
                "yref": "paper",
                "x": 0.0,
                "y": 1.23,
                "xanchor": "left",
                "yanchor": "bottom",
                "showarrow": False,
                "text": (
                    f"Top 3 · {c.top3_pct:.0f}%   |   "
                    f"Top 5 · {c.top5_pct:.0f}%   |   "
                    f"Top 10 · {c.top10_pct:.0f}%   |   "
                    f"HHI {c.hhi:.2f}"
                ),
                "font": {
                    "family": family_str,
                    "size": font["tick_label_size"],
                    "color": colours["neutral"],
                },
            }
        )

    layout: dict[str, Any] = {
        "title": {
            "text": title,
            "x": 0.5,
            "xanchor": "center",
            "y": 0.97,
            "yref": "container",
            "yanchor": "top",
        },
        "yaxis": {
            "type": "category",
            "autorange": "reversed",  # largest fund on top
            "automargin": True,
            "title": {"text": ""},
        },
        "xaxis": {
            "tickformat": ".2s",
            "rangemode": "tozero",
            # No axis title — the bar labels already carry the money unit,
            # and dropping it frees the bottom row for the horizontal
            # legend.
            "title": {"text": ""},
        },
        "xaxis2": {
            "overlaying": "x",
            "side": "top",
            "range": x2_range,
            "ticksuffix": "%",
            "showgrid": False,
            "zeroline": False,
            "title": {
                "text": "IRR",
                "font": {
                    "family": family_str,
                    "size": font["axis_label_size"],
                    "color": colours["text"],
                },
            },
            "linecolor": colours["axis_line"],
            "tickfont": {
                "family": family_str,
                "size": font["tick_label_size"],
                "color": colours["text"],
            },
        },
        "annotations": annotations,
        "hovermode": "y unified",
        "showlegend": True,
        "legend": {"orientation": "h", "y": -0.12, "x": 0.0},
        # No centred title (the card header carries the tile name), so the
        # top margin only reserves room for the concentration strip and the
        # IRR (x2) top axis — trimmed from 110 to align the plot's top edge
        # with the left/middle tiles (their theme floor t=40, plus a small
        # delta for the strip + top-axis ticks/title). The bottom margin
        # keeps room for the SI ticks and the horizontal legend.
        "margin": {"l": 10, "r": 30, "t": 65, "b": 50},
    }

    fig: dict[str, Any] = {
        "data": [bar_trace, irr_trace],
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)
