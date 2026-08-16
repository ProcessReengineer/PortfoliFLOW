# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""core/chart_helpers.py
========================
Shared matplotlib figure and axes helpers for themed chart rendering.

All chart-rendering code — GUI widgets, AI tools, export engines — should
use these functions instead of applying theme parameters manually.  Visual
parameters come exclusively from ``config/chart_theme.json`` via
:func:`core.chart_theme.get_chart_theme`.

Usage::

    from core.chart_helpers import create_themed_figure, apply_axes_theme

    theme = get_chart_theme()
    fig = create_themed_figure(theme)
    ax = fig.add_subplot(111)
    apply_axes_theme(ax, theme)
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable

import matplotlib.ticker as mticker
from matplotlib.axes import Axes
from matplotlib.figure import Figure


def create_themed_figure(
    theme: dict,
    width: float = 4.0,
    height_px: int | None = None,
) -> Figure:
    """Create a new Figure with the chart-theme background colour.

    Args:
        theme: The full chart theme dict from
            :func:`core.chart_theme.get_chart_theme`.
        width: Figure width in inches.  Defaults to ``4.0``.
        height_px: Figure height in pixels.  Defaults to
            ``theme["layout"]["chart_height_px"]``.

    Returns:
        A :class:`~matplotlib.figure.Figure` with the background colour set
        from ``colours.background``.
    """
    if height_px is None:
        height_px = theme["layout"]["chart_height_px"]
    dpi: int = theme["layout"]["figure_dpi"]
    fig = Figure(figsize=(width, height_px / dpi), dpi=dpi)
    fig.patch.set_facecolor(theme["colours"]["background"])
    return fig


def apply_axes_theme(ax: Axes, theme: dict) -> None:
    """Apply the full chart theme to a primary matplotlib Axes.

    Sets plot-area background, spine colours and visibility, tick parameters,
    and grid style.  X- and Y-label colours are also updated.

    Args:
        ax: A matplotlib :class:`~matplotlib.axes.Axes` instance.
        theme: The full chart theme dict.
    """
    colours = theme["colours"]
    font = theme["font"]
    axis_cfg = theme["axis"]
    line_cfg = theme["line"]

    ax.set_facecolor(colours["plot_area"])

    ax.spines["top"].set_visible(axis_cfg["spine_visible_top"])
    ax.spines["right"].set_visible(axis_cfg["spine_visible_right"])
    ax.spines["bottom"].set_visible(axis_cfg["spine_visible_bottom"])
    ax.spines["left"].set_visible(axis_cfg["spine_visible_left"])
    for spine in ax.spines.values():
        spine.set_edgecolor(colours["axis_line"])
        spine.set_linewidth(line_cfg["width_axis"])

    ax.tick_params(
        axis="both",
        direction=axis_cfg["tick_direction"],
        length=axis_cfg["tick_length"],
        width=axis_cfg["tick_width"],
        colors=colours["text"],
        labelsize=font["tick_label_size"],
    )

    ax.grid(
        True,
        color=colours["grid"],
        linewidth=line_cfg["width_grid"],
        linestyle=line_cfg["style_grid"],
        alpha=line_cfg["alpha_grid"],
    )
    ax.set_axisbelow(True)

    ax.xaxis.label.set_color(colours["text"])
    ax.yaxis.label.set_color(colours["text"])


def apply_twin_axes_theme(ax: Axes, theme: dict) -> None:
    """Apply chart theme to a twin (right-side) y-axis created via ``twinx()``.

    Only the right spine is shown; the grid is suppressed because the primary
    axis already draws it.

    Args:
        ax: The twin :class:`~matplotlib.axes.Axes` instance (returned by
            ``ax_primary.twinx()``).
        theme: The full chart theme dict.
    """
    colours = theme["colours"]
    font = theme["font"]
    line_cfg = theme["line"]
    axis_cfg = theme["axis"]

    ax.spines["top"].set_visible(False)
    ax.spines["bottom"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["right"].set_visible(True)
    ax.spines["right"].set_edgecolor(colours["axis_line"])
    ax.spines["right"].set_linewidth(line_cfg["width_axis"])

    ax.tick_params(
        axis="y",
        direction=axis_cfg["tick_direction"],
        length=axis_cfg["tick_length"],
        width=axis_cfg["tick_width"],
        colors=colours["text"],
        labelsize=font["tick_label_size"],
    )
    ax.yaxis.label.set_color(colours["text"])
    ax.grid(False)


def compute_left_padding(labels: Iterable[object], theme: dict) -> float:
    """Return a left subplot-margin fraction sized for the longest label.

    Long y-axis category labels (e.g. ``"Real Estate"``, ``"Consumer Staples"``)
    overflow the default ``layout.padding_left`` and are visually truncated.
    This helper grows the left margin proportionally to the longest label.

    Args:
        labels: Iterable of category labels that will appear on the y-axis.
        theme: The full chart theme dict.

    Returns:
        A fraction in ``[0.0, 0.40]`` suitable for
        :meth:`matplotlib.figure.Figure.subplots_adjust`.
    """
    layout_cfg = theme["layout"]
    base = float(layout_cfg.get("padding_left", 0.12))
    materialised = [str(lbl) for lbl in labels]
    if not materialised:
        return base
    max_chars = max(len(s) for s in materialised)
    ref_chars = int(layout_cfg.get("y_label_padding_chars", 18))
    extra = max(0.0, (max_chars - ref_chars) * 0.008)
    return min(0.40, base + extra)


def format_eur_millions_axis(ax: Axes, theme: dict, axis: str = "y") -> None:
    """Apply a fixed "value / 1e6" tick formatter and a ``"Mio. €"`` label.

    Removes the ``1e8``-style scientific exponent that matplotlib defaults to
    for large EUR values.  The label is always set on the configured axis so
    that callers do not have to remember to set it themselves.

    Args:
        ax: The matplotlib :class:`~matplotlib.axes.Axes` instance.
        theme: The full chart theme dict.
        axis: Either ``"y"`` (default) or ``"x"``.
    """
    target = ax.yaxis if axis == "y" else ax.xaxis
    # ticklabel_format is unavailable on some specialised axes (or
    # raises when a non-default formatter is already set); the
    # FuncFormatter below produces plain output regardless.
    with contextlib.suppress(AttributeError, ValueError):
        ax.ticklabel_format(style="plain", axis=axis, useOffset=False)
    target.set_major_formatter(mticker.FuncFormatter(lambda v, _pos: f"{v / 1e6:,.0f}"))
    if axis == "y":
        ax.set_ylabel(
            "Mio. €",
            color=theme["colours"]["text"],
            fontsize=theme["font"]["axis_label_size"],
            fontfamily=theme["font"]["family"],
        )
    else:
        ax.set_xlabel(
            "Mio. €",
            color=theme["colours"]["text"],
            fontsize=theme["font"]["axis_label_size"],
            fontfamily=theme["font"]["family"],
        )


def get_series_colour(theme: dict, index: int) -> str:
    """Return the series colour for the given index from the theme palette.

    Cycles through ``theme["colours"]["series_palette"]`` using modulo
    arithmetic so the index never exceeds the palette length.

    Args:
        theme: The full chart theme dict.
        index: Zero-based series index.

    Returns:
        A hex colour string (e.g. ``"#E8304A"``).
    """
    palette: list[str] = theme["colours"]["series_palette"]
    return palette[index % len(palette)]
