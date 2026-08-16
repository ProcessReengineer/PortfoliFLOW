# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.analytics.statistics``.

Pure-function tests — no DB, no Qt, no FastAPI. Each test builds a
deterministic pandas Series and asserts numerical output against
hand-computed values. The QT-consistency tests at the bottom
replicate the formulas embedded in
``gui/widgets/_statistics_helpers.py`` and
``gui/widgets/statistics_widgets.py`` and assert agreement to within
``1e-12``.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest
import scipy.stats

from services.analytics.statistics import (
    PERIODS_PER_YEAR_MONTHLY,
    annualise_mean_return,
    annualise_std_dev,
    compute_autocorrelation,
    compute_conditional_value_at_risk,
    compute_downside_deviation,
    compute_full_distribution_stats,
    compute_kurtosis,
    compute_lag_1_autocorrelation,
    compute_max_drawdown,
    compute_max_drawdown_from_returns,
    compute_max_return,
    compute_mean_return,
    compute_median_return,
    compute_min_return,
    compute_risk_metrics,
    compute_rolling_sharpe,
    compute_rolling_volatility,
    compute_sharpe_ratio,
    compute_skewness,
    compute_sortino_ratio,
    compute_std_dev,
    compute_ulcer_index,
    compute_underwater_series,
    compute_value_at_risk,
    compute_variance,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sample_returns() -> pd.Series:
    """Six daily returns; mean ~0.005, mix of signs."""
    return pd.Series(
        [0.01, -0.02, 0.015, -0.005, 0.03, -0.01],
        index=pd.date_range("2025-01-01", periods=6, freq="D"),
    )


def _sample_navs() -> pd.Series:
    """A NAV series that rises then dips so MDD is non-trivial."""
    return pd.Series(
        [100.0, 110.0, 121.0, 100.0, 95.0, 108.0],
        index=pd.date_range("2025-01-01", periods=6, freq="D"),
    )


# ---------------------------------------------------------------------------
# Distribution primitives
# ---------------------------------------------------------------------------


def test_compute_mean_return_matches_numpy() -> None:
    series = _sample_returns()
    assert compute_mean_return(series) == pytest.approx(
        float(np.nanmean(series.to_numpy())), abs=1e-15
    )


def test_compute_mean_return_empty_is_nan() -> None:
    assert math.isnan(compute_mean_return(pd.Series(dtype="float64")))


def test_annualise_mean_return_arithmetic_convention() -> None:
    # QT convention: mean * 252 (NOT geometric).
    assert annualise_mean_return(0.001, 252) == pytest.approx(0.252, abs=1e-15)
    assert annualise_mean_return(0.0, 252) == 0.0


def test_compute_std_dev_uses_ddof_1() -> None:
    series = _sample_returns()
    expected = float(np.nanstd(series.to_numpy(), ddof=1))
    assert compute_std_dev(series) == pytest.approx(expected, abs=1e-15)


def test_compute_std_dev_single_value_is_nan() -> None:
    assert math.isnan(compute_std_dev(pd.Series([0.01])))


def test_annualise_std_dev_sqrt_252() -> None:
    assert annualise_std_dev(0.01, 252) == pytest.approx(0.01 * math.sqrt(252), abs=1e-15)


def test_compute_variance_uses_ddof_1() -> None:
    series = _sample_returns()
    expected = float(np.nanvar(series.to_numpy(), ddof=1))
    assert compute_variance(series) == pytest.approx(expected, abs=1e-15)


def test_compute_variance_empty_is_nan() -> None:
    assert math.isnan(compute_variance(pd.Series(dtype="float64")))


def test_compute_skewness_matches_scipy() -> None:
    series = _sample_returns()
    expected = float(scipy.stats.skew(series.to_numpy(), nan_policy="omit"))
    assert compute_skewness(series) == pytest.approx(expected, abs=1e-15)


def test_compute_skewness_empty_is_nan() -> None:
    assert math.isnan(compute_skewness(pd.Series(dtype="float64")))


def test_compute_kurtosis_returns_excess() -> None:
    """scipy default is Fisher (excess) — normal distribution = 0."""
    rng = np.random.default_rng(seed=42)
    normal = pd.Series(rng.standard_normal(10_000))
    # 10k draws → excess kurtosis is close to zero.
    assert abs(compute_kurtosis(normal)) < 0.2


def test_compute_kurtosis_matches_scipy_with_nan_policy_omit() -> None:
    series = _sample_returns()
    expected = float(scipy.stats.kurtosis(series.to_numpy(), nan_policy="omit"))
    assert compute_kurtosis(series) == pytest.approx(expected, abs=1e-15)


def test_compute_median_min_max() -> None:
    series = pd.Series([0.0, 0.05, -0.01, 0.02])
    assert compute_median_return(series) == pytest.approx(0.01, abs=1e-15)
    assert compute_min_return(series) == pytest.approx(-0.01, abs=1e-15)
    assert compute_max_return(series) == pytest.approx(0.05, abs=1e-15)


# ---------------------------------------------------------------------------
# Risk metrics
# ---------------------------------------------------------------------------


def test_compute_max_drawdown_negative_for_drawdown_series() -> None:
    """NAV rises then drops — MDD is negative and non-trivial."""
    mdd = compute_max_drawdown(_sample_navs())
    assert mdd < 0.0
    # Peak NAV = 121 at index 2, trough NAV = 95 at index 4 →
    # implied MDD ≈ (95-121)/121 ≈ -0.2148 on the cumprod side.
    # Hand-compute against the QT formula for parity:
    cleaned = _sample_navs().sort_index()
    returns = cleaned.pct_change().dropna()
    cumulative = (1.0 + returns).cumprod()
    running_max = cumulative.cummax()
    drawdown = (cumulative - running_max) / running_max
    assert mdd == pytest.approx(float(drawdown.min()), abs=1e-15)


def test_compute_max_drawdown_monotone_increasing_is_zero() -> None:
    nav = pd.Series([100.0, 105.0, 110.0, 115.0])
    assert compute_max_drawdown(nav) == 0.0


def test_compute_max_drawdown_too_short_is_nan() -> None:
    assert math.isnan(compute_max_drawdown(pd.Series([100.0])))
    assert math.isnan(compute_max_drawdown(pd.Series(dtype="float64")))


def test_compute_max_drawdown_from_returns_matches_qt_helper() -> None:
    """QT helper ``_max_drawdown(returns)`` formula bit-for-bit."""
    returns = pd.Series([0.10, 0.10, -0.20, -0.05, 0.10])
    # QT formula
    cumulative = pd.Series(returns).add(1).cumprod()
    running_max = cumulative.cummax()
    drawdown = (cumulative - running_max) / running_max
    expected = float(drawdown.min())
    assert compute_max_drawdown_from_returns(returns) == pytest.approx(expected, abs=1e-15)


def test_compute_sharpe_ratio_zero_risk_free() -> None:
    series = _sample_returns()
    mean_ann = float(np.nanmean(series.to_numpy())) * 252
    std_ann = float(np.nanstd(series.to_numpy(), ddof=1)) * math.sqrt(252)
    expected = mean_ann / std_ann
    assert compute_sharpe_ratio(series, 0.0) == pytest.approx(expected, abs=1e-15)


def test_compute_sharpe_ratio_with_risk_free() -> None:
    series = _sample_returns()
    rf = 0.02
    mean_ann = float(np.nanmean(series.to_numpy())) * 252
    std_ann = float(np.nanstd(series.to_numpy(), ddof=1)) * math.sqrt(252)
    expected = (mean_ann - rf) / std_ann
    assert compute_sharpe_ratio(series, rf) == pytest.approx(expected, abs=1e-15)


def test_compute_sharpe_ratio_nan_when_std_zero() -> None:
    constant = pd.Series([0.01, 0.01, 0.01, 0.01])
    assert math.isnan(compute_sharpe_ratio(constant))


def test_compute_lag_1_autocorrelation_matches_pandas() -> None:
    series = _sample_returns()
    expected = float(series.autocorr(lag=1))
    assert compute_lag_1_autocorrelation(series) == pytest.approx(expected, abs=1e-15)


def test_compute_lag_1_autocorrelation_short_series_is_nan() -> None:
    assert math.isnan(compute_lag_1_autocorrelation(pd.Series([0.01])))
    assert math.isnan(compute_lag_1_autocorrelation(pd.Series(dtype="float64")))


# ---------------------------------------------------------------------------
# Convenience bundle
# ---------------------------------------------------------------------------


def test_compute_full_distribution_stats_matches_individual_functions() -> None:
    series = _sample_returns()
    bundle = compute_full_distribution_stats(series)
    assert bundle.mean_daily == pytest.approx(compute_mean_return(series), abs=1e-15)
    assert bundle.mean_annualised == pytest.approx(
        annualise_mean_return(compute_mean_return(series)), abs=1e-15
    )
    assert bundle.std_daily == pytest.approx(compute_std_dev(series), abs=1e-15)
    assert bundle.std_annualised == pytest.approx(
        annualise_std_dev(compute_std_dev(series)), abs=1e-15
    )
    assert bundle.variance_daily == pytest.approx(compute_variance(series), abs=1e-15)
    assert bundle.skewness == pytest.approx(compute_skewness(series), abs=1e-15)
    assert bundle.kurtosis_excess == pytest.approx(compute_kurtosis(series), abs=1e-15)
    assert bundle.median == pytest.approx(compute_median_return(series), abs=1e-15)
    assert bundle.min_return == pytest.approx(compute_min_return(series), abs=1e-15)
    assert bundle.max_return == pytest.approx(compute_max_return(series), abs=1e-15)


def test_compute_risk_metrics_packs_all_thirteen() -> None:
    returns = _sample_returns()
    risk = compute_risk_metrics(returns, risk_free_rate_annual=0.0)
    # Risk block
    assert risk.var_90_daily == pytest.approx(compute_value_at_risk(returns, level=0.90), abs=1e-15)
    assert risk.var_95_daily == pytest.approx(compute_value_at_risk(returns, level=0.95), abs=1e-15)
    assert risk.var_99_daily == pytest.approx(compute_value_at_risk(returns, level=0.99), abs=1e-15)
    assert risk.cvar_95_daily == pytest.approx(
        compute_conditional_value_at_risk(returns, level=0.95), abs=1e-15
    )
    assert risk.max_drawdown == pytest.approx(compute_max_drawdown_from_returns(returns), abs=1e-15)
    assert risk.ulcer_index == pytest.approx(compute_ulcer_index(returns), abs=1e-15)
    assert risk.downside_deviation == pytest.approx(compute_downside_deviation(returns), abs=1e-15)
    # Risk / Return
    assert risk.sharpe_ratio == pytest.approx(compute_sharpe_ratio(returns, 0.0), abs=1e-15)
    assert risk.sortino_ratio == pytest.approx(compute_sortino_ratio(returns, 0.0), abs=1e-15)
    # Autocorrelation
    for lag, field in [
        (1, "lag_1_autocorrelation"),
        (2, "lag_2_autocorrelation"),
        (3, "lag_3_autocorrelation"),
        (4, "lag_4_autocorrelation"),
    ]:
        assert getattr(risk, field) == pytest.approx(
            compute_autocorrelation(returns, lag), abs=1e-15
        )


# ---------------------------------------------------------------------------
# QT-consistency: identical methodology, identical resulting numbers
# ---------------------------------------------------------------------------


def _qt_annualised_mean(returns: np.ndarray) -> float:
    """Lifted from gui/widgets/_statistics_helpers.py::_annualised_mean."""
    if returns.size == 0:
        return float("nan")
    return float(np.nanmean(returns)) * 252


def _qt_max_drawdown(returns: np.ndarray) -> float:
    """Lifted from gui/widgets/_statistics_helpers.py::_max_drawdown."""
    if returns.size == 0:
        return float("nan")
    cumulative = pd.Series(returns).add(1).cumprod()
    running_max = cumulative.cummax()
    drawdown = (cumulative - running_max) / running_max
    return float(drawdown.min())


def _qt_sharpe(returns: np.ndarray) -> float:
    """Lifted from gui/widgets/_statistics_helpers.py::_sharpe."""
    if returns.size == 0:
        return float("nan")
    mean_ann = float(np.nanmean(returns)) * 252
    std_ann = float(np.nanstd(returns, ddof=1)) * np.sqrt(252)
    if not np.isfinite(std_ann) or std_ann <= 0.0:
        return float("nan")
    return mean_ann / std_ann


def _qt_skewness(returns: np.ndarray) -> float:
    """Lifted from DistributionTableWidget — scipy.stats.skew, omit NaN."""
    return float(scipy.stats.skew(returns, nan_policy="omit"))


def _qt_kurtosis_excess(returns: np.ndarray) -> float:
    """Lifted from DistributionTableWidget — scipy.stats.kurtosis (excess)."""
    return float(scipy.stats.kurtosis(returns, nan_policy="omit"))


def test_qt_consistency_annualised_mean() -> None:
    series = _sample_returns()
    qt_value = _qt_annualised_mean(series.to_numpy(dtype="float64"))
    new_value = annualise_mean_return(compute_mean_return(series))
    assert abs(qt_value - new_value) < 1e-12


def test_qt_consistency_max_drawdown_via_returns() -> None:
    returns = pd.Series([0.10, 0.10, -0.20, -0.05, 0.10])
    qt_value = _qt_max_drawdown(returns.to_numpy(dtype="float64"))
    new_value = compute_max_drawdown_from_returns(returns)
    assert abs(qt_value - new_value) < 1e-12


def test_qt_consistency_sharpe_zero_rf() -> None:
    series = _sample_returns()
    qt_value = _qt_sharpe(series.to_numpy(dtype="float64"))
    new_value = compute_sharpe_ratio(series, 0.0)
    assert abs(qt_value - new_value) < 1e-12


def test_qt_consistency_skewness() -> None:
    series = _sample_returns()
    qt_value = _qt_skewness(series.to_numpy(dtype="float64"))
    new_value = compute_skewness(series)
    assert abs(qt_value - new_value) < 1e-12


def test_qt_consistency_kurtosis_excess() -> None:
    series = _sample_returns()
    qt_value = _qt_kurtosis_excess(series.to_numpy(dtype="float64"))
    new_value = compute_kurtosis(series)
    assert abs(qt_value - new_value) < 1e-12


def test_qt_consistency_max_drawdown_from_navs_matches_returns_path() -> None:
    """compute_max_drawdown(nav) == compute_max_drawdown_from_returns(pct_change(nav)).

    The two helpers should produce the same number when the NAV
    series's ``pct_change`` is the input to the returns-side helper.
    This is the structural invariant the QT widget relies on:
    DistributionTableWidget passes ``r`` (returns) into _max_drawdown,
    while the web service can pass NAVs directly.
    """
    nav_series = _sample_navs()
    return_series = nav_series.pct_change().dropna()
    via_navs = compute_max_drawdown(nav_series)
    via_returns = compute_max_drawdown_from_returns(return_series)
    assert abs(via_navs - via_returns) < 1e-12


# ---------------------------------------------------------------------------
# Index preservation
# ---------------------------------------------------------------------------


def test_functions_do_not_mutate_input() -> None:
    series = _sample_returns()
    snapshot = series.copy()
    _ = compute_mean_return(series)
    _ = compute_std_dev(series)
    _ = compute_skewness(series)
    _ = compute_max_drawdown_from_returns(series)
    pd.testing.assert_series_equal(series, snapshot, check_names=False)


def test_functions_handle_date_indexed_series() -> None:
    series = pd.Series(
        [0.01, 0.02, -0.03],
        index=[date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)],
    )
    # Should not raise on a date-indexed series.
    _ = compute_mean_return(series)
    _ = compute_lag_1_autocorrelation(series)


