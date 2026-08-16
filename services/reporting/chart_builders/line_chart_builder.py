# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Multi-line time-series chart builder.

Renders a date-indexed DataFrame where each column becomes one line.  Used
for the per-investment Total Return chart (rebased to 100 at inception).
The builder is intentionally generic — it does not assume EUR formatting,
percent formatting, or year-integer X axes — so it can serve future
multi-line use cases (e.g. fund vs. benchmark).

Visual properties (colours, line widths, fonts) are read from the chart
theme; no hardcoded values.
"""

from __future__ import annotations

import logging

import matplotlib.dates as mdates
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from core.chart_helpers import apply_axes_theme, create_themed_figure
from services.reporting.chart_builders.base import (
    ChartBuilder,
    apply_legend,
    apply_subplots_adjust,
    apply_title,
    make_no_data_figure,
)

logger = logging.getLogger(__name__)


class LineChartBuilder(ChartBuilder):
    """Multi-line time-series chart for date-indexed numeric series.

    Each column of the input DataFrame becomes one line.  Colours cycle
    through ``theme["chart"]["line_palette"]`` if defined, falling back to
    ``theme["colours"]["primary"]`` for the first series,
    ``theme["colours"]["accent_line"]`` for the second,
    ``theme["colours"]["nav_line"]`` for the third (and so on — defensive
    against missing keys).
    """

    _DEFAULT_COLOUR_FALLBACKS: tuple[str, ...] = (
        "primary",
        "accent_line",
        "nav_line",
        "secondary",
        "tertiary",
    )

    def __init__(
        self,
        y_label: str = "",
        baseline: float | None = None,
    ) -> None:
        """Construct the builder.

        Args:
            y_label: Y-axis label.  Empty string omits the label.
            baseline: If not ``None``, draws a horizontal dashed line at
                this Y value.  Useful for the "rebased to 100" baseline of
                the Total Return chart.
        """
        self._y_label = y_label
        self._baseline = baseline

    # ------------------------------------------------------------------
    # ChartBuilder API
    # ------------------------------------------------------------------

    def build(self, data: pd.DataFrame, theme: dict, title: str) -> Figure:
        """Build the multi-line figure.

        Args:
            data: Date-indexed DataFrame.  Each column is rendered as one
                line.  An empty DataFrame yields a no-data figure.
            theme: The full chart theme dict.
            title: Chart title.

        Returns:
            A themed :class:`~matplotlib.figure.Figure`.
        """
        if data is None or data.empty or data.shape[1] < 1:
            return make_no_data_figure(theme, title)

        colours = theme["colours"]
        font = theme["font"]
        line_cfg = theme["line"]

        # Convert index to datetime if possible; fall back to numeric.
        try:
            x_values = pd.to_datetime(data.index)
            x_is_date = True
        except (TypeError, ValueError):
            x_values = np.asarray(data.index)
            x_is_date = False

        numeric_cols: list[str] = []
        column_arrays: list[np.ndarray] = []
        for col in data.columns:
            arr = pd.to_numeric(data[col], errors="coerce").to_numpy(dtype=float)
            if np.isfinite(arr).any():
                numeric_cols.append(str(col))
                column_arrays.append(arr)

        if not numeric_cols:
            return make_no_data_figure(theme, title)

        fig = create_themed_figure(theme)
        ax = fig.add_subplot(111)
        apply_axes_theme(ax, theme)

        palette = self._resolve_palette(theme, len(numeric_cols))

        legend_handles: list = []
        legend_labels: list[str] = []
        for idx, (col, values) in enumerate(zip(numeric_cols, column_arrays, strict=True)):
            colour = palette[idx % len(palette)]
            (line_handle,) = ax.plot(
                x_values,
                values,
                color=colour,
                linewidth=line_cfg.get("width_px", line_cfg.get("width_primary", 1.5)),
                label=col,
            )
            legend_handles.append(line_handle)
            legend_labels.append(col)

        if self._baseline is not None:
            ax.axhline(
                y=float(self._baseline),
                color=colours.get("axis_line", colours["text"]),
                linewidth=line_cfg.get("width_axis", 0.8),
                linestyle="--",
                alpha=0.8,
            )

        if x_is_date:
            locator = mdates.AutoDateLocator()
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _pos: f"{v:,.0f}"))
        if self._y_label:
            ax.set_ylabel(
                self._y_label,
                color=colours["text"],
                fontsize=font["axis_label_size"],
                fontfamily=font["family"],
            )

        apply_title(ax, title, theme)
        if len(legend_labels) > 1:
            apply_legend(ax, legend_handles, legend_labels, theme)
        apply_subplots_adjust(fig, theme)
        return fig

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_palette(self, theme: dict, n: int) -> list[str]:
        """Resolve a colour palette of at least ``n`` entries.

        Honours ``theme["chart"]["line_palette"]`` if present, otherwise
        falls back to ``_DEFAULT_COLOUR_FALLBACKS`` resolved against
        ``theme["colours"]``.

        Args:
            theme: Full chart theme dict.
            n: Number of distinct colours required (the result may still be
                shorter — callers cycle via modulo).

        Returns:
            List of hex colour strings, never empty.
        """
        colours = theme["colours"]
        chart_cfg = theme.get("chart", {})
        palette_keys = chart_cfg.get("line_palette")
        if palette_keys:
            resolved = [colours.get(key, colours.get("primary", "#000000")) for key in palette_keys]
            if resolved:
                return resolved

        primary_default = colours.get("primary", "#000000")
        fallback = [colours.get(key, primary_default) for key in self._DEFAULT_COLOUR_FALLBACKS]
        # Trim to at least n distinct entries when possible; modulo cycling
        # by the caller handles the rest.
        return fallback[: max(n, 1)] or [primary_default]
