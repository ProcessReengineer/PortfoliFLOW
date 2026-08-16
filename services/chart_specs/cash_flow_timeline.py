# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plotly figure spec — the Cash Flow Planning timeline (ADR-0104 §4/§6).

Projects the S2.4a result object
(:class:`services.investments.cash_flow_timeline.CashFlowPlanningResult`)
into the chart half of the Cash Flow Planning lens. The table half projects
the **same** object in the section template — there is no second computation
path, so chart and table cannot disagree about a number (ADR-0104 §5).

**One scale for two worlds.** The y-axis range is computed over the *union*
of the baseline and the scenario series, never over the drawn series alone.
Two consequences, and both are the point:

* Flipping the Baseline/Scenario toggle does not rescale the chart, so the
  eye compares two pictures rather than two axes.
* The baseline ghost line and the scenario line are measured against one
  ruler. Auto-scaling either world independently would visually lie about
  the size of the gap between them — the honest-comparison rule the mockup's
  §⑥ annotation states for the projected-path pair, applied here.

**What is drawn, per view.** In *scenario* view the chart carries the
scenario's per-currency balance lines, the scenario total in the accent
role, and the baseline total as a dashed grey ghost — the gap between the
two totals is the scenario's cash effect. In *baseline* view it carries the
baseline's own lines and total and **no** scenario series: the baseline is
the plan world as the book states it (ADR-0104 §1, D19), and a scenario line
drawn across it would contradict the toggle. The shared range is what keeps
the two views comparable regardless.

**Empty is not zero** (the S2.4a semantics, carried through). A ``None``
balance travels into the y-list as ``None``, which Plotly renders as a gap in
the line. It must never become ``0.0`` here: a fabricated zero in a currency
the mandate had not yet opened reads as an account drawn to nil.

Pure: no DB, no repository, no clock. The result DTOs are imported under
:data:`typing.TYPE_CHECKING` only — their defining module is DB-coupled
(:mod:`services.investments.cash_flow_timeline` reads repositories), and this
package keeps its DB-free import graph (ADR-0045 §1).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from services.chart_specs._theme import apply_theme
from services.chart_specs.base import get_chart_theme

if TYPE_CHECKING:  # pragma: no cover - types only; see the module docstring
    from services.investments.cash_flow_timeline import (
        CashFlowPlanningResult,
        CashFlowTimeline,
    )

#: The amber of the actual/plan seam — the single vertical rule the chart and
#: the table both draw (ADR-0104 §6). It mirrors the shell's
#: ``--ui-semantic-warning`` token (``#FFC107``), which
#: ``config/chart_theme.json`` has no counterpart for: the chart theme carries
#: a palette and a canvas, not the shell's semantic status colours. Stated here
#: rather than added to the theme contract, which is a wider decision than this
#: surface.
SEAM_COLOUR: str = "#FFC107"

#: The grey of the baseline ghost line. ``colours.neutral`` of the chart
#: theme — deliberately *not* a palette entry, so the ghost cannot be confused
#: with a currency series.
_GHOST_KEY: str = "neutral"

#: Fraction of the value span added above and below the y-range, so a line
#: never runs along the plot edge.
_Y_PADDING: float = 0.08

#: Fallback padding for a flat series (span of zero), as a fraction of the
#: level itself. A flat balance is an ordinary shape for a cash position that
#: has not moved, and a zero-height axis would render it as a line on the
#: floor.
_FLAT_PADDING: float = 0.05


class CurrencyView(StrEnum):
    """Whether the lens shows the currency rows or the converted total alone.

    The "Per currency ⇄ Functional only" toggle of ADR-0104 §6. It selects
    what is *shown*, never what is computed: both timelines are assembled in
    full either way, so the y-range (and therefore the chart's scale) is a
    function of the view, not of a second computation.

    Members:
        PER_CURRENCY: The per-currency balance lines (position currency) plus
            the functional-currency total.
        FUNCTIONAL_ONLY: The functional-currency totals alone.
    """

    PER_CURRENCY = "per-currency"
    FUNCTIONAL_ONLY = "functional-only"