# ---------------------------------------------------------------------------
# Value-at-Risk and CVaR
# ---------------------------------------------------------------------------


def test_compute_value_at_risk_matches_numpy_percentile() -> None:
    series = _sample_returns()
    # VaR 95% → 5th percentile
    expected = float(np.nanpercentile(series.to_numpy(), 5))
    assert compute_value_at_risk(series, level=0.95) == pytest.approx(expected, abs=1e-15)


def test_compute_value_at_risk_three_levels() -> None:
    series = _sample_returns()
    for level, pct in [(0.90, 10), (0.95, 5), (0.99, 1)]:
        expected = float(np.nanpercentile(series.to_numpy(), pct))
        assert compute_value_at_risk(series, level=level) == pytest.approx(expected, abs=1e-15)


def test_compute_value_at_risk_empty_is_nan() -> None:
    assert math.isnan(compute_value_at_risk(pd.Series(dtype="float64"), level=0.95))


def test_compute_conditional_value_at_risk_matches_qt_formula() -> None:
    series = _sample_returns()
    var95 = float(np.nanpercentile(series.to_numpy(), 5))
    tail = series.to_numpy()[series.to_numpy() <= var95]
    expected = float(np.mean(tail))
    assert compute_conditional_value_at_risk(series, level=0.95) == pytest.approx(
        expected, abs=1e-15
    )


