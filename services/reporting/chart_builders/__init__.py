# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Chart builders for the Portfolio Review report.

Each builder produces a :class:`matplotlib.figure.Figure` from a normalised
DataFrame plus the chart theme dict.  Builders contain no Qt code and never
touch the DataStore.
"""

from services.reporting.chart_builders.base import ChartBuilder
from services.reporting.chart_builders.clustered_horizontal_bar_builder import (
    ClusteredHorizontalBarBuilder,
)
from services.reporting.chart_builders.horizontal_bar_builder import (
    HorizontalBarBuilder,
)
from services.reporting.chart_builders.line_chart_builder import LineChartBuilder
from services.reporting.chart_builders.stacked_area_with_line_builder import (
    StackedAreaWithLineBuilder,
)
from services.reporting.chart_builders.stacked_bar_builder import StackedBarBuilder
from services.reporting.chart_builders.stacked_bar_with_line_builder import (
    StackedBarWithLineBuilder,
)
from services.reporting.chart_builders.treemap_builder import TreemapBuilder
from services.reporting.chart_builders.vertical_bar_builder import (
    VerticalBarBuilder,
)

__all__ = [
    "ChartBuilder",
    "StackedBarBuilder",
    "StackedAreaWithLineBuilder",
    "StackedBarWithLineBuilder",
    "ClusteredHorizontalBarBuilder",
    "HorizontalBarBuilder",
    "LineChartBuilder",
    "TreemapBuilder",
    "VerticalBarBuilder",
]
