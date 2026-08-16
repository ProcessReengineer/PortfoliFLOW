# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Stacked area + line chart builder.

Renders a 2-column DataFrame as a filled area for the first column and a
line on top for the second column.  Used for the Tile-1 "Invested Capital
& NAV" chart.

Visual properties (colours, alpha, line width) are read from the chart
theme — no hardcoded values.
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


class StackedAreaWithLineBuilder(ChartBuilder):
    """Filled-area-plus-line chart builder for two parallel year-indexed series."""

    def build(self, data: pd.DataFrame, theme: dict, title: str) -> Figure:
        """Build the area-plus-line figure.

        Args:
            data: Year-indexed DataFrame with exactly two columns.  The first
                column is rendered as a filled area, the second as a line.
                Column names are taken from the DataFrame and used as legend
                entries — they are not hardcoded.
            theme: The full chart theme dict.
            title: Chart title.

        Returns:
            A themed :class:`~matplotlib.figure.Figure`.
        """
        if data is None or data.empty or data.shape[1] < 2:
            return make_no_data_figure(theme, title)

        colours = theme["colours"]
        chart_cfg = theme.get("chart", {})
        line_cfg = theme["line"]

        area_col = data.columns[0]
        line_col = data.columns[1]

        x = np.array([int(v) for v in data.index], dtype=int)
        y_area = pd.to_numeric(data[area_col], errors="coerce").to_numpy(dtype=float)
        y_line = pd.to_numeric(data[line_col], errors="coerce").to_numpy(dtype=float)

        if not (np.isfinite(y_area).any() or np.isfinite(y_line).any()):
            return make_no_data_figure(theme, title)

        fig = create_themed_figure(theme)
        ax = fig.add_subplot(111)
        apply_axes_theme(ax, theme)

        area_handle = ax.fill_between(
            x,
            np.zeros_like(y_area),
            y_area,
            color=colours["primary"],
            alpha=float(chart_cfg.get("area_alpha", 0.65)),
            label=str(area_col),
        )
        (line_handle,) = ax.plot(
            x,
            y_line,
            color=colours["accent_line"],
            linewidth=line_cfg.get("width_px", line_cfg.get("width_primary", 1.5)),
            label=str(line_col),
        )

        ax.set_xticks(list(x))
        ax.tick_params(axis="x", labelrotation=0)
        format_eur_millions_axis(ax, theme)

        apply_title(ax, title, theme)
        apply_legend(ax, [area_handle, line_handle], [str(area_col), str(line_col)], theme)
        apply_subplots_adjust(fig, theme)
        return fig