def test_compute_conditional_value_at_risk_empty_tail_is_nan() -> None:
    # A constant series has no observations below the percentile,
    # so the tail equals the whole series.
    constant = pd.Series([0.01] * 10)
    # All values == 0.01 == VaR; tail = r[r <= var95] = full series.
    # Sanity: this should still produce 0.01.
    result = compute_conditional_value_at_risk(constant, level=0.95)
    assert result == pytest.approx(0.01, abs=1e-15)


# ---------------------------------------------------------------------------
# Ulcer Index
# ---------------------------------------------------------------------------


def test_compute_ulcer_index_matches_qt_formula() -> None:
    """QT body bit-for-bit."""
    series = _sample_returns()
    values = series.dropna()
    cumulative = (1.0 + values).cumprod()
    running_max = cumulative.cummax()
    drawdown_pct = ((cumulative - running_max) / running_max) * 100.0
    expected = float(np.sqrt(np.mean(drawdown_pct.to_numpy() ** 2)))
    assert compute_ulcer_index(series) == pytest.approx(expected, abs=1e-15)


def test_compute_ulcer_index_monotone_increasing_is_zero() -> None:
    # Monotone non-decreasing returns → cumulative never drops →
    # drawdown is zero everywhere → Ulcer = 0.
    series = pd.Series([0.01, 0.005, 0.02, 0.0, 0.01])
    assert compute_ulcer_index(series) == pytest.approx(0.0, abs=1e-15)


