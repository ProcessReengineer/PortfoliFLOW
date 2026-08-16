# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.analytics.efficient_frontier``.

Pure-function tests against the sub-stream 5d analytics layer. The
underlying SLSQP optimiser is exercised at length under
``analytics/portfolio_optimizer`` — this module focuses on the
pandas-typed facade contract: derive_expected_returns_and_cov
(annualisation + alignment), the EfficientFrontierResult shape,
tangency / min-variance / CML reconstruction from the inputs the
result carries, and the current-portfolio evaluator.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from services.analytics.portfolio_optimizer import PortfolioConstraints, PortfolioOptimizer
from services.analytics.efficient_frontier import (
    CapitalMarketLine,
    EfficientFrontierResult,
    MinVariancePortfolio,
    TangencyPortfolio,
    compute_capital_market_line,
    compute_current_portfolio_position,
    compute_efficient_frontier,
    compute_min_variance_portfolio,
    compute_tangency_portfolio,
    derive_expected_returns_and_cov,
)


def _two_asset_universe() -> tuple[pd.Series, pd.DataFrame]:
    """Hand-verified two-asset universe.

    A: μ_a = 0.10, σ_a = 0.15
    B: μ_b = 0.04, σ_b = 0.06
    ρ_ab = 0.20
    """
    mu = pd.Series({"A": 0.10, "B": 0.04})
    sigma_a, sigma_b, rho = 0.15, 0.06, 0.20
    cov = pd.DataFrame(
        {
            "A": [sigma_a**2, rho * sigma_a * sigma_b],
            "B": [rho * sigma_a * sigma_b, sigma_b**2],
        },
        index=["A", "B"],
    )
    return mu, cov


def _three_asset_universe() -> tuple[pd.Series, pd.DataFrame]:
    """Three-asset hand-verified universe with weak correlations."""
    mu = pd.Series({"X": 0.08, "Y": 0.12, "Z": 0.05})
    sigmas = np.array([0.12, 0.20, 0.07])
    corr = np.array(
        [
            [1.0, 0.1, 0.05],
            [0.1, 1.0, 0.2],
            [0.05, 0.2, 1.0],
        ]
    )
    cov_arr = np.outer(sigmas, sigmas) * corr
    cov = pd.DataFrame(cov_arr, index=mu.index, columns=mu.index)
    return mu, cov


# ---------------------------------------------------------------------------
# derive_expected_returns_and_cov
# ---------------------------------------------------------------------------


def test_derive_expected_returns_and_cov_geometric_annualisation() -> None:
    """Mean is annualised geometrically; covariance scales by periods/year."""
    rng = np.random.default_rng(7)
    daily_a = rng.normal(0.001, 0.01, size=300)
    daily_b = rng.normal(0.0005, 0.02, size=300)
    idx = pd.date_range("2024-01-01", periods=300, freq="B")
    series_a = pd.Series(daily_a, index=idx)
    series_b = pd.Series(daily_b, index=idx)

    mu, cov = derive_expected_returns_and_cov({"A": series_a, "B": series_b}, periods_per_year=252)

    # Geometric annualisation: (1 + daily_mean)**252 - 1
    expected_a = (1.0 + float(series_a.mean())) ** 252 - 1.0
    expected_b = (1.0 + float(series_b.mean())) ** 252 - 1.0
    assert mu["A"] == pytest.approx(expected_a, abs=1e-12)
    assert mu["B"] == pytest.approx(expected_b, abs=1e-12)

    # Covariance scales linearly with periods per year.
    expected_cov = pd.DataFrame({"A": series_a, "B": series_b}).cov() * 252
    pd.testing.assert_frame_equal(cov, expected_cov, check_names=False)


def test_derive_expected_returns_preserves_input_order() -> None:
    idx = pd.date_range("2025-01-01", periods=10, freq="B")
    series_dict = {
        "Zeta": pd.Series(0.001, index=idx),
        "Alpha": pd.Series(0.002, index=idx),
        "Mu": pd.Series(0.0015, index=idx),
    }
    mu, cov = derive_expected_returns_and_cov(series_dict)
    assert list(mu.index) == ["Zeta", "Alpha", "Mu"]
    assert list(cov.index) == ["Zeta", "Alpha", "Mu"]
    assert list(cov.columns) == ["Zeta", "Alpha", "Mu"]


