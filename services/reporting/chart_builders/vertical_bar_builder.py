# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Vertical bar chart builder.

Renders a single-column or two-column DataFrame as vertical bars.  When a
secondary ``label_column`` is supplied, its value is written as a numeric
label centered above each bar — useful for the vintages chart, where the
bar height is the NAV share and the label is the investment count
(``"n=3"``).
"""

from __future__ import annotations

import logging

import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from core.chart_helpers import apply_axes_theme, create_themed_figure
from services.reporting.chart_builders.base import (
    ChartBuilder,
    apply_subplots_adjust,
    apply_title,
    make_no_data_figure,
)

logger = logging.getLogger(__name__)


class VerticalBarBuilder(ChartBuilder):
    """Vertical bar chart builder.

    Args:
        value_column: Name of the column whose values become bar heights.
        label_column: Optional column whose value is shown as a numeric
            label above each bar.  If the value is integer-like, the label
            is formatted as ``"n=3"``; otherwise it is formatted with one
            decimal.
        as_percent: If ``True`` (default), bar heights are interpreted as
            decimals in ``[0, 1]`` and the y-axis uses a percent formatter.
    """

    def __init__(
        self,
        value_column: str,
        label_column: str | None = None,
        as_percent: bool = True,
    ) -> None:
        self._value_column = value_column
        self._label_column = label_column
        self._as_percent = as_percent

    def build(self, data: pd.DataFrame, theme: dict, title: str) -> Figure:
        """Build the vertical bar figure.

        Args:
            data: DataFrame indexed by category (e.g. vintage year) with at
                least the configured ``value_column``.  Empty input yields a
                no-data figure.
            theme: The full chart theme dict.
            title: Chart title.

        Returns:
            A themed :class:`~matplotlib.figure.Figure`.
        """
        if data is None or data.empty or self._value_column not in data.columns:
            return make_no_data_figure(theme, title)

        values = pd.to_numeric(data[self._value_column], errors="coerce").to_numpy(dtype=float)
        if not np.isfinite(values).any():
            return make_no_data_figure(theme, title)

        x = np.array([int(v) for v in data.index], dtype=int)

        colours = theme["colours"]
        bar_cfg = theme["bar"]
        font = theme["font"]

        fig = create_themed_figure(theme)
        ax = fig.add_subplot(111)
        apply_axes_theme(ax, theme)

        ax.bar(
            x,
            values,
            color=colours["primary"],
            alpha=bar_cfg["alpha"],
            width=bar_cfg["width"],
            linewidth=bar_cfg["edge_width"],
        )

        if self._label_column is not None and self._label_column in data.columns:
            label_values = data[self._label_column].tolist()
            for xi, top, lbl in zip(x, values, label_values, strict=True):
                if not np.isfinite(top):
                    continue
                text = self._format_label(lbl)
                ax.text(
                    xi,
                    top,
                    text,
                    ha="center",
                    va="bottom",
                    color=colours["text"],
                    fontsize=font.get("label_size", font["tick_label_size"]),
                    fontfamily=font["family"],
                )

        ax.set_xticks(list(x))
        ax.tick_params(axis="x", labelrotation=0)
        if self._as_percent:
            ax.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=1.0, decimals=1))

        apply_title(ax, title, theme)
        apply_subplots_adjust(fig, theme)
        return fig

    def _format_label(self, value: object) -> str:
        """Format the secondary label value as ``"n=3"`` for ints, else one decimal."""
        if value is None:
            return ""
        if isinstance(value, float) and pd.isna(value):
            return ""
        try:
            num = float(value)
        except (TypeError, ValueError):
            return str(value)
        if not np.isfinite(num):
            return ""
        if float(num).is_integer():
            return f"n={int(num)}"
        return f"{num:.1f}"