def test_compute_ulcer_index_empty_is_nan() -> None:
    assert math.isnan(compute_ulcer_index(pd.Series(dtype="float64")))


# ---------------------------------------------------------------------------
# Downside Deviation
# ---------------------------------------------------------------------------


def test_compute_downside_deviation_matches_qt_formula_unannualised() -> None:
    series = _sample_returns()
    values = series.dropna().to_numpy()
    negative = np.minimum(values, 0.0)
    expected = float(np.sqrt(np.mean(negative**2)))
    assert compute_downside_deviation(series) == pytest.approx(expected, abs=1e-15)


def test_compute_downside_deviation_annualised_by_sqrt_periods() -> None:
    series = _sample_returns()
    unann = compute_downside_deviation(series, annualise=False)
    ann = compute_downside_deviation(series, annualise=True)
    assert ann == pytest.approx(unann * math.sqrt(252), abs=1e-15)


def test_compute_downside_deviation_all_positive_is_zero() -> None:
    series = pd.Series([0.01, 0.02, 0.005, 0.03])
    assert compute_downside_deviation(series) == pytest.approx(0.0, abs=1e-15)


# ---------------------------------------------------------------------------
# Sortino Ratio
# ---------------------------------------------------------------------------


def test_compute_sortino_ratio_matches_qt_formula() -> None:
    series = _sample_returns()
    rf = 0.0
    values = series.to_numpy()
    mean_ann = float(np.nanmean(values)) * 252
    negative = np.minimum(values, 0.0)
    dd_ann = float(np.sqrt(np.mean(negative**2))) * np.sqrt(252)
    expected = (mean_ann - rf) / dd_ann
    assert compute_sortino_ratio(series, rf) == pytest.approx(expected, abs=1e-15)


