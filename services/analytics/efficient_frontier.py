# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Investment-universe efficient frontier — sub-stream 5d.

Per ADR-0045 §3, this module is a thin pandas-typed facade over the
SLSQP optimiser already in service for the Phase-3 SAA module
(``analytics.portfolio_optimizer.PortfolioOptimizer``). The web side
of Phase 5 consumes these helpers; the QT side keeps calling the
optimiser directly. The two paths land at the same numerical answer
(verified to ``1e-6`` by the QT-consistency regression test).

The functions are pure: they take pandas / numpy inputs and return
plain dataclasses. None of them reach into the database directly —
that is the orchestration layer's job (see
:class:`services.portfolio_analysis.PortfolioAnalysisService`).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from services.analytics.portfolio_optimizer import (
    PortfolioConstraints,
    PortfolioOptimizer,
    PortfolioResult,
)


@dataclass(frozen=True)
class EfficientFrontierResult:
    """Discrete efficient frontier and the inputs that produced it.

    The frontier arrays are kept in three parallel arrays so they
    can be inspected and serialised without DTO churn. The inputs
    (``expected_returns``, ``cov_matrix``) and the long-only bounds
    are carried alongside so downstream tangency / min-variance
    derivations can reconstruct the same analytical
    :class:`PortfolioOptimizer` instance — that reconstruction is
    the seam that keeps the web tangency / min-var numerically
    identical to the QT widget at the ``1e-6`` level required by
    sub-stream 5d's QT-consistency acceptance.

    Attributes:
        frontier_returns: Per-frontier-point annualised expected
            returns. Shape ``(n_points,)``.
        frontier_volatilities: Per-frontier-point annualised
            volatilities. Shape ``(n_points,)``.
        frontier_weights: Per-frontier-point weight vectors. Shape
            ``(n_points, n_assets)``.
        asset_names: Display names matching the column order of
            ``frontier_weights`` and the entries of
            ``expected_returns``.
        expected_returns: Annualised expected returns supplied to the
            optimiser. Shape ``(n_assets,)``.
        cov_matrix: Annualised covariance matrix supplied to the
            optimiser. Shape ``(n_assets, n_assets)``.
        bounds_min: Per-asset weight lower bound applied to every
            optimisation derived from this frontier (typically
            ``0.0`` for long-only).
        bounds_max: Per-asset weight upper bound (typically ``1.0``).
    """

    frontier_returns: np.ndarray
    frontier_volatilities: np.ndarray
    frontier_weights: np.ndarray
    asset_names: list[str]
    expected_returns: np.ndarray
    cov_matrix: np.ndarray
    bounds_min: float
    bounds_max: float


@dataclass(frozen=True)
class TangencyPortfolio:
    """Maximum-Sharpe portfolio on the efficient frontier."""

    weights: np.ndarray
    expected_return: float
    volatility: float
    sharpe_ratio: float
    asset_names: list[str]


@dataclass(frozen=True)
class MinVariancePortfolio:
    """Global minimum-variance portfolio."""

    weights: np.ndarray
    expected_return: float
    volatility: float
    asset_names: list[str]


@dataclass(frozen=True)
class CapitalMarketLine:
    """Capital Market Line geometry sampled at ``n_points``."""

    points: list[tuple[float, float]]


