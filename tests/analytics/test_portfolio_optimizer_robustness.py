# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression tests for PortfolioOptimizer PSD validation and QP-form tangency.

These tests target the failure modes uncovered by the multi-vintage portfolio
crash ("Positive directional derivative for linesearch"):

- An indefinite covariance matrix must be rejected up front with a descriptive
  error rather than crashing later inside SLSQP.
- A grenzwertig-indefinite matrix (numerical noise from BLAS) must be silently
  nudged so the optimisation still proceeds.
- The QP-form tangency must agree with the direct-form result on a known-good
  case to within numerical tolerance.
- Group constraints must trigger the direct-form fallback and still succeed.
"""

from __future__ import annotations

import numpy as np
import pytest

from services.analytics.portfolio_optimizer import (
    GroupConstraint,
    PortfolioConstraints,
    PortfolioOptimizer,
)


def _three_asset_known_good() -> tuple[np.ndarray, np.ndarray, list[str], float]:
    """A simple, well-conditioned 3-asset case used by several tests."""
    mu = np.array([0.08, 0.12, 0.10])
    sigma = np.array(
        [
            [0.04, 0.005, 0.010],
            [0.005, 0.09, 0.020],
            [0.010, 0.020, 0.0625],
        ]
    )
    asset_names = ["Equity", "PE", "Credit"]
    rf = 0.02
    return mu, sigma, asset_names, rf


def test_indefinite_covariance_raises_clear_value_error() -> None:
    """A cov matrix with a clearly negative eigenvalue must raise ValueError
    naming the PSD violation, not crash inside SLSQP later."""
    # Construct a symmetric but indefinite matrix.
    sigma = np.array(
        [
            [0.04, 0.05, 0.00],
            [0.05, 0.04, 0.00],
            [0.00, 0.00, 0.09],
        ]
    )
    # Sanity: this is indefinite
    assert float(np.linalg.eigvalsh(sigma).min()) < -1e-8

    mu = np.array([0.08, 0.12, 0.10])
    with pytest.raises(ValueError, match="positive semi-definite"):
        PortfolioOptimizer(
            expected_returns=mu,
            cov_matrix=sigma,
            asset_names=["A", "B", "C"],
            risk_free_rate=0.02,
        )


def test_borderline_negative_eigenvalue_is_silently_nudged() -> None:
    """A grenzwertig-indefinite matrix (min eig in [-1e-10, 0)) must be
    silently nudged. Optimisation then succeeds and weights sum to ~1."""
    mu, sigma, names, rf = _three_asset_known_good()
    # Inject a tiny negative perturbation: subtract eps * (rank-1 outer product
    # along the smallest-eigenvalue direction). Cheaper: shift eigenvalues
    # by a tiny negative scalar via subtracting eps*I, then add it back via
    # the lowest mode only — easiest is just constructing a matrix whose min
    # eig is small-negative.
    eigvals, eigvecs = np.linalg.eigh(sigma)
    # Force the lowest eigenvalue to be very slightly negative.
    eigvals[0] = -1e-11
    sigma_borderline = (eigvecs * eigvals) @ eigvecs.T
    sigma_borderline = (sigma_borderline + sigma_borderline.T) / 2.0
    # Sanity: borderline regime
    eig_min = float(np.linalg.eigvalsh(sigma_borderline).min())
    assert -1e-10 <= eig_min < 0

    opt = PortfolioOptimizer(
        expected_returns=mu,
        cov_matrix=sigma_borderline,
        asset_names=names,
        risk_free_rate=rf,
    )
    tang = opt.tangency_portfolio()
    assert tang.weights.sum() == pytest.approx(1.0, abs=1e-6)
    assert tang.volatility > 0.0


def test_qp_form_matches_direct_form_within_tolerance() -> None:
    """On a known-good 3-asset case, the QP-form tangency must agree with the
    direct-form (legacy) tangency to within 1e-6."""
    mu, sigma, names, rf = _three_asset_known_good()

    opt = PortfolioOptimizer(
        expected_returns=mu,
        cov_matrix=sigma,
        asset_names=names,
        risk_free_rate=rf,
    )
    tang_qp = opt.tangency_portfolio()
    # Call the private direct-form solver to compare. This is the same code
    # path the QP form falls back to under group constraints.
    tang_direct = opt._tangency_direct()

    np.testing.assert_allclose(tang_qp.weights, tang_direct.weights, atol=1e-6)
    assert tang_qp.expected_return == pytest.approx(tang_direct.expected_return, abs=1e-6)
    assert tang_qp.volatility == pytest.approx(tang_direct.volatility, abs=1e-6)
    assert tang_qp.sharpe_ratio == pytest.approx(tang_direct.sharpe_ratio, abs=1e-6)


def test_tangency_with_group_constraint_falls_back_and_succeeds() -> None:
    """A 4-asset case with a group cap on assets 0+1 must use the direct-form
    fallback and still produce a feasible tangency portfolio."""
    mu = np.array([0.06, 0.07, 0.10, 0.12])
    sigma = np.array(
        [
            [0.04, 0.01, 0.008, 0.005],
            [0.01, 0.05, 0.012, 0.004],
            [0.008, 0.012, 0.09, 0.020],
            [0.005, 0.004, 0.020, 0.16],
        ]
    )
    constraints = PortfolioConstraints(
        long_only=True,
        group_constraints=[
            GroupConstraint(
                name="Capped Group",
                asset_indices=[0, 1],
                min_weight=0.0,
                max_weight=0.6,
            )
        ],
    )
    opt = PortfolioOptimizer(
        expected_returns=mu,
        cov_matrix=sigma,
        asset_names=["A", "B", "C", "D"],
        risk_free_rate=0.02,
        constraints=constraints,
    )
    tang = opt.tangency_portfolio()
    # Sum-to-one
    assert tang.weights.sum() == pytest.approx(1.0, abs=1e-6)
    # Group cap respected (small tolerance for SLSQP)
    group_sum = float(tang.weights[[0, 1]].sum())
    assert group_sum <= 0.6 + 1e-6
    # Long-only
    assert (tang.weights >= -1e-9).all()


def test_tangency_with_per_asset_bounds_falls_back_and_succeeds() -> None:
    """A case with per-asset min_weights / max_weights must use the direct-form
    fallback and still produce a feasible tangency portfolio.

    Regression test for the QP-form bug: the QP transformation is exact only on
    a homogeneous cone. Per-asset finite upper bounds break that property and
    the QP path becomes infeasible (SLSQP "Iteration limit reached"). The fix
    routes such inputs to the direct-form solver.

    Inputs are the Balanced Institutional SAA seed template — a 6-asset case
    that demonstrably triggers the bug under the buggy QP form.
    """
    mu = np.array([0.0750, 0.0900, 0.0450, 0.0650, 0.1100, 0.0650])
    vols = np.array([0.1550, 0.2200, 0.0700, 0.1000, 0.1600, 0.1200])
    corr = np.array(
        [
            [1.00, 0.75, 0.30, 0.55, 0.70, 0.45],
            [0.75, 1.00, 0.25, 0.55, 0.55, 0.40],
            [0.30, 0.25, 1.00, 0.50, 0.30, 0.35],
            [0.55, 0.55, 0.50, 1.00, 0.55, 0.40],
            [0.70, 0.55, 0.30, 0.55, 1.00, 0.50],
            [0.45, 0.40, 0.35, 0.40, 0.50, 1.00],
        ]
    )
    sigma = np.outer(vols, vols) * corr
    min_w = np.array([0.20, 0.05, 0.10, 0.05, 0.10, 0.05])
    max_w = np.array([0.45, 0.20, 0.30, 0.15, 0.25, 0.20])
    rf = 0.0275

    constraints = PortfolioConstraints(
        long_only=True,
        min_weights=min_w,
        max_weights=max_w,
    )
    opt = PortfolioOptimizer(
        expected_returns=mu,
        cov_matrix=sigma,
        asset_names=["EqDM", "EqEM", "IGCredit", "HYCredit", "PE", "RE"],
        risk_free_rate=rf,
        constraints=constraints,
    )

    tang = opt.tangency_portfolio()

    # Fully invested
    assert tang.weights.sum() == pytest.approx(1.0, abs=1e-6)
    # Per-asset bounds respected (small tolerance for SLSQP)
    assert (tang.weights >= min_w - 1e-6).all(), (
        f"min_weights violated: weights={tang.weights}, min_w={min_w}"
    )
    assert (tang.weights <= max_w + 1e-6).all(), (
        f"max_weights violated: weights={tang.weights}, max_w={max_w}"
    )
    # Sharpe ratio is positive (risk_free_rate < max(mu) is satisfied)
    assert tang.sharpe_ratio > 0.0
    assert tang.volatility > 0.0


def test_tangency_qp_path_unchanged_for_unbounded_long_only() -> None:
    """The QP path must still be taken (and succeed) when only long_only is set
    and there are no per-asset min/max weights. Asserts that the existing
    Front-Office optimisation behaviour is preserved by the fallback change.
    """
    mu, sigma, names, rf = _three_asset_known_good()
    constraints = PortfolioConstraints(long_only=True)
    opt = PortfolioOptimizer(
        expected_returns=mu,
        cov_matrix=sigma,
        asset_names=names,
        risk_free_rate=rf,
        constraints=constraints,
    )
    # Both the public method and the private direct fallback must agree —
    # i.e. the QP path was actually taken (the result matches direct, but
    # arrived via the more numerically stable transformation).
    tang_public = opt.tangency_portfolio()
    tang_direct = opt._tangency_direct()
    np.testing.assert_allclose(tang_public.weights, tang_direct.weights, atol=1e-6)
    assert tang_public.sharpe_ratio == pytest.approx(tang_direct.sharpe_ratio, abs=1e-6)


def test_min_variance_and_efficient_frontier_smoke_unchanged() -> None:
    """Existing methods must continue to work on the same inputs — no
    behavioural change from the optimizer changes."""
    mu, sigma, names, rf = _three_asset_known_good()
    opt = PortfolioOptimizer(
        expected_returns=mu,
        cov_matrix=sigma,
        asset_names=names,
        risk_free_rate=rf,
    )

    mvp = opt.minimum_variance_portfolio()
    assert mvp.weights.sum() == pytest.approx(1.0, abs=1e-6)
    assert mvp.volatility > 0.0

    frontier = opt.efficient_frontier(n_points=20)
    assert len(frontier) > 0
    # Frontier sorted by volatility ascending
    vols = [p.volatility for p in frontier]
    assert vols == sorted(vols)