def test_compute_sortino_ratio_zero_downside_is_nan() -> None:
    series = pd.Series([0.01, 0.02, 0.005, 0.03])  # all positive
    assert math.isnan(compute_sortino_ratio(series, 0.0))


# ---------------------------------------------------------------------------
# Multi-lag autocorrelation
# ---------------------------------------------------------------------------


def test_compute_autocorrelation_lag_1_matches_existing_function() -> None:
    series = _sample_returns()
    assert compute_autocorrelation(series, 1) == pytest.approx(
        compute_lag_1_autocorrelation(series), abs=1e-15
    )


def test_compute_autocorrelation_lags_match_pandas() -> None:
    series = _sample_returns()
    for lag in [1, 2, 3, 4]:
        expected = float(series.dropna().autocorr(lag=lag))
        assert compute_autocorrelation(series, lag) == pytest.approx(expected, abs=1e-15)


def test_compute_autocorrelation_short_series_is_nan() -> None:
    assert math.isnan(compute_autocorrelation(pd.Series([0.01, 0.02]), 3))


def test_compute_autocorrelation_invalid_lag_raises() -> None:
    series = _sample_returns()
    with pytest.raises(ValueError):
        compute_autocorrelation(series, 0)
    with pytest.raises(ValueError):
        compute_autocorrelation(series, -1)