def derive_expected_returns_and_cov(
    return_series_by_investment: dict[str, pd.Series],
    periods_per_year: int = 252,
) -> tuple[pd.Series, pd.DataFrame]:
    """Annualise expected returns and covariance from periodic returns.

    Mirrors the numerical convention used by the QT widget at
    ``gui/widgets/portfolio_analysis_widget.py::_on_compute_clicked``:
    geometric compounding for expected returns
    (``(1 + μ_daily) ** periods_per_year - 1``) and linear scaling
    for the covariance matrix (``Σ_daily * periods_per_year``). The
    geometric convention differs from
    :func:`services.analytics.statistics.annualise_mean_return`,
    which is arithmetic — the optimiser-side "expected return" is
    the realised compound rate, not the Sharpe-ratio numerator.

    Investments are aligned by the union of their periodic-return
    indices; missing observations stay ``NaN`` so the caller can
    decide between pairwise and complete-case handling. Use
    :func:`analytics.sample_window.restrict_to_common_window` before
    this function to enforce a complete-case window for SLSQP.

    Args:
        return_series_by_investment: Mapping of investment display
            name to a periodic-return :class:`pandas.Series`. Each
            series is indexed by the date of the *later* end of the
            return period.
        periods_per_year: Periods per year used in annualisation.
            Daily returns: 252.

    Returns:
        Tuple ``(expected_returns, cov_matrix)``. ``expected_returns``
        is indexed by investment name; ``cov_matrix`` is a square
        :class:`pandas.DataFrame` with the same index and columns.
        Order matches the iteration order of the input mapping
        (insertion order — Python 3.7+ guarantee).

    Raises:
        ValueError: If the input mapping is empty.
    """
    if not return_series_by_investment:
        raise ValueError("derive_expected_returns_and_cov requires at least one return series.")

    names = list(return_series_by_investment.keys())
    aligned = pd.DataFrame({name: return_series_by_investment[name] for name in names})

    daily_mean = aligned.mean()
    expected_returns = (1.0 + daily_mean) ** float(periods_per_year) - 1.0
    expected_returns = expected_returns.reindex(names)

    cov_matrix = aligned.cov() * float(periods_per_year)
    cov_matrix = cov_matrix.reindex(index=names, columns=names)

    return expected_returns, cov_matrix


def _build_optimizer(
    expected_returns: pd.Series | np.ndarray,
    cov_matrix: pd.DataFrame | np.ndarray,
    *,
    asset_names: list[str] | None = None,
    risk_free_rate: float = 0.0,
    bounds_min: float = 0.0,
    bounds_max: float = 1.0,
) -> PortfolioOptimizer:
    """Construct the underlying :class:`PortfolioOptimizer`.

    Internal helper. Translates pandas-typed inputs into the numpy
    arrays the optimiser expects, encodes the per-asset bound
    parameters as a :class:`PortfolioConstraints`, and applies
    ``long_only=True`` whenever ``bounds_min >= 0`` (which it is in
    every sub-stream 5d code path — bounds_min is exposed only for
    future extension).
    """
    if isinstance(expected_returns, pd.Series):
        names = list(expected_returns.index)
        mu = expected_returns.to_numpy(dtype=float)
    else:
        if asset_names is None:
            raise ValueError("asset_names is required when expected_returns is not a Series.")
        names = list(asset_names)
        mu = np.asarray(expected_returns, dtype=float)

    if isinstance(cov_matrix, pd.DataFrame):
        sigma = cov_matrix.to_numpy(dtype=float)
    else:
        sigma = np.asarray(cov_matrix, dtype=float)

    n = len(mu)
    constraints = PortfolioConstraints(
        long_only=bounds_min >= 0.0,
        min_weights=np.full(n, float(bounds_min)),
        max_weights=np.full(n, float(bounds_max)),
    )
    return PortfolioOptimizer(
        expected_returns=mu,
        cov_matrix=sigma,
        asset_names=names,
        risk_free_rate=float(risk_free_rate),
        constraints=constraints,
    )


