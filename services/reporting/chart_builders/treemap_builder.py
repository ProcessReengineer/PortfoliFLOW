# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Treemap chart builder.

Renders a treemap from a 2-column DataFrame ``["category", "share"]``.  Tile
sizes are proportional to ``share``; tiles cycle through a colour palette
read from the theme (``chart.treemap_palette`` resolved against
``colours``).  Each tile shows the category label and its share as a
percentage when there is enough room.
"""

from __future__ import annotations

import logging

import pandas as pd
import squarify
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

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


_TILE_W: int = 30
_TILE_H: int = 15


class TreemapBuilder(ChartBuilder):
    """Treemap renderer with theme-aware colours and labels.

    Args:
        category_column: Name of the column holding category labels.
        share_column: Name of the column holding numeric shares.
    """

    def __init__(
        self,
        category_column: str = "category",
        share_column: str = "share",
    ) -> None:
        self._category_column = category_column
        self._share_column = share_column

    def build(self, data: pd.DataFrame, theme: dict, title: str) -> Figure:
        """Build the treemap figure.

        Args:
            data: DataFrame with the configured category and share columns.
                Rows with ``share <= 0`` are dropped.  Empty input yields a
                no-data figure.
            theme: The full chart theme dict.
            title: Chart title.

        Returns:
            A themed :class:`~matplotlib.figure.Figure`.
        """
        if (
            data is None
            or data.empty
            or self._category_column not in data.columns
            or self._share_column not in data.columns
        ):
            return make_no_data_figure(theme, title)

        categories = [str(v) for v in data[self._category_column].tolist()]
        shares = pd.to_numeric(data[self._share_column], errors="coerce").to_numpy(dtype=float)
        mask = (shares > 0.0) & pd.Series(shares).notna().to_numpy()
        if not mask.any():
            return make_no_data_figure(theme, title)

        categories = [c for c, keep in zip(categories, mask, strict=True) if keep]
        shares = shares[mask]

        colours = theme["colours"]
        font = theme["font"]
        chart_cfg = theme.get("chart", {})

        palette_keys: list[str] = list(chart_cfg.get("treemap_palette") or ["primary"])
        palette = [colours.get(key, colours["primary"]) for key in palette_keys]

        # Layout tiles in a unit square.
        normed = squarify.normalize_sizes(list(shares), 100.0, 100.0)
        rects = squarify.squarify(normed, 0.0, 0.0, 100.0, 100.0)

        fig = create_themed_figure(theme)
        ax = fig.add_subplot(111)
        apply_axes_theme(ax, theme)
        ax.set_xlim(0, 100)
        ax.set_ylim(0, 100)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.grid(False)
        ax.invert_yaxis()  # Top-to-bottom reading order.

        for idx, (rect, label, share) in enumerate(zip(rects, categories, shares, strict=True)):
            colour = palette[idx % len(palette)]
            patch = Rectangle(
                (rect["x"], rect["y"]),
                rect["dx"],
                rect["dy"],
                facecolor=colour,
                edgecolor=colours["background"],
                linewidth=1.0,
                alpha=0.9,
            )
            ax.add_patch(patch)

            # Convert tile dimensions to pixels to decide whether to draw a
            # label.  We approximate using the figure DPI and the data->axes
            # mapping (axes is unit-square 0..100).
            ax_bbox = ax.get_window_extent()
            tile_w_px = (rect["dx"] / 100.0) * ax_bbox.width
            tile_h_px = (rect["dy"] / 100.0) * ax_bbox.height
            if tile_w_px < _TILE_W or tile_h_px < _TILE_H:
                continue

            cx = rect["x"] + rect["dx"] / 2.0
            cy = rect["y"] + rect["dy"] / 2.0
            text = f"{label}\n{share * 100:.1f} %"
            ax.text(
                cx,
                cy,
                text,
                ha="center",
                va="center",
                color=colours["text"],
                fontsize=font.get("label_size", font["tick_label_size"]),
                fontfamily=font["family"],
            )

        apply_title(ax, title, theme)
        # Reduce horizontal padding — treemap fills the axes.
        left_pad = compute_left_padding(categories, theme)
        fig.subplots_adjust(
            top=theme["layout"]["padding_top"],
            bottom=theme["layout"]["padding_bottom"],
            left=max(0.04, left_pad - 0.06),
            right=theme["layout"]["padding_right"],
        )
        # Run apply_subplots_adjust as a no-op fallback — the explicit
        # subplots_adjust above takes precedence but this keeps the helper
        # call uniform with the other builders.
        _ = apply_subplots_adjust  # silence unused-import for future maintainers
        return fig
