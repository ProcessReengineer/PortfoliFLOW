# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — the Scenario Analysis impact chart pair (ADR-0104 §5).

Projects the deltas-first :class:`~services.planning_desk.scenario_results.ScenarioResult`
into the two side-by-side panels of the Scenario Analysis lens: **Baseline** (the
unmodified plan world, D19) on the left, **Scenario** (the active parameter set
applied) on the right. Each panel is a dual-axis figure — the Σ-NAV path (full
universe incl. cash, :attr:`ScenarioResult.nav_path`) on the left axis, the
ADR-0066 cumulative return index (performance universe,
:attr:`ScenarioResult.return_index`) on the right. The two DTOs are already the
right universes (the E5 decision, S34.3); this spec re-filters nothing.

**One scale for two worlds (binding).** Both panels share identical axis scales:
the left (NAV) range and the right (return) range are each the joint extrema of
*both* worlds, computed once and applied to both panels. Independent auto-scaling
would visually understate the scenario's impact — the honest-comparison rule
(ADR-0104 §5, closure decision 3.11), the same rule
:mod:`services.chart_specs.cash_flow_timeline` draws over its own pair.

**The ghost baseline.** The scenario panel repeats the baseline series as grey
ghosts (:data:`_GHOST_KEY`) so the gap reads within one panel — it matters when
the panels stack on a narrow viewport. The baseline panel shows the baseline
alone.

**The identical-history invariant.** Left of the seam the two worlds are equal by
construction — the assembly asserts it (ADR-0104 §5) — so the scenario NAV line
and its baseline ghost are coincident there. The chart renders the equality; it
does not manufacture it.

**Why Σ NAV can fall with nothing rising to meet it (E4).** A reader watching the
left-axis Σ-NAV path drop between baseline and scenario will look for the position
that rose against a cash movement and must not: E4 (ADR-0105 §15) forbids the
offsetting NAV path, so a TA-generated call and a deferred (re-paced) call move
cash **down** with no plan NAV position moving **up**. The fall is the chosen v1
posture — :func:`~services.overlay.executors.execute_repace_flows` moves flows and
:func:`~services.investments.plan_world._with_ta_profiles` generates them, each
asserting no NAV consequence, with :func:`~services.overlay.executors.execute_market_shock`
the deliberate counter-case where NAV *does* move. This spec renders that fall; it
does not certify it as balanced.

**The seam.** The amber dashed rule at t₀ is the **same** :data:`SEAM_COLOUR`
:mod:`services.chart_specs.cash_flow_timeline` draws — imported from that module,
one formulation (closure §8.5 / decision 4.13), never a second ``"#FFC107"``
literal.

