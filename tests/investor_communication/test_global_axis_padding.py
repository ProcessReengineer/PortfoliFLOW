# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :func:`core.chart_helpers.compute_left_padding`."""

from __future__ import annotations

from core.chart_helpers import compute_left_padding
from core.chart_theme import get_chart_theme


def test_long_labels_increase_padding() -> None:
    """Longer labels yield a strictly larger padding fraction than short labels."""
    theme = get_chart_theme()
    short = compute_left_padding(["A", "B", "C"], theme)
    long_labels = ["Consumer Discretionary Sector"] * 3
    long = compute_left_padding(long_labels, theme)
    assert long > short


def test_padding_is_capped_at_40_percent() -> None:
    """The returned padding is always within ``[0.0, 0.40]``."""
    theme = get_chart_theme()
    extreme = compute_left_padding(["X" * 200], theme)
    assert 0.0 <= extreme <= 0.40


def test_empty_labels_return_base_padding() -> None:
    """No labels falls back to the theme's ``layout.padding_left``."""
    theme = get_chart_theme()
    base = compute_left_padding([], theme)
    assert base == theme["layout"]["padding_left"]
