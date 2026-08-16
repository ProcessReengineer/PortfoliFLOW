# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — Stage-c SAA-hypothetical three-line chart.

Three traces, all cumulative decimal returns:
  - Actual portfolio (solid, primary red).
  - SAA × Benchmark (dashed, secondary blue).
  - SAA × Composite (dotted, accent orange).

Two further traces drawn underneath provide a single positive-tinted
fill between the Actual and SAA × Benchmark lines so the gap between
the two reads as the allocation effect at a glance. The headline
finding — the three cumulative endpoints plus the allocation effect
in percentage points — is rendered as an in-chart annotation top-
right of the plot area (ADR-0062 §6).

The signed split (green where Actual > Benchmark, red where below)
is a Phase-1c follow-up; the current pragmatic version uses a single
positive tint regardless of sign and lets the headline annotation
carry the sign explicitly.

Pure dict-emitting helper per ADR-0045; no DB / FastAPI / Qt imports.
The optional ``effects`` argument is typed via a local ``Protocol``
so the module does not import from ``services.benchmark_comparison``
(dependency direction preserved).
"""

from __future__ import annotations

from typing import Any, Protocol

import pandas as pd

from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme


class _Effects(Protocol):
    """Structural type for the Stage-c effects DTO.

    Mirrors ``services.benchmark_comparison.SAAHypotheticalEffects``
    without importing it. Any object exposing these five attributes
    is acceptable as input to :func:`build_benchmark_saa_hypothetical_spec`.
    """

    actual_cumulative_endpoint: float | None
    saa_x_benchmark_cumulative_endpoint: float | None
    saa_x_composite_cumulative_endpoint: float | None
    allocation_effect_pp: float | None
    selection_effect_pp: float | None


def build_benchmark_saa_hypothetical_spec(
    actual: pd.Series,
    saa_x_benchmark: pd.Series,
    saa_x_composite: pd.Series,
    saa_label: str,
    *,
    effects: _Effects | None = None,
) -> dict[str, Any]:
    """Build the Stage-c SAA-hypothetical Plotly spec.

    The inputs are *monthly return series* (not cumulative). The spec
    compounds them into cumulative-return series for display so the
    chart is self-contained and the caller does not need to mirror
    the cumulative-return transform.

    Visual conventions:
      - Actual: red (``colours.primary``), solid, width 2.2.
      - SAA × Benchmark: blue (``colours.secondary``), dashed, width 1.8.
      - SAA × Composite: orange (``colours.accent_line``), dotted, width 1.8.
      - Excess shading between Actual and SAA × Benchmark: single
        positive tint (``colours.positive_bar`` at alpha 0.18) drawn
        below the visible lines via a baseline + ``fill="tonexty"``
        scatter pair. Omitted when either series is empty.
      - Legend repositioned to top-left inside the plot area on a
        semi-transparent panel so the top-right is free for the
        headline annotation.

    Args:
        actual: Actual portfolio monthly return series.
        saa_x_benchmark: SAA × benchmark monthly return series.
        saa_x_composite: SAA × composite monthly return series.
        saa_label: Operator-facing label for the SAA weight set
            ("Tangency — Standard 2026").
        effects: Optional cumulative-endpoint + allocation-effect
            summary. When provided and ``allocation_effect_pp`` is
            not ``None``, a top-right headline annotation is added
            naming the three numbers; the allocation-effect clause
            is colour-coded by sign. Defaults to ``None`` for
            backward compatibility with callers not yet passing it.

    Returns:
        Plotly figure spec dict (themed). Empty input → themed
        figure with empty-state annotation.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    if actual.empty and saa_x_benchmark.empty and saa_x_composite.empty:
        return _empty_spec(saa_label, colours)

    actual_cum = _to_cumulative(actual)
    saa_b_cum = _to_cumulative(saa_x_benchmark)
    saa_c_cum = _to_cumulative(saa_x_composite)

    traces: list[dict[str, Any]] = []

    # Shading: invisible baseline (SAA × Benchmark) + fill trace
    # (Actual with ``fill="tonexty"``). Drawn first so subsequent
    # visible line traces render on top.
    if not actual_cum.empty and not saa_b_cum.empty:
        bench_x, bench_y = _series_to_plotly_xy(saa_b_cum)
        actual_x, actual_y = _series_to_plotly_xy(actual_cum)
        traces.append(
            {
                "type": "scatter",
                "mode": "lines",
                "x": bench_x,
                "y": bench_y,
                "line": {"width": 0, "color": colours["secondary"]},
                "showlegend": False,
                "hoverinfo": "skip",
            }
        )
        traces.append(
            {
                "type": "scatter",
                "mode": "lines",
                "x": actual_x,
                "y": actual_y,
                "line": {"width": 0, "color": colours["primary"]},
                "fill": "tonexty",
                "fillcolor": _hex_to_rgba(colours["positive_bar"], 0.18),
                "showlegend": False,
                "hoverinfo": "skip",
            }
        )

    if not actual_cum.empty:
        x, y = _series_to_plotly_xy(actual_cum)
        traces.append(
            {
                "type": "scatter",
                "mode": "lines",
                "x": x,
                "y": y,
                "name": "Actual Portfolio",
                "line": {"color": colours["primary"], "width": 2.2},
                "hovertemplate": ("<b>Actual</b><br>%{x|%Y-%m-%d}<br>%{y:.2%}<extra></extra>"),
            }
        )
    if not saa_b_cum.empty:
        x, y = _series_to_plotly_xy(saa_b_cum)
        traces.append(
            {
                "type": "scatter",
                "mode": "lines",
                "x": x,
                "y": y,
                "name": "SAA × Benchmark",
                "line": {
                    "color": colours["secondary"],
                    "width": 1.8,
                    "dash": "dash",
                },
                "hovertemplate": (
                    "<b>SAA × Benchmark</b><br>%{x|%Y-%m-%d}<br>%{y:.2%}<extra></extra>"
                ),
            }
        )
    if not saa_c_cum.empty:
        x, y = _series_to_plotly_xy(saa_c_cum)
        traces.append(
            {
                "type": "scatter",
                "mode": "lines",
                "x": x,
                "y": y,
                "name": "SAA × Composite",
                "line": {
                    "color": colours["accent_line"],
                    "width": 1.8,
                    "dash": "dot",
                },
                "hovertemplate": (
                    "<b>SAA × Composite</b><br>%{x|%Y-%m-%d}<br>%{y:.2%}<extra></extra>"
                ),
            }
        )

    layout: dict[str, Any] = {
        "title": {
            "text": f"Hypothetical Portfolio Returns — {saa_label}",
            "x": 0.02,
            "xanchor": "left",
        },
        # Deterministic height: without it the chart relies on Plotly's
        # responsive autosize, which oversizes the canvas and pushes the
        # title/legend/headline/y-axis title to the bottom, overflowing
        # into the next section. Mirrors build_limits_coverage_spec's
        # layout.height convention. margin.t leaves room for the title
        # plus the y=1.06 headline annotation; yaxis.automargin keeps the
        # "Cumulative Return" title and tick labels inside the left margin.
        "height": 480,
        "margin": {"l": 60, "r": 30, "t": 56, "b": 48},
        "xaxis": {
            "type": "date",
            "tickformat": "%Y-%m-%d",
            "title": {"text": ""},
        },
        "yaxis": {
            "tickformat": ".0%",
            "title": {"text": "Cumulative Return"},
            "automargin": True,
        },
        "legend": {
            "orientation": "v",
            "x": 0.02,
            "y": 0.98,
            "xanchor": "left",
            "yanchor": "top",
            "bgcolor": "rgba(0, 0, 0, 0.4)",
        },
        "showlegend": True,
        "hovermode": "x unified",
    }

    annotations: list[dict[str, Any]] = []
    headline = _build_headline_annotation(effects, colours)
    if headline is not None:
        annotations.append(headline)
    if annotations:
        layout["annotations"] = annotations

    fig: dict[str, Any] = {
        "data": traces,
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)


