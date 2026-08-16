# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.analytics.benchmark_comparison``.

Pure-function tests — no DB, no Qt, no FastAPI. Each test builds a
deterministic pandas series and asserts numerical output via
``pytest.approx``. Four conceptual test groups:

    Group A — :func:`compute_benchmark_comparison`
    Group B — :func:`compute_asset_class_composites`
    Group C — :func:`compute_saa_hypothetical_series`
    Group D — internal helpers and module-purity guard
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from services.analytics.benchmark_comparison import (
    AssetClassCompositeSeries,
    BenchmarkComparisonBundle,
    BenchmarkComparisonMetrics,
    SAAHypotheticalSeries,
    _align_monthly_series,
    _resample_daily_to_monthly_return,
    compute_asset_class_composites,
    compute_benchmark_comparison,
    compute_saa_hypothetical_series,
)


# ---------------------------------------------------------------------------
# Fixtures (plain helpers — small and deterministic).
# ---------------------------------------------------------------------------


@pytest.fixture
def month_end_index_24() -> pd.DatetimeIndex:
    """Two years of month-end timestamps starting 2023-01-31."""
    return pd.date_range("2023-01-31", periods=24, freq="ME")


@pytest.fixture
def month_end_index_12() -> pd.DatetimeIndex:
    """Twelve month-end timestamps starting 2024-01-31."""
    return pd.date_range("2024-01-31", periods=12, freq="ME")