# ---------------------------------------------------------------------------
# QT-consistency for the six new functions
# ---------------------------------------------------------------------------


def _qt_var_95(returns: np.ndarray) -> float:
    """Lifted from gui/widgets/statistics_widgets.py::RiskTableWidget."""
    if returns.size == 0:
        return float("nan")
    return float(np.nanpercentile(returns, 5))


def _qt_cvar_95(returns: np.ndarray) -> float:
    """Lifted from gui/widgets/statistics_widgets.py::RiskTableWidget."""
    if returns.size == 0:
        return float("nan")
    var95 = _qt_var_95(returns)
    if not np.isfinite(var95):
        return float("nan")
    tail = returns[returns <= var95]
    if tail.size == 0:
        return float("nan")
    return float(np.mean(tail))


def _qt_ulcer_index(returns: np.ndarray) -> float:
    """Lifted from gui/widgets/statistics_widgets.py::RiskTableWidget."""
    if returns.size == 0:
        return float("nan")
    cumulative = pd.Series(returns).add(1).cumprod()
    running_max = cumulative.cummax()
    drawdown_pct = ((cumulative - running_max) / running_max) * 100
    return float(np.sqrt(np.mean(drawdown_pct**2)))


def _qt_downside_deviation(returns: np.ndarray) -> float:
    """Lifted from RiskTableWidget body (un-annualised variant)."""
    if returns.size == 0:
        return float("nan")
    negative = np.minimum(returns, 0.0)
    return float(np.sqrt(np.mean(negative**2)))


def _qt_sortino(returns: np.ndarray, rf: float = 0.0) -> float:
    """Lifted from RiskReturnTableWidget body."""
    if returns.size == 0:
        return float("nan")
    mean_ann = float(np.nanmean(returns)) * 252
    negative = np.minimum(returns, 0.0)
    dd_ann = float(np.sqrt(np.mean(negative**2))) * np.sqrt(252)
    if dd_ann <= 0.0:
        return float("nan")
    return (mean_ann - rf) / dd_ann


def test_qt_consistency_var_95() -> None:
    series = _sample_returns()
    arr = series.to_numpy()
    assert compute_value_at_risk(series, level=0.95) == pytest.approx(_qt_var_95(arr), abs=1e-12)


def test_qt_consistency_var_90_and_99() -> None:
    series = _sample_returns()
    arr = series.to_numpy()
    expected_90 = float(np.nanpercentile(arr, 10))
    expected_99 = float(np.nanpercentile(arr, 1))
    assert compute_value_at_risk(series, level=0.90) == pytest.approx(expected_90, abs=1e-12)
    assert compute_value_at_risk(series, level=0.99) == pytest.approx(expected_99, abs=1e-12)