class WorldView(StrEnum):
    """Which of the two worlds the lens states (ADR-0104 §4).

    Members:
        SCENARIO: The overlaid plan world — the active parameter set applied.
            With an empty overlay this is value-identical to the baseline,
            which is the toggle's contract, not an accident of the data.
        BASELINE: The plan world as the book states it, untouched by any chip
            (ADR-0104 §1, D19). The chips stay in the strip, greyed.
    """

    SCENARIO = "scenario"
    BASELINE = "baseline"


def build_cash_flow_timeline_spec(
    result: CashFlowPlanningResult,
    *,
    currency_view: CurrencyView = CurrencyView.PER_CURRENCY,
    view: WorldView = WorldView.SCENARIO,
) -> dict[str, Any]:
    """Build the Plotly spec for the Cash Flow Planning timeline.

    Args:
        result: The S2.4a result — both worlds over one grid.
        currency_view: Whether to draw the per-currency lines beside the
            total, or the total alone.
        view: Which world the lens states. Selects the drawn series; it does
            **not** select the y-range, which always spans both worlds.

    Returns:
        A themed Plotly figure dict
        (``{"data": [...], "layout": {...}, "config": {...}}``). A result
        whose grid is empty yields a themed empty-state figure.
    """
    theme = get_chart_theme()
    colours = theme["colours"]

    active = result.baseline if view is WorldView.BASELINE else result.scenario
    if not active.periods:
        return _empty_spec(colours)

    labels = [period.label for period in active.periods]
    data: list[dict[str, Any]] = []

    if currency_view is CurrencyView.PER_CURRENCY:
        palette = _currency_palette(colours)
        for index, row in enumerate(active.currency_rows):
            data.append(
                _line(
                    name=row.currency,
                    labels=labels,
                    values=row.balances,
                    colour=palette[index % len(palette)],
                    width=2.0,
                    unit=row.currency,
                )
            )

    total_name = "Total (baseline)" if view is WorldView.BASELINE else "Total (scenario)"
    data.append(
        _line(
            name=total_name,
            labels=labels,
            values=active.total,
            colour=colours["primary"],
            width=2.6,
            unit=active.functional_currency,
        )
    )
    if view is WorldView.SCENARIO:
        data.append(
            _line(
                name="Total (baseline)",
                labels=labels,
                values=result.baseline.total,
                colour=colours[_GHOST_KEY],
                width=2.0,
                unit=result.baseline.functional_currency,
                dash="dash",
            )
        )

    layout: dict[str, Any] = {
        "margin": {"l": 60, "r": 20, "t": 30, "b": 40},
        "showlegend": True,
        "legend": {"orientation": "h", "y": 1.12, "x": 0},
        "xaxis": {
            "type": "category",
            "categoryorder": "array",
            "categoryarray": labels,
        },
        "yaxis": {"tickformat": ",.0f", "title": {"text": "Balance"}},
        "shapes": [_seam_shape(active)],
        "annotations": [_seam_annotation(active)],
        "height": 320,
    }
    y_range = _shared_y_range(result, currency_view)
    if y_range is not None:
        layout["yaxis"]["range"] = y_range

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


def _currency_palette(colours: dict[str, Any]) -> list[str]:
    """Return the palette the currency lines cycle through.

    The accent (``colours.primary``) is withheld: it is the total's colour,
    and a currency line wearing it would read as the total.
    """
    palette = [
        colour for colour in colours.get("series_palette", []) if colour != colours["primary"]
    ]
    return palette or [colours["secondary"]]


