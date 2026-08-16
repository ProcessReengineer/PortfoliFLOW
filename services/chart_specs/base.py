# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Theme-aware Plotly layout helpers.

The single source of truth for chart visuals is
``config/chart_theme.json`` (per ADR-0021 / ADR-0042 §4). The PyQt6
matplotlib path reads it through
:func:`core.chart_theme.get_chart_theme` — that loader transitively
imports matplotlib, so this module cannot reuse it: the Phase-3
regression guard requires that nothing under ``services/chart_specs/``
imports matplotlib.

Instead this module loads the same JSON file directly and caches the
result. Both code paths therefore consume the same canonical
parameters, but the import graph here stays Qt-free, FastAPI-free,
and matplotlib-free.

The active theme filename is resolved through
:class:`core.theme_service.ThemeService` when available — the same
Phase-B picker the PyQt6 widgets use — so that switching themes via
the GUI also affects the web variant on the next request. When the
service has not been told otherwise, the filename defaults to
``chart_theme.json`` and behaviour matches the Phase-A loader.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_DEFAULT_CHART_THEME_FILENAME = "chart_theme.json"

_theme_cache: dict[str, dict[str, Any]] = {}


def _resolve_active_filename() -> str:
    """Return the active chart-theme filename.

    Delegates to :class:`core.theme_service.ThemeService` so the GUI
    theme picker (Phase B) and the web variant stay in sync. The
    service is matplotlib-free; importing it does not break the
    Qt-free / matplotlib-free invariants of this package.

    Returns:
        The bare filename (e.g. ``"chart_theme.json"``) inside the
        repo's ``config/`` directory.
    """
    try:
        from core.theme_service import get_theme_service

        return get_theme_service().get_active_chart_theme_filename()
    except Exception as exc:  # noqa: BLE001 - fall back to the default
        logger.debug(
            "chart_specs: theme service unavailable (%s); falling back to %s.",
            exc,
            _DEFAULT_CHART_THEME_FILENAME,
        )
        return _DEFAULT_CHART_THEME_FILENAME


def _load_theme(filename: str) -> dict[str, Any]:
    """Read and parse ``config/<filename>`` from disk.

    Args:
        filename: Bare filename (no directory) inside the repo's
            ``config/`` directory.

    Returns:
        The parsed JSON dictionary.
    """
    path = _REPO_ROOT / "config" / filename
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object at {path}, got {type(data).__name__}.")
    return data


def get_chart_theme() -> dict[str, Any]:
    """Return the active chart theme dict, loading from disk on first call.

    Cached per filename so repeated calls do not re-read the JSON.
    The cache is keyed by filename, so a runtime theme switch (the
    GUI Phase-B picker calling
    :meth:`ThemeService.set_active_chart_theme`) is honoured on the
    next request without further bookkeeping.

    Returns:
        A dictionary mirroring the JSON structure exactly. Keys
        ``font``, ``colours``, ``line``, ``optimization``, ``axis``,
        ``legend``, ``layout`` are guaranteed by the canonical theme.
    """
    filename = _resolve_active_filename()
    cached = _theme_cache.get(filename)
    if cached is not None:
        return cached
    theme = _load_theme(filename)
    _theme_cache[filename] = theme
    return theme


def reload_chart_theme() -> dict[str, Any]:
    """Force-reload the active chart theme from disk.

    Mirrors :func:`core.chart_theme.reload_chart_theme` so the two
    rendering paths can be reset in lockstep from a future test
    fixture.

    Returns:
        The freshly loaded theme dict.
    """
    _theme_cache.clear()
    return get_chart_theme()


def layout_from_theme(
    *,
    title: str,
    xlabel: str,
    ylabel: str,
    show_legend: bool = True,
) -> dict[str, Any]:
    """Build a Plotly layout dict from the canonical chart theme.

    The translation maps theme parameters onto the Plotly layout
    schema: ``colours.background`` → ``paper_bgcolor`` /
    ``plot_bgcolor``, ``colours.grid`` → axis ``gridcolor``,
    ``font.family`` → axis / title / legend font family. Both numeric
    and percentage-formatted x / y axes are handled by the caller via
    layout overlays — this helper sets the percentage formatter as
    the default since it is what every SAA chart uses; specs that
    need a different formatter override after the fact.

    Args:
        title: Chart title displayed at the top.
        xlabel: X-axis label.
        ylabel: Y-axis label.
        show_legend: Whether to render the Plotly legend.

    Returns:
        A dict in Plotly's layout schema, fully themed. Pass directly
        as ``layout`` to ``Plotly.newPlot(...)``.
    """
    theme = get_chart_theme()
    colours = theme["colours"]
    font = theme["font"]

    family = font["family"]
    # Plotly accepts a comma-separated string; the JSON canonical
    # form is a list of fallbacks (matplotlib idiom). Convert at
    # the seam so the JSON stays single-source.
    family_str = ", ".join(family) if isinstance(family, list) else str(family)

    common_axis = {
        "gridcolor": colours["grid"],
        "linecolor": colours["axis_line"],
        "zerolinecolor": colours["grid"],
        "tickformat": ".1%",
        "tickfont": {
            "family": family_str,
            "size": font["tick_label_size"],
            "color": colours["text"],
        },
    }

    return {
        "title": {
            "text": title,
            "font": {
                "family": family_str,
                "size": font["title_size"],
                "color": colours["text"],
            },
            "x": 0.5,
        },
        "xaxis": {
            **common_axis,
            "title": {
                "text": xlabel,
                "font": {
                    "family": family_str,
                    "size": font["axis_label_size"],
                    "color": colours["text"],
                },
            },
        },
        "yaxis": {
            **common_axis,
            "title": {
                "text": ylabel,
                "font": {
                    "family": family_str,
                    "size": font["axis_label_size"],
                    "color": colours["text"],
                },
            },
        },
        "paper_bgcolor": colours["background"],
        "plot_bgcolor": colours["plot_area"],
        "showlegend": show_legend,
        "legend": {
            "font": {
                "family": family_str,
                "size": font["legend_size"],
                "color": colours["text"],
            },
            "bgcolor": colours["background"],
            "bordercolor": colours["grid"],
            "borderwidth": 1,
        },
        "hoverlabel": {
            "font": {
                "family": family_str,
                "size": font["tick_label_size"],
                "color": colours["text"],
            },
            "bgcolor": colours["plot_area"],
            "bordercolor": colours["grid"],
        },
        "margin": {"l": 70, "r": 30, "t": 60, "b": 60},
    }