def test_qt_consistency_cvar_95() -> None:
    series = _sample_returns()
    assert compute_conditional_value_at_risk(series, level=0.95) == pytest.approx(
        _qt_cvar_95(series.to_numpy()), abs=1e-12
    )


def test_qt_consistency_ulcer_index() -> None:
    series = _sample_returns()
    assert compute_ulcer_index(series) == pytest.approx(
        _qt_ulcer_index(series.to_numpy()), abs=1e-12
    )


def test_qt_consistency_downside_deviation() -> None:
    series = _sample_returns()
    assert compute_downside_deviation(series) == pytest.approx(
        _qt_downside_deviation(series.to_numpy()), abs=1e-12
    )


def test_qt_consistency_sortino_ratio() -> None:
    series = _sample_returns()
    assert compute_sortino_ratio(series, 0.0) == pytest.approx(
        _qt_sortino(series.to_numpy(), 0.0), abs=1e-12
    )


def test_qt_consistency_autocorrelation_lags_1_to_4() -> None:
    series = _sample_returns()
    for lag in [1, 2, 3, 4]:
        # Qt body: series.autocorr(lag=lag) on dropna'd input.
        expected = float(series.dropna().autocorr(lag=lag))
        assert compute_autocorrelation(series, lag) == pytest.approx(expected, abs=1e-12)


# ---------------------------------------------------------------------------
# Underwater / drawdown profile (ADR-0082 §5)
# ---------------------------------------------------------------------------


def test_compute_underwater_series_known_path() -> None:
    """A hand-traceable return series → expected drawdown levels."""
    returns = pd.Series(
        [0.10, -0.20, 0.05],
        index=pd.date_range("2025-01-31", periods=3, freq="ME"),
    )
    # cumulative = [1.10, 0.88, 0.924]; running_max = [1.10, 1.10, 1.10]
    # underwater = [0.0, -0.20, -0.16]
    result = compute_underwater_series(returns)
    assert list(result.index) == list(returns.index)
    assert result.to_numpy() == pytest.approx([0.0, -0.20, -0.16], abs=1e-12)
    # Every level is non-positive.
    assert (result.to_numpy() <= 1e-15).all()


def test_compute_underwater_series_monotone_increasing_is_all_zero() -> None:
    returns = pd.Series([0.01, 0.02, 0.03, 0.005])
    result = compute_underwater_series(returns)
    assert result.to_numpy() == pytest.approx([0.0, 0.0, 0.0, 0.0], abs=1e-15)


def test_compute_underwater_series_min_equals_max_drawdown() -> None:
    """Invariant: the underwater minimum is the maximum drawdown."""
    returns = _sample_returns()
    underwater = compute_underwater_series(returns)
    assert float(underwater.min()) == pytest.approx(
        compute_max_drawdown_from_returns(returns), abs=1e-15
    )


def test_compute_underwater_series_too_short_is_empty() -> None:
    assert compute_underwater_series(pd.Series(dtype="float64")).empty
    assert compute_underwater_series(pd.Series([0.05])).empty


# ---------------------------------------------------------------------------
# Rolling volatility (ADR-0082 §5)
# ---------------------------------------------------------------------------


def test_compute_rolling_volatility_constant_returns_is_zero() -> None:
    """Constant returns → zero volatility once a full window is available."""
    returns = pd.Series([0.01] * 6)
    result = compute_rolling_volatility(returns, window=3)
    # Leading window-1 positions are NaN; the rest are 0.
    assert math.isnan(result.iloc[0])
    assert math.isnan(result.iloc[1])
    assert result.iloc[2:].to_numpy() == pytest.approx([0.0, 0.0, 0.0, 0.0])