def _line(
    *,
    name: str,
    labels: list[str],
    values: tuple[Decimal | None, ...],
    colour: str,
    width: float,
    unit: str,
    dash: str | None = None,
) -> dict[str, Any]:
    """Build one scatter trace over the period grid.

    ``None`` balances survive as ``None`` — an empty cell is a gap in the
    line, not a zero (:mod:`services.investments.cash_flow_timeline`).
    """
    line: dict[str, Any] = {"color": colour, "width": width}
    if dash is not None:
        line["dash"] = dash
    return {
        "type": "scatter",
        "mode": "lines+markers",
        "name": name,
        "x": labels,
        "y": [None if value is None else float(value) for value in values],
        "line": line,
        "marker": {"size": 5},
        "connectgaps": False,
        "hovertemplate": f"{name}: %{{y:,.0f}} {unit}<extra></extra>",
    }


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


def _seam_x(timeline: CashFlowTimeline) -> float:
    """Return the seam's x position on the category axis.

    Category coordinates run ``0, 1, 2, …`` over the columns, so the rule
    between the last actual column and the first plan one sits half a step
    left of :attr:`CashFlowTimeline.seam_index` — the same "actuals left, plan
    right, no interleaving" split the table draws (ADR-0104 §6). A book whose
    grid opens on a plan column (``seam_index == 0``) puts the rule at the
    chart's left edge, which is the truth: everything shown is plan.
    """
    return timeline.seam_index - 0.5


def _seam_shape(timeline: CashFlowTimeline) -> dict[str, Any]:
    """Build the amber dashed rule at the actual/plan seam."""
    x = _seam_x(timeline)
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


def _seam_annotation(timeline: CashFlowTimeline) -> dict[str, Any]:
    """Label the seam with the date it means, not the column it sits beside."""
    return {
        "text": f"last actual {timeline.seam_date.isoformat()}",
        "x": _seam_x(timeline),
        "xref": "x",
        "y": 1.0,
        "yref": "paper",
        "xanchor": "left",
        "yanchor": "top",
        "showarrow": False,
        "font": {"size": 11, "color": SEAM_COLOUR},
    }


# ---------------------------------------------------------------------------
# The shared range — the honest-comparison rule
# ---------------------------------------------------------------------------


def _shared_y_range(
    result: CashFlowPlanningResult,
    currency_view: CurrencyView,
) -> list[float] | None:
    """Return the y-range spanning **both** worlds, or ``None`` if empty.

    The binding correctness rule of this spec (ADR-0104 §5, deltas-first):
    the range is computed over the union of the baseline and the scenario
    series — the ones drawn *and* the ones the other view would draw. So the
    Baseline/Scenario toggle re-draws the picture without re-scaling the
    ruler, and the gap between the scenario line and its baseline ghost is
    readable as a magnitude rather than as a shape.

    Args:
        result: Both worlds.
        currency_view: Selects whether the per-currency balances enter the
            union. Under ``FUNCTIONAL_ONLY`` they are not drawn in either
            world, so including them would scale the chart to data nobody can
            see.

    Returns:
        ``[low, high]`` with padding, or ``None`` where no world carries a
        single balance — a book whose grid is entirely empty cells, which
        Plotly may auto-scale as it likes.
    """
    values: list[float] = []
    for timeline in (result.baseline, result.scenario):
        if currency_view is CurrencyView.PER_CURRENCY:
            for row in timeline.currency_rows:
                values.extend(float(value) for value in row.balances if value is not None)
        values.extend(float(value) for value in timeline.total if value is not None)
    if not values:
        return None

    low, high = min(values), max(values)
    span = high - low
    padding = span * _Y_PADDING if span > 0 else max(abs(high) * _FLAT_PADDING, 1.0)
    return [low - padding, high + padding]


def _empty_spec(colours: dict[str, Any]) -> dict[str, Any]:
    """Return a themed figure carrying a single empty-state annotation."""
    return apply_theme(
        {
            "data": [],
            "layout": {
                "margin": {"l": 60, "r": 20, "t": 30, "b": 40},
                "showlegend": False,
                "height": 320,
                "annotations": [
                    {
                        "text": "No cash-flow periods to project",
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
    "CurrencyView",
    "WorldView",
    "build_cash_flow_timeline_spec",
]
