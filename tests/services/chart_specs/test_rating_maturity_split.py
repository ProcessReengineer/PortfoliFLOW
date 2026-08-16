# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural tests for ``services.chart_specs.investment_rating_maturity_split``.

These assert the two-panel domain-split invariants the route consumer
relies on: one vertical-bar trace per panel, the fixed canonical bucket
orders rendered in full (missing buckets as zero), the per-panel
annotations, explicit theming of the secondary axis, and empty-input
safety.
"""

from __future__ import annotations

from typing import Any

from services.chart_specs import build_rating_maturity_split_spec
from services.chart_specs.investment_rating_maturity_split import (
    MATURITY_ORDER,
    RATING_ORDER,
)
from services.chart_specs.base import get_chart_theme


def _ratings() -> dict[str, float]:
    # Partial coverage — only some buckets present.
    return {"AAA": 0.20, "A": 0.50, "BBB": 0.30}


def _maturities() -> dict[str, float]:
    return {"1-3y": 0.40, "3-5y": 0.60}


def _trace_on(spec: dict[str, Any], xaxis: str) -> dict[str, Any]:
    matches = [t for t in spec["data"] if t.get("xaxis", "x") == xaxis]
    assert len(matches) == 1
    return matches[0]


def test_top_level_keys() -> None:
    spec = build_rating_maturity_split_spec(_ratings(), _maturities(), "Test Fund")
    assert set(spec.keys()) == {"data", "layout", "config"}


def test_two_bar_traces_one_on_secondary_axes() -> None:
    spec = build_rating_maturity_split_spec(_ratings(), _maturities(), "Test Fund")
    assert len(spec["data"]) == 2
    assert all(t["type"] == "bar" for t in spec["data"])
    maturity = _trace_on(spec, "x2")
    assert maturity["yaxis"] == "y2"


def test_rating_categories_in_canonical_order() -> None:
    spec = build_rating_maturity_split_spec(_ratings(), _maturities(), "Test Fund")
    rating = _trace_on(spec, "x")
    assert rating["x"] == RATING_ORDER


def test_maturity_categories_in_canonical_order() -> None:
    spec = build_rating_maturity_split_spec(_ratings(), _maturities(), "Test Fund")
    maturity = _trace_on(spec, "x2")
    assert maturity["x"] == MATURITY_ORDER


def test_missing_buckets_render_as_zero() -> None:
    spec = build_rating_maturity_split_spec(_ratings(), _maturities(), "Test Fund")
    rating = _trace_on(spec, "x")
    # Full axis: one y value per canonical bucket.
    assert len(rating["y"]) == len(RATING_ORDER)
    # AAA → 20, A → 50, BBB → 30; absent buckets → 0.
    by_bucket = dict(zip(rating["x"], rating["y"]))
    assert by_bucket["AAA"] == 20.0
    assert by_bucket["A"] == 50.0
    assert by_bucket["BBB"] == 30.0
    assert by_bucket["AA"] == 0.0
    assert by_bucket["NR"] == 0.0


def test_value_axes_use_percent_suffix() -> None:
    spec = build_rating_maturity_split_spec(_ratings(), _maturities(), "Test Fund")
    assert spec["layout"]["yaxis"]["ticksuffix"] == "%"
    assert spec["layout"]["yaxis2"]["ticksuffix"] == "%"


def test_axis_domains_set_and_non_overlapping() -> None:
    spec = build_rating_maturity_split_spec(_ratings(), _maturities(), "Test Fund")
    left = spec["layout"]["xaxis"]["domain"]
    right = spec["layout"]["xaxis2"]["domain"]
    assert left[1] <= right[0]


def test_panel_annotations_present() -> None:
    spec = build_rating_maturity_split_spec(_ratings(), _maturities(), "Test Fund")
    texts = {a["text"] for a in spec["layout"]["annotations"]}
    assert {"Credit rating", "Maturity"} <= texts


def test_secondary_axis_is_themed() -> None:
    theme = get_chart_theme()
    spec = build_rating_maturity_split_spec(_ratings(), _maturities(), "Test Fund")
    assert spec["layout"]["xaxis2"]["linecolor"] == theme["colours"]["axis_line"]
    assert "tickfont" in spec["layout"]["xaxis2"]
    assert "tickfont" in spec["layout"]["yaxis2"]


def test_panels_use_distinct_colours() -> None:
    theme = get_chart_theme()
    spec = build_rating_maturity_split_spec(_ratings(), _maturities(), "Test Fund")
    assert _trace_on(spec, "x")["marker"]["color"] == theme["colours"]["primary"]
    assert _trace_on(spec, "x2")["marker"]["color"] == theme["colours"]["secondary"]


def test_empty_inputs_render_full_zero_axes() -> None:
    spec = build_rating_maturity_split_spec({}, {}, "Empty Fund")
    rating = _trace_on(spec, "x")
    maturity = _trace_on(spec, "x2")
    assert rating["x"] == RATING_ORDER
    assert rating["y"] == [0.0] * len(RATING_ORDER)
    assert maturity["x"] == MATURITY_ORDER
    assert maturity["y"] == [0.0] * len(MATURITY_ORDER)
    assert "paper_bgcolor" in spec["layout"]


def test_no_legend() -> None:
    spec = build_rating_maturity_split_spec(_ratings(), _maturities(), "Test Fund")
    assert spec["layout"]["showlegend"] is False


def test_pure_function_deterministic() -> None:
    spec_a = build_rating_maturity_split_spec(_ratings(), _maturities(), "Test Fund")
    spec_b = build_rating_maturity_split_spec(_ratings(), _maturities(), "Test Fund")
    assert spec_a == spec_b
