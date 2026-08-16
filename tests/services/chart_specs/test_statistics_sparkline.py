# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural tests for ``services.chart_specs.statistics_sparkline``."""

from __future__ import annotations

from services.chart_specs import build_sparkline_spec


def test_top_level_keys() -> None:
    spec = build_sparkline_spec([1.0, 1.05, 1.10])
    assert set(spec.keys()) == {"data", "layout", "config"}


def test_single_line_trace() -> None:
    spec = build_sparkline_spec([1.0, 1.05, 1.10])
    assert len(spec["data"]) == 1
    assert spec["data"][0]["type"] == "scatter"
    assert spec["data"][0]["mode"] == "lines"


def test_y_values_match_input() -> None:
    spec = build_sparkline_spec([1.0, 1.05, 1.10])
    assert spec["data"][0]["y"] == [1.0, 1.05, 1.10]


def test_axes_hidden() -> None:
    spec = build_sparkline_spec([1.0, 1.05])
    assert spec["layout"]["xaxis"]["visible"] is False
    assert spec["layout"]["yaxis"]["visible"] is False


def test_transparent_canvas() -> None:
    spec = build_sparkline_spec([1.0, 1.05])
    assert spec["layout"]["paper_bgcolor"] == "rgba(0,0,0,0)"
    assert spec["layout"]["plot_bgcolor"] == "rgba(0,0,0,0)"


def test_hover_disabled_and_static() -> None:
    spec = build_sparkline_spec([1.0, 1.05])
    assert spec["data"][0]["hoverinfo"] == "skip"
    assert spec["config"]["staticPlot"] is True
    assert spec["config"]["displayModeBar"] is False


def test_empty_input_still_produces_valid_spec() -> None:
    spec = build_sparkline_spec([])
    assert len(spec["data"]) == 1
    assert spec["data"][0]["y"] == []


def test_pure_function_deterministic() -> None:
    a = build_sparkline_spec([1.0, 0.95, 1.10])
    b = build_sparkline_spec([1.0, 0.95, 1.10])
    assert a == b
