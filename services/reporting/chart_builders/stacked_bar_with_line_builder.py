# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Stacked-bar-plus-line chart builder.

Renders a year-indexed DataFrame as a vertical stacked bar (positive columns
above zero, negative columns below zero) with one or more lines overlaid on
either the same axis or a secondary y-axis.  Optionally writes a numeric
label centered above each bar position.

Two shapes are configured by the engine:

* *Cashflow + NAV*: bar columns ``("calls", "distributions")``, line columns
  ``("nav", "ncg")``, all on the primary axis, EUR-millions formatter.
* *Multiples timeseries*: bar columns ``("dpi", "rvpi")``, line column
  ``("irr",)`` on a secondary axis, ``"x"`` multiple formatter on the bars,
  and a TVPI label written above each bar.
"""

from __future__ import annotations

import logging

import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from core.chart_helpers import (
    apply_axes_theme,
    apply_twin_axes_theme,
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

_SUPPORTED_FORMATS = ("millions_eur", "multiple_x", "percent")


class StackedBarWithLineBuilder(ChartBuilder):
    """Stacked vertical bars with one or more line overlays.

    Args:
        bar_columns: DataFrame columns rendered as stacked bars.
        line_columns: DataFrame columns rendered as lines.
        bar_label_column: Optional DataFrame column whose value is written
            as a numeric label centered above each bar position.
        bar_y_format: Y-axis tick formatter for the bars.  One of
            ``"millions_eur"``, ``"multiple_x"``, ``"percent"``.
        line_y_format: Y-axis tick formatter for the lines.  Same options as
            ``bar_y_format``.  Used on the secondary axis when
            ``line_axis == "secondary"``, otherwise ignored.
        line_axis: ``"primary"`` plots lines on the bar axis,
            ``"secondary"`` creates a twin y-axis on the right.
    """

    def __init__(
        self,
        bar_columns: tuple[str, ...],
        line_columns: tuple[str, ...],
        bar_label_column: str | None = None,
        bar_y_format: str = "millions_eur",
        line_y_format: str = "millions_eur",
        line_axis: str = "primary",
    ) -> None:
        if bar_y_format not in _SUPPORTED_FORMATS:
            raise ValueError(
                f"Unsupported bar_y_format {bar_y_format!r}; expected one of {_SUPPORTED_FORMATS}."
            )
        if line_y_format not in _SUPPORTED_FORMATS:
            raise ValueError(
                f"Unsupported line_y_format {line_y_format!r}; expected one of {_SUPPORTED_FORMATS}."
            )
        if line_axis not in ("primary", "secondary"):
            raise ValueError(
                f"Unsupported line_axis {line_axis!r}; expected 'primary' or 'secondary'."
            )
        self._bar_columns = tuple(bar_columns)
        self._line_columns = tuple(line_columns)
        self._bar_label_column = bar_label_column
        self._bar_y_format = bar_y_format
        self._line_y_format = line_y_format
        self._line_axis = line_axis

    # ------------------------------------------------------------------
    # ChartBuilder API
    # ------------------------------------------------------------------

    def build(self, data: pd.DataFrame, theme: dict, title: str) -> Figure:
        """Build the stacked-bar-plus-line figure.

        Args:
            data: Year-indexed DataFrame with at least the configured
                ``bar_columns`` and ``line_columns``.
            theme: The full chart theme dict.
            title: Chart title.

        Returns:
            A themed :class:`~matplotlib.figure.Figure`.
        """
        if data is None or data.empty:
            return make_no_data_figure(theme, title)
        if not all(c in data.columns for c in self._bar_columns):
            return make_no_data_figure(theme, title)

        colours = theme["colours"]
        bar_cfg = theme["bar"]
        line_cfg = theme["line"]

        x = np.array([int(v) for v in data.index], dtype=int)

        fig = create_themed_figure(theme)
        ax = fig.add_subplot(111)
        apply_axes_theme(ax, theme)

        legend_handles: list = []
        legend_labels: list[str] = []

        # ---- Bars (positive columns above zero, negative columns below) ----
        pos_running = np.zeros_like(x, dtype=float)
        neg_running = np.zeros_like(x, dtype=float)
        for col in self._bar_columns:
            values = pd.to_numeric(data[col], errors="coerce").to_numpy(dtype=float)
            values = np.nan_to_num(values, nan=0.0)
            colour = colours.get(col, colours["primary"])

            pos_part = np.where(values > 0, values, 0.0)
            neg_part = np.where(values < 0, values, 0.0)

            handle = None
            if (pos_part != 0).any():
                handle = ax.bar(
                    x,
                    pos_part,
                    bottom=pos_running,
                    color=colour,
                    alpha=bar_cfg["alpha"],
                    width=bar_cfg["width"],
                    linewidth=bar_cfg["edge_width"],
                )
                pos_running = pos_running + pos_part
            if (neg_part != 0).any():
                neg_handle = ax.bar(
                    x,
                    neg_part,
                    bottom=neg_running,
                    color=colour,
                    alpha=bar_cfg["alpha"],
                    width=bar_cfg["width"],
                    linewidth=bar_cfg["edge_width"],
                )
                neg_running = neg_running + neg_part
                if handle is None:
                    handle = neg_handle
            if handle is not None:
                legend_handles.append(handle)
                legend_labels.append(str(col))

        ax.axhline(
            y=0,
            color=colours["axis_line"],
            linewidth=line_cfg["width_axis"],
        )

        # ---- Lines ----
        line_ax = ax
        if self._line_axis == "secondary" and self._line_columns:
            line_ax = ax.twinx()
            apply_twin_axes_theme(line_ax, theme)

        for col in self._line_columns:
            if col not in data.columns:
                continue
            values = pd.to_numeric(data[col], errors="coerce").to_numpy(dtype=float)
            colour = colours.get(col, colours["accent_line"])
            (line_handle,) = line_ax.plot(
                x,
                values,
                color=colour,
                linewidth=line_cfg.get("width_px", line_cfg.get("width_primary", 1.5)),
                label=str(col),
            )
            legend_handles.append(line_handle)
            legend_labels.append(str(col))

        # ---- Bar labels (e.g. TVPI above each bar) ----
        if self._bar_label_column is not None and self._bar_label_column in data.columns:
            label_values = pd.to_numeric(data[self._bar_label_column], errors="coerce").to_numpy(
                dtype=float
            )
            label_colour = colours.get("tvpi_label", colours["text"])
            font = theme["font"]
            bar_tops = np.maximum(pos_running, 0.0)
            for xi, top, val in zip(x, bar_tops, label_values, strict=True):
                if not np.isfinite(val):
                    continue
                text = self._format_value(val, self._bar_y_format)
                ax.text(
                    xi,
                    top,
                    text,
                    ha="center",
                    va="bottom",
                    color=label_colour,
                    fontsize=font.get("label_size", font["tick_label_size"]),
                    fontfamily=font["family"],
                )

        # ---- X-axis: integer years ----
        ax.set_xticks(list(x))
        ax.tick_params(axis="x", labelrotation=0)

        # ---- Y-axis formatters ----
        self._apply_y_format(ax, self._bar_y_format, theme)
        if self._line_axis == "secondary":
            self._apply_y_format(line_ax, self._line_y_format, theme)

        apply_title(ax, title, theme)
        if legend_labels:
            apply_legend(ax, legend_handles, legend_labels, theme)
        apply_subplots_adjust(fig, theme)
        return fig

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _apply_y_format(self, ax, fmt: str, theme: dict) -> None:
        """Apply the configured tick formatter and clean up scientific notation."""
        if fmt == "millions_eur":
            format_eur_millions_axis(ax, theme)
        elif fmt == "multiple_x":
            ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _pos: f"{v:.2f}x"))
        elif fmt == "percent":
            ax.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=1.0, decimals=1))

    def _format_value(self, value: float, fmt: str) -> str:
        """Format a single bar-label value according to ``fmt``."""
        if fmt == "multiple_x":
            return f"{value:.2f}x"
        if fmt == "percent":
            return f"{value:.1%}"
        return f"{value / 1e6:.1f} M €"