def compute_efficient_frontier(
    expected_returns: pd.Series,
    cov_matrix: pd.DataFrame,
    n_points: int = 100,
    bounds_min: float = 0.0,
    bounds_max: float = 1.0,
) -> EfficientFrontierResult:
    """Compute the efficient frontier across an investment universe.

    Delegates to :class:`PortfolioOptimizer.efficient_frontier`. The
    inputs and bounds are carried into the result so derived
    computations (tangency, min-variance) reconstruct the same
    optimiser configuration.

    Args:
        expected_returns: Annualised expected returns indexed by
            investment name.
        cov_matrix: Annualised covariance matrix with matching index
            and columns.
        n_points: Number of frontier samples. Range 20–500; default
            100. Higher values smooth the curve; cost is roughly
            linear in ``n_points``.
        bounds_min: Per-asset weight lower bound (default 0.0 →
            long-only).
        bounds_max: Per-asset weight upper bound (default 1.0).

    Returns:
        :class:`EfficientFrontierResult` with three parallel arrays
        of length ``len(frontier)`` (≤ ``n_points`` — the optimiser
        skips non-converged points) plus the inputs needed to
        reconstruct downstream computations.

    Raises:
        ValueError: If the inputs are inconsistent (length mismatch,
            asymmetric covariance, etc.) — propagated from
            :class:`PortfolioOptimizer`.
    """
    if not expected_returns.index.equals(cov_matrix.index):
        raise ValueError("expected_returns.index must equal cov_matrix.index.")
    if not cov_matrix.index.equals(cov_matrix.columns):
        raise ValueError("cov_matrix must be square with identical index and columns.")

    asset_names: list[str] = list(expected_returns.index)
    optimizer = _build_optimizer(
        expected_returns,
        cov_matrix,
        bounds_min=bounds_min,
        bounds_max=bounds_max,
    )

    frontier_points: list[PortfolioResult] = optimizer.efficient_frontier(n_points=n_points)

    if frontier_points:
        returns_arr = np.array([p.expected_return for p in frontier_points], dtype=float)
        vols_arr = np.array([p.volatility for p in frontier_points], dtype=float)
        weights_arr = np.vstack([p.weights for p in frontier_points])
    else:
        returns_arr = np.empty(0, dtype=float)
        vols_arr = np.empty(0, dtype=float)
        weights_arr = np.empty((0, len(asset_names)), dtype=float)

    return EfficientFrontierResult(
        frontier_returns=returns_arr,
        frontier_volatilities=vols_arr,
        frontier_weights=weights_arr,
        asset_names=asset_names,
        expected_returns=expected_returns.to_numpy(dtype=float),
        cov_matrix=cov_matrix.to_numpy(dtype=float),
        bounds_min=float(bounds_min),
        bounds_max=float(bounds_max),
    )


def compute_tangency_portfolio(
    frontier_result: EfficientFrontierResult,
    risk_free_rate: float,
) -> TangencyPortfolio:
    """Compute the tangency (maximum-Sharpe) portfolio.

    Reconstructs the optimiser from the inputs carried in
    ``frontier_result`` and calls
    :class:`PortfolioOptimizer.tangency_portfolio`, which solves the
    QP transformation analytically (with a fallback to direct
    Sharpe maximisation when finite per-asset bounds break the
    QP's scale-invariance). This is the same code path the QT
    widget exercises, which is the prerequisite for the
    sub-stream 5d QT-consistency acceptance.

    Args:
        frontier_result: Output of :func:`compute_efficient_frontier`.
        risk_free_rate: Annualised risk-free rate used for the
            Sharpe-ratio optimum. Decimal (``0.025`` = 2.5 %).

    Returns:
        :class:`TangencyPortfolio` with weights aligned to
        ``frontier_result.asset_names``.

    Raises:
        ValueError: If the optimiser fails to converge or the
            risk-free rate is at or above every expected return
            (Sharpe has no positive maximum).
    """
    optimizer = _build_optimizer(
        frontier_result.expected_returns,
        frontier_result.cov_matrix,
        asset_names=frontier_result.asset_names,
        risk_free_rate=float(risk_free_rate),
        bounds_min=frontier_result.bounds_min,
        bounds_max=frontier_result.bounds_max,
    )
    result = optimizer.tangency_portfolio()
    return TangencyPortfolio(
        weights=np.asarray(result.weights, dtype=float),
        expected_return=float(result.expected_return),
        volatility=float(result.volatility),
        sharpe_ratio=float(result.sharpe_ratio),
        asset_names=list(frontier_result.asset_names),
    )


