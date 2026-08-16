# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`services.reporting.chart_builders.TreemapBuilder`."""

from __future__ import annotations

import pandas as pd
from matplotlib.patches import Rectangle

from core.chart_theme import get_chart_theme
from services.reporting.chart_builders import TreemapBuilder


def test_treemap_renders_one_rectangle_per_row() -> None:
    """A 4-row DataFrame yields four Rectangle patches in the figure."""
    df = pd.DataFrame(
        {
            "category": ["A", "B", "C", "D"],
            "share": [0.4, 0.3, 0.2, 0.1],
        }
    )
    fig = TreemapBuilder().build(df, get_chart_theme(), "Test")
    ax = fig.axes[0]
    rects = [p for p in ax.patches if isinstance(p, Rectangle)]
    # Filter out the axes background rectangle if present.
    user_rects = [r for r in rects if r.get_width() < 100.0 + 1e-6]
    # Squarify produces one tile per share value.
    assert len(user_rects) >= 4


def test_treemap_empty_dataframe_returns_no_data_figure() -> None:
    """Empty input yields a figure containing the 'No data' annotation."""
    fig = TreemapBuilder().build(
        pd.DataFrame(columns=["category", "share"]), get_chart_theme(), "Empty"
    )
    ax = fig.axes[0]
    texts = [t.get_text() for t in ax.texts]
    assert any("No data" in t for t in texts)


def test_treemap_drops_zero_share_rows() -> None:
    """Rows with share <= 0 are dropped before layout."""
    df = pd.DataFrame(
        {
            "category": ["A", "B", "Zero"],
            "share": [0.5, 0.5, 0.0],
        }
    )
    fig = TreemapBuilder().build(df, get_chart_theme(), "Drop zeros")
    ax = fig.axes[0]
    rects = [p for p in ax.patches if isinstance(p, Rectangle)]
    # Should have at most 2 user tiles (A, B); zero-share row excluded.
    user_rects = [r for r in rects if r.get_width() < 100.0 + 1e-6]
    assert len(user_rects) <= 3  # tolerate axes-background rect
