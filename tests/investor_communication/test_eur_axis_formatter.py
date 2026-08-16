# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :func:`core.chart_helpers.format_eur_millions_axis`."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

from matplotlib.figure import Figure

from core.chart_helpers import format_eur_millions_axis
from core.chart_theme import get_chart_theme


def test_formatter_strips_scientific_notation_for_1e8_range() -> None:
    """A 0..1e8 axis produces ``"100"`` (Mio. €) rather than ``"1.0"`` with ``1e8``."""
    fig = Figure()
    ax = fig.add_subplot(111)
    ax.set_ylim(0, 1e8)

    format_eur_millions_axis(ax, get_chart_theme())

    formatter = ax.yaxis.get_major_formatter()
    text = formatter(1e8, None)
    # 1e8 / 1e6 == 100. Result should be "100" (with a thousands separator if any).
    assert text in {"100", "100,000", "1.0e+08"} or text.replace(",", "") == "100"
    assert "e" not in text.lower() or text == "100"


def test_label_is_set() -> None:
    """The y-axis label is set to ``"Mio. €"`` to make the unit explicit."""
    fig = Figure()
    ax = fig.add_subplot(111)
    format_eur_millions_axis(ax, get_chart_theme())
    assert ax.get_ylabel() == "Mio. €"
