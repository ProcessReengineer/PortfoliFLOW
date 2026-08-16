# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""QT-consistency regression for the sub-stream 5d efficient frontier.

The QT Front-Office widget at
``gui/widgets/portfolio_analysis_widget.py`` and the web analytics
layer at ``services/analytics/efficient_frontier.py`` must yield
the same tangency / minimum-variance / current-portfolio numerics
to within ``1e-6``. This test pins that contract by:

1. Synthesising a deterministic three-asset universe of daily
   returns (seeded RNG).
2. Reproducing the QT widget's annualisation step exactly:
   ``mu = (1 + daily_mean) ** 252 - 1`` and ``Σ = daily_cov * 252``.
3. Constructing a :class:`PortfolioOptimizer` with long-only
   constraints — the same construction the QT widget makes.
4. Calling :meth:`PortfolioOptimizer.tangency_portfolio` and
   :meth:`PortfolioOptimizer.minimum_variance_portfolio` directly
   (the QT path) and the
   :func:`services.analytics.compute_tangency_portfolio` /
   :func:`compute_min_variance_portfolio` facade (the web path).
5. Asserting the two paths agree on volatility, expected return,
   and Sharpe to within ``1e-6``.

Because the facade reconstructs the same :class:`PortfolioOptimizer`
under the hood (see ``services/analytics/efficient_frontier.py::_build_optimizer``),
the comparison is structural rather than numerical-by-coincidence:
deviation would mean a regression in the facade, not floating-point
drift.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from services.analytics.portfolio_optimizer import (
    PortfolioConstraints,
    PortfolioOptimizer,
)
from services.analytics import (
    compute_efficient_frontier,
    compute_min_variance_portfolio,
    compute_tangency_portfolio,
    derive_expected_returns_and_cov,
)

_QT_TOLERANCE = 1e-6


def _deterministic_universe() -> tuple[pd.Series, pd.DataFrame]:
    """Three-asset universe of daily returns annualised QT-style."""
    rng = np.random.default_rng(20260507)
    n_days = 600
    dates = pd.date_range("2023-01-02", periods=n_days, freq="B")

    # Mean daily returns (≈ 8 / 12 / 5 % p.a. arithmetic) and a
    # Cholesky factor that produces a positive-definite covariance.
    mean_daily = np.array([0.0003, 0.00045, 0.0002])
    chol = np.array(
        [
            [0.010, 0.000, 0.000],
            [0.003, 0.012, 0.000],
            [0.001, 0.002, 0.008],
        ]
    )
    shocks = rng.standard_normal((n_days, 3)) @ chol.T
    data = mean_daily + shocks

    df = pd.DataFrame(data, index=dates, columns=["A", "B", "C"])

    # QT-style annualisation — geometric for the mean, linear for cov.
    mu = (1.0 + df.mean()) ** 252 - 1.0
    cov = df.cov() * 252.0
    return mu, cov


def _qt_optimizer(mu: pd.Series, cov: pd.DataFrame, *, risk_free_rate: float) -> PortfolioOptimizer:
    """Replicate the QT widget's optimiser construction."""
    return PortfolioOptimizer(
        expected_returns=mu.to_numpy(dtype=float),
        cov_matrix=cov.to_numpy(dtype=float),
        asset_names=list(mu.index),
        risk_free_rate=risk_free_rate,
        constraints=PortfolioConstraints(long_only=True),
    )


def test_tangency_within_1e_6_of_qt() -> None:
    mu, cov = _deterministic_universe()
    rfr = 0.025

    qt_tang = _qt_optimizer(mu, cov, risk_free_rate=rfr).tangency_portfolio()

    efr = compute_efficient_frontier(mu, cov, n_points=100)
    web_tang = compute_tangency_portfolio(efr, risk_free_rate=rfr)

    assert web_tang.volatility == pytest.approx(qt_tang.volatility, abs=_QT_TOLERANCE)
    assert web_tang.expected_return == pytest.approx(qt_tang.expected_return, abs=_QT_TOLERANCE)
    assert web_tang.sharpe_ratio == pytest.approx(qt_tang.sharpe_ratio, abs=_QT_TOLERANCE)
    np.testing.assert_allclose(web_tang.weights, qt_tang.weights, atol=_QT_TOLERANCE)


def test_min_variance_within_1e_6_of_qt() -> None:
    mu, cov = _deterministic_universe()

    qt_mv = _qt_optimizer(mu, cov, risk_free_rate=0.0).minimum_variance_portfolio()

    efr = compute_efficient_frontier(mu, cov, n_points=100)
    web_mv = compute_min_variance_portfolio(efr)

    assert web_mv.volatility == pytest.approx(qt_mv.volatility, abs=_QT_TOLERANCE)
    assert web_mv.expected_return == pytest.approx(qt_mv.expected_return, abs=_QT_TOLERANCE)
    np.testing.assert_allclose(web_mv.weights, qt_mv.weights, atol=_QT_TOLERANCE)


def test_derive_expected_returns_matches_qt_annualisation() -> None:
    """``derive_expected_returns_and_cov`` reproduces the QT formulas."""
    rng = np.random.default_rng(123)
    n_days = 252
    dates = pd.date_range("2024-01-02", periods=n_days, freq="B")
    daily = rng.normal(0.0005, 0.011, size=(n_days, 2))
    df = pd.DataFrame(daily, index=dates, columns=["A", "B"])

    qt_mu = (1.0 + df.mean()) ** 252 - 1.0
    qt_cov = df.cov() * 252.0

    web_mu, web_cov = derive_expected_returns_and_cov(
        {"A": df["A"], "B": df["B"]}, periods_per_year=252
    )

    np.testing.assert_allclose(
        web_mu.to_numpy(dtype=float),
        qt_mu.to_numpy(dtype=float),
        atol=1e-12,
    )
    np.testing.assert_allclose(
        web_cov.to_numpy(dtype=float),
        qt_cov.to_numpy(dtype=float),
        atol=1e-12,
    )
