# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec for the investment-universe Portfolio Analysis chart.

The PyQt6 reference is
``gui/widgets/portfolio_analysis_widget.py::_render_chart``. This
module mirrors its visual choices — the same colours, the same
marker shapes, the same axis formatters — so that the side-by-side
acceptance comparison in sub-stream 5d shows visual identity, not
divergence (per ADR-0042 §4 visual-acceptance discipline).

The chart has up to seven trace groups:

1. Efficient frontier line (red).
2. Capital Market Line (blue, dashed).
3. Risk-free rate horizontal reference line (dotted).
4. Tangency portfolio (red star marker).
5. Minimum-variance portfolio (green diamond marker).
6. Current portfolio (orange circle, only when a current allocation
   is available).
7. Individual investments as cross-marker scatter with name labels.
"""

from __future__ import annotations

from typing import Any

from services.analytics import (
    CapitalMarketLine,
    EfficientFrontierResult,
    MinVariancePortfolio,
    TangencyPortfolio,
)
from services.chart_specs.base import (
    color_palette,
    get_chart_theme,
    layout_from_theme,
)

_WEIGHT_DISPLAY_THRESHOLD = 0.001


def _format_weights_html(asset_names: list[str], weights: list[float]) -> str:
    """Format a per-asset weight breakdown as an HTML hover fragment."""
    parts: list[str] = []
    for name, weight in zip(asset_names, weights):
        if weight > _WEIGHT_DISPLAY_THRESHOLD:
            parts.append(f"{name}: {weight * 100:.1f}%")
    return "<br>".join(parts) if parts else "—"


def build_frontier_spec(
    *,
    frontier: EfficientFrontierResult,
    tangency: TangencyPortfolio,
    min_variance: MinVariancePortfolio,
    capital_market_line: CapitalMarketLine,
    current_portfolio: tuple[float, float] | None,
    investment_points: dict[str, tuple[float, float]],
    risk_free_rate: float,
    title: str = "Portfolio Optimisation — Efficient Frontier",
) -> dict[str, Any]:
    """Build a Plotly figure spec for the Portfolio Analysis chart.

    Args:
        frontier: Discrete efficient frontier produced by
            :func:`services.analytics.compute_efficient_frontier`.
        tangency: Tangency portfolio (max-Sharpe) on the frontier.
        min_variance: Global minimum-variance portfolio.
        capital_market_line: CML geometry — sampled past the
            tangency so the line extends to the right of every
            individual investment marker.
        current_portfolio: Current NAV-weighted ``(volatility,
            expected_return)``. Pass ``None`` (or ``(nan, nan)``)
            when no current allocation is computable for the
            tenant — the trace is then omitted.
        investment_points: Per-investment ``(volatility,
            expected_return)`` keyed by display name. Drawn as
            ``×`` markers with text labels above each point.
        risk_free_rate: Annualised risk-free rate (decimal). Used
            for the dotted reference line and the legend label.
        title: Figure title. Defaults to the QT-side wording for
            visual identity; web routes can override (e.g. to
            include the as-of date).

    Returns:
        A Plotly figure spec dict with keys ``data``, ``layout``,
        ``config``. Serialise to JSON and pass to
        ``Plotly.newPlot(target, fig.data, fig.layout, fig.config)``.
    """
    theme = get_chart_theme()
    palette = color_palette()
    colours = theme["colours"]
    optimization = theme["optimization"]

    data: list[dict[str, Any]] = []

    # 1 — Efficient frontier line.
    if frontier.frontier_returns.size:
        data.append(
            {
                "type": "scatter",
                "x": frontier.frontier_volatilities.tolist(),
                "y": frontier.frontier_returns.tolist(),
                "mode": "lines",
                "line": {
                    "color": palette["frontier"],
                    "width": optimization["frontier_linewidth"],
                },
                "name": "Efficient Frontier",
                "hovertemplate": ("Vol: %{x:.2%}<br>Return: %{y:.2%}<extra></extra>"),
            }
        )

    # 2 — Capital Market Line.
    if capital_market_line.points:
        data.append(
            {
                "type": "scatter",
                "x": [pt[0] for pt in capital_market_line.points],
                "y": [pt[1] for pt in capital_market_line.points],
                "mode": "lines",
                "line": {
                    "color": palette["cml"],
                    "width": optimization["cml_linewidth"],
                    "dash": "dash",
                },
                "name": "Capital Market Line",
                "hoverinfo": "skip",
            }
        )

    # 3 — Risk-free rate horizontal reference line. Drawn as a
    # trace rather than a layout shape so it appears in the legend
    # like the PyQt6 version does.
    x_max_candidates: list[float] = []
    if frontier.frontier_volatilities.size:
        x_max_candidates.append(float(frontier.frontier_volatilities.max()))
    if capital_market_line.points:
        x_max_candidates.append(max(pt[0] for pt in capital_market_line.points))
    if investment_points:
        x_max_candidates.append(max(vol for vol, _ in investment_points.values()))
    x_max = max(x_max_candidates) * 1.05 if x_max_candidates else 0.5
    data.append(
        {
            "type": "scatter",
            "x": [0.0, x_max],
            "y": [risk_free_rate, risk_free_rate],
            "mode": "lines",
            "line": {
                "color": palette["rf_line"],
                "width": 1.0,
                "dash": "dot",
            },
            "name": f"Risk-Free Rate ({risk_free_rate * 100:.1f}%)",
            "opacity": optimization["rf_line_alpha"],
            "hoverinfo": "skip",
        }
    )

    # 4 — Tangency portfolio marker.
    tangency_weights_text = _format_weights_html(tangency.asset_names, tangency.weights.tolist())
    data.append(
        {
            "type": "scatter",
            "x": [tangency.volatility],
            "y": [tangency.expected_return],
            "mode": "markers",
            "marker": {
                "size": 18,
                "color": palette["tangency"],
                "symbol": "star",
                "line": {"color": colours["background"], "width": 1},
            },
            "name": "Tangency Portfolio",
            "hovertemplate": (
                "<b>Tangency Portfolio</b><br>"
                "Vol: %{x:.2%}<br>"
                "Return: %{y:.2%}<br>"
                f"Sharpe: {tangency.sharpe_ratio:.2f}<br><br>"
                f"<b>Weights</b><br>{tangency_weights_text}<extra></extra>"
            ),
        }
    )

    # 5 — Minimum-variance portfolio marker.
    min_var_weights_text = _format_weights_html(
        min_variance.asset_names, min_variance.weights.tolist()
    )
    data.append(
        {
            "type": "scatter",
            "x": [min_variance.volatility],
            "y": [min_variance.expected_return],
            "mode": "markers",
            "marker": {
                "size": 12,
                "color": palette["min_var"],
                "symbol": "diamond",
                "line": {"color": colours["background"], "width": 1},
            },
            "name": "Min Variance",
            "hovertemplate": (
                "<b>Min-Variance Portfolio</b><br>"
                "Vol: %{x:.2%}<br>"
                "Return: %{y:.2%}<br><br>"
                f"<b>Weights</b><br>{min_var_weights_text}<extra></extra>"
            ),
        }
    )

    # 6 — Current portfolio marker (NAV-weighted; only when finite).
    if current_portfolio is not None:
        cp_vol, cp_ret = current_portfolio
        if (
            isinstance(cp_vol, float)
            and isinstance(cp_ret, float)
            and cp_vol == cp_vol  # not NaN
            and cp_ret == cp_ret
        ):
            data.append(
                {
                    "type": "scatter",
                    "x": [cp_vol],
                    "y": [cp_ret],
                    "mode": "markers",
                    "marker": {
                        "size": 14,
                        "color": optimization["current_portfolio_colour"],
                        "symbol": "circle",
                        "line": {
                            "color": colours["text"],
                            "width": 1.5,
                        },
                    },
                    "name": "Current Portfolio",
                    "hovertemplate": (
                        "<b>Current Portfolio</b><br>"
                        "Vol: %{x:.2%}<br>"
                        "Return: %{y:.2%}<extra></extra>"
                    ),
                }
            )

    # 7 — Individual investments as cross markers with text labels.
    if investment_points:
        names_sorted = sorted(investment_points.keys())
        xs = [investment_points[n][0] for n in names_sorted]
        ys = [investment_points[n][1] for n in names_sorted]
        data.append(
            {
                "type": "scatter",
                "x": xs,
                "y": ys,
                "mode": "markers+text",
                "marker": {
                    "size": 10,
                    "symbol": "x",
                    "color": colours["text"],
                    "line": {"width": 1.5},
                },
                "text": names_sorted,
                "textposition": "top center",
                "textfont": {
                    "color": colours["text"],
                    "size": theme["font"]["tick_label_size"],
                },
                "name": "Investments",
                "hovertemplate": (
                    "<b>%{text}</b><br>Vol: %{x:.2%}<br>Return: %{y:.2%}<extra></extra>"
                ),
            }
        )

    layout = layout_from_theme(
        title=title,
        xlabel="Volatility (annualised)",
        ylabel="Expected Return (annualised)",
        show_legend=True,
    )
    layout["yaxis"]["rangemode"] = "tozero"
    layout["xaxis"]["rangemode"] = "tozero"

    config = {
        "displayModeBar": True,
        "displaylogo": False,
        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
        "responsive": True,
    }

    return {"data": data, "layout": layout, "config": config}
