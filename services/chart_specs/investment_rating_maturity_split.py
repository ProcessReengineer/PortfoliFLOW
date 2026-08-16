# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — fixed-income rating | maturity split.

Fixed-Income Tile 3 of the liquid-archetype triplet (ADR-0082).
Renders a bond investment's credit-rating distribution and its
maturity ladder as two side-by-side vertical-bar panels in a single
figure. Both axes use the canonical bucket orders from ADR-0079 §2 and
render **every** bucket — missing buckets as zero-height bars — so the
distribution and ladder shapes stay comparable across investments.

This codebase builds two-panel figures by hand via axis ``domain``
splits (see ``services/chart_specs/`` siblings). ``apply_theme`` only
themes the primary axes, so the right panel's ``xaxis2``/``yaxis2`` are
themed explicitly via :func:`themed_secondary_axis`.

The function is pure — mappings in, a plain Plotly dict out — so it is
callable from any non-GUI consumer (ADR-0013 / ADR-0045).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.chart_specs._theme import apply_theme, themed_secondary_axis
from services.chart_specs.base import get_chart_theme

# Canonical bucket orders (ADR-0079 §2) — fixed, never derived from the
# data, so every investment's chart shares the same category axis.
RATING_ORDER: list[str] = ["AAA", "AA", "A", "BBB", "BB", "B", "CCC_and_below", "NR"]
MATURITY_ORDER: list[str] = ["0-1y", "1-3y", "3-5y", "5-7y", "7-10y", "10y+"]

# Horizontal axis domains for the left (rating) and right (maturity)
# panels, with a gap between them for the right panel's tick labels.
_LEFT_DOMAIN = [0.0, 0.46]
_RIGHT_DOMAIN = [0.54, 1.0]


def _percents(weights: Mapping[str, float], order: list[str]) -> list[float]:
    """Project a weight mapping onto a fixed bucket order, scaled to percent.

    Buckets absent from ``weights`` map to ``0.0`` so the full
    category axis always renders.

    Args:
        weights: Mapping of bucket label to decimal weight.
        order: The canonical bucket order to project onto.

    Returns:
        Percent values aligned one-to-one with ``order``.
    """
    return [float(weights.get(bucket, 0.0)) * 100.0 for bucket in order]


def build_rating_maturity_split_spec(
    rating_weights: Mapping[str, float],
    maturity_weights: Mapping[str, float],
    investment_name: str,
) -> dict[str, Any]:
    """Build the two-panel rating | maturity split Plotly spec.

    Two vertical-bar panels share one figure via axis ``domain``
    splits: the credit-rating distribution on the left axes
    (``xaxis``/``yaxis``, primary colour) in fixed :data:`RATING_ORDER`,
    and the maturity ladder on the right axes (``xaxis2``/``yaxis2``,
    secondary colour) in fixed :data:`MATURITY_ORDER`. Every canonical
    bucket renders even at zero weight, so the shapes stay comparable
    across investments.

    Empty mappings are valid: each panel then renders a full bucket
    axis of zero-height bars rather than crashing.

    Args:
        rating_weights: Mapping of credit-rating bucket to decimal
            weight.
        maturity_weights: Mapping of maturity bucket to decimal weight.
        investment_name: Display name of the investment (used in the
            chart title).

    Returns:
        Plotly figure spec dict ``{"data": [...], "layout": {...},
        "config": {...}}``. Serialise to JSON for ``Plotly.newPlot``.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    rating_trace = {
        "type": "bar",
        "x": list(RATING_ORDER),
        "y": _percents(rating_weights, RATING_ORDER),
        "xaxis": "x",
        "yaxis": "y",
        "marker": {"color": colours["primary"]},
        "name": "Credit rating",
        "hovertemplate": "<b>%{x}</b><br>%{y:.1f}%<extra></extra>",
    }
    maturity_trace = {
        "type": "bar",
        "x": list(MATURITY_ORDER),
        "y": _percents(maturity_weights, MATURITY_ORDER),
        "xaxis": "x2",
        "yaxis": "y2",
        "marker": {"color": colours["secondary"]},
        "name": "Maturity",
        "hovertemplate": "<b>%{x}</b><br>%{y:.1f}%<extra></extra>",
    }

    layout: dict[str, Any] = {
        "title": {
            "text": f"Rating & Maturity — {investment_name}",
            "x": 0.5,
        },
        "xaxis": {
            "domain": _LEFT_DOMAIN,
            "type": "category",
            "categoryorder": "array",
            "categoryarray": list(RATING_ORDER),
            "title": {"text": ""},
        },
        "yaxis": {"ticksuffix": "%", "tickformat": ".1f"},
        "xaxis2": {
            "domain": _RIGHT_DOMAIN,
            "type": "category",
            "categoryorder": "array",
            "categoryarray": list(MATURITY_ORDER),
            "title": {"text": ""},
            **themed_secondary_axis(),
        },
        "yaxis2": {
            "anchor": "x2",
            "ticksuffix": "%",
            "tickformat": ".1f",
            **themed_secondary_axis(),
        },
        # Domain splits have no per-panel titles; label via annotations.
        "annotations": [
            {
                "text": "Credit rating",
                "xref": "paper",
                "yref": "paper",
                "x": sum(_LEFT_DOMAIN) / 2.0,
                "y": 1.04,
                "xanchor": "center",
                "yanchor": "bottom",
                "showarrow": False,
            },
            {
                "text": "Maturity",
                "xref": "paper",
                "yref": "paper",
                "x": sum(_RIGHT_DOMAIN) / 2.0,
                "y": 1.04,
                "xanchor": "center",
                "yanchor": "bottom",
                "showarrow": False,
            },
        ],
        "showlegend": False,
        "hovermode": "closest",
        "bargap": 0.2,
    }

    fig: dict[str, Any] = {
        "data": [rating_trace, maturity_trace],
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)
