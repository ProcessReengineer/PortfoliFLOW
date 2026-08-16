# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`services.reporting.chart_builders.LineChartBuilder`."""

from __future__ import annotations

import pandas as pd
from matplotlib.lines import Line2D

from core.chart_theme import get_chart_theme
from services.reporting.chart_builders import LineChartBuilder


def _theme() -> dict:
    return get_chart_theme()


def test_single_column_no_legend() -> None:
    """A single-column DataFrame produces one Line2D and no legend."""
    df = pd.DataFrame(
        {"rebased": [100.0, 102.0, 101.0]},
        index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
    )
    builder = LineChartBuilder(y_label="y")
    fig = builder.build(df, _theme(), "Total Return")

    ax = fig.axes[0]
    data_lines = [ln for ln in ax.lines if ln.get_label() and not ln.get_label().startswith("_")]
    assert len(data_lines) == 1
    assert ax.get_legend() is None


def test_multi_column_legend_present() -> None:
    """A two-column DataFrame produces two Line2Ds and a legend."""
    df = pd.DataFrame(
        {
            "fund": [100.0, 105.0, 102.0],
            "benchmark": [100.0, 101.0, 99.5],
        },
        index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
    )
    builder = LineChartBuilder(y_label="y")
    fig = builder.build(df, _theme(), "Comparison")

    ax = fig.axes[0]
    data_lines = [ln for ln in ax.lines if ln.get_label() and not ln.get_label().startswith("_")]
    assert len(data_lines) == 2
    assert ax.get_legend() is not None


def test_baseline_drawn_as_dashed_line() -> None:
    """``baseline=100.0`` adds a horizontal dashed line at y=100."""
    df = pd.DataFrame(
        {"rebased": [100.0, 102.0]},
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )
    builder = LineChartBuilder(y_label="y", baseline=100.0)
    fig = builder.build(df, _theme(), "Total Return")
    ax = fig.axes[0]

    dashed_at_100 = [
        ln
        for ln in ax.lines
        if isinstance(ln, Line2D)
        and ln.get_linestyle() == "--"
        and len(ln.get_ydata()) >= 1
        and float(ln.get_ydata()[0]) == 100.0
    ]
    assert dashed_at_100, "Expected at least one dashed line at y=100."


def test_empty_dataframe_yields_no_data_figure() -> None:
    """An empty DataFrame yields a no-data figure (no plotted lines)."""
    df = pd.DataFrame(columns=["rebased"])
    builder = LineChartBuilder(y_label="y")
    fig = builder.build(df, _theme(), "Total Return")

    ax = fig.axes[0]
    data_lines = [ln for ln in ax.lines if ln.get_label() and not ln.get_label().startswith("_")]
    assert data_lines == []
