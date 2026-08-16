# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Abstract base class for chart builders.

A builder takes a DataFrame (from a :class:`DataProvider`) plus a chart theme
dict and returns a matplotlib :class:`~matplotlib.figure.Figure`.  Builders
contain NO Qt code and NO DataStore access.

Use :func:`core.chart_helpers.create_themed_figure` and
:func:`core.chart_helpers.apply_axes_theme` — do NOT duplicate theming code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure

from core.chart_helpers import apply_axes_theme, create_themed_figure


class ChartBuilder(ABC):
    """Base class for the report's matplotlib chart builders."""

    @abstractmethod
    def build(self, data: pd.DataFrame, theme: dict, title: str) -> Figure:
        """Build a Figure for the given data.

        Args:
            data: The provider DataFrame.  Builders document their expected
                shape.  An empty DataFrame must yield a no-data figure (a
                centered "No data" annotation matching the theme background).
            theme: The full chart theme dict from
                :func:`core.chart_theme.get_chart_theme`.
            title: Chart title.

        Returns:
            A themed :class:`~matplotlib.figure.Figure`.
        """


def make_no_data_figure(theme: dict, title: str) -> Figure:
    """Return a themed Figure containing only a centered 'No data' annotation.

    Args:
        theme: The full chart theme dict.
        title: Title to display above the empty plot area.

    Returns:
        A themed :class:`~matplotlib.figure.Figure` with no plotted data.
    """
    fig = create_themed_figure(theme)
    ax = fig.add_subplot(111)
    apply_axes_theme(ax, theme)
    apply_title(ax, title, theme)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.text(
        0.5,
        0.5,
        "No data",
        transform=ax.transAxes,
        ha="center",
        va="center",
        color=theme["colours"]["grid"],
        fontsize=theme["font"]["tick_label_size"],
        fontfamily=theme["font"]["family"],
    )
    apply_subplots_adjust(fig, theme)
    return fig


def apply_title(ax: Axes, title: str, theme: dict) -> None:
    """Set an axes title using the theme's title font and colour.

    Args:
        ax: A matplotlib :class:`~matplotlib.axes.Axes` instance.
        title: Title text.
        theme: The full chart theme dict.
    """
    colours = theme["colours"]
    font = theme["font"]
    layout_cfg = theme["layout"]
    ax.set_title(
        title,
        color=colours["text"],
        fontsize=font["title_size"],
        fontweight=font["weight_title"],
        pad=layout_cfg["title_pad"],
        fontfamily=font["family"],
    )


def apply_subplots_adjust(fig: Figure, theme: dict) -> None:
    """Apply subplot padding from the theme's ``layout`` section.

    Args:
        fig: The :class:`~matplotlib.figure.Figure` to adjust.
        theme: The full chart theme dict.
    """
    layout_cfg = theme["layout"]
    fig.subplots_adjust(
        top=layout_cfg["padding_top"],
        bottom=layout_cfg["padding_bottom"],
        left=layout_cfg["padding_left"],
        right=layout_cfg["padding_right"],
    )


def apply_legend(ax: Axes, handles: list, labels: list, theme: dict) -> None:
    """Add a themed legend (bottom-center) to the given axes.

    Args:
        ax: A matplotlib :class:`~matplotlib.axes.Axes` instance.
        handles: List of legend handles (artists).
        labels: List of legend label strings.
        theme: The full chart theme dict.
    """
    legend_cfg = theme["legend"]
    font = theme["font"]
    colours = theme["colours"]
    ax.legend(
        handles,
        labels,
        loc="lower center",
        bbox_to_anchor=(0.5, -0.18),
        frameon=legend_cfg["frame_on"],
        framealpha=legend_cfg["frame_alpha"],
        ncols=max(1, len(labels)),
        markerscale=legend_cfg["marker_scale"],
        fontsize=font["legend_size"],
        labelcolor=colours["text"],
    )
