# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Single-series horizontal bar chart builder.

Used for the strategy / country / sector breakdown charts.  Input is a
2-column DataFrame with category labels and a numeric share column.
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
)
from services.reporting.chart_builders.base import (
    ChartBuilder,
    apply_subplots_adjust,
    apply_title,
    make_no_data_figure,
)

logger = logging.getLogger(__name__)


class HorizontalBarBuilder(ChartBuilder):
    """Single-series horizontal bar chart.

    Args:
        category_column: Name of the column that holds the category labels.
        value_column: Name of the column that holds the numeric share.
        as_percent: If ``True`` (default), values are interpreted as
            decimals in ``[0, 1]`` and rendered as percentages on the
            x-axis.
    """

    def __init__(
        self,
        category_column: str,
        value_column: str,
        as_percent: bool = True,
    ) -> None:
        self._category_column = category_column
        self._value_column = value_column
        self._as_percent = as_percent

    def build(self, data: pd.DataFrame, theme: dict, title: str) -> Figure:
        """Build the horizontal bar figure.

        Args:
            data: DataFrame containing the configured category and value
                columns.  Empty input yields a no-data figure.
            theme: The full chart theme dict.
            title: Chart title.

        Returns:
            A themed :class:`~matplotlib.figure.Figure`.
        """
        if (
            data is None
            or data.empty
            or self._category_column not in data.columns
            or self._value_column not in data.columns
        ):
            return make_no_data_figure(theme, title)

        labels = [str(v) for v in data[self._category_column].tolist()]
        values = pd.to_numeric(data[self._value_column], errors="coerce").to_numpy(dtype=float)
        finite_mask = np.isfinite(values)
        if not finite_mask.any():
            return make_no_data_figure(theme, title)

        labels = [labels[i] for i in range(len(labels)) if finite_mask[i]]
        values = values[finite_mask]

        display_values = values * 100.0 if self._as_percent else values

        colours = theme["colours"]
        bar_cfg = theme["bar"]

        fig = create_themed_figure(theme)
        ax = fig.add_subplot(111)
        apply_axes_theme(ax, theme)

        y_positions = np.arange(len(labels))[::-1]
        ax.barh(
            y_positions,
            display_values,
            color=colours["primary"],
            alpha=bar_cfg["alpha"],
            height=bar_cfg["width"],
            linewidth=bar_cfg["edge_width"],
        )

        ax.set_yticks(y_positions)
        ax.set_yticklabels(labels)

        if self._as_percent:
            ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:.0f} %"))

        apply_title(ax, title, theme)
        apply_subplots_adjust(fig, theme)
        fig.subplots_adjust(left=compute_left_padding(labels, theme))
        return fig