def test_derive_expected_returns_empty_input_raises() -> None:
    with pytest.raises(ValueError):
        derive_expected_returns_and_cov({})


# ---------------------------------------------------------------------------
# compute_efficient_frontier
# ---------------------------------------------------------------------------


def test_efficient_frontier_shape_and_monotonicity_two_assets() -> None:
    mu, cov = _two_asset_universe()
    result = compute_efficient_frontier(mu, cov, n_points=30)

    assert isinstance(result, EfficientFrontierResult)
    assert result.frontier_returns.shape == (30,)
    assert result.frontier_volatilities.shape == (30,)
    assert result.frontier_weights.shape == (30, 2)

    # Frontier is sorted by volatility ascending — same contract as
    # PortfolioOptimizer.efficient_frontier.
    diffs = np.diff(result.frontier_volatilities)
    assert (diffs >= -1e-10).all()

    # Each weight vector sums to ~1 and is non-negative (long-only default).
    sums = result.frontier_weights.sum(axis=1)
    assert np.allclose(sums, 1.0, atol=1e-6)
    assert (result.frontier_weights >= -1e-9).all()


def test_efficient_frontier_carries_inputs_for_downstream() -> None:
    mu, cov = _three_asset_universe()
    result = compute_efficient_frontier(mu, cov, n_points=20)

    np.testing.assert_allclose(result.expected_returns, mu.to_numpy(dtype=float))
    np.testing.assert_allclose(result.cov_matrix, cov.to_numpy(dtype=float))
    assert result.bounds_min == 0.0
    assert result.bounds_max == 1.0
    assert result.asset_names == list(mu.index)


def test_efficient_frontier_index_mismatch_raises() -> None:
    mu = pd.Series({"A": 0.1, "B": 0.05})
    cov = pd.DataFrame(
        np.eye(2),
        index=["A", "C"],
        columns=["A", "C"],
    )
    with pytest.raises(ValueError):
        compute_efficient_frontier(mu, cov)


# ---------------------------------------------------------------------------
# compute_tangency_portfolio + compute_min_variance_portfolio
# ---------------------------------------------------------------------------


def test_tangency_matches_underlying_optimizer() -> None:
    """The facade and the underlying optimiser yield identical numbers."""
    mu, cov = _three_asset_universe()
    rfr = 0.02

    efr = compute_efficient_frontier(mu, cov, n_points=50)
    tang = compute_tangency_portfolio(efr, risk_free_rate=rfr)

    optimizer = PortfolioOptimizer(
        expected_returns=mu.to_numpy(dtype=float),
        cov_matrix=cov.to_numpy(dtype=float),
        asset_names=list(mu.index),
        risk_free_rate=rfr,
        constraints=PortfolioConstraints(
            long_only=True,
            min_weights=np.zeros(len(mu)),
            max_weights=np.ones(len(mu)),
        ),
    )
    expected = optimizer.tangency_portfolio()

    assert tang.expected_return == pytest.approx(expected.expected_return, abs=1e-9)
    assert tang.volatility == pytest.approx(expected.volatility, abs=1e-9)
    assert tang.sharpe_ratio == pytest.approx(expected.sharpe_ratio, abs=1e-9)
    np.testing.assert_allclose(tang.weights, expected.weights, atol=1e-9)
    assert tang.asset_names == list(mu.index)


def test_min_variance_matches_underlying_optimizer() -> None:
    mu, cov = _three_asset_universe()

    efr = compute_efficient_frontier(mu, cov, n_points=50)
    mv = compute_min_variance_portfolio(efr)

    optimizer = PortfolioOptimizer(
        expected_returns=mu.to_numpy(dtype=float),
        cov_matrix=cov.to_numpy(dtype=float),
        asset_names=list(mu.index),
        risk_free_rate=0.0,
        constraints=PortfolioConstraints(
            long_only=True,
            min_weights=np.zeros(len(mu)),
            max_weights=np.ones(len(mu)),
        ),
    )
    expected = optimizer.minimum_variance_portfolio()

    assert mv.volatility == pytest.approx(expected.volatility, abs=1e-9)
    assert mv.expected_return == pytest.approx(expected.expected_return, abs=1e-9)
    np.testing.assert_allclose(mv.weights, expected.weights, atol=1e-9)