Pure: no DB, no repository, no clock, no Plotly import. The result DTOs are
imported under :data:`typing.TYPE_CHECKING` only — their defining module is
DB-coupled (:mod:`services.planning_desk.scenario_results` imports repository
DTOs), and this package keeps its DB-free import graph (ADR-0045 §1), exactly as
:mod:`services.chart_specs.cash_flow_timeline` does.
"""

from __future__ import annotations

from datetime import date as _date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from services.chart_specs._theme import apply_theme, themed_secondary_axis
from services.chart_specs.base import get_chart_theme

# The seam colour — the single formulation, imported rather than restated
# (closure §8.5 / decision 4.13). ``config/chart_theme.json`` carries a palette
# and a canvas, not the shell's semantic-status colours, so promoting the amber
# to the theme contract is a wider decision than this surface warrants; the two
# specs share the one constant instead. Re-exported so a reader of the pair sees
# the same seam both specs draw.
from services.chart_specs.cash_flow_timeline import SEAM_COLOUR

if TYPE_CHECKING:  # pragma: no cover - types only; see the module docstring
    from services.planning_desk.scenario_results import (
        ScenarioResult,
        ScenarioSeriesPair,
    )

#: The grey of the baseline ghost line. ``colours.neutral`` of the chart theme —
#: deliberately *not* a palette entry, so the ghost cannot be confused with a
#: live series. The same choice :mod:`cash_flow_timeline` makes.
_GHOST_KEY: str = "neutral"

#: The Σ-NAV axis is stated in millions of the functional currency (the mockup's
#: "€m"). A display scale only — it never touches a delta, which is formed on the
#: raw DTO values by the assembly (ADR-0104 §5) and by the panel footer.
_MILLIONS: float = 1_000_000.0

#: Fraction of the value span added above and below a shared range, so a line
#: never runs along the plot edge. Mirrors :mod:`cash_flow_timeline`.
_Y_PADDING: float = 0.08

#: Fallback padding for a flat series (span of zero), as a fraction of the level.
_FLAT_PADDING: float = 0.05

#: Panel height, matching the cash-flow timeline so the surfaces read as one.
_HEIGHT: int = 300


def build_scenario_impact_pair(
    result: ScenarioResult,
    *,
    functional_currency: str = "EUR",
    labels: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the Baseline and Scenario impact panels (ADR-0104 §5).

    Args:
        result: The deltas-first scenario result — both worlds over one grid.
        functional_currency: The currency the Σ-NAV axis is stated in (its
            millions). Titles the left axis; never scales a return index.
        labels: The per-period column labels, positionally aligned with
            ``result.nav_path.period_ends``. When ``None`` the labels are
            derived from the period ends (quarter or month form). Passing the
            cash-flow lens's own labels keeps the two charts' x-axes identical.

    Returns:
        ``(baseline_figure, scenario_figure)`` — two themed Plotly figure dicts.
        The baseline panel draws the baseline world alone; the scenario panel
        draws the scenario world plus the baseline ghost. Both share identical
        axis scales. A result whose grid is empty yields two themed empty-state
        figures.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    nav_path = result.nav_path
    return_index = result.return_index
    period_ends = nav_path.period_ends

    if not period_ends:
        empty = _empty_spec(colours)
        return empty, dict(empty)

    axis_labels = (
        labels if labels is not None else [_period_label(period_end) for period_end in period_ends]
    )
    seam_index = nav_path.seam_index

    # The two shared ranges — each the joint extrema of *both* worlds, computed
    # once and applied to both panels (the binding honest-comparison rule).
    nav_range = _shared_range(_series_floats(nav_path, scale=_MILLIONS))
    return_range = _shared_range(_series_floats(return_index, scale=1.0))

    baseline = _panel(
        colours=colours,
        labels=axis_labels,
        seam_index=seam_index,
        nav_values=nav_path.baseline,
        return_values=return_index.baseline,
        nav_colour=colours["secondary"],
        return_colour=colours["tertiary"],
        ghost_nav=None,
        ghost_return=None,
        nav_range=nav_range,
        return_range=return_range,
        functional_currency=functional_currency,
    )
    scenario = _panel(
        colours=colours,
        labels=axis_labels,
        seam_index=seam_index,
        nav_values=nav_path.scenario,
        return_values=return_index.scenario,
        nav_colour=colours["primary"],
        return_colour=colours["quaternary"],
        ghost_nav=nav_path.baseline,
        ghost_return=return_index.baseline,
        nav_range=nav_range,
        return_range=return_range,
        functional_currency=functional_currency,
    )
    return baseline, scenario


# ---------------------------------------------------------------------------
# One panel
# ---------------------------------------------------------------------------


def _panel(
    *,
    colours: dict[str, Any],
    labels: list[str],
    seam_index: int,
    nav_values: tuple[Decimal | float | None, ...],
    return_values: tuple[Decimal | float | None, ...],
    nav_colour: str,
    return_colour: str,
    ghost_nav: tuple[Decimal | float | None, ...] | None,
    ghost_return: tuple[Decimal | float | None, ...] | None,
    nav_range: list[float] | None,
    return_range: list[float] | None,
    functional_currency: str,
) -> dict[str, Any]:
    """Build one dual-axis panel — Σ-NAV left, return index right.

    The scenario panel passes the baseline series as ``ghost_nav`` /
    ``ghost_return``; the baseline panel passes ``None`` for both.
    """
    data: list[dict[str, Any]] = []

    # The ghost first, so the live series draw over it (the gap reads as the
    # live line departing from a reference, not the reverse).
    if ghost_nav is not None:
        data.append(
            _line(
                name="NAV (baseline)",
                labels=labels,
                values=ghost_nav,
                colour=colours[_GHOST_KEY],
                width=1.6,
                axis="y",
                unit=f"{functional_currency}m",
                scale=_MILLIONS,
                dash="dash",
            )
        )
    if ghost_return is not None:
        data.append(
            _line(
                name="Total return (baseline)",
                labels=labels,
                values=ghost_return,
                colour=colours[_GHOST_KEY],
                width=1.3,
                axis="y2",
                unit="idx",
                scale=1.0,
                dash="dash",
            )
        )

    data.append(
        _line(
            name=f"Portfolio NAV (Σ, {functional_currency}m)",
            labels=labels,
            values=nav_values,
            colour=nav_colour,
            width=2.6,
            axis="y",
            unit=f"{functional_currency}m",
            scale=_MILLIONS,
        )
    )
    data.append(
        _line(
            name="Total return (idx)",
            labels=labels,
            values=return_values,
            colour=return_colour,
            width=2.0,
            axis="y2",
            unit="idx",
            scale=1.0,
        )
    )

    yaxis: dict[str, Any] = {
        "tickformat": ",.0f",
        "title": {"text": f"Σ NAV ({functional_currency}m)"},
    }
    if nav_range is not None:
        yaxis["range"] = nav_range

    yaxis2: dict[str, Any] = {
        "domain": [0.0, 1.0],
        "overlaying": "y",
        "side": "right",
        "title": {"text": "Return (idx)"},
        "tickformat": ",.0f",
        **themed_secondary_axis(),
    }
    if return_range is not None:
        yaxis2["range"] = return_range

    layout: dict[str, Any] = {
        "margin": {"l": 58, "r": 52, "t": 30, "b": 40},
        "showlegend": True,
        "legend": {"orientation": "h", "y": 1.12, "x": 0},
        "xaxis": {
            "type": "category",
            "categoryorder": "array",
            "categoryarray": labels,
        },
        "yaxis": yaxis,
        "yaxis2": yaxis2,
        "shapes": [_seam_shape(seam_index)],
        "annotations": [_seam_annotation(seam_index)],
        "height": _HEIGHT,
    }

    return apply_theme(
        {
            "data": data,
            "layout": layout,
            "config": {
                "displayModeBar": False,
                "displaylogo": False,
                "responsive": True,
            },
        }
    )


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------


def _line(
    *,
    name: str,
    labels: list[str],
    values: tuple[Decimal | float | None, ...],
    colour: str,
    width: float,
    axis: str,
    unit: str,
    scale: float,
    dash: str | None = None,
) -> dict[str, Any]:
    """Build one scatter trace over the period grid.

    ``None`` values survive as ``None`` — a gap in the line, never a fabricated
    zero (the empty-is-not-zero rule the DTOs carry). ``scale`` divides the
    plotted value (the Σ-NAV axis is stated in millions); the hover unit names
    the scale so a reader is never left guessing.
    """
    line: dict[str, Any] = {"color": colour, "width": width}
    if dash is not None:
        line["dash"] = dash
    return {
        "type": "scatter",
        "mode": "lines",
        "name": name,
        "x": labels,
        "y": [None if value is None else float(value) / scale for value in values],
        "yaxis": axis,
        "line": line,
        "connectgaps": False,
        "hovertemplate": f"{name}: %{{y:,.1f}} {unit}<extra></extra>",
    }


def _series_floats(pair: ScenarioSeriesPair, *, scale: float) -> list[float]:
    """Collect every non-``None`` value of **both** legs, as scaled floats.

    The union the shared range is taken over: the baseline's and the scenario's
    values together, so the range brackets both worlds and does not move between
    panels.
    """
    values: list[float] = []
    for leg in (pair.baseline, pair.scenario):
        values.extend(float(value) / scale for value in leg if value is not None)
    return values


def _shared_range(values: list[float]) -> list[float] | None:
    """Return ``[low, high]`` with padding over ``values``, or ``None``.

    ``None`` when no value exists — a grid of empty cells, which Plotly may
    auto-scale as it likes. Mirrors :func:`cash_flow_timeline._shared_y_range`.
    """
    if not values:
        return None
    low, high = min(values), max(values)
    span = high - low
    padding = span * _Y_PADDING if span > 0 else max(abs(high) * _FLAT_PADDING, 1.0)
    return [low - padding, high + padding]


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


def _seam_x(seam_index: int) -> float:
    """Return the seam's x position on the category axis.

    Category coordinates run ``0, 1, 2, …``; the rule between the last actual
    column and the first plan one sits half a step left of ``seam_index`` — the
    same split :mod:`cash_flow_timeline` draws. ``seam_index == 0`` (a plan-only
    grid) puts the rule at the left edge, which is the truth.
    """
    return seam_index - 0.5


def _seam_shape(seam_index: int) -> dict[str, Any]:
    """Build the amber dashed rule at the actual/plan seam."""
    x = _seam_x(seam_index)
    return {
        "type": "line",
        "xref": "x",
        "yref": "paper",
        "x0": x,
        "x1": x,
        "y0": 0.0,
        "y1": 1.0,
        "line": {"color": SEAM_COLOUR, "width": 2, "dash": "dash"},
        "layer": "below",
    }


def _seam_annotation(seam_index: int) -> dict[str, Any]:
    """Label the seam with t₀ — the plan/actual cut-over (ADR-0060)."""
    return {
        "text": "t₀",
        "x": _seam_x(seam_index),
        "xref": "x",
        "y": 1.0,
        "yref": "paper",
        "xanchor": "left",
        "yanchor": "top",
        "showarrow": False,
        "font": {"size": 11, "color": SEAM_COLOUR},
    }


# ---------------------------------------------------------------------------
# Labels & empty state
# ---------------------------------------------------------------------------


def _period_label(end: _date) -> str:
    """Label a period end — a quarter form on a quarter-end, else a month form.

    Used only when the caller passes no explicit labels; the route passes the
    cash-flow lens's own labels so the two charts' axes agree exactly.
    """
    if end.month in (3, 6, 9, 12):
        return f"Q{(end.month - 1) // 3 + 1} {end.year}"
    return f"{end:%b %Y}"


def _empty_spec(colours: dict[str, Any]) -> dict[str, Any]:
    """Return a themed figure carrying a single empty-state annotation."""
    return apply_theme(
        {
            "data": [],
            "layout": {
                "margin": {"l": 58, "r": 52, "t": 30, "b": 40},
                "showlegend": False,
                "height": _HEIGHT,
                "annotations": [
                    {
                        "text": "No projection to compare",
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
    )


__all__ = [
    "SEAM_COLOUR",
    "build_scenario_impact_pair",
]