def _empty_spec(saa_label: str, colours: dict[str, str]) -> dict[str, Any]:
    fig: dict[str, Any] = {
        "data": [],
        "layout": {
            "title": {
                "text": f"Hypothetical Portfolio Returns — {saa_label}",
                "x": 0.5,
            },
            "showlegend": False,
            "annotations": [
                {
                    "text": "No aligned data for the selected SAA",
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


def _build_headline_annotation(
    effects: _Effects | None,
    colours: dict[str, str],
) -> dict[str, Any] | None:
    """Build the top-right headline annotation, or ``None`` when n/a."""
    if effects is None or effects.allocation_effect_pp is None:
        return None

    actual_str = _format_cum_pct(effects.actual_cumulative_endpoint)
    bench_str = _format_cum_pct(effects.saa_x_benchmark_cumulative_endpoint)
    alloc_pp = effects.allocation_effect_pp
    alloc_str = _format_pp(alloc_pp)

    if alloc_pp > 0:
        accent_colour = colours["positive_bar"]
    elif alloc_pp < 0:
        accent_colour = colours["negative_bar"]
    else:
        accent_colour = colours["text"]

    text = (
        f'<span style="color:{colours["primary"]}"><b>Actual</b></span>: '
        f"{actual_str}   |   "
        f'<span style="color:{colours["secondary"]}"><b>SAA × Benchmark</b>'
        f"</span>: {bench_str}   |   "
        f'<span style="color:{accent_colour}"><b>Allocation effect: '
        f"{alloc_str}</b></span>"
    )

    return {
        "text": text,
        "xref": "paper",
        "yref": "paper",
        "x": 1.0,
        "y": 1.06,
        "xanchor": "right",
        "yanchor": "bottom",
        "showarrow": False,
        "font": {"size": 11, "color": colours["text"]},
        "align": "right",
    }


_MINUS_SIGN = "−"


def _format_pp(value: float) -> str:
    """Format a percentage-point value with sign and one decimal."""
    if value > 0:
        return f"+{value:.1f}pp"
    if value < 0:
        return f"{_MINUS_SIGN}{abs(value):.1f}pp"
    return "0.0pp"


def _format_cum_pct(value: float | None) -> str:
    """Format a cumulative decimal return as a signed percentage string."""
    if value is None:
        return "—"
    pct = value * 100.0
    if pct > 0:
        return f"+{pct:.1f}%"
    if pct < 0:
        return f"{_MINUS_SIGN}{abs(pct):.1f}%"
    return "0.0%"


def _to_cumulative(monthly: pd.Series) -> pd.Series:
    if monthly is None or monthly.empty:
        return pd.Series(dtype="float64")
    cleaned = monthly.dropna().sort_index()
    if cleaned.empty:
        return pd.Series(dtype="float64")
    return (1.0 + cleaned).cumprod() - 1.0


def _series_to_plotly_xy(series: pd.Series) -> tuple[list[str], list[float]]:
    if series is None or series.empty:
        return [], []
    cleaned = series.dropna().sort_index()
    if cleaned.empty:
        return [], []
    x_values = [pd.Timestamp(idx).isoformat() for idx in cleaned.index]
    y_values = [float(v) for v in cleaned.values]
    return x_values, y_values


def _hex_to_rgba(hex_colour: str, alpha: float) -> str:
    h = hex_colour.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"Expected six-digit hex colour, got {hex_colour!r}")
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


__all__ = ["build_benchmark_saa_hypothetical_spec"]