@pytest.fixture
def zero_series_24(month_end_index_24: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(np.zeros(24, dtype="float64"), index=month_end_index_24)


# ---------------------------------------------------------------------------
# Group A — compute_benchmark_comparison
# ---------------------------------------------------------------------------


def test_perfect_correlation_yields_beta_one_alpha_zero_rsq_one(
    month_end_index_24: pd.DatetimeIndex,
) -> None:
    rng = np.random.default_rng(42)
    benchmark_vals = rng.uniform(-0.04, 0.04, size=24)
    benchmark = pd.Series(benchmark_vals, index=month_end_index_24)
    investment = benchmark.copy()
    risk_free = pd.Series(np.zeros(24), index=month_end_index_24)

    result = compute_benchmark_comparison(investment, benchmark, risk_free, "inv", "bm")

    assert result.metrics.beta == pytest.approx(1.0, abs=1e-9)
    assert result.metrics.alpha_annualised == pytest.approx(0.0, abs=1e-9)
    assert result.metrics.r_squared == pytest.approx(1.0, abs=1e-9)
    assert result.metrics.excess_return_total == pytest.approx(0.0, abs=1e-12)
    assert result.metrics.n_observations == 24


def test_constant_investment_zero_benchmark_yields_zero_metrics(
    zero_series_24: pd.Series,
) -> None:
    result = compute_benchmark_comparison(
        zero_series_24, zero_series_24, zero_series_24, "inv", "bm"
    )

    assert result.metrics.excess_return_total == pytest.approx(0.0, abs=1e-12)
    assert result.metrics.excess_return_annualised == pytest.approx(0.0, abs=1e-12)
    assert result.metrics.alpha_annualised == pytest.approx(0.0, abs=1e-12)
    assert math.isnan(result.metrics.beta)
    assert math.isnan(result.metrics.r_squared)
    assert result.metrics.tracking_error_annualised == pytest.approx(0.0, abs=1e-12)
    assert math.isnan(result.metrics.information_ratio)
    assert math.isnan(result.metrics.up_capture_ratio)
    assert math.isnan(result.metrics.down_capture_ratio)
    assert math.isnan(result.metrics.sharpe_investment)
    assert math.isnan(result.metrics.sharpe_benchmark)
    assert math.isnan(result.metrics.sharpe_difference)


def test_excess_return_arithmetic_definition(
    month_end_index_12: pd.DatetimeIndex,
) -> None:
    investment_vals = np.array(
        [0.02, 0.01, 0.03, -0.01, 0.04, 0.00, 0.02, 0.01, 0.03, -0.02, 0.01, 0.02]
    )
    benchmark_vals = np.array(
        [0.01, 0.00, 0.02, -0.02, 0.03, 0.01, 0.01, 0.00, 0.02, -0.03, 0.00, 0.01]
    )
    investment = pd.Series(investment_vals, index=month_end_index_12)
    benchmark = pd.Series(benchmark_vals, index=month_end_index_12)
    risk_free = pd.Series(np.zeros(12), index=month_end_index_12)

    result = compute_benchmark_comparison(investment, benchmark, risk_free, "inv", "bm")

    expected_excess_annualised = float((investment_vals - benchmark_vals).mean() * 12.0)
    assert result.metrics.excess_return_annualised == pytest.approx(
        expected_excess_annualised, abs=1e-12
    )


def test_information_ratio_formula(
    month_end_index_12: pd.DatetimeIndex,
) -> None:
    # Investment alternates between +1% and +2%; benchmark is flat.
    investment_vals = np.array(
        [0.01, 0.02, 0.01, 0.02, 0.01, 0.02, 0.01, 0.02, 0.01, 0.02, 0.01, 0.02]
    )
    investment = pd.Series(investment_vals, index=month_end_index_12)
    benchmark = pd.Series(np.zeros(12), index=month_end_index_12)
    risk_free = pd.Series(np.zeros(12), index=month_end_index_12)

    result = compute_benchmark_comparison(investment, benchmark, risk_free, "inv", "bm")

    excess = investment_vals - 0.0
    expected_ir = excess.mean() * math.sqrt(12.0) / excess.std(ddof=1)
    assert result.metrics.information_ratio == pytest.approx(expected_ir, abs=1e-9)


def test_up_capture_one_when_investment_matches_benchmark_on_up_months(
    month_end_index_12: pd.DatetimeIndex,
) -> None:
    benchmark_vals = np.array(
        [0.02, -0.01, 0.03, -0.02, 0.01, -0.005, 0.025, -0.015, 0.015, -0.01, 0.02, -0.005]
    )
    # Investment matches benchmark in up months, diverges in down months.
    up_mask = benchmark_vals > 0.0
    investment_vals = benchmark_vals.copy()
    investment_vals[~up_mask] = 0.005  # arbitrary down-month behaviour
    investment = pd.Series(investment_vals, index=month_end_index_12)
    benchmark = pd.Series(benchmark_vals, index=month_end_index_12)
    risk_free = pd.Series(np.zeros(12), index=month_end_index_12)

    result = compute_benchmark_comparison(investment, benchmark, risk_free, "inv", "bm")

    assert result.metrics.up_capture_ratio == pytest.approx(1.0, abs=1e-12)


def test_fewer_than_twelve_observations_yields_nan_for_ratios() -> None:
    idx = pd.date_range("2024-01-31", periods=6, freq="ME")
    rng = np.random.default_rng(7)
    benchmark = pd.Series(rng.uniform(-0.03, 0.03, 6), index=idx)
    investment = pd.Series(rng.uniform(-0.03, 0.03, 6), index=idx)
    risk_free = pd.Series(np.zeros(6), index=idx)

    result = compute_benchmark_comparison(investment, benchmark, risk_free, "inv", "bm")

    assert math.isnan(result.metrics.information_ratio)
    assert math.isnan(result.metrics.r_squared)
    # Excess total is a simple sum-difference — must be a number.
    assert not math.isnan(result.metrics.excess_return_total)
    # Beta is computable with variance > 0.
    assert not math.isnan(result.metrics.beta)
    assert result.metrics.n_observations == 6


def test_empty_inputs_yield_zero_observations() -> None:
    empty = pd.Series(dtype="float64")
    result = compute_benchmark_comparison(empty, empty, empty, "inv", "bm")

    assert result.metrics.n_observations == 0
    assert math.isnan(result.metrics.excess_return_total)
    assert math.isnan(result.metrics.alpha_annualised)
    assert math.isnan(result.metrics.beta)
    assert result.metrics.period_start == result.metrics.period_end
    assert result.aligned_investment_returns.empty
    assert result.aligned_benchmark_returns.empty
    assert result.aligned_excess_returns.empty


def test_misaligned_dates_inner_joined() -> None:
    idx_inv = pd.date_range("2024-01-31", periods=12, freq="ME")
    idx_bm = pd.date_range("2024-03-31", periods=12, freq="ME")
    idx_rf = pd.date_range("2024-02-29", periods=12, freq="ME")

    investment = pd.Series(np.linspace(0.001, 0.012, 12), index=idx_inv)
    benchmark = pd.Series(np.linspace(0.002, 0.013, 12), index=idx_bm)
    risk_free = pd.Series(np.zeros(12), index=idx_rf)

    result = compute_benchmark_comparison(investment, benchmark, risk_free, "inv", "bm")

    # Inner join: months from 2024-03-31 to 2024-12-31 — 10 months.
    assert result.metrics.n_observations == 10


def test_zero_benchmark_variance_handled(
    month_end_index_12: pd.DatetimeIndex,
) -> None:
    investment_vals = np.linspace(-0.02, 0.03, 12)
    investment = pd.Series(investment_vals, index=month_end_index_12)
    benchmark = pd.Series(np.zeros(12), index=month_end_index_12)
    risk_free = pd.Series(np.zeros(12), index=month_end_index_12)

    result = compute_benchmark_comparison(investment, benchmark, risk_free, "inv", "bm")

    assert math.isnan(result.metrics.beta)
    assert math.isnan(result.metrics.r_squared)
    expected_alpha = float(investment_vals.mean() * 12.0)
    assert result.metrics.alpha_annualised == pytest.approx(expected_alpha, abs=1e-12)
    # Benchmark is exactly zero — no up or down months, capture NaN.
    assert math.isnan(result.metrics.up_capture_ratio)
    assert math.isnan(result.metrics.down_capture_ratio)


# ---------------------------------------------------------------------------
# Group B — compute_asset_class_composites
# ---------------------------------------------------------------------------


def _build_daily_constant_returns(start: str, end: str, daily_rate: float) -> pd.Series:
    idx = pd.date_range(start, end, freq="D")
    return pd.Series(np.full(len(idx), daily_rate), index=idx)


def _build_monthly_nav_series(months: list[str], values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.DatetimeIndex([pd.Timestamp(m) for m in months]))


def test_single_investment_in_asset_class_composite_equals_investment() -> None:
    daily_returns = _build_daily_constant_returns("2024-01-01", "2024-05-31", 0.0)
    # Inject a single non-zero return on Feb 15.
    daily_returns.loc[pd.Timestamp("2024-02-15")] = 0.05
    nav_daily = pd.Series(
        [100.0, 105.0, 105.0, 105.0],
        index=pd.DatetimeIndex(
            [
                "2024-01-31",
                "2024-02-29",
                "2024-03-31",
                "2024-04-30",
            ]
        ),
    )
    result = compute_asset_class_composites(
        investment_returns_daily={"inv1": daily_returns},
        investment_navs_daily={"inv1": nav_daily},
        investment_to_asset_class={"inv1": "PE"},
    )

    assert len(result) == 1
    composite = result[0]
    assert composite.asset_class_code == "PE"
    # Single investment → composite return equals investment monthly return.
    assert composite.monthly_returns.loc[pd.Timestamp("2024-02-29")] == (
        pytest.approx(0.05, abs=1e-12)
    )
    assert composite.n_investments == 1


def test_two_equal_weight_investments_composite_is_simple_average() -> None:
    idx = pd.date_range("2024-01-01", "2024-04-30", freq="D")
    daily_zero = pd.Series(np.zeros(len(idx)), index=idx)
    inv1_returns = daily_zero.copy()
    inv2_returns = daily_zero.copy()
    inv1_returns.loc[pd.Timestamp("2024-02-15")] = 0.04
    inv2_returns.loc[pd.Timestamp("2024-02-20")] = 0.02

    # Identical NAVs throughout.
    nav_idx = pd.DatetimeIndex(["2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30"])
    nav1 = pd.Series([100.0, 100.0, 100.0, 100.0], index=nav_idx)
    nav2 = pd.Series([100.0, 100.0, 100.0, 100.0], index=nav_idx)

    result = compute_asset_class_composites(
        investment_returns_daily={"inv1": inv1_returns, "inv2": inv2_returns},
        investment_navs_daily={"inv1": nav1, "inv2": nav2},
        investment_to_asset_class={"inv1": "PE", "inv2": "PE"},
    )

    composite = result[0]
    assert composite.monthly_returns.loc[pd.Timestamp("2024-02-29")] == (
        pytest.approx(0.03, abs=1e-12)
    )
    assert composite.n_investments == 2


def test_bop_weighting_respects_previous_month_nav() -> None:
    idx = pd.date_range("2024-01-01", "2024-03-31", freq="D")
    inv1_daily = pd.Series(np.zeros(len(idx)), index=idx)
    inv2_daily = pd.Series(np.zeros(len(idx)), index=idx)
    inv1_daily.loc[pd.Timestamp("2024-02-15")] = 0.01
    inv2_daily.loc[pd.Timestamp("2024-02-15")] = 0.02

    nav_idx = pd.DatetimeIndex(["2024-01-31", "2024-02-29"])
    # BoP NAVs for Feb composite come from Jan-end: inv1=100, inv2=200.
    nav1 = pd.Series([100.0, 100.0], index=nav_idx)
    nav2 = pd.Series([200.0, 100.0], index=nav_idx)

    result = compute_asset_class_composites(
        investment_returns_daily={"inv1": inv1_daily, "inv2": inv2_daily},
        investment_navs_daily={"inv1": nav1, "inv2": nav2},
        investment_to_asset_class={"inv1": "PE", "inv2": "PE"},
    )

    composite = result[0]
    # Expected: (100*0.01 + 200*0.02) / (100+200) = 5/300.
    assert composite.monthly_returns.loc[pd.Timestamp("2024-02-29")] == (
        pytest.approx(5.0 / 300.0, abs=1e-12)
    )


def test_forward_fill_during_nav_gap() -> None:
    # NAV observed only at Jan-end and Apr-end (60-day gap).
    nav_daily = pd.Series(
        [100.0, 100.0, 110.0],
        index=pd.DatetimeIndex(["2024-01-01", "2024-01-31", "2024-04-30"]),
    )
    # Daily returns: zero everywhere except a spike in April reflecting
    # the quarterly NAV step. Feb/Mar are "honest zero-return months".
    idx = pd.date_range("2024-01-01", "2024-04-30", freq="D")
    daily_returns = pd.Series(np.zeros(len(idx)), index=idx)
    daily_returns.loc[pd.Timestamp("2024-04-15")] = 0.10

    result = compute_asset_class_composites(
        investment_returns_daily={"inv1": daily_returns},
        investment_navs_daily={"inv1": nav_daily},
        investment_to_asset_class={"inv1": "PE"},
    )

    composite = result[0].monthly_returns
    # February composite needs end-of-January NAV (present): zero return.
    assert composite.loc[pd.Timestamp("2024-02-29")] == pytest.approx(0.0, abs=1e-12)
    # March composite needs end-of-February NAV (ffilled from Jan-end).
    assert composite.loc[pd.Timestamp("2024-03-31")] == pytest.approx(0.0, abs=1e-12)
    # April composite reflects the quarterly spike.
    assert composite.loc[pd.Timestamp("2024-04-30")] == pytest.approx(0.10, abs=1e-12)


def test_investment_outside_lifetime_contributes_zero_weight() -> None:
    # inv1 exists Jan onwards; inv2 first NAV at end of March.
    idx_full = pd.date_range("2024-01-01", "2024-05-31", freq="D")
    inv1_daily = pd.Series(np.zeros(len(idx_full)), index=idx_full)
    inv1_daily.loc[pd.Timestamp("2024-02-15")] = 0.01
    inv1_daily.loc[pd.Timestamp("2024-04-15")] = 0.02

    # inv2 starts in March (no daily returns before then).
    idx_inv2 = pd.date_range("2024-03-01", "2024-05-31", freq="D")
    inv2_daily = pd.Series(np.zeros(len(idx_inv2)), index=idx_inv2)
    inv2_daily.loc[pd.Timestamp("2024-04-15")] = 0.05

    nav1 = pd.Series(
        [100.0, 100.0, 100.0, 100.0, 100.0],
        index=pd.DatetimeIndex(
            [
                "2024-01-31",
                "2024-02-29",
                "2024-03-31",
                "2024-04-30",
                "2024-05-31",
            ]
        ),
    )
    nav2 = pd.Series(
        [200.0, 200.0, 200.0],
        index=pd.DatetimeIndex(["2024-03-31", "2024-04-30", "2024-05-31"]),
    )

    result = compute_asset_class_composites(
        investment_returns_daily={"inv1": inv1_daily, "inv2": inv2_daily},
        investment_navs_daily={"inv1": nav1, "inv2": nav2},
        investment_to_asset_class={"inv1": "PE", "inv2": "PE"},
    )

    composite = result[0].monthly_returns
    # Feb composite: inv2 has no BoP NAV (no Jan-end NAV) → only inv1.
    assert composite.loc[pd.Timestamp("2024-02-29")] == pytest.approx(0.01, abs=1e-12)
    # April composite: both inv1 and inv2 have BoP NAV (end of March).
    # Weighted: (100*0.02 + 200*0.05) / 300 = 12/300 = 0.04.
    assert composite.loc[pd.Timestamp("2024-04-30")] == pytest.approx(12.0 / 300.0, abs=1e-12)


def test_empty_asset_class_returned_with_zero_investments() -> None:
    result = compute_asset_class_composites(
        investment_returns_daily={},
        investment_navs_daily={},
        investment_to_asset_class={"inv1": "PE"},
    )

    assert len(result) == 1
    assert result[0].asset_class_code == "PE"
    assert result[0].monthly_returns.empty
    assert result[0].n_investments == 0


def test_stable_alphabetical_ordering_in_output() -> None:
    idx = pd.date_range("2024-01-01", "2024-03-31", freq="D")
    daily_returns = pd.Series(np.zeros(len(idx)), index=idx)
    daily_returns.loc[pd.Timestamp("2024-02-15")] = 0.01

    nav = pd.Series(
        [100.0, 100.0, 100.0],
        index=pd.DatetimeIndex(["2024-01-31", "2024-02-29", "2024-03-31"]),
    )

    result = compute_asset_class_composites(
        investment_returns_daily={
            "inv_zz": daily_returns,
            "inv_aa": daily_returns,
            "inv_mm": daily_returns,
        },
        investment_navs_daily={
            "inv_zz": nav,
            "inv_aa": nav,
            "inv_mm": nav,
        },
        investment_to_asset_class={
            "inv_zz": "zz",
            "inv_aa": "aa",
            "inv_mm": "mm",
        },
    )

    assert [r.asset_class_code for r in result] == ["aa", "mm", "zz"]


# ---------------------------------------------------------------------------
# Group C — compute_saa_hypothetical_series
# ---------------------------------------------------------------------------


def test_weights_sum_validation() -> None:
    with pytest.raises(ValueError, match="sum to 1.0"):
        compute_saa_hypothetical_series(
            saa_weights={"eq": 0.5, "pe": 0.4},
            benchmark_returns_by_asset_class={},
            composite_returns_by_asset_class={},
            actual_portfolio_returns=pd.Series(dtype="float64"),
            saa_label="bad",
        )


def test_two_asset_class_weighted_sum(
    month_end_index_12: pd.DatetimeIndex,
) -> None:
    bench_eq = pd.Series(np.full(12, 0.01), index=month_end_index_12)
    bench_pe = pd.Series(np.full(12, 0.02), index=month_end_index_12)
    comp_eq = pd.Series(np.full(12, 0.015), index=month_end_index_12)
    comp_pe = pd.Series(np.full(12, 0.025), index=month_end_index_12)
    actual = pd.Series(np.full(12, 0.018), index=month_end_index_12)

    result = compute_saa_hypothetical_series(
        saa_weights={"eq": 0.6, "pe": 0.4},
        benchmark_returns_by_asset_class={"eq": bench_eq, "pe": bench_pe},
        composite_returns_by_asset_class={"eq": comp_eq, "pe": comp_pe},
        actual_portfolio_returns=actual,
        saa_label="Target — Standard 2026",
    )

    # 0.6 * 0.01 + 0.4 * 0.02 = 0.014
    assert result.saa_x_benchmark.iloc[0] == pytest.approx(0.014, abs=1e-12)
    # 0.6 * 0.015 + 0.4 * 0.025 = 0.019
    assert result.saa_x_composite.iloc[0] == pytest.approx(0.019, abs=1e-12)


def test_missing_benchmark_treated_as_zero_contribution(
    month_end_index_12: pd.DatetimeIndex,
) -> None:
    bench_eq = pd.Series(np.full(12, 0.02), index=month_end_index_12)
    actual = pd.Series(np.zeros(12), index=month_end_index_12)

    result = compute_saa_hypothetical_series(
        saa_weights={"eq": 0.7, "cash": 0.3},
        benchmark_returns_by_asset_class={"eq": bench_eq},
        composite_returns_by_asset_class={},
        actual_portfolio_returns=actual,
        saa_label="Mixed",
    )

    # Cash has no benchmark → 0.3 * 0 = 0; total = 0.7 * 0.02 = 0.014.
    assert result.saa_x_benchmark.iloc[0] == pytest.approx(0.014, abs=1e-12)


def test_actual_returns_passed_through_unmodified(
    month_end_index_12: pd.DatetimeIndex,
) -> None:
    actual_vals = np.linspace(0.001, 0.012, 12)
    actual = pd.Series(actual_vals, index=month_end_index_12)
    bench = pd.Series(np.zeros(12), index=month_end_index_12)

    result = compute_saa_hypothetical_series(
        saa_weights={"eq": 1.0},
        benchmark_returns_by_asset_class={"eq": bench},
        composite_returns_by_asset_class={"eq": bench},
        actual_portfolio_returns=actual,
        saa_label="100% Equity",
    )

    np.testing.assert_allclose(
        result.actual_portfolio_returns.to_numpy(),
        actual_vals,
        atol=1e-12,
    )


def test_period_start_end_align_with_common_index() -> None:
    # Three series with different start/end dates.
    idx_a = pd.date_range("2024-01-31", periods=6, freq="ME")
    idx_b = pd.date_range("2024-04-30", periods=6, freq="ME")
    idx_c = pd.date_range("2024-02-29", periods=8, freq="ME")

    bench_a = pd.Series(np.zeros(6), index=idx_a)
    bench_b = pd.Series(np.zeros(6), index=idx_b)
    actual = pd.Series(np.zeros(8), index=idx_c)

    result = compute_saa_hypothetical_series(
        saa_weights={"a": 0.5, "b": 0.5},
        benchmark_returns_by_asset_class={"a": bench_a, "b": bench_b},
        composite_returns_by_asset_class={},
        actual_portfolio_returns=actual,
        saa_label="Test",
    )

    # Union: min start = 2024-01-31, max end = 2024-09-30
    # (idx_b ends 2024-09-30; idx_c ends 2024-09-30).
    expected_starts = [
        idx_a.min().date(),
        idx_b.min().date(),
        idx_c.min().date(),
    ]
    expected_ends = [
        idx_a.max().date(),
        idx_b.max().date(),
        idx_c.max().date(),
    ]
    assert result.period_start == min(expected_starts)
    assert result.period_end == max(expected_ends)


# ---------------------------------------------------------------------------
# Group D — internal helpers + module-purity guard
# ---------------------------------------------------------------------------


def test_align_monthly_series_inner_join() -> None:
    idx_a = pd.date_range("2024-01-31", periods=6, freq="ME")
    idx_b = pd.date_range("2024-03-31", periods=6, freq="ME")
    idx_c = pd.date_range("2024-02-29", periods=6, freq="ME")

    s_a = pd.Series(np.arange(6, dtype="float64"), index=idx_a)
    s_b = pd.Series(np.arange(6, dtype="float64"), index=idx_b)
    s_c = pd.Series(np.arange(6, dtype="float64"), index=idx_c)

    aligned = _align_monthly_series(s_a, s_b, s_c)
    assert all(len(s) == 4 for s in aligned)
    expected_index = pd.date_range("2024-03-31", periods=4, freq="ME")
    for s in aligned:
        assert list(s.index) == list(expected_index)


def test_resample_daily_to_monthly_compounding_identity() -> None:
    # All zero daily returns → zero monthly returns.
    idx_jan = pd.date_range("2024-01-01", "2024-01-31", freq="D")
    zero_daily = pd.Series(np.zeros(len(idx_jan)), index=idx_jan)
    zero_monthly = _resample_daily_to_monthly_return(zero_daily)
    assert zero_monthly.loc[pd.Timestamp("2024-01-31")] == pytest.approx(0.0, abs=1e-12)

    # Constant +0.1% daily → monthly = (1.001)^31 - 1 for January.
    flat_daily = pd.Series(np.full(len(idx_jan), 0.001), index=idx_jan)
    flat_monthly = _resample_daily_to_monthly_return(flat_daily)
    expected = (1.001) ** 31 - 1.0
    assert flat_monthly.loc[pd.Timestamp("2024-01-31")] == pytest.approx(expected, abs=1e-12)


def test_module_source_has_no_db_or_qt_or_fastapi_imports() -> None:
    """Defence-in-depth: complement to the global regression guard."""
    module_path = (
        Path(__file__).resolve().parents[3] / "services" / "analytics" / "benchmark_comparison.py"
    )
    source = module_path.read_text(encoding="utf-8")
    for forbidden in (
        "import sqlalchemy",
        "from sqlalchemy",
        "import fastapi",
        "from fastapi",
        "from PyQt6",
        "import PyQt6",
        "AsyncSession",
        "async_session",
        "get_db_session",
    ):
        assert forbidden not in source, (
            f"benchmark_comparison.py contains forbidden token {forbidden!r}"
        )


# ---------------------------------------------------------------------------
# Smoke-test the returned types so refactors don't silently drift.
# ---------------------------------------------------------------------------


def test_return_types_are_frozen_dataclasses(
    month_end_index_12: pd.DatetimeIndex,
) -> None:
    bench = pd.Series(np.zeros(12), index=month_end_index_12)
    bundle = compute_benchmark_comparison(bench, bench, bench, "inv", "bm")
    assert isinstance(bundle, BenchmarkComparisonBundle)
    assert isinstance(bundle.metrics, BenchmarkComparisonMetrics)
    with pytest.raises(Exception):
        bundle.metrics.beta = 0.0  # type: ignore[misc]

    composites = compute_asset_class_composites(
        investment_returns_daily={},
        investment_navs_daily={},
        investment_to_asset_class={},
    )
    assert composites == []

    saa = compute_saa_hypothetical_series(
        saa_weights={"eq": 1.0},
        benchmark_returns_by_asset_class={"eq": bench},
        composite_returns_by_asset_class={"eq": bench},
        actual_portfolio_returns=bench,
        saa_label="ALL-EQ",
    )
    assert isinstance(saa, SAAHypotheticalSeries)
    assert isinstance(composites, list)
    if composites:
        assert isinstance(composites[0], AssetClassCompositeSeries)
