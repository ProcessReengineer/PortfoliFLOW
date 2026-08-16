# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural tests for ``services.chart_specs.investment_total_return``.

Plotly figures are large dicts; full-fidelity diffs are brittle and
do not surface real regressions. These tests instead assert the
structural invariants that the route consumers rely on: trace count,
trace types, axis titles, theme propagation.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from services.chart_specs import build_total_return_spec
from services.chart_specs.base import get_chart_theme


def _sample_returns() -> pd.Series:
    return pd.Series(
        [0.05, -0.02, 0.04],
        index=[date(2024, 12, 31), date(2025, 3, 31), date(2025, 6, 30)],
    )


def test_top_level_keys() -> None:
    spec = build_total_return_spec(_sample_returns(), "Test Fund")
    assert set(spec.keys()) == {"data", "layout", "config"}


def test_single_bar_trace() -> None:
    spec = build_total_return_spec(_sample_returns(), "Test Fund")
    assert len(spec["data"]) == 1
    assert spec["data"][0]["type"] == "bar"


def test_y_values_are_percent_scaled() -> None:
    spec = build_total_return_spec(_sample_returns(), "Test Fund")
    # 0.05 → 5.0, -0.02 → -2.0, 0.04 → 4.0.
    assert spec["data"][0]["y"] == [5.0, -2.0, 4.0]


def test_title_includes_investment_name() -> None:
    spec = build_total_return_spec(_sample_returns(), "My Fund")
    assert "My Fund" in spec["layout"]["title"]["text"]


def test_x_axis_is_date_typed() -> None:
    spec = build_total_return_spec(_sample_returns(), "Test Fund")
    assert spec["layout"]["xaxis"]["type"] == "date"


def test_y_axis_uses_percent_suffix() -> None:
    spec = build_total_return_spec(_sample_returns(), "Test Fund")
    assert spec["layout"]["yaxis"]["ticksuffix"] == "%"


def test_theme_applied_paper_and_plot_bgcolor() -> None:
    theme = get_chart_theme()
    spec = build_total_return_spec(_sample_returns(), "Test Fund")
    assert spec["layout"]["paper_bgcolor"] == theme["colours"]["background"]
    assert spec["layout"]["plot_bgcolor"] == theme["colours"]["plot_area"]


def test_empty_series_still_produces_one_trace_with_empty_arrays() -> None:
    spec = build_total_return_spec(pd.Series(dtype="float64"), "Empty Fund")
    assert len(spec["data"]) == 1
    assert spec["data"][0]["x"] == []
    assert spec["data"][0]["y"] == []


def test_pure_function_deterministic() -> None:
    spec_a = build_total_return_spec(_sample_returns(), "Test Fund")
    spec_b = build_total_return_spec(_sample_returns(), "Test Fund")
    assert spec_a == spec_b


# ---------------------------------------------------------------------------
# ADR-0113 §1 — unified right axis end ("universe as-of")
# ---------------------------------------------------------------------------


def test_axis_end_omitted_leaves_spec_unchanged() -> None:
    """The default is byte-identical to the pre-ADR-0113 output."""
    assert build_total_return_spec(_sample_returns(), "Test Fund") == build_total_return_spec(
        _sample_returns(), "Test Fund", axis_end=None
    )
    assert (
        "autorangeoptions"
        not in build_total_return_spec(_sample_returns(), "Test Fund")["layout"]["xaxis"]
    )


def test_axis_end_extends_the_x_axis_autorange() -> None:
    spec = build_total_return_spec(_sample_returns(), "Test Fund", axis_end=date(2025, 9, 30))
    assert spec["layout"]["xaxis"]["autorangeoptions"] == {"include": "2025-09-30"}


def test_axis_end_leaves_the_data_untouched() -> None:
    """The axis end extends the range only — it never adds a datapoint."""
    without = build_total_return_spec(_sample_returns(), "Test Fund")
    with_end = build_total_return_spec(_sample_returns(), "Test Fund", axis_end=date(2025, 9, 30))
    assert with_end["data"] == without["data"]


def test_axis_end_not_applied_to_an_empty_figure() -> None:
    spec = build_total_return_spec(
        pd.Series(dtype="float64"), "Empty Fund", axis_end=date(2025, 9, 30)
    )
    assert "autorangeoptions" not in spec["layout"]["xaxis"]
