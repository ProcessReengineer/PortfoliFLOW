# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural tests for ``services.chart_specs.benchmark_investment_total_return``.

The hero tile of both listed archetypes: investment and benchmark
cumulative-return lines plus the filled excess area. These tests cover
the ADR-0113 §1 shared axis end and the empty-state contract it must
not disturb — the "No aligned observations" figure hides both axes, so
pinning a date onto its hidden x-axis would be meaningless.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from services.chart_specs import build_benchmark_investment_total_return_spec


def _sample_series() -> tuple[pd.Series, pd.Series, pd.Series]:
    index = [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30)]
    investment = pd.Series([0.02, 0.05, 0.09], index=index)
    benchmark = pd.Series([0.01, 0.04, 0.07], index=index)
    return investment, benchmark, investment - benchmark


def _empty_series() -> tuple[pd.Series, pd.Series, pd.Series]:
    empty = pd.Series(dtype="float64")
    return empty, empty.copy(), empty.copy()


def test_three_traces_on_populated_input() -> None:
    investment, benchmark, excess = _sample_series()
    spec = build_benchmark_investment_total_return_spec(
        "Fund X", "MSCI World", investment, benchmark, excess
    )
    assert [trace["name"] for trace in spec["data"]] == ["Fund X", "MSCI World", "Excess"]


def test_axis_end_omitted_leaves_spec_unchanged() -> None:
    """The default is byte-identical to the pre-ADR-0113 output."""
    investment, benchmark, excess = _sample_series()
    baseline = build_benchmark_investment_total_return_spec(
        "Fund X", "MSCI World", investment, benchmark, excess
    )
    assert baseline == build_benchmark_investment_total_return_spec(
        "Fund X", "MSCI World", investment, benchmark, excess, axis_end=None
    )
    assert "autorangeoptions" not in baseline["layout"]["xaxis"]


def test_axis_end_extends_the_x_axis_autorange() -> None:
    investment, benchmark, excess = _sample_series()
    spec = build_benchmark_investment_total_return_spec(
        "Fund X", "MSCI World", investment, benchmark, excess, axis_end=date(2025, 11, 14)
    )
    assert spec["layout"]["xaxis"]["autorangeoptions"] == {"include": "2025-11-14"}


def test_axis_end_leaves_the_data_untouched() -> None:
    """ADR-0113 §6: the drawn line still ends on its own monthly grid."""
    investment, benchmark, excess = _sample_series()
    without = build_benchmark_investment_total_return_spec(
        "Fund X", "MSCI World", investment, benchmark, excess
    )
    with_end = build_benchmark_investment_total_return_spec(
        "Fund X", "MSCI World", investment, benchmark, excess, axis_end=date(2025, 11, 14)
    )
    assert with_end["data"] == without["data"]
    assert with_end["data"][0]["x"][-1].startswith("2025-09-30")


def test_series_of_different_lengths_keep_their_own_x_arrays() -> None:
    """ADR-0113 §5: the builder assumes no shared index across the three."""
    investment = pd.Series(
        [0.02, 0.05, 0.09, 0.11],
        index=[date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31)],
    )
    benchmark = pd.Series([0.01, 0.04], index=[date(2025, 3, 31), date(2025, 6, 30)])
    excess = pd.Series([0.01, 0.01], index=[date(2025, 3, 31), date(2025, 6, 30)])

    spec = build_benchmark_investment_total_return_spec(
        "Fund X", "MSCI World", investment, benchmark, excess
    )

    inv_trace, bench_trace, excess_trace = spec["data"]
    assert len(inv_trace["x"]) == 4
    assert len(bench_trace["x"]) == len(excess_trace["x"]) == 2
    assert inv_trace["x"][-1].startswith("2025-12-31")
    assert bench_trace["x"][-1].startswith("2025-06-30")
    assert excess_trace["x"][-1].startswith("2025-06-30")
    # No padding, no reindexing — the y arrays match their own x arrays.
    assert inv_trace["y"] == [0.02, 0.05, 0.09, 0.11]
    assert bench_trace["y"] == [0.01, 0.04]


def test_axis_end_never_reaches_the_empty_state_figure() -> None:
    """The empty state hides both axes; it must not gain an axis range."""
    investment, benchmark, excess = _empty_series()
    spec = build_benchmark_investment_total_return_spec(
        "Fund X", "MSCI World", investment, benchmark, excess, axis_end=date(2025, 11, 14)
    )
    assert spec["data"] == []
    assert spec["layout"]["xaxis"]["visible"] is False
    assert "autorangeoptions" not in spec["layout"]["xaxis"]
    assert spec["layout"]["annotations"][0]["text"] == "No aligned observations"
