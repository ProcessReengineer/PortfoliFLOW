# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural tests for ``services.chart_specs.investment_composition_split``.

These assert the two-panel domain-split invariants the route consumer
relies on: one bar trace per panel, non-overlapping axis domains,
horizontal orientation, the per-panel annotations, explicit theming of
the secondary axis, and per-panel empty-input safety.
"""

from __future__ import annotations

from typing import Any

from services.chart_specs import build_composition_split_spec
from services.chart_specs.base import get_chart_theme


def _sectors() -> dict[str, float]:
    return {"Technology": 0.40, "Financials": 0.35, "Healthcare": 0.25}


def _regions() -> dict[str, float]:
    return {"North America": 0.60, "Europe": 0.30, "Asia": 0.10}


def _trace_on(spec: dict[str, Any], xaxis: str) -> dict[str, Any]:
    """Return the single trace bound to the given x-axis (``x`` or ``x2``)."""
    matches = [t for t in spec["data"] if t.get("xaxis", "x") == xaxis]
    assert len(matches) == 1
    return matches[0]


def test_top_level_keys() -> None:
    spec = build_composition_split_spec(_sectors(), _regions(), "Test Fund")
    assert set(spec.keys()) == {"data", "layout", "config"}


def test_two_bar_traces_one_on_secondary_axes() -> None:
    spec = build_composition_split_spec(_sectors(), _regions(), "Test Fund")
    assert len(spec["data"]) == 2
    assert all(t["type"] == "bar" for t in spec["data"])
    region = _trace_on(spec, "x2")
    assert region["yaxis"] == "y2"


def test_orientation_is_horizontal() -> None:
    spec = build_composition_split_spec(_sectors(), _regions(), "Test Fund")
    assert all(t["orientation"] == "h" for t in spec["data"])


def test_panels_sorted_descending_by_weight() -> None:
    spec = build_composition_split_spec(_sectors(), _regions(), "Test Fund")
    sector = _trace_on(spec, "x")
    # Descending: 40, 35, 25.
    assert sector["x"] == [40.0, 35.0, 25.0]
    assert sector["y"] == ["Technology", "Financials", "Healthcare"]


def test_axis_domains_set_and_non_overlapping() -> None:
    spec = build_composition_split_spec(_sectors(), _regions(), "Test Fund")
    left = spec["layout"]["xaxis"]["domain"]
    right = spec["layout"]["xaxis2"]["domain"]
    assert left[1] <= right[0]  # gap, no overlap


def test_value_axes_use_percent_suffix() -> None:
    spec = build_composition_split_spec(_sectors(), _regions(), "Test Fund")
    assert spec["layout"]["xaxis"]["ticksuffix"] == "%"
    assert spec["layout"]["xaxis2"]["ticksuffix"] == "%"


def test_panel_annotations_present() -> None:
    spec = build_composition_split_spec(_sectors(), _regions(), "Test Fund")
    texts = {a["text"] for a in spec["layout"]["annotations"]}
    assert {"Sector", "Region"} <= texts


def test_secondary_axis_is_themed() -> None:
    theme = get_chart_theme()
    spec = build_composition_split_spec(_sectors(), _regions(), "Test Fund")
    xaxis2 = spec["layout"]["xaxis2"]
    assert xaxis2["linecolor"] == theme["colours"]["axis_line"]
    assert "tickfont" in xaxis2
    assert "tickfont" in spec["layout"]["yaxis2"]


def test_panels_use_distinct_colours() -> None:
    theme = get_chart_theme()
    spec = build_composition_split_spec(_sectors(), _regions(), "Test Fund")
    assert _trace_on(spec, "x")["marker"]["color"] == theme["colours"]["primary"]
    assert _trace_on(spec, "x2")["marker"]["color"] == theme["colours"]["secondary"]


def test_empty_sector_other_panel_still_renders() -> None:
    spec = build_composition_split_spec({}, _regions(), "Test Fund")
    assert len(spec["data"]) == 2
    assert _trace_on(spec, "x")["x"] == []  # sector empty
    assert _trace_on(spec, "x2")["x"] == [60.0, 30.0, 10.0]  # region renders


def test_both_empty_still_valid_figure() -> None:
    spec = build_composition_split_spec({}, {}, "Empty Fund")
    assert len(spec["data"]) == 2
    assert all(t["x"] == [] for t in spec["data"])
    assert "paper_bgcolor" in spec["layout"]


def test_no_legend() -> None:
    spec = build_composition_split_spec(_sectors(), _regions(), "Test Fund")
    assert spec["layout"]["showlegend"] is False


def test_pure_function_deterministic() -> None:
    spec_a = build_composition_split_spec(_sectors(), _regions(), "Test Fund")
    spec_b = build_composition_split_spec(_sectors(), _regions(), "Test Fund")
    assert spec_a == spec_b