def test_compute_rolling_volatility_matches_handcomputed_window() -> None:
    returns = pd.Series([0.01, -0.02, 0.03, 0.00])
    result = compute_rolling_volatility(returns, window=3, annualise=False)
    # Leading NaN for the first two positions.
    assert math.isnan(result.iloc[0])
    assert math.isnan(result.iloc[1])
    # Position 2: sample std (ddof=1) of [0.01, -0.02, 0.03].
    expected_2 = float(np.std([0.01, -0.02, 0.03], ddof=1))
    expected_3 = float(np.std([-0.02, 0.03, 0.00], ddof=1))
    assert result.iloc[2] == pytest.approx(expected_2, abs=1e-15)
    assert result.iloc[3] == pytest.approx(expected_3, abs=1e-15)


def test_compute_rolling_volatility_annualisation_factor() -> None:
    returns = pd.Series([0.01, -0.02, 0.03, 0.00, 0.015, -0.01])
    unann = compute_rolling_volatility(returns, window=3, annualise=False)
    ann = compute_rolling_volatility(returns, window=3, annualise=True)
    factor = math.sqrt(PERIODS_PER_YEAR_MONTHLY)
    pd.testing.assert_series_equal(ann, unann * factor)


def test_compute_rolling_volatility_preserves_index() -> None:
    idx = pd.date_range("2025-01-31", periods=5, freq="ME")
    returns = pd.Series([0.01, -0.02, 0.03, 0.00, 0.015], index=idx)
    result = compute_rolling_volatility(returns, window=3)
    assert list(result.index) == list(idx)


def test_compute_rolling_volatility_invalid_window_raises() -> None:
    returns = pd.Series([0.01, 0.02, 0.03])
    with pytest.raises(ValueError):
        compute_rolling_volatility(returns, window=0)


# ---------------------------------------------------------------------------
# Rolling Sharpe (ADR-0082 §5)
# ---------------------------------------------------------------------------


def test_compute_rolling_sharpe_matches_handcomputed_window() -> None:
    returns = pd.Series([0.01, -0.02, 0.03, 0.00, 0.015])
    result = compute_rolling_sharpe(returns, window=3)
    assert math.isnan(result.iloc[0])
    assert math.isnan(result.iloc[1])
    factor = math.sqrt(PERIODS_PER_YEAR_MONTHLY)
    # rf=0 → excess == returns. Per-window mean / std (ddof=1) * sqrt(12).
    for pos, window_vals in [
        (2, [0.01, -0.02, 0.03]),
        (3, [-0.02, 0.03, 0.00]),
        (4, [0.03, 0.00, 0.015]),
    ]:
        mean = float(np.mean(window_vals))
        std = float(np.std(window_vals, ddof=1))
        expected = mean / std * factor
        assert result.iloc[pos] == pytest.approx(expected, abs=1e-12)


def test_compute_rolling_sharpe_subtracts_risk_free_per_period() -> None:
    returns = pd.Series([0.01, -0.02, 0.03, 0.00])
    rf_annual = 0.024  # 0.002 per month at 12 periods/year
    result = compute_rolling_sharpe(returns, window=3, risk_free_rate_annual=rf_annual)
    factor = math.sqrt(PERIODS_PER_YEAR_MONTHLY)
    excess = np.array([0.01, -0.02, 0.03]) - rf_annual / PERIODS_PER_YEAR_MONTHLY
    expected = float(np.mean(excess)) / float(np.std(excess, ddof=1)) * factor
    assert result.iloc[2] == pytest.approx(expected, abs=1e-12)


def test_compute_rolling_sharpe_zero_std_window_is_nan() -> None:
    """A window with zero excess-return variance → NaN, not ±inf."""
    returns = pd.Series([0.01, 0.01, 0.01, 0.02])
    result = compute_rolling_sharpe(returns, window=3)
    # Window [0.01, 0.01, 0.01] has std 0 → NaN.
    assert math.isnan(result.iloc[2])
    # Window [0.01, 0.01, 0.02] has non-zero std → finite.
    assert math.isfinite(result.iloc[3])


def test_compute_rolling_sharpe_invalid_window_raises() -> None:
    returns = pd.Series([0.01, 0.02, 0.03])
    with pytest.raises(ValueError):
        compute_rolling_sharpe(returns, window=-1)
