# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Clustered horizontal bar chart builder.

Used for the multiples (TVPI / DPI) chart and the per-investment IRR list.
Each row of the input DataFrame becomes a row of clustered bars on the y-axis,
with one cluster per data column.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import matplotlib.ticker as mticker
from matplotlib.figure import Figure

from core.chart_helpers import (
    apply_axes_theme,
    compute_left_padding,
    create_themed_figure,
    get_series_colour,
)
from services.reporting.chart_builders.base import (
    ChartBuilder,
    apply_legend,
    apply_subplots_adjust,
    apply_title,
    make_no_data_figure,
)

logger = logging.getLogger(__name__)


class ClusteredHorizontalBarBuilder(ChartBuilder):
    """Clustered horizontal bar chart with up to two side-by-side series per row.

    Args:
        value_format: Optional format string for the x-axis tick labels and
            for treating values.  ``"x"`` formats values as multiples
            (``1.42x``); ``"%"`` formats them as percentages
            (``12.3 %`` — input values are interpreted as decimals and
            scaled by 100).  ``None`` (default) leaves the formatter alone.
    """

    def __init__(self, value_format: str | None = None) -> None:
        self._value_format = value_format

    def build(self, data: pd.DataFrame, theme: dict, title: str) -> Figure:
        """Build the clustered horizontal bar figure.

        Args:
            data: DataFrame indexed by row label.  Each column is one series.
                Cells may contain :data:`numpy.nan` to suppress that bar.
            theme: The full chart theme dict.
            title: Chart title.

        Returns:
            A themed :class:`~matplotlib.figure.Figure`.
        """
        if data is None or data.empty or data.shape[1] == 0:
            return make_no_data_figure(theme, title)

        bar_cfg = theme["bar"]
        n_rows = data.shape[0]
        n_series = data.shape[1]
        if n_rows == 0:
            return make_no_data_figure(theme, title)

        values = data.to_numpy(dtype=float)
        if not np.isfinite(values).any():
            return make_no_data_figure(theme, title)

        if self._value_format == "%":
            display_values = values * 100.0
        else:
            display_values = values

        fig = create_themed_figure(theme)
        ax = fig.add_subplot(111)
        apply_axes_theme(ax, theme)

        y_positions = np.arange(n_rows)[::-1]  # top-to-bottom matches DataFrame order
        cluster_height = bar_cfg["width"]
        bar_height = cluster_height / max(n_series, 1)

        legend_handles: list = []
        legend_labels: list[str] = []

        for series_idx, col in enumerate(data.columns):
            offsets = (series_idx - (n_series - 1) / 2.0) * bar_height
            ys = y_positions + offsets
            xs = display_values[:, series_idx]
            mask = np.isfinite(xs)
            if not mask.any():
                continue
            bars = ax.barh(
                ys[mask],
                xs[mask],
                height=bar_height,
                color=get_series_colour(theme, series_idx),
                alpha=bar_cfg["alpha"],
                linewidth=bar_cfg["edge_width"],
                label=str(col),
            )
            legend_handles.append(bars)
            legend_labels.append(str(col))

        ax.set_yticks(y_positions)
        ax.set_yticklabels([str(lbl) for lbl in data.index])

        if self._value_format == "x":
            ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.2f}x"))
        elif self._value_format == "%":
            ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.1f} %"))

        apply_title(ax, title, theme)
        if len(legend_labels) > 1:
            apply_legend(ax, legend_handles, legend_labels, theme)
        apply_subplots_adjust(fig, theme)
        fig.subplots_adjust(left=compute_left_padding(data.index, theme))
        return fig
