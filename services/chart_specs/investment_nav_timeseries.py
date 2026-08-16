# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec builder for investment NAV time-series charts.

Renders the per-investment NAV history with the Phase-4 plan/actual
parallelism explicit on screen: the actual NAV series is drawn as a
solid line, the plan NAV series as a dashed line. Both share the
canonical chart theme via :func:`services.chart_specs.base.get_chart_theme`.

Under ``plan_tail_end`` (ADR-0113 §2) the dashed trace narrows to the
**plan tail** — the continuation beyond the last actual, up to the
Charts section's unified axis end. The investment-detail surface leaves
the parameter unset and keeps the full plan horizon.

The function is pure — no FastAPI import, no DataStore, no matplotlib.
The Phase-3 regression guard
(``tests/regression/test_no_matplotlib_in_web.py``) keeps this module
matplotlib-free; consumers (``web/routes/investments.py``) serialise
the returned dict to JSON and the browser hands it to
``Plotly.newPlot``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from core.repositories.investment_nav_repository import InvestmentNavDTO
from core.repositories.investment_repository import InvestmentDTO
from services.chart_specs.base import (
    apply_axis_end,
    color_palette,
    get_chart_theme,
    layout_from_theme,
    plan_tail_window,
)

# Trace-level opacity that mutes the plan tail against the actual line
# it continues (ADR-0113 §2: "same hue as the actual trace but muted").
# The canonical theme carries no muted variant of the line colours, and
# ADR-0021 keeps ``config/chart_theme.json`` the single source of truth
# for palette entries — so the muting is expressed as opacity here
# rather than by inventing a second hue.
_PLAN_TAIL_OPACITY = 0.65


def build_nav_timeseries_spec(
    investment: InvestmentDTO,
    navs: list[InvestmentNavDTO],
    *,
    axis_end: date | None = None,
    plan_tail_end: date | None = None,
) -> dict[str, Any]:
    """Build a Plotly figure spec for an investment's NAV time series.

    Two traces are produced — one for ``actual`` NAVs (solid line)
    and one for ``plan`` NAVs (dashed line). Either trace may be
    empty (e.g. an investment with only actual NAVs and no plan, or
    a fresh investment with no NAVs at all). An empty trace is
    rendered as an empty line so the legend remains stable as new
    NAVs are added.

    ``plan_tail_end`` switches the plan trace from the full plan
    horizon (the Phase-4 plan/actual parallelism, used by the
    investment-detail surface) to the ADR-0113 §2 **plan tail**: only
    the stretch that continues the actual line towards the unified axis
    end, anchored at the last actual point and muted against it.

    Args:
        investment: The investment whose NAV history is rendered.
            Used for the chart title and the y-axis currency label.
        navs: NAV rows belonging to ``investment``, in any order.
            The function partitions them by ``nav_kind`` and sorts
            each partition by ``as_of_date`` ascending.
        axis_end: Optional shared x-axis end (the ADR-0113 §1 universe
            as-of). Extends the auto-range on the right only; the start
            stays data-driven. ``None`` leaves the tile on its own
            auto-range.
        plan_tail_end: Optional upper bound of the ADR-0113 §2 plan-tail
            **data** window. ``None`` (the default) draws the full plan
            series in the pre-ADR-0113 styling — the output is then
            unchanged, which is what keeps the investment-detail chart
            on its own long plan horizon. The Charts route passes the
            same date as ``axis_end``, but the two stay distinct
            parameters because they are distinct concerns: ``axis_end``
            ranges the axis, ``plan_tail_end`` bounds the data.

    Returns:
        A Plotly figure spec dict with keys ``data``, ``layout``,
        ``config``. Serialise to JSON and pass to
        ``Plotly.newPlot(target, fig.data, fig.layout, fig.config)``.
    """
    theme = get_chart_theme()
    palette = color_palette()

    actual_navs = sorted(
        (n for n in navs if n.nav_kind == "actual"),
        key=lambda n: n.as_of_date,
    )
    plan_navs = sorted(
        (n for n in navs if n.nav_kind == "plan"),
        key=lambda n: n.as_of_date,
    )

    actual_trace = {
        "type": "scatter",
        "x": [n.as_of_date.isoformat() for n in actual_navs],
        "y": [float(n.nav_value) for n in actual_navs],
        "mode": "lines",
        "line": {"color": palette["frontier"], "width": 2},
        "name": "Actual",
        "hovertemplate": ("<b>Actual</b><br>%{x|%Y-%m-%d}<br>NAV: %{y:,.2f}<extra></extra>"),
    }

    if plan_tail_end is None:
        plan_trace: dict[str, Any] = {
            "type": "scatter",
            "x": [n.as_of_date.isoformat() for n in plan_navs],
            "y": [float(n.nav_value) for n in plan_navs],
            "mode": "lines",
            "line": {
                "color": palette["min_var"],
                "width": 2,
                "dash": "dash",
            },
            "name": "Plan",
            "hovertemplate": ("<b>Plan</b><br>%{x|%Y-%m-%d}<br>NAV: %{y:,.2f}<extra></extra>"),
        }
    else:
        anchor = actual_navs[-1] if actual_navs else None
        tail = [
            plan_navs[position]
            for position in plan_tail_window(
                [n.as_of_date for n in plan_navs],
                last_actual_date=anchor.as_of_date if anchor is not None else None,
                plan_tail_end=plan_tail_end,
            )
        ]
        # The anchor joins the dashed tail to the solid line; with no
        # tail there is nothing to join, and the empty trace keeps the
        # legend stable while drawing the honest gap (ADR-0113 §2).
        points = [anchor, *tail] if (anchor is not None and tail) else tail
        plan_trace = {
            "type": "scatter",
            "x": [n.as_of_date.isoformat() for n in points],
            "y": [float(n.nav_value) for n in points],
            "mode": "lines",
            "line": {
                "color": palette["frontier"],
                "width": 2,
                "dash": "dash",
            },
            "opacity": _PLAN_TAIL_OPACITY,
            "name": "Plan",
            "hovertemplate": ("%{x|%Y-%m-%d}<br>NAV: %{y:,.2f} (Plan)<extra></extra>"),
        }

    layout = layout_from_theme(
        title=f"NAV Time Series — {investment.name}",
        xlabel="As-of date",
        ylabel=f"NAV ({investment.currency})",
        show_legend=True,
    )
    # Dates on the x-axis, raw NAV values on the y-axis: override the
    # percentage-formatter defaults set by ``layout_from_theme``.
    layout["xaxis"]["type"] = "date"
    layout["xaxis"]["tickformat"] = "%Y-%m-%d"
    layout["yaxis"]["tickformat"] = ",.0f"
    layout["yaxis"]["rangemode"] = "tozero"
    # The y-axis-line colour comes from the theme already; remove the
    # zerolinecolor seam so the bottom edge is uncluttered.
    layout["yaxis"]["zerolinecolor"] = theme["colours"]["axis_line"]
    apply_axis_end(layout, axis_end, has_data=bool(actual_trace["x"] or plan_trace["x"]))

    config = {
        "displayModeBar": True,
        "displaylogo": False,
        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
        "responsive": True,
    }

    return {
        "data": [actual_trace, plan_trace],
        "layout": layout,
        "config": config,
    }
