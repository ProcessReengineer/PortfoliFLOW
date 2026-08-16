# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — investment Cash Flows & NAV (dual-axis).

Migration of the QT ``_make_cash_flow_nav_chart`` widget to Plotly.
matplotlib's ``twinx`` is mapped onto Plotly's ``yaxis2`` with
``overlaying='y'`` and ``side='right'``: cashflows and the Net
Capital Gain line live on the left axis, NAV lives on the right.

The function is pure — pandas in, plain dict out — so it is callable
from any non-GUI consumer.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from services.chart_specs._theme import apply_theme
from services.chart_specs.base import apply_axis_end, get_chart_theme, plan_tail_window

# Trace-level opacity that mutes the plan tail against the NAV line it
# continues (ADR-0113 §2: "same hue as the actual trace but muted").
# Mirrors ``investment_nav_timeseries._PLAN_TAIL_OPACITY``; the canonical
# theme carries no muted variant of ``nav_line`` and ADR-0021 keeps
# ``config/chart_theme.json`` the single source of palette entries.
_PLAN_TAIL_OPACITY = 0.65


def build_cashflows_nav_spec(
    cashflows_actual: pd.DataFrame,
    nav_series: pd.Series,
    net_capital_gain: pd.Series,
    investment_name: str,
    *,
    axis_end: date | None = None,
    nav_plan: pd.Series | None = None,
    plan_tail_end: date | None = None,
) -> dict[str, Any]:
    """Build the dual-axis Cash Flows & NAV Plotly spec.

    Trace layout (left to right in the legend):

    - **Calls** — bar trace on ``yaxis``, capital-call magnitudes
      drawn negative (``amount < 0``) in the canonical red.
    - **Distributions** — bar trace on ``yaxis``, distribution
      amounts drawn positive (``amount > 0``) in the canonical
      green. Hidden when no distributions exist (empty trace) so
      the layout remains stable.
    - **Net Capital Gain** — line trace on ``yaxis``, orange.
    - **NAV** — line trace on ``yaxis2`` (right-hand side), blue.
    - **NAV (Plan)** — optional fifth trace on ``yaxis2``, the ADR-0113
      §2 plan tail: dashed, muted, present only when both ``nav_plan``
      and ``plan_tail_end`` are given.

    The QT widget renders calls as red bars and distributions as
    green bars, with NAV on the right axis and the Net Capital Gain
    line overlaid in orange — see
    ``gui/widgets/chart_widgets.py::_make_cash_flow_nav_chart``.

    Plan **cashflows** are deliberately not drawn: the bars and the Net
    Capital Gain line stay actual-only (ADR-0113 "Not in scope").

    Args:
        cashflows_actual: DataFrame with at least ``flow_timestamp``
            and ``amount`` columns. Caller has filtered to actuals.
        nav_series: Pandas Series indexed by ``as_of_date``.
        net_capital_gain: Pandas Series indexed by timestamp,
            typically produced by
            :func:`services.analytics.investment_returns.compute_net_capital_gain`.
        investment_name: Display name (used in the chart title).
        axis_end: Optional shared x-axis end (the ADR-0113 §1 universe
            as-of). Extends the auto-range on the right only; the start
            stays data-driven. ``None`` leaves the tile on its own
            auto-range.
        nav_plan: The investment's full ``'plan'`` NAV series, indexed by
            ``as_of_date``. ``None`` or empty suppresses the plan-tail
            trace entirely.
        plan_tail_end: Optional upper bound of the ADR-0113 §2 plan-tail
            **data** window; ``None`` suppresses the trace. The Charts
            route passes the same date as ``axis_end``, but the two stay
            distinct parameters because they are distinct concerns:
            ``axis_end`` ranges the axis, ``plan_tail_end`` bounds the
            data.

    Returns:
        Plotly figure spec dict ``{"data", "layout", "config"}``.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    if not cashflows_actual.empty:
        df_cf = cashflows_actual.copy()
        df_cf["flow_timestamp"] = pd.to_datetime(df_cf["flow_timestamp"], utc=True)
        df_cf["amount"] = df_cf["amount"].astype("float64")
        calls = df_cf[df_cf["amount"] < 0.0]
        distributions = df_cf[df_cf["amount"] > 0.0]
    else:
        calls = cashflows_actual
        distributions = cashflows_actual

    calls_trace = {
        "type": "bar",
        "x": (
            [pd.Timestamp(t).isoformat() for t in calls["flow_timestamp"]]
            if not calls.empty
            else []
        ),
        "y": ([float(a) for a in calls["amount"]] if not calls.empty else []),
        "name": "Calls",
        "marker": {"color": colours.get("calls", colours["primary"])},
        "yaxis": "y",
        "hovertemplate": ("<b>Call</b><br>%{x|%Y-%m-%d}<br>%{y:,.0f}<extra></extra>"),
    }
    distributions_trace = {
        "type": "bar",
        "x": (
            [pd.Timestamp(t).isoformat() for t in distributions["flow_timestamp"]]
            if not distributions.empty
            else []
        ),
        "y": ([float(a) for a in distributions["amount"]] if not distributions.empty else []),
        "name": "Distributions",
        "marker": {"color": colours.get("distributions", colours.get("tertiary", "#4CAF50"))},
        "yaxis": "y",
        "hovertemplate": ("<b>Distribution</b><br>%{x|%Y-%m-%d}<br>%{y:,.0f}<extra></extra>"),
    }

    ncg_clean = (
        net_capital_gain.dropna().sort_index() if not net_capital_gain.empty else net_capital_gain
    )
    ncg_trace = {
        "type": "scatter",
        "mode": "lines",
        "x": (
            [pd.Timestamp(idx).isoformat() for idx in ncg_clean.index]
            if not ncg_clean.empty
            else []
        ),
        "y": ([float(v) for v in ncg_clean.values] if not ncg_clean.empty else []),
        "name": "Net Capital Gain",
        "line": {
            "color": colours.get(
                "net_capital_gain_line",
                colours.get("ncg_line", colours.get("accent_line", "#FFA726")),
            ),
            "width": 2,
        },
        "yaxis": "y",
        "hovertemplate": ("<b>NCG</b><br>%{x|%Y-%m-%d}<br>%{y:,.0f}<extra></extra>"),
    }

    nav_clean = nav_series.dropna().sort_index() if not nav_series.empty else nav_series
    nav_trace = {
        "type": "scatter",
        "mode": "lines",
        "x": (
            [pd.Timestamp(idx).isoformat() for idx in nav_clean.index]
            if not nav_clean.empty
            else []
        ),
        "y": ([float(v) for v in nav_clean.values] if not nav_clean.empty else []),
        "name": "NAV",
        "line": {"color": colours.get("nav_line", colours["secondary"]), "width": 2},
        "yaxis": "y2",
        "hovertemplate": ("<b>NAV</b><br>%{x|%Y-%m-%d}<br>%{y:,.0f}<extra></extra>"),
    }

    plan_trace = _plan_tail_trace(nav_clean, nav_plan, plan_tail_end, colours)

    layout: dict[str, Any] = {
        "title": {
            "text": f"Cash Flows & NAV — {investment_name}",
            "x": 0.5,
        },
        "xaxis": {"type": "date", "tickformat": "%Y-%m-%d", "title": {"text": ""}},
        "yaxis": {
            "title": {"text": "Cashflows / NCG"},
            "tickformat": ",.0f",
            "side": "left",
            "zeroline": True,
            "zerolinewidth": 1,
            "zerolinecolor": colours["axis_line"],
        },
        "yaxis2": {
            "title": {"text": "NAV"},
            "tickformat": ",.0f",
            "overlaying": "y",
            "side": "right",
            "showgrid": False,
            "rangemode": "tozero",
        },
        "barmode": "relative",
        "showlegend": True,
        "legend": {
            "x": 0.0,
            "y": 1.0,
            "xanchor": "left",
            "yanchor": "top",
        },
        "hovermode": "x unified",
    }
    apply_axis_end(
        layout,
        axis_end,
        has_data=bool(
            calls_trace["x"]
            or distributions_trace["x"]
            or ncg_trace["x"]
            or nav_trace["x"]
            or (plan_trace is not None and plan_trace["x"])
        ),
    )

    data: list[dict[str, Any]] = [calls_trace, distributions_trace, ncg_trace, nav_trace]
    if plan_trace is not None:
        data.append(plan_trace)

    fig: dict[str, Any] = {
        "data": data,
        "layout": layout,
        "config": {
            "displayModeBar": True,
            "displaylogo": False,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
            "responsive": True,
        },
    }
    return apply_theme(fig)


def _plan_tail_trace(
    nav_clean: pd.Series,
    nav_plan: pd.Series | None,
    plan_tail_end: date | None,
    colours: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the ADR-0113 §2 plan-tail trace for the NAV line, or ``None``.

    The window and the anchor follow
    :func:`services.chart_specs.base.plan_tail_window`: plan rows strictly
    after the last NAV observation and no later than ``plan_tail_end``,
    prefixed with that last observation so the dashed line joins the
    solid one. Both x values are formatted from the *original* index
    entries, so the anchor coincides with the NAV trace's last point
    exactly rather than through a date round-trip.

    Args:
        nav_clean: The cleaned, sorted actual NAV series already drawn as
            the ``NAV`` trace.
        nav_plan: The full plan-NAV series; ``None`` or empty suppresses
            the trace.
        plan_tail_end: Upper bound of the tail window; ``None``
            suppresses the trace.
        colours: The active theme's colour mapping.

    Returns:
        The plan-tail scatter trace, or ``None`` when the caller asked
        for no tail. A caller-requested tail that filters to nothing
        still yields an (empty) trace, so the legend stays stable while
        the chart shows the honest gap.
    """
    if plan_tail_end is None or nav_plan is None or nav_plan.empty:
        return None

    plan_clean = nav_plan.dropna().sort_index()
    last_actual = pd.Timestamp(nav_clean.index[-1]).date() if not nav_clean.empty else None
    positions = plan_tail_window(
        [pd.Timestamp(idx).date() for idx in plan_clean.index],
        last_actual_date=last_actual,
        plan_tail_end=plan_tail_end,
    )
    x_values = [pd.Timestamp(plan_clean.index[p]).isoformat() for p in positions]
    y_values = [float(plan_clean.iloc[p]) for p in positions]
    if x_values and not nav_clean.empty:
        x_values.insert(0, pd.Timestamp(nav_clean.index[-1]).isoformat())
        y_values.insert(0, float(nav_clean.iloc[-1]))

    return {
        "type": "scatter",
        "mode": "lines",
        "x": x_values,
        "y": y_values,
        "name": "NAV (Plan)",
        "line": {
            "color": colours.get("nav_line", colours["secondary"]),
            "width": 2,
            "dash": "dash",
        },
        "opacity": _PLAN_TAIL_OPACITY,
        "yaxis": "y2",
        "hovertemplate": ("<b>NAV</b><br>%{x|%Y-%m-%d}<br>%{y:,.0f} (Plan)<extra></extra>"),
    }
