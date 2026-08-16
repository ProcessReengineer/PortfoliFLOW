# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``build_benchmark_saa_hypothetical_spec`` (Stage c).

Pure-spec tests: no DB, no FastAPI. Covers the Phase-1b Stage-c
polish per ADR-0062 §6 (headline annotation), the colour separation
of the two SAA lines, and the excess-shading baseline/fill traces.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from services.chart_specs.base import get_chart_theme
from services.chart_specs.benchmark_saa_hypothetical import (
    build_benchmark_saa_hypothetical_spec,
)


@dataclass(frozen=True)
class _EffectsStub:
    actual_cumulative_endpoint: float | None
    saa_x_benchmark_cumulative_endpoint: float | None
    saa_x_composite_cumulative_endpoint: float | None
    allocation_effect_pp: float | None
    selection_effect_pp: float | None


def _ts_index(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2023-01-31", periods=n, freq="ME")


def _populated_series() -> tuple[pd.Series, pd.Series, pd.Series]:
    idx = _ts_index(6)
    actual = pd.Series([0.02, 0.01, -0.005, 0.015, 0.01, 0.008], index=idx)
    bench = pd.Series([0.015, 0.005, 0.0, 0.012, 0.008, 0.006], index=idx)
    comp = pd.Series([0.018, 0.008, -0.002, 0.014, 0.009, 0.007], index=idx)
    return actual, bench, comp


def _find_named_trace(spec: dict, name: str) -> dict | None:
    for trace in spec["data"]:
        if trace.get("name") == name:
            return trace
    return None


# ---------------------------------------------------------------------------
# Headline annotation (ADR-0062 §6)
# ---------------------------------------------------------------------------


def test_spec_emits_headline_annotation_when_effects_provided() -> None:
    """A non-None effects with a non-None allocation_pp produces an annotation."""
    actual, bench, comp = _populated_series()
    effects = _EffectsStub(
        actual_cumulative_endpoint=1.485,
        saa_x_benchmark_cumulative_endpoint=0.048,
        saa_x_composite_cumulative_endpoint=0.060,
        allocation_effect_pp=143.7,
        selection_effect_pp=142.5,
    )
    spec = build_benchmark_saa_hypothetical_spec(
        actual=actual,
        saa_x_benchmark=bench,
        saa_x_composite=comp,
        saa_label="Tangency — Standard 2026",
        effects=effects,
    )
    annotations = spec["layout"].get("annotations", [])
    assert annotations, "expected a headline annotation"
    headline = annotations[0]
    assert headline["xanchor"] == "right"
    assert headline["x"] == 1.0
    assert "Allocation effect" in headline["text"]
    assert "+143.7pp" in headline["text"]


def test_spec_omits_annotation_when_effects_is_none() -> None:
    """No effects → no headline annotation in the layout."""
    actual, bench, comp = _populated_series()
    spec = build_benchmark_saa_hypothetical_spec(
        actual=actual,
        saa_x_benchmark=bench,
        saa_x_composite=comp,
        saa_label="X",
        effects=None,
    )
    # Either there are no annotations at all, or none are the
    # headline (no headline annotation is identifiable by its
    # right-anchored "Allocation effect" copy).
    annotations = spec["layout"].get("annotations", [])
    headlines = [a for a in annotations if "Allocation effect" in str(a.get("text", ""))]
    assert headlines == []


def test_spec_omits_annotation_when_allocation_pp_is_none() -> None:
    """Effects with allocation_pp=None → still no headline annotation."""
    actual, bench, comp = _populated_series()
    effects = _EffectsStub(
        actual_cumulative_endpoint=0.15,
        saa_x_benchmark_cumulative_endpoint=None,
        saa_x_composite_cumulative_endpoint=None,
        allocation_effect_pp=None,
        selection_effect_pp=None,
    )
    spec = build_benchmark_saa_hypothetical_spec(
        actual=actual,
        saa_x_benchmark=bench,
        saa_x_composite=comp,
        saa_label="X",
        effects=effects,
    )
    annotations = spec["layout"].get("annotations", [])
    headlines = [a for a in annotations if "Allocation effect" in str(a.get("text", ""))]
    assert headlines == []


def test_spec_annotation_colour_codes_negative_allocation_effect() -> None:
    """Negative allocation → the clause uses negative_bar colour."""
    actual, bench, comp = _populated_series()
    colours = get_chart_theme()["colours"]
    effects = _EffectsStub(
        actual_cumulative_endpoint=0.04,
        saa_x_benchmark_cumulative_endpoint=0.10,
        saa_x_composite_cumulative_endpoint=0.08,
        allocation_effect_pp=-6.0,
        selection_effect_pp=-4.0,
    )
    spec = build_benchmark_saa_hypothetical_spec(
        actual=actual,
        saa_x_benchmark=bench,
        saa_x_composite=comp,
        saa_label="X",
        effects=effects,
    )
    headline = spec["layout"]["annotations"][0]["text"]
    assert colours["negative_bar"] in headline
    # True minus sign is used for the formatted value.
    assert "−6.0pp" in headline


# ---------------------------------------------------------------------------
# Colour separation
# ---------------------------------------------------------------------------


def test_saa_composite_trace_uses_accent_line_colour() -> None:
    """SAA × Composite line moves from neutral text → accent_line orange."""
    actual, bench, comp = _populated_series()
    colours = get_chart_theme()["colours"]
    spec = build_benchmark_saa_hypothetical_spec(
        actual=actual,
        saa_x_benchmark=bench,
        saa_x_composite=comp,
        saa_label="X",
    )
    composite = _find_named_trace(spec, "SAA × Composite")
    assert composite is not None
    assert composite["line"]["color"] == colours["accent_line"]
    assert composite["line"]["dash"] == "dot"


def test_actual_and_saa_x_benchmark_keep_primary_and_secondary() -> None:
    """Actual stays red (primary); SAA × Benchmark stays blue (secondary)."""
    actual, bench, comp = _populated_series()
    colours = get_chart_theme()["colours"]
    spec = build_benchmark_saa_hypothetical_spec(
        actual=actual,
        saa_x_benchmark=bench,
        saa_x_composite=comp,
        saa_label="X",
    )
    actual_trace = _find_named_trace(spec, "Actual Portfolio")
    bench_trace = _find_named_trace(spec, "SAA × Benchmark")
    assert actual_trace is not None
    assert bench_trace is not None
    assert actual_trace["line"]["color"] == colours["primary"]
    assert bench_trace["line"]["color"] == colours["secondary"]
    assert bench_trace["line"]["dash"] == "dash"


# ---------------------------------------------------------------------------
# Deterministic height
# ---------------------------------------------------------------------------


def test_full_data_spec_pins_deterministic_height() -> None:
    """Full-data spec carries an explicit layout.height to stop autosize overflow."""
    actual, bench, comp = _populated_series()
    spec = build_benchmark_saa_hypothetical_spec(
        actual=actual,
        saa_x_benchmark=bench,
        saa_x_composite=comp,
        saa_label="X",
    )
    assert spec["layout"]["height"] == 480


# ---------------------------------------------------------------------------
# Excess shading
# ---------------------------------------------------------------------------


def test_spec_emits_shading_baseline_and_fill_traces() -> None:
    """Both lines populated → baseline + tonexty fill traces emitted."""
    actual, bench, comp = _populated_series()
    spec = build_benchmark_saa_hypothetical_spec(
        actual=actual,
        saa_x_benchmark=bench,
        saa_x_composite=comp,
        saa_label="X",
    )
    fill_traces = [t for t in spec["data"] if t.get("fill") == "tonexty"]
    assert len(fill_traces) == 1
    fill = fill_traces[0]
    assert fill["showlegend"] is False
    assert fill["hoverinfo"] == "skip"
    assert fill["line"]["width"] == 0
    # Single positive tint (positive_bar at alpha 0.18) regardless of sign.
    assert fill["fillcolor"].startswith("rgba(")
    assert "0.18" in fill["fillcolor"]


def test_spec_omits_shading_when_actual_empty() -> None:
    """Empty Actual → no shading traces."""
    _, bench, comp = _populated_series()
    spec = build_benchmark_saa_hypothetical_spec(
        actual=pd.Series(dtype="float64"),
        saa_x_benchmark=bench,
        saa_x_composite=comp,
        saa_label="X",
    )
    fill_traces = [t for t in spec["data"] if t.get("fill") == "tonexty"]
    assert fill_traces == []


def test_spec_omits_shading_when_benchmark_empty() -> None:
    """Empty SAA × Benchmark → no shading traces."""
    actual, _, comp = _populated_series()
    spec = build_benchmark_saa_hypothetical_spec(
        actual=actual,
        saa_x_benchmark=pd.Series(dtype="float64"),
        saa_x_composite=comp,
        saa_label="X",
    )
    fill_traces = [t for t in spec["data"] if t.get("fill") == "tonexty"]
    assert fill_traces == []


# ---------------------------------------------------------------------------
# Legend repositioning
# ---------------------------------------------------------------------------


def test_legend_repositioned_inside_plot_area() -> None:
    """Legend sits top-left inside the plot area on a semi-transparent panel."""
    actual, bench, comp = _populated_series()
    spec = build_benchmark_saa_hypothetical_spec(
        actual=actual,
        saa_x_benchmark=bench,
        saa_x_composite=comp,
        saa_label="X",
    )
    legend = spec["layout"]["legend"]
    assert legend["x"] == 0.02
    assert legend["y"] == 0.98
    assert legend["xanchor"] == "left"
    assert legend["yanchor"] == "top"
    assert legend["bgcolor"].startswith("rgba(")


# ---------------------------------------------------------------------------
# Empty-state path still works (regression)
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_state_spec() -> None:
    empty = pd.Series(dtype="float64")
    spec = build_benchmark_saa_hypothetical_spec(
        actual=empty,
        saa_x_benchmark=empty,
        saa_x_composite=empty,
        saa_label="X",
    )
    # Empty-state has no series traces.
    assert spec["data"] == []
    # Empty-state has its own centred annotation.
    annotations = spec["layout"]["annotations"]
    assert any(a.get("text") == "No aligned data for the selected SAA" for a in annotations)
