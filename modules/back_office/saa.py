# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Back Office — Strategic Asset Allocation (SAA) module.

Purpose:
    Derives a target allocation across asset classes using manually entered
    forward-looking expected returns, volatilities, and a correlation matrix.
    This is the back-office complement to the Front Office Portfolio Optimizer:
    SAA operates on *asset classes* with user-supplied expectations rather than
    on individual investments with historical time-series data.

Inputs:
    - Asset class names
    - Annualised expected returns (decimal)
    - Annualised volatilities (decimal)
    - Correlation matrix (symmetric, diagonal = 1)
    - Per-asset minimum and maximum weight bounds
    - Risk-free rate (decimal)
    - Number of frontier points

Outputs:
    - Efficient frontier (list of PortfolioResult)
    - Tangency portfolio (PortfolioResult)
    - Minimum-variance portfolio (PortfolioResult)
    - Random portfolio cloud (list of PortfolioResult)
    - Capital Market Line (list of (float, float) tuples)

DataStore keys written:
    - ``"saa_inputs"``  — DataFrame with asset class names, returns, vols, bounds
    - ``"saa_results"`` — DataFrame with key optimisation result metrics
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from services.analytics.portfolio_optimizer import (
    PortfolioConstraints,
    PortfolioOptimizer,
    PortfolioResult,
)
from core.base_module import BaseModule
from core.data_store import get_data_store
from core.exceptions import ModuleError, ValidationError
from modules.module_registry import registry

logger = logging.getLogger(__name__)