def compute_min_variance_portfolio(
    frontier_result: EfficientFrontierResult,
) -> MinVariancePortfolio:
    """Compute the global minimum-variance portfolio.

    Args:
        frontier_result: Output of :func:`compute_efficient_frontier`.

    Returns:
        :class:`MinVariancePortfolio` with weights aligned to
        ``frontier_result.asset_names``.

    Raises:
        ValueError: If the optimiser fails to converge.
    """
    optimizer = _build_optimizer(
        frontier_result.expected_returns,
        frontier_result.cov_matrix,
        asset_names=frontier_result.asset_names,
        bounds_min=frontier_result.bounds_min,
        bounds_max=frontier_result.bounds_max,
    )
    result = optimizer.minimum_variance_portfolio()
    return MinVariancePortfolio(
        weights=np.asarray(result.weights, dtype=float),
        expected_return=float(result.expected_return),
        volatility=float(result.volatility),
        asset_names=list(frontier_result.asset_names),
    )


def compute_capital_market_line(
    risk_free_rate: float,
    tangency: TangencyPortfolio,
    x_max: float,
    n_points: int = 50,
) -> CapitalMarketLine:
    """Sample the Capital Market Line from ``(0, rf)`` through tangency.

    The CML is ``return(vol) = rf + Sharpe * vol`` for any
    ``vol >= 0``, with ``Sharpe`` taken from the supplied tangency
    portfolio.

    Args:
        risk_free_rate: Annualised risk-free rate (decimal).
        tangency: Tangency portfolio whose Sharpe drives the slope.
        x_max: Upper bound of the volatility axis sample. Typically
            ``1.5 * tangency.volatility`` to mirror the QT widget,
            but the orchestrator can extend the line to whatever
            volatility is needed to keep the chart readable across
            random portfolios and individual investments.
        n_points: Number of samples on ``[0, x_max]``. Default 50.

    Returns:
        :class:`CapitalMarketLine` with ``n_points`` ``(vol, return)``
        pairs sorted by ascending volatility.
    """
    vols = np.linspace(0.0, float(x_max), int(n_points))
    sharpe = float(tangency.sharpe_ratio)
    rf = float(risk_free_rate)
    points = [(float(v), rf + sharpe * float(v)) for v in vols]
    return CapitalMarketLine(points=points)


def compute_current_portfolio_position(
    weights: dict[str, float],
    expected_returns: pd.Series,
    cov_matrix: pd.DataFrame,
) -> tuple[float, float]:
    """Evaluate volatility and expected return of a given allocation.

    Investments listed in ``expected_returns`` but absent from
    ``weights`` get an implicit zero weight. Investments listed in
    ``weights`` but absent from ``expected_returns`` are silently
    dropped — the orchestrator decides how to handle them. The
    weight vector is normalised to sum to 1 if its sum is finite
    and non-zero; the function returns ``(nan, nan)`` when the
    inputs cannot define a portfolio (e.g. all-zero weights).

    Args:
        weights: Mapping of investment display name to weight. Need
            not sum to 1.0 — the function normalises.
        expected_returns: Annualised expected returns indexed by
            investment name.
        cov_matrix: Annualised covariance matrix with matching index
            and columns.

    Returns:
        Tuple ``(volatility, expected_return)`` in annualised units
        (decimal).
    """
    names: list[str] = list(expected_returns.index)
    w = np.array([float(weights.get(name, 0.0)) for name in names], dtype=float)
    total = float(np.sum(w))
    if not np.isfinite(total) or abs(total) < 1e-12:
        return float("nan"), float("nan")
    w = w / total

    mu = expected_returns.to_numpy(dtype=float)
    sigma = cov_matrix.to_numpy(dtype=float)
    expected_return = float(w @ mu)
    variance = float(w @ sigma @ w)
    if variance < 0.0:
        variance = 0.0
    volatility = float(np.sqrt(variance))
    return volatility, expected_return