def test_tangency_lies_on_or_above_min_var_volatility() -> None:
    """The tangency portfolio cannot lie left of the global min-var point."""
    mu, cov = _three_asset_universe()
    efr = compute_efficient_frontier(mu, cov, n_points=80)
    tang = compute_tangency_portfolio(efr, risk_free_rate=0.02)
    mv = compute_min_variance_portfolio(efr)
    assert tang.volatility >= mv.volatility - 1e-8


def test_tangency_returns_match_dataclass_shape() -> None:
    mu, cov = _two_asset_universe()
    efr = compute_efficient_frontier(mu, cov, n_points=20)
    tang = compute_tangency_portfolio(efr, risk_free_rate=0.01)

    assert isinstance(tang, TangencyPortfolio)
    assert tang.weights.shape == (2,)
    assert math.isfinite(tang.expected_return)
    assert math.isfinite(tang.volatility)
    assert math.isfinite(tang.sharpe_ratio)


def test_min_variance_returns_match_dataclass_shape() -> None:
    mu, cov = _two_asset_universe()
    efr = compute_efficient_frontier(mu, cov, n_points=20)
    mv = compute_min_variance_portfolio(efr)
    assert isinstance(mv, MinVariancePortfolio)
    assert mv.weights.shape == (2,)


# ---------------------------------------------------------------------------
# compute_capital_market_line
# ---------------------------------------------------------------------------


def test_capital_market_line_starts_at_rf_and_passes_through_tangency() -> None:
    rfr = 0.025
    tang = TangencyPortfolio(
        weights=np.array([0.5, 0.5]),
        expected_return=0.10,
        volatility=0.12,
        sharpe_ratio=(0.10 - 0.025) / 0.12,
        asset_names=["A", "B"],
    )
    cml = compute_capital_market_line(risk_free_rate=rfr, tangency=tang, x_max=0.20, n_points=21)
    assert isinstance(cml, CapitalMarketLine)
    assert len(cml.points) == 21
    # First sample is at vol = 0 → return == rfr.
    first_vol, first_ret = cml.points[0]
    assert first_vol == pytest.approx(0.0, abs=1e-12)
    assert first_ret == pytest.approx(rfr, abs=1e-12)
    # The CML line passes exactly through (tang.vol, tang.ret); we
    # don't sample tang.vol exactly but the slope reproduces it.
    last_vol, last_ret = cml.points[-1]
    assert last_vol == pytest.approx(0.20, abs=1e-12)
    expected_last_ret = rfr + tang.sharpe_ratio * 0.20
    assert last_ret == pytest.approx(expected_last_ret, abs=1e-12)


# ---------------------------------------------------------------------------
# compute_current_portfolio_position
# ---------------------------------------------------------------------------


def test_current_portfolio_position_normalises_unit_weights() -> None:
    mu, cov = _two_asset_universe()
    weights = {"A": 1.0, "B": 1.0}  # Equal weights — sum 2; normalise.
    vol, ret = compute_current_portfolio_position(weights, mu, cov)
    expected_ret = 0.5 * mu["A"] + 0.5 * mu["B"]
    assert ret == pytest.approx(expected_ret, abs=1e-12)
    expected_var = (
        0.25 * cov.loc["A", "A"] + 0.25 * cov.loc["B", "B"] + 2 * 0.5 * 0.5 * cov.loc["A", "B"]
    )
    assert vol == pytest.approx(math.sqrt(expected_var), abs=1e-12)


def test_current_portfolio_position_treats_missing_as_zero_weight() -> None:
    mu, cov = _three_asset_universe()
    weights = {"X": 1.0}  # Y, Z absent → 0
    vol, ret = compute_current_portfolio_position(weights, mu, cov)
    # Single-asset portfolio in X.
    assert ret == pytest.approx(mu["X"], abs=1e-12)
    assert vol == pytest.approx(math.sqrt(cov.loc["X", "X"]), abs=1e-12)


def test_current_portfolio_position_empty_returns_nan() -> None:
    mu, cov = _two_asset_universe()
    vol, ret = compute_current_portfolio_position({}, mu, cov)
    assert math.isnan(vol)
    assert math.isnan(ret)
