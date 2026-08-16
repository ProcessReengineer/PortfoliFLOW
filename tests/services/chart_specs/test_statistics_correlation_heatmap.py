# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural tests for ``services.chart_specs.statistics_correlation_heatmap``.

Plotly figures are large dicts; full-fidelity diffs are brittle and
do not surface real regressions. These tests assert the structural
invariants the route consumer relies on: trace type, color scale
stops, annotation count and contents.
"""

from __future__ import annotations

import math

import pandas as pd

from services.chart_specs import build_correlation_heatmap_spec
from services.chart_specs.base import get_chart_theme


def _sample_corr() -> pd.DataFrame:
    return pd.DataFrame(
        [
            [1.00, 0.67, -0.04],
            [0.67, 1.00, 0.21],
            [-0.04, 0.21, 1.00],
        ],
        index=["A", "B", "C"],
        columns=["A", "B", "C"],
    )


def test_top_level_keys() -> None:
    spec = build_correlation_heatmap_spec(_sample_corr())
    assert set(spec.keys()) == {"data", "layout", "config"}


def test_single_heatmap_trace() -> None:
    spec = build_correlation_heatmap_spec(_sample_corr())
    assert len(spec["data"]) == 1
    assert spec["data"][0]["type"] == "heatmap"


def test_zmin_zmax_pin_to_minus_one_plus_one() -> None:
    spec = build_correlation_heatmap_spec(_sample_corr())
    assert spec["data"][0]["zmin"] == -1.0
    assert spec["data"][0]["zmax"] == 1.0


def test_color_scale_has_three_stops_at_minus_one_zero_plus_one() -> None:
    spec = build_correlation_heatmap_spec(_sample_corr())
    cs = spec["data"][0]["colorscale"]
    assert len(cs) == 3
    assert cs[0][0] == 0.0  # corresponds to -1 in normalised space
    assert cs[1][0] == 0.5  # neutral
    assert cs[2][0] == 1.0  # corresponds to +1


def test_axis_labels_are_investment_names_in_order() -> None:
    spec = build_correlation_heatmap_spec(_sample_corr())
    assert spec["data"][0]["x"] == ["A", "B", "C"]
    assert spec["data"][0]["y"] == ["A", "B", "C"]


def test_annotations_one_per_finite_cell() -> None:
    spec = build_correlation_heatmap_spec(_sample_corr())
    annotations = spec["layout"]["annotations"]
    # 3x3 = 9 finite cells.
    assert len(annotations) == 9


def test_annotations_carry_two_decimal_text() -> None:
    spec = build_correlation_heatmap_spec(_sample_corr())
    annotations = spec["layout"]["annotations"]
    texts = {a["text"] for a in annotations}
    assert "0.67" in texts
    assert "-0.04" in texts
    assert "1.00" in texts


def test_high_magnitude_cells_get_white_text() -> None:
    spec = build_correlation_heatmap_spec(_sample_corr())
    # Find the diagonal (1.00) annotation — it must be white.
    diag = [a for a in spec["layout"]["annotations"] if a["text"] == "1.00"]
    assert all(a["font"]["color"] == "#FFFFFF" for a in diag)


def test_low_magnitude_cells_use_theme_text_colour() -> None:
    spec = build_correlation_heatmap_spec(_sample_corr())
    theme_text = get_chart_theme()["colours"]["text"]
    low_cells = [a for a in spec["layout"]["annotations"] if a["text"] == "-0.04"]
    assert low_cells, "expected at least one low-magnitude annotation"
    assert all(a["font"]["color"] == theme_text for a in low_cells)


def test_yaxis_reversed_so_diagonal_runs_top_left_to_bottom_right() -> None:
    spec = build_correlation_heatmap_spec(_sample_corr())
    assert spec["layout"]["yaxis"]["autorange"] == "reversed"


def test_theme_applied_paper_and_plot_bgcolor() -> None:
    theme = get_chart_theme()
    spec = build_correlation_heatmap_spec(_sample_corr())
    assert spec["layout"]["paper_bgcolor"] == theme["colours"]["background"]
    assert spec["layout"]["plot_bgcolor"] == theme["colours"]["plot_area"]


def test_empty_dataframe_yields_empty_trace() -> None:
    spec = build_correlation_heatmap_spec(pd.DataFrame())
    assert len(spec["data"]) == 1
    assert spec["data"][0]["x"] == []
    assert spec["data"][0]["y"] == []
    assert spec["data"][0]["z"] == []
    assert spec["layout"]["annotations"] == []


def test_nan_cells_are_not_annotated() -> None:
    df = pd.DataFrame(
        [[1.00, math.nan], [math.nan, 1.00]],
        index=["A", "B"],
        columns=["A", "B"],
    )
    spec = build_correlation_heatmap_spec(df)
    annotations = spec["layout"]["annotations"]
    # 2 finite cells (the diagonal), 2 NaN cells (off-diagonal).
    assert len(annotations) == 2


def test_pure_function_deterministic() -> None:
    spec_a = build_correlation_heatmap_spec(_sample_corr())
    spec_b = build_correlation_heatmap_spec(_sample_corr())
    assert spec_a == spec_b


def test_title_present() -> None:
    spec = build_correlation_heatmap_spec(_sample_corr())
    assert "Correlation Matrix" in spec["layout"]["title"]["text"]