def apply_axis_end(
    layout: dict[str, Any],
    axis_end: date | None,
    *,
    has_data: bool,
) -> None:
    """Extend a time-axis auto-range to reach the ADR-0113 §1 universe as-of.

    Every time-series tile in the Front-Office Charts section shares one
    x-axis **end** — the latest ``'actual'`` NAV date across the active
    investment universe — so equal tile widths stop implying equal
    periods. Plotly's ``xaxis.autorangeoptions.include`` (≥ 2.26; the
    bundled runtime is 2.35.2) extends the computed range to cover the
    given date while the left edge stays data-driven: the axis *start*
    is deliberately not unified (vintages differ by years).

    A no-op when ``axis_end`` is ``None`` (empty universe, or a caller
    outside the Charts section) or when the figure carries no data —
    pinning a date onto an empty or hidden axis would draw a range with
    nothing in it.

    Args:
        layout: The figure's ``layout`` dict. Mutated in place; call
            before :func:`services.chart_specs._theme.apply_theme`,
            which only fills in defaults and so preserves the entry.
        axis_end: The universe as-of date, or ``None`` to leave the tile
            on its own auto-range (the pre-ADR-0113 behaviour).
        has_data: Whether the figure carries at least one datapoint.
    """
    if axis_end is None or not has_data:
        return
    xaxis = layout.setdefault("xaxis", {})
    xaxis["autorangeoptions"] = {"include": axis_end.isoformat()}


def plan_tail_window(
    plan_dates: Sequence[date],
    *,
    last_actual_date: date | None,
    plan_tail_end: date,
) -> list[int]:
    """Select the plan rows inside the ADR-0113 §2 tail window.

    The tail is the half-open stretch
    ``last_actual_date < as_of_date <= plan_tail_end``: plan rows that
    lie *beyond* the solid actual line but not past the unified axis end.
    Rows at or before the last actual are dropped — they would redraw a
    period the actual line already states, in a projection's styling.
    When the investment has no actual row at all, the lower bound is
    open and the whole plan series up to ``plan_tail_end`` qualifies.

    Positions are returned rather than values so each spec builder keeps
    its own container and date-formatting conventions (a list of NAV
    DTOs, a pandas Series) and the anchor point joins the solid line
    exactly.

    Args:
        plan_dates: The plan rows' ``as_of_date`` values, ascending. The
            caller sorts; the returned positions follow that order.
        last_actual_date: The last actual observation's date, or ``None``
            when the investment carries no actual row.
        plan_tail_end: The unified axis end (the ADR-0113 §1 universe
            as-of). Plan rows beyond it are dropped rather than drawn
            past every other tile's right edge.

    Returns:
        Positions into ``plan_dates`` of the rows inside the window;
        empty when nothing qualifies (the honest-gap case — the caller
        renders an empty trace, fabricating nothing).
    """
    return [
        position
        for position, as_of in enumerate(plan_dates)
        if (last_actual_date is None or as_of > last_actual_date) and as_of <= plan_tail_end
    ]


def color_palette() -> dict[str, str]:
    """Return the named colour palette for SAA-flavoured charts.

    The mapping mirrors the matplotlib choices in
    ``gui/widgets/saa_widget.py`` so the Plotly and matplotlib
    renderings of the same data look the same:

    * ``frontier`` — ``colours.primary`` (red) for the efficient frontier line.
    * ``tangency`` — ``colours.primary`` (matplotlib uses the same colour
      as the frontier line; the marker shape distinguishes it).
    * ``min_var`` — ``colours.tertiary`` (green).
    * ``cml`` — ``colours.secondary`` (blue) for the Capital Market Line.
    * ``cloud`` — ``optimization.cloud_colour`` (dark grey).

    Returns:
        Dict of semantic role → hex colour string.
    """
    theme = get_chart_theme()
    colours = theme["colours"]
    optimization = theme["optimization"]
    return {
        "frontier": colours["primary"],
        "tangency": colours["primary"],
        "min_var": colours["tertiary"],
        "cml": colours["secondary"],
        "cloud": optimization["cloud_colour"],
        "rf_line": colours["text"],
    }
