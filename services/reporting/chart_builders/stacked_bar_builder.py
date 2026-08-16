# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Vertical stacked-bar chart builder for cashflows-by-year.

Renders two series stacked at zero — capital calls (negative) below, and
distributions (positive) above.  The x-axis uses year labels.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from core.chart_helpers import (
    apply_axes_theme,
    create_themed_figure,
    format_eur_millions_axis,
)
from services.reporting.chart_builders.base import (
    ChartBuilder,
    apply_legend,
    apply_subplots_adjust,
    apply_title,
    make_no_data_figure,
)

logger = logging.getLogger(__name__)


class StackedBarBuilder(ChartBuilder):
    """Builder for the cashflows-by-year stacked bar chart."""

    def build(self, data: pd.DataFrame, theme: dict, title: str) -> Figure:
        """Build the cashflows-by-year figure.

        Args:
            data: Year-indexed DataFrame with columns ``["calls",
                "distributions"]``.  ``calls`` are expected to be negative.
            theme: The full chart theme dict.
            title: Chart title.

        Returns:
            A themed :class:`~matplotlib.figure.Figure`.
        """
        if data is None or data.empty:
            return make_no_data_figure(theme, title)

        colours = theme["colours"]
        bar_cfg = theme["bar"]

        years_int = np.array(
            [int(idx.year) if hasattr(idx, "year") else int(idx) for idx in data.index],
            dtype=int,
        )
        calls = data["calls"].astype(float).to_numpy() if "calls" in data.columns else None
        dists = (
            data["distributions"].astype(float).to_numpy()
            if "distributions" in data.columns
            else None
        )

        fig = create_themed_figure(theme)
        ax = fig.add_subplot(111)
        apply_axes_theme(ax, theme)

        legend_handles: list = []
        legend_labels: list[str] = []

        if calls is not None and calls.any():
            bars_calls = ax.bar(
                years_int,
                calls,
                color=colours["negative_bar"],
                alpha=bar_cfg["alpha"],
                width=bar_cfg["width"],
                linewidth=bar_cfg["edge_width"],
                label="Calls",
            )
            legend_handles.append(bars_calls)
            legend_labels.append("Calls")

        if dists is not None and dists.any():
            bars_dists = ax.bar(
                years_int,
                dists,
                color=colours["positive_bar"],
                alpha=bar_cfg["alpha"],
                width=bar_cfg["width"],
                linewidth=bar_cfg["edge_width"],
                label="Distributions",
            )
            legend_handles.append(bars_dists)
            legend_labels.append("Distributions")

        ax.axhline(
            y=0,
            color=colours["axis_line"],
            linewidth=theme["line"]["width_axis"],
        )

        ax.set_xticks(list(years_int))
        ax.tick_params(axis="x", labelrotation=0)
        format_eur_millions_axis(ax, theme)

        apply_title(ax, title, theme)
        if len(legend_labels) > 1:
            apply_legend(ax, legend_handles, legend_labels, theme)
        apply_subplots_adjust(fig, theme)
        return fig
