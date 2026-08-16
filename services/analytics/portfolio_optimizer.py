# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Mean-variance portfolio optimisation engine for PortfoliFLOW.

This module provides a standalone, stateless portfolio optimiser built on
scipy.optimize.minimize (SLSQP). It is designed to be importable without any
PortfoliFLOW application infrastructure — no GUI, no DataStore, no configuration
files are required.

Typical usage::

    import numpy as np
    from services.analytics.portfolio_optimizer import PortfolioOptimizer, PortfolioConstraints

    opt = PortfolioOptimizer(
        expected_returns=np.array([0.08, 0.12, 0.06]),
        cov_matrix=cov,
        asset_names=["Equity", "PE", "Bonds"],
        risk_free_rate=0.02,
    )
    frontier = opt.efficient_frontier(n_points=100)
    tangency = opt.tangency_portfolio()
"""

import logging
import time
from dataclasses import dataclass, field
from collections.abc import Callable

import numpy as np
from scipy.optimize import OptimizeResult, minimize

_logger = logging.getLogger(__name__)

# SLSQP options shared by every optimisation in this module.
# Centralised so a single change propagates to all callers.
_SLSQP_OPTIONS = {"ftol": 1e-12, "maxiter": 1000}


@dataclass(frozen=True)
class PortfolioResult:
    """Result of a single portfolio optimisation.

    Attributes:
        weights: Array of asset weights summing to 1.0.
        expected_return: Annualised expected portfolio return.
        volatility: Annualised portfolio standard deviation.
        sharpe_ratio: (expected_return - risk_free_rate) / volatility.
        asset_names: Human-readable asset labels matching weights order.
    """

    weights: np.ndarray
    expected_return: float
    volatility: float
    sharpe_ratio: float
    asset_names: list[str]


@dataclass
class GroupConstraint:
    """Weight constraint for a group of assets (e.g. an asset class).

    Example: "Private Equity" assets (indices [4, 5, 6]) may not exceed
    30% combined weight.

    Attributes:
        name: Human-readable group label (e.g. "Private Equity").
        asset_indices: Indices into the asset array identifying group members.
        min_weight: Minimum combined weight for this group (default 0.0).
        max_weight: Maximum combined weight for this group (default 1.0).
    """

    name: str
    asset_indices: list[int]
    min_weight: float = 0.0
    max_weight: float = 1.0


@dataclass
class PortfolioConstraints:
    """Constraint set for portfolio optimisation.

    This structure is designed to be extensible: a future mandate-restrictions
    module can translate regulatory rules (e.g. ESG exclusions, asset-class caps)
    into PortfolioConstraints without changing the optimizer itself.

    To exclude an asset: set its entry in max_weights to 0.0.
    To enforce a regulatory asset-class cap: add a GroupConstraint.

    Attributes:
        long_only: If True, all weights >= 0 (no short selling).
        min_weights: Per-asset minimum weights (1-D array, or None for no minimums).
        max_weights: Per-asset maximum weights (1-D array, or None for no maximums).
        group_constraints: List of GroupConstraint for asset-class-level limits.
    """

    long_only: bool = True
    min_weights: np.ndarray | None = None
    max_weights: np.ndarray | None = None
    group_constraints: list[GroupConstraint] = field(default_factory=list)


class PortfolioOptimizer:
    """Mean-variance portfolio optimiser using scipy.optimize.

    This class is the computational core of PortfoliFLOW's portfolio analysis.
    It is designed to be called from multiple contexts:

    - Front Office GUI (efficient frontier visualisation)
    - Strategic Asset Allocation module (Back Office, future)
    - AI Assistants (programmatic portfolio advice)

    The optimizer is stateless after construction — all inputs are provided at
    __init__ time, and all methods return new PortfolioResult objects without
    mutating internal state.

    Mathematical background:
        The efficient frontier is the set of portfolios that minimise variance
        for each level of expected return, subject to constraints. The tangency
        portfolio (market portfolio) maximises the Sharpe ratio and is found
        where a line from the risk-free rate is tangent to the efficient frontier.

    Args:
        expected_returns: 1-D array of annualised expected returns per asset.
        cov_matrix: 2-D annualised covariance matrix (n × n).
            Must be symmetric positive semi-definite.
        asset_names: Human-readable names for each asset, matching the order
            of expected_returns and cov_matrix rows/columns.
        risk_free_rate: Annualised risk-free rate for Sharpe ratio computation
            and Capital Market Line.
        constraints: Portfolio constraints (weight bounds, group limits).
            Defaults to long-only with no further restrictions.

    Raises:
        ValueError: If input dimensions are inconsistent, asset count < 2,
            or cov_matrix is not square/symmetric.
    """

    def __init__(
        self,
        expected_returns: np.ndarray,
        cov_matrix: np.ndarray,
        asset_names: list[str],
        risk_free_rate: float = 0.0,
        constraints: PortfolioConstraints | None = None,
    ) -> None:
        expected_returns = np.asarray(expected_returns, dtype=float)
        cov_matrix = np.asarray(cov_matrix, dtype=float)
        n = len(expected_returns)

        if expected_returns.ndim != 1:
            raise ValueError(f"expected_returns must be 1-D, got shape {expected_returns.shape}")
        if n < 2:
            raise ValueError(f"At least 2 assets required, got {n}")
        if cov_matrix.ndim != 2:
            raise ValueError(f"cov_matrix must be 2-D, got shape {cov_matrix.shape}")
        if cov_matrix.shape != (n, n):
            raise ValueError(
                f"cov_matrix shape {cov_matrix.shape} does not match expected_returns length {n}"
            )
        if not np.allclose(cov_matrix, cov_matrix.T, atol=1e-8):
            raise ValueError("cov_matrix must be approximately symmetric (|Σ - Σᵀ| < 1e-8)")
        if len(asset_names) != n:
            raise ValueError(
                f"asset_names length {len(asset_names)} does not match expected_returns length {n}"
            )
        if constraints is not None:
            if (
                constraints.min_weights is not None
                and len(np.asarray(constraints.min_weights)) != n
            ):
                raise ValueError(
                    f"constraints.min_weights length "
                    f"{len(np.asarray(constraints.min_weights))} != n_assets {n}"
                )
            if (
                constraints.max_weights is not None
                and len(np.asarray(constraints.max_weights)) != n
            ):
                raise ValueError(
                    f"constraints.max_weights length "
                    f"{len(np.asarray(constraints.max_weights))} != n_assets {n}"
                )

        self._mu: np.ndarray = expected_returns
        # Defensively symmetrise to absorb tiny floating-point asymmetry
        self._sigma: np.ndarray = (cov_matrix + cov_matrix.T) / 2.0

        # PSD validation. The sample covariance from non-overlapping
        # investment lifetimes can be indefinite when computed pairwise;
        # even on a complete-case window, BLAS rounding can produce a
        # tiny negative eigenvalue. Reject the former, nudge the latter.
        eig_min = float(np.linalg.eigvalsh(self._sigma).min())
        if eig_min < -1e-8:
            raise ValueError(
                f"cov_matrix is not positive semi-definite "
                f"(min eigenvalue {eig_min:.2e}). This typically indicates "
                f"pairwise covariance over time series of unequal length — "
                f"restrict to the common observation window first using "
                f"analytics.sample_window.restrict_to_common_window()."
            )
        if eig_min < 0:
            self._sigma = self._sigma + (abs(eig_min) + 1e-12) * np.eye(n)
            _logger.debug("cov_matrix nudged to PSD (original min eig %.2e)", eig_min)

        self._asset_names: list[str] = list(asset_names)
        self._rf: float = float(risk_free_rate)
        self._n: int = n
        self._constraints: PortfolioConstraints = (
            constraints if constraints is not None else PortfolioConstraints()
        )

    # ------------------------------------------------------------------
    # Private helpers — objective functions and optimiser plumbing
    # ------------------------------------------------------------------

    def _variance(self, w: np.ndarray) -> float:
        """Portfolio variance w'Σw. Used as objective in min-variance problems."""
        return float(w @ self._sigma @ w)

    def _variance_grad(self, w: np.ndarray) -> np.ndarray:
        """Gradient of w'Σw with respect to w is 2Σw."""
        return 2.0 * (self._sigma @ w)

    def _equal_weight_w0(self) -> np.ndarray:
        """Equal-weight starting point — always feasible for sum=1."""
        return np.full(self._n, 1.0 / self._n)

    def _build_bounds(self) -> list[tuple[float | None, float | None]]:
        """Build per-asset weight bounds for scipy.optimize.minimize.

        Combines long_only with per-asset min_weights and max_weights from
        PortfolioConstraints. None means unconstrained in that direction.

        Returns:
            List of (lower, upper) tuples, one per asset.
        """
        c = self._constraints
        bounds: list[tuple[float | None, float | None]] = []
        for i in range(self._n):
            lb: float | None = 0.0 if c.long_only else None
            ub: float | None = None
            if c.min_weights is not None:
                per_asset_min = float(c.min_weights[i])
                lb = max(lb, per_asset_min) if lb is not None else per_asset_min
            if c.max_weights is not None:
                ub = float(c.max_weights[i])
            bounds.append((lb, ub))
        return bounds

    def _build_constraints(self, target_return: float | None = None) -> list[dict]:
        """Build scipy constraint dicts for SLSQP optimisation.

        Always includes: equality constraint sum(w) = 1 (fully invested portfolio).
        Optionally includes: equality constraint w'μ = target_return.
        For each GroupConstraint: two inequality constraints on the group weight sum.

        scipy 'ineq' constraints require fun(w) >= 0, so:
        - group min: fun(w) = sum(w_group) - group.min_weight >= 0
        - group max: fun(w) = group.max_weight - sum(w_group) >= 0

        Default arguments in lambdas prevent the late-binding closure bug that
        would otherwise cause all loop iterations to share the last value.

        Args:
            target_return: If given, adds an equality constraint fixing the
                portfolio return to exactly this value.

        Returns:
            List of scipy constraint dicts for use with minimize(..., constraints=...).
        """
        cons: list[dict] = [
            {"type": "eq", "fun": lambda w: float(np.sum(w)) - 1.0},
        ]

        if target_return is not None:
            tr = float(target_return)
            cons.append(
                {
                    "type": "eq",
                    "fun": lambda w, _tr=tr: float(w @ self._mu) - _tr,
                }
            )

        for gc in self._constraints.group_constraints:
            indices = list(gc.asset_indices)
            group_min = float(gc.min_weight)
            group_max = float(gc.max_weight)

            if group_min > 0.0:
                cons.append(
                    {
                        "type": "ineq",
                        "fun": lambda w, _idx=indices, _mn=group_min: float(np.sum(w[_idx])) - _mn,
                    }
                )
            if group_max < 1.0:
                cons.append(
                    {
                        "type": "ineq",
                        "fun": lambda w, _idx=indices, _mx=group_max: _mx - float(np.sum(w[_idx])),
                    }
                )

        return cons

    def _run_slsqp(
        self,
        objective: Callable[[np.ndarray], float],
        *,
        jac: Callable[[np.ndarray], np.ndarray] | None = None,
        w0: np.ndarray | None = None,
        bounds: list | None = None,
        constraints: list[dict] | None = None,
        label: str,
        raise_on_failure: bool = True,
    ) -> OptimizeResult:
        """Run scipy SLSQP with PortfoliFLOW's standard options, timing, and logging.

        Centralises the boilerplate that would otherwise repeat in every
        optimisation method: equal-weight w0 default, standard bounds and
        constraints, fixed ftol/maxiter, perf timing, debug logging, and
        success-handling.

        Args:
            objective: Function to minimise.
            jac: Optional analytic gradient. If None, scipy uses finite differences.
            w0: Initial guess. Defaults to equal weights.
            bounds: Per-asset bounds. Defaults to self._build_bounds().
            constraints: Constraint dicts. Defaults to self._build_constraints().
            label: Identifier for log lines and error messages.
            raise_on_failure: If True (default), raise ValueError on non-convergence.
                Set False when the caller wants to inspect the result manually
                (e.g. efficient_frontier skips failed points instead of raising).

        Returns:
            scipy OptimizeResult. Caller is responsible for extracting result.x.

        Raises:
            ValueError: If raise_on_failure=True and optimisation fails.
        """
        if w0 is None:
            w0 = self._equal_weight_w0()
        if bounds is None:
            bounds = self._build_bounds()
        if constraints is None:
            constraints = self._build_constraints()

        _logger.debug("%s: starting (n_assets=%d)", label, self._n)
        t0 = time.perf_counter()

        result: OptimizeResult = minimize(
            objective,
            w0,
            jac=jac,
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options=_SLSQP_OPTIONS,
        )

        elapsed = time.perf_counter() - t0
        _logger.debug(
            "%s: done in %.3fs, success=%s, message=%s",
            label,
            elapsed,
            result.success,
            result.message,
        )

        if raise_on_failure and not result.success:
            raise ValueError(f"{label} failed: {result.message}")

        return result

    def _portfolio_stats(self, weights: np.ndarray) -> tuple[float, float, float]:
        """Compute expected return, volatility, and Sharpe ratio for a weight vector.

        Returns:
            Tuple of (expected_return, volatility, sharpe_ratio).
            Sharpe ratio is 0.0 if volatility is effectively zero (< 1e-10).
        """
        er = float(weights @ self._mu)
        # w'Σw can be slightly negative due to floating-point; clamp to zero
        variance = max(self._variance(weights), 0.0)
        vol = float(np.sqrt(variance))
        sharpe = (er - self._rf) / vol if vol > 1e-10 else 0.0
        return er, vol, sharpe

    def _make_result(self, weights: np.ndarray) -> PortfolioResult:
        """Wrap a weight vector in a PortfolioResult with computed statistics."""
        w = np.asarray(weights, dtype=float)
        er, vol, sharpe = self._portfolio_stats(w)
        return PortfolioResult(
            weights=w,
            expected_return=er,
            volatility=vol,
            sharpe_ratio=sharpe,
            asset_names=self._asset_names,
        )

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def minimum_variance_portfolio(self) -> PortfolioResult:
        """Find the global minimum-variance portfolio.

        Method: Minimises w'Σw subject to sum(w) = 1 and constraints.
        This is the leftmost point on the efficient frontier.

        Returns:
            PortfolioResult for the minimum-variance portfolio.

        Raises:
            ValueError: If optimisation fails to converge.
        """
        result = self._run_slsqp(
            self._variance,
            jac=self._variance_grad,
            label="minimum_variance_portfolio",
        )
        return self._make_result(result.x)

    def tangency_portfolio(self) -> PortfolioResult:
        """Find the portfolio that maximises the Sharpe ratio.

        This is the market portfolio in CAPM — the point where a line from the
        risk-free rate is tangent to the efficient frontier.

        Method: Solves the QP transformation
            min_y  y'Σy   s.t.  (μ-rf)'y = 1, y satisfies per-asset bounds
        and recovers w = y / sum(y). This is mathematically equivalent to the
        direct Sharpe maximisation but numerically far more stable. When a
        GroupConstraint is present the optimiser falls back to the direct
        form (group inequalities do not translate cleanly to the unnormalised
        y-space).

        Returns:
            PortfolioResult for the tangency portfolio.

        Raises:
            ValueError: If optimisation fails to converge, or if the
                risk-free rate is >= all expected returns (Sharpe always ≤ 0).
        """
        max_mu = float(np.max(self._mu))
        if self._rf >= max_mu:
            raise ValueError(
                f"risk_free_rate ({self._rf:.4f}) >= max(expected_returns) "
                f"({max_mu:.4f}). The Sharpe ratio has no positive maximum — "
                f"the tangency portfolio is undefined."
            )

        # The QP transformation is exact only when the feasible region for y
        # is invariant under positive scaling (a homogeneous cone). Group
        # constraints break this because group-weight inequalities become
        # non-affine in unnormalised y. Per-asset finite bounds break it for
        # the same reason: in y-space (where y is unnormalised, w = y/sum(y))
        # a finite upper bound is no longer scale-invariant, and the QP
        # constraint (mu-rf)'y = 1 typically forces y far outside w-space
        # bounds — making the problem infeasible. In all such cases, fall
        # back to direct Sharpe maximisation in w-space, which uses the
        # standard bounds and constraints unmodified.
        if (
            self._constraints.group_constraints
            or self._constraints.min_weights is not None
            or self._constraints.max_weights is not None
        ):
            return self._tangency_direct()

        # QP transformation: argmax (mu-rf)'w / sqrt(w'Σw)  s.t. constraints
        # is equivalent to argmin y'Σy  s.t. (mu-rf)'y = 1, y satisfies the
        # same per-asset bounds, then w = y / sum(y).
        mu_excess = self._mu - self._rf

        result = self._run_slsqp(
            self._variance,
            jac=self._variance_grad,
            constraints=[
                {
                    "type": "eq",
                    "fun": lambda y, _e=mu_excess: float(_e @ y) - 1.0,
                }
            ],
            label="tangency_portfolio[QP]",
        )

        y = result.x
        y_sum = float(y.sum())
        if y_sum <= 0:
            raise ValueError(
                "tangency_portfolio: QP yielded non-positive y-sum — "
                "the tangency portfolio may be undefined for these inputs."
            )

        return self._make_result(y / y_sum)

    def _tangency_direct(self) -> PortfolioResult:
        """Direct-form Sharpe maximisation (fallback for group constraints).

        Method: Minimises the negative Sharpe ratio:
            min_w  -(w'μ - rf) / sqrt(w'Σw)
        subject to sum(w) = 1 and all constraints (including group
        constraints).

        Returns:
            PortfolioResult for the tangency portfolio.

        Raises:
            ValueError: If optimisation fails to converge.
        """

        def neg_sharpe(w: np.ndarray) -> float:
            # Floor variance at 1e-20 to prevent NaN at zero-variance portfolios.
            variance = max(self._variance(w), 1e-20)
            er = float(w @ self._mu)
            return -(er - self._rf) / np.sqrt(variance)

        # Use the minimum-variance portfolio as initial guess — it is always
        # feasible and lies on the efficient frontier, close to the tangency point.
        try:
            w0 = self.minimum_variance_portfolio().weights.copy()
        except ValueError:
            w0 = self._equal_weight_w0()

        result = self._run_slsqp(
            neg_sharpe,
            w0=w0,
            label="tangency_portfolio[direct]",
        )
        return self._make_result(result.x)

    def efficient_frontier(self, n_points: int = 100) -> list[PortfolioResult]:
        """Compute n_points along the efficient frontier.

        Method: Determines the feasible return range by finding the
        minimum-variance portfolio return (lower bound) and the maximum
        achievable single-asset return (upper bound). Then solves n_points
        constrained minimum-variance problems at linearly spaced target
        returns using scipy.optimize.minimize (SLSQP).

        Points where the optimiser fails to converge are skipped with a
        DEBUG-level log warning (they typically occur at the extreme ends
        of the feasible range).

        Args:
            n_points: Number of points to compute. Higher values give a
                smoother curve at the cost of computation time.
                Range: 20–500, default 100.

        Returns:
            List of PortfolioResult sorted by ascending volatility.
        """
        _logger.debug("efficient_frontier: computing %d points (n_assets=%d)", n_points, self._n)
        t0 = time.perf_counter()

        # Lower bound: minimum-variance portfolio return (leftmost feasible point)
        try:
            mvp = self.minimum_variance_portfolio()
            return_lower = mvp.expected_return
            w_warm = mvp.weights.copy()
        except ValueError as exc:
            _logger.debug("efficient_frontier: min-var failed (%s), falling back to min(mu)", exc)
            return_lower = float(np.min(self._mu))
            w_warm = self._equal_weight_w0()

        # Upper bound: maximum achievable single-asset expected return
        return_upper = float(np.max(self._mu))

        if return_upper <= return_lower + 1e-8:
            _logger.debug(
                "efficient_frontier: degenerate return range [%.4f, %.4f] — returning MVP only",
                return_lower,
                return_upper,
            )
            try:
                return [self.minimum_variance_portfolio()]
            except ValueError:
                return []

        target_returns = np.linspace(return_lower, return_upper, n_points)
        results: list[PortfolioResult] = []
        n_failed = 0

        # Inner loop: skip-on-failure rather than raise. We use _run_slsqp
        # with raise_on_failure=False so the helper handles timing/logging
        # but lets us decide what to do with non-converged points.
        for tr in target_returns:
            res = self._run_slsqp(
                self._variance,
                jac=self._variance_grad,
                w0=w_warm,
                constraints=self._build_constraints(target_return=float(tr)),
                label=f"efficient_frontier[tr={tr:.4f}]",
                raise_on_failure=False,
            )
            if res.success:
                results.append(self._make_result(res.x))
                # Warm-start: adjacent frontier points are close, so reusing
                # the previous solution improves convergence.
                w_warm = res.x.copy()
            else:
                n_failed += 1

        elapsed = time.perf_counter() - t0
        _logger.debug(
            "efficient_frontier: done in %.3fs, %d/%d points computed, %d skipped",
            elapsed,
            len(results),
            n_points,
            n_failed,
        )

        # Sort ascending by volatility (left-to-right on the risk axis)
        results.sort(key=lambda p: p.volatility)
        return results

    def capital_market_line(self, n_points: int = 50) -> list[tuple[float, float]]:
        """Compute points on the Capital Market Line.

        The CML is the straight line from (0, risk_free_rate) through the
        tangency portfolio, extended to 1.5× the tangency portfolio's volatility.

        Args:
            n_points: Number of (volatility, return) pairs to generate.

        Returns:
            List of (volatility, expected_return) tuples defining the CML.
        """
        _logger.debug("capital_market_line: computing %d points", n_points)
        tang = self.tangency_portfolio()

        # CML equation: return(vol) = rf + Sharpe * vol
        # This follows from the definition: Sharpe = (tang.return - rf) / tang.vol,
        # so at any volatility v: return = rf + Sharpe * v
        vol_max = 1.5 * tang.volatility
        vols = np.linspace(0.0, vol_max, n_points)

        return [(float(v), float(self._rf + tang.sharpe_ratio * v)) for v in vols]

    def random_portfolios(self, n_portfolios: int = 5000) -> list[PortfolioResult]:
        """Generate random feasible portfolios for the opportunity-set cloud.

        Used for visualisation: the scatter of random portfolios shows the full
        opportunity set, making the efficient frontier's superiority visually
        apparent.

        Method: Generates random weight vectors using Dirichlet distribution
        (long_only) or normalised uniform (unrestricted). Applies min/max weight
        bounds by clipping and renormalising. Respects group constraints by
        rejection sampling (up to 10× n_portfolios attempts, then returns
        whatever was collected).

        Args:
            n_portfolios: Target number of random portfolios to generate.

        Returns:
            List of PortfolioResult for the generated portfolios.
        """
        _logger.debug(
            "random_portfolios: generating %d portfolios (n_assets=%d)",
            n_portfolios,
            self._n,
        )
        t0 = time.perf_counter()

        c = self._constraints
        # Non-seeded RNG for visualisation variety — each call gives a fresh cloud
        rng = np.random.default_rng()

        # Pre-compute per-asset clip bounds to avoid recomputing inside the loop
        lb = np.zeros(self._n) if c.long_only else np.full(self._n, -1.0)
        ub = np.ones(self._n)
        if c.min_weights is not None:
            lb = np.maximum(lb, np.asarray(c.min_weights, dtype=float))
        if c.max_weights is not None:
            ub = np.minimum(ub, np.asarray(c.max_weights, dtype=float))

        results: list[PortfolioResult] = []
        max_attempts = n_portfolios * 10
        total_attempts = 0
        batch_size = 500

        while len(results) < n_portfolios and total_attempts < max_attempts:
            bs = min(batch_size, max_attempts - total_attempts, n_portfolios)
            total_attempts += bs

            if c.long_only:
                # Dirichlet(α=1,...,1) = uniform distribution on the probability simplex.
                # All weights >= 0, sum = 1 by construction — no normalisation needed.
                raw = rng.dirichlet(np.ones(self._n), size=bs)
            else:
                # Uniform random weights in [-1, 1] normalised to sum=1 (allows shorts).
                raw = rng.uniform(-1.0, 1.0, size=(bs, self._n))
                row_sums = raw.sum(axis=1, keepdims=True)
                row_sums = np.where(np.abs(row_sums) < 1e-8, 1e-8, row_sums)
                raw = raw / row_sums

            # Apply per-asset bounds: clip to [lb, ub] then renormalise to sum=1
            clipped = np.clip(raw, lb, ub)
            row_sums = clipped.sum(axis=1, keepdims=True)

            for i in range(bs):
                if len(results) >= n_portfolios:
                    break
                s = float(row_sums[i, 0])
                if abs(s) < 1e-12:
                    continue  # Degenerate sample — skip

                w = clipped[i] / s

                # Rejection sampling for group constraints
                if c.group_constraints:
                    feasible = True
                    for gc in c.group_constraints:
                        group_sum = float(np.sum(w[gc.asset_indices]))
                        if group_sum < gc.min_weight - 1e-6 or group_sum > gc.max_weight + 1e-6:
                            feasible = False
                            break
                    if not feasible:
                        continue

                results.append(self._make_result(w))

        elapsed = time.perf_counter() - t0
        _logger.debug(
            "random_portfolios: generated %d/%d in %.3fs (%d attempts)",
            len(results),
            n_portfolios,
            elapsed,
            total_attempts,
        )

        if len(results) < n_portfolios:
            _logger.debug(
                "random_portfolios: target not reached (%d/%d) — group constraints may be tight",
                len(results),
                n_portfolios,
            )

        return results

    def evaluate_portfolio(self, weights: np.ndarray) -> PortfolioResult:
        """Compute risk/return characteristics for a given weight vector.

        This method does NOT optimise — it simply evaluates the portfolio
        defined by the given weights against the expected returns and
        covariance matrix stored in this optimizer instance.

        Use case: evaluating the current (actual) portfolio allocation
        to compare it against the efficient frontier.

        Args:
            weights: 1-D array of asset weights. Must have the same length
                as the asset count. Does not need to sum to 1.0 — the method
                normalises automatically if the sum deviates.

        Returns:
            PortfolioResult with the portfolio's expected return, volatility,
            Sharpe ratio, and the (possibly normalised) weights.

        Raises:
            ValueError: If weights length does not match asset count.
        """
        w = np.asarray(weights, dtype=float)
        if w.ndim != 1 or len(w) != self._n:
            raise ValueError(
                f"weights must be a 1-D array of length {self._n}, got shape {w.shape}"
            )

        # Normalise if the sum deviates from 1.0 (e.g. due to rounding)
        total = float(np.sum(w))
        if abs(total - 1.0) > 1e-8:
            if abs(total) < 1e-12:
                raise ValueError("weights sum to (near) zero — cannot normalise.")
            w = w / total
            _logger.debug("evaluate_portfolio: normalised weights (sum was %.6f)", total)

        return self._make_result(w)
