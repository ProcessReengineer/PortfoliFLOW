# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`services.reporting.chart_builders.StackedBarWithLineBuilder`."""

from __future__ import annotations

import matplotlib.ticker as mticker
import pandas as pd

from core.chart_theme import get_chart_theme
from services.reporting.chart_builders import StackedBarWithLineBuilder


def _multiples_ts_config() -> StackedBarWithLineBuilder:
    """The exact config used by the engine for the multiples-timeseries chart."""
    return StackedBarWithLineBuilder(
        bar_columns=("dpi", "rvpi"),
        line_columns=("irr",),
        bar_label_column="tvpi",
        bar_y_format="multiple_x",
        line_y_format="percent",
        line_axis="secondary",
    )


def test_secondary_axis_is_present_for_multiples_ts_config() -> None:
    """When line_axis='secondary' the figure has a twin y-axis."""
    df = pd.DataFrame(
        {
            "dpi": [0.0, 0.1, 0.5],
            "rvpi": [1.2, 1.3, 1.0],
            "tvpi": [1.2, 1.4, 1.5],
            "irr": [0.0, 0.05, 0.12],
        },
        index=[2022, 2023, 2024],
    )
    fig = _multiples_ts_config().build(df, get_chart_theme(), "Multiples TS")
    assert len(fig.axes) >= 2

    primary, secondary = fig.axes[0], fig.axes[1]
    # Multiple_x formatter on the primary axis: emits "1.42x"-style labels.
    assert isinstance(primary.yaxis.get_major_formatter(), mticker.FuncFormatter)
    primary_label = primary.yaxis.get_major_formatter()(1.42, None)
    assert primary_label.endswith("x")
    # Percent formatter on the secondary axis.
    assert isinstance(secondary.yaxis.get_major_formatter(), mticker.PercentFormatter)


def test_cashflow_nav_config_uses_primary_axis_only() -> None:
    """When line_axis='primary' the figure has a single y-axis."""
    builder = StackedBarWithLineBuilder(
        bar_columns=("calls", "distributions"),
        line_columns=("nav", "ncg"),
        bar_y_format="millions_eur",
        line_y_format="millions_eur",
        line_axis="primary",
    )
    df = pd.DataFrame(
        {
            "calls": [-100.0, -50.0, 0.0],
            "distributions": [0.0, 30.0, 50.0],
            "nav": [120.0, 150.0, 180.0],
            "ncg": [20.0, 30.0, 50.0],
        },
        index=[2022, 2023, 2024],
    )
    fig = builder.build(df, get_chart_theme(), "Cashflow + NAV")
    assert len(fig.axes) == 1


def test_invalid_format_raises() -> None:
    """An unsupported bar_y_format raises ``ValueError`` at construction time."""
    try:
        StackedBarWithLineBuilder(
            bar_columns=("a",),
            line_columns=("b",),
            bar_y_format="bogus",
        )
    except ValueError:
        return
    raise AssertionError("Expected ValueError for unsupported format")