@registry.register
class StrategicAssetAllocation(BaseModule):
    """Strategic Asset Allocation module for institutional back-office operations.

    Derives an optimal target allocation across asset classes using mean-variance
    optimisation.  All inputs are manually entered forward-looking expectations —
    no historical time-series data is required.

    The GUI widget (:class:`gui.widgets.saa_widget.SAAWidget`) calls the
    analytics engine directly for interactive use.  This module exists for
    registry completeness and for programmatic / AI-assistant use.

    Attributes:
        module_name: ``"saa"``
        module_area: ``"back_office"``
    """

    module_name = "saa"
    module_area = "back_office"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """Run the SAA optimisation pipeline.

        Dispatches on ``kwargs["action"]`` (default: ``"optimize"``).

        Keyword Args:
            action (str): Currently only ``"optimize"`` is supported.
            asset_names (list[str]): Asset class labels (min. 2).
            expected_returns (list[float]): Annualised expected returns as
                decimals (e.g. ``0.08`` for 8 %).
            volatilities (list[float]): Annualised standard deviations as
                decimals (e.g. ``0.16`` for 16 %).
            correlation_matrix (list[list[float]]): Symmetric n×n correlation
                matrix.  Diagonal entries must equal 1.0.
            min_weights (list[float] | None): Per-asset lower weight bounds as
                decimals.  Defaults to ``0.0`` for each asset when omitted.
            max_weights (list[float] | None): Per-asset upper weight bounds as
                decimals.  Defaults to ``1.0`` for each asset when omitted.
            risk_free_rate (float): Annualised risk-free rate as a decimal.
                Defaults to ``0.0``.
            n_points (int): Number of efficient frontier points.
                Defaults to ``100``.
            n_cloud (int): Number of random portfolio cloud points.
                Defaults to ``5000``.
            store_results (bool): Write inputs and results to the DataStore.
                Defaults to ``True``.

        Returns:
            dict with keys:

            - ``"status"`` (str): ``"ok"`` on success.
            - ``"frontier"`` (list[PortfolioResult]): Efficient frontier.
            - ``"tangency"`` (PortfolioResult): Maximum-Sharpe portfolio.
            - ``"min_var"`` (PortfolioResult): Global minimum-variance portfolio.
            - ``"cloud"`` (list[PortfolioResult]): Random portfolio cloud.
            - ``"cml"`` (list[tuple[float, float]]): Capital Market Line.

        Raises:
            ValidationError: If inputs are missing, have wrong dimensions, or
                contain out-of-range correlation values.
            ModuleError: If the numerical optimisation fails.
        """
        action: str = kwargs.get("action", "optimize")
        if action == "optimize":
            return self._run_optimize(kwargs)
        raise ValidationError(f"Unknown action '{action}'.", field="action")

    def validate_inputs(self, **kwargs: Any) -> None:  # type: ignore[override]
        """Validate SAA inputs before optimisation.

        Args:
            asset_names: List of asset class labels.
            raw_returns: Annualised expected returns (decimals).
            raw_vols: Annualised volatilities (decimals).
            raw_corr: Symmetric correlation matrix (list of lists).

        Raises:
            ValidationError: If any required input is absent, has incorrect
                length, or contains values outside the valid range.
        """
        asset_names: list[str] = kwargs.get("asset_names", [])
        raw_returns: list[float] = kwargs.get("raw_returns", [])
        raw_vols: list[float] = kwargs.get("raw_vols", [])
        raw_corr: list[list[float]] = kwargs.get("raw_corr", [])

        if len(asset_names) < 2:
            raise ValidationError(
                "At least 2 asset classes are required for optimisation.",
                field="asset_names",
            )
        n = len(asset_names)
        if len(raw_returns) != n:
            raise ValidationError(
                f"expected_returns length ({len(raw_returns)}) does not match asset count ({n}).",
                field="expected_returns",
            )
        if len(raw_vols) != n:
            raise ValidationError(
                f"volatilities length ({len(raw_vols)}) does not match asset count ({n}).",
                field="volatilities",
            )
        if len(raw_corr) != n or any(len(row) != n for row in raw_corr):
            raise ValidationError(
                f"correlation_matrix must be {n}×{n}.",
                field="correlation_matrix",
            )
        for i in range(n):
            for j in range(n):
                val = float(raw_corr[i][j])
                if not (-1.0 <= val <= 1.0):
                    raise ValidationError(
                        f"Correlation[{i},{j}] = {val:.4f} is outside [-1, 1].",
                        field="correlation_matrix",
                    )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_optimize(self, params: dict) -> dict:
        """Execute the optimisation pipeline.

        Args:
            params: The full ``kwargs`` dict from :meth:`run`.

        Returns:
            Result dict (see :meth:`run`).

        Raises:
            ValidationError: On invalid or inconsistent inputs.
            ModuleError: On numerical optimisation failure.
        """
        asset_names: list[str] = params.get("asset_names", [])
        raw_returns: list[float] = params.get("expected_returns", [])
        raw_vols: list[float] = params.get("volatilities", [])
        raw_corr: list[list[float]] = params.get("correlation_matrix", [])
        raw_min: list[float] | None = params.get("min_weights")
        raw_max: list[float] | None = params.get("max_weights")
        rf_rate: float = float(params.get("risk_free_rate", 0.0))
        n_points: int = int(params.get("n_points", 100))
        n_cloud: int = int(params.get("n_cloud", 5000))
        store: bool = bool(params.get("store_results", True))

        self.validate_inputs(
            asset_names=asset_names,
            raw_returns=raw_returns,
            raw_vols=raw_vols,
            raw_corr=raw_corr,
        )

        n = len(asset_names)
        expected_returns = np.array(raw_returns, dtype=float)
        volatilities = np.array(raw_vols, dtype=float)
        corr_matrix = np.array(raw_corr, dtype=float)
        cov_matrix: np.ndarray = np.outer(volatilities, volatilities) * corr_matrix

        min_w = np.zeros(n, dtype=float) if raw_min is None else np.array(raw_min, dtype=float)
        max_w = np.ones(n, dtype=float) if raw_max is None else np.array(raw_max, dtype=float)

        constraints = PortfolioConstraints(
            long_only=True,
            min_weights=min_w,
            max_weights=max_w,
        )

        logger.debug(
            "SAA._run_optimize: %d asset classes, rf=%.4f, n_points=%d, n_cloud=%d",
            n,
            rf_rate,
            n_points,
            n_cloud,
        )

        try:
            optimizer = PortfolioOptimizer(
                expected_returns=expected_returns,
                cov_matrix=cov_matrix,
                asset_names=asset_names,
                risk_free_rate=rf_rate,
                constraints=constraints,
            )
            cloud = optimizer.random_portfolios(n_portfolios=n_cloud)
            frontier = optimizer.efficient_frontier(n_points=n_points)
            tangency = optimizer.tangency_portfolio()
            min_var = optimizer.minimum_variance_portfolio()
            cml = optimizer.capital_market_line(n_points=50)
        except ValueError as exc:
            raise ModuleError(f"SAA optimisation failed: {exc}") from exc

        if store:
            self._store_in_datastore(
                asset_names=asset_names,
                expected_returns=expected_returns,
                volatilities=volatilities,
                min_w=min_w,
                max_w=max_w,
                rf_rate=rf_rate,
                tangency=tangency,
                min_var=min_var,
                frontier=frontier,
                cloud=cloud,
            )

        return {
            "status": "ok",
            "frontier": frontier,
            "tangency": tangency,
            "min_var": min_var,
            "cloud": cloud,
            "cml": cml,
        }

    def _store_in_datastore(
        self,
        *,
        asset_names: list[str],
        expected_returns: np.ndarray,
        volatilities: np.ndarray,
        min_w: np.ndarray,
        max_w: np.ndarray,
        rf_rate: float,
        tangency: PortfolioResult,
        min_var: PortfolioResult,
        frontier: list[PortfolioResult],
        cloud: list[PortfolioResult],
    ) -> None:
        """Write SAA inputs and result summary to the DataStore.

        Stores two DataFrames:

        - ``"saa_inputs"``  — per-asset-class parameters (returns, vols, bounds).
        - ``"saa_results"`` — scalar optimisation result metrics.

        Args:
            asset_names: Asset class labels.
            expected_returns: Annualised expected returns (decimal array).
            volatilities: Annualised standard deviations (decimal array).
            min_w: Minimum weight bounds (decimal array).
            max_w: Maximum weight bounds (decimal array).
            rf_rate: Risk-free rate (decimal).
            tangency: Tangency portfolio result.
            min_var: Minimum-variance portfolio result.
            frontier: Efficient frontier results.
            cloud: Random portfolio cloud results.
        """
        data_store = get_data_store()

        inputs_df = pd.DataFrame(
            {
                "Asset Class": asset_names,
                "Expected Return": expected_returns,
                "Volatility": volatilities,
                "Min Weight": min_w,
                "Max Weight": max_w,
            }
        )
        data_store.store("saa_inputs", inputs_df)

        results_df = pd.DataFrame(
            {
                "Metric": [
                    "Risk-Free Rate",
                    "Tangency Return",
                    "Tangency Volatility",
                    "Tangency Sharpe",
                    "MinVar Return",
                    "MinVar Volatility",
                    "Frontier Points",
                    "Cloud Portfolios",
                ],
                "Value": [
                    rf_rate,
                    tangency.expected_return,
                    tangency.volatility,
                    tangency.sharpe_ratio,
                    min_var.expected_return,
                    min_var.volatility,
                    float(len(frontier)),
                    float(len(cloud)),
                ],
            }
        )
        data_store.store("saa_results", results_df)
        logger.debug("SAA: stored saa_inputs and saa_results in DataStore.")
