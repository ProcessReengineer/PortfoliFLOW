# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SAAService — workflow aggregator for the SAA domain.

Aggregates the four SAA repositories (asset classes, configurations,
inputs, correlations) into coherent domain workflows. Web routes
consume the service; future cross-module consumers (cash-flow
projection with allocation-limit checks, front-office reporting
that overlays target vs. actual allocation, Shirley) will consume a
documented Public API surface — see ADR-0042 §3.

Phase 3 implements only the SAA-area-internal workflows. The cross-
module API methods on the service shape are intentionally not
implemented yet; they are reserved as additive extensions that can
land in 30–50 lines plus tests when the first real consumer arrives.

Three documented method groups
-----------------------------
- **Read workflows.** Routes consume aggregate read DTOs; the service
  hides the multi-repository fan-out behind a single call.
- **Write workflows.** Atomic save / activate / delete operations
  that combine validation with repository writes.
- **Compute workflows.** Run the pure analytics engine
  (``analytics.portfolio_optimizer``) against a persisted
  configuration; results are computed on demand per ADR-0042 §2 and
  not persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import numpy as np

from services.analytics.portfolio_optimizer import (
    PortfolioConstraints,
    PortfolioOptimizer,
    PortfolioResult,
)
from core.repositories.asset_class_repository import (
    AssetClassDTO,
    AssetClassRepository,
)
from core.repositories.saa_asset_class_input_repository import (
    SAAAssetClassInputDTO,
    SAAAssetClassInputRepository,
)
from core.repositories.saa_configuration_repository import (
    SAAConfigurationDTO,
    SAAConfigurationRepository,
)
from core.repositories.saa_correlation_repository import (
    SAACorrelationDTO,
    SAACorrelationRepository,
)
from services.saa.validation import (
    SAAAssetClassInputSpec,
    SAACorrelationSpec,
    SAAValidationError,
    validate_correlations,
    validate_inputs,
)


@dataclass(frozen=True)
class SAAConfigurationDetailDTO:
    """Aggregate read shape: configuration + inputs + correlations."""

    configuration: SAAConfigurationDTO
    inputs: list[SAAAssetClassInputDTO]
    correlations: list[SAACorrelationDTO]


@dataclass(frozen=True)
class SAAOptimizationResultDTO:
    """Aggregate compute shape: every output the SAA detail view consumes."""

    asset_names: list[str]
    frontier: list[PortfolioResult]
    tangency: PortfolioResult
    min_var: PortfolioResult
    cloud: list[PortfolioResult]
    cml: list[tuple[float, float]]


class SAAService:
    """Workflow aggregator over the four SAA repositories.

    All four repositories must be tenant-scoped (the caller
    constructs them with a session obtained via
    :func:`core.repositories.tenant_context`). The service does not
    set or read ``app.tenant_id`` itself — that responsibility lives
    on the session.
    """

    def __init__(
        self,
        configurations: SAAConfigurationRepository,
        asset_classes: AssetClassRepository,
        inputs: SAAAssetClassInputRepository,
        correlations: SAACorrelationRepository,
    ) -> None:
        self._configurations = configurations
        self._asset_classes = asset_classes
        self._inputs = inputs
        self._correlations = correlations

    # ------------------------------------------------------------------
    # Group 1: read workflows
    # ------------------------------------------------------------------

    async def list_configurations(self) -> list[SAAConfigurationDTO]:
        """List every SAA configuration in the active tenant."""
        return await self._configurations.list_all()

    async def get_active_configuration(
        self,
    ) -> SAAConfigurationDTO | None:
        """Return the tenant's active configuration, or ``None`` if none."""
        return await self._configurations.get_active()

    async def get_configuration(self, config_id: UUID) -> SAAConfigurationDTO | None:
        """Return the configuration metadata only (no inputs / correlations)."""
        return await self._configurations.get_by_id(config_id)

    async def get_configuration_full(self, config_id: UUID) -> SAAConfigurationDetailDTO | None:
        """Return a configuration with all inputs and correlations.

        The aggregate DTO is the consumption shape for the SAA detail
        view. The route handler does not need to orchestrate four
        separate repository calls.

        Returns:
            ``None`` if no configuration with this id exists in the
            active tenant; otherwise an :class:`SAAConfigurationDetailDTO`.
        """
        config = await self._configurations.get_by_id(config_id)
        if config is None:
            return None
        inputs = await self._inputs.list_by_configuration(config_id)
        correlations = await self._correlations.list_by_configuration(config_id)
        return SAAConfigurationDetailDTO(
            configuration=config,
            inputs=inputs,
            correlations=correlations,
        )

    async def list_asset_classes(self) -> list[AssetClassDTO]:
        """List every asset class in the active tenant catalogue."""
        return await self._asset_classes.list_all()

    async def get_asset_class(self, asset_class_id: UUID) -> AssetClassDTO | None:
        """Return one asset class by id, or ``None`` if absent."""
        return await self._asset_classes.get_by_id(asset_class_id)

    async def count_configurations_using_asset_class(self, asset_class_id: UUID) -> int:
        """Return the number of configurations referencing this asset class.

        Used by the asset-class management page (to enable / disable
        the per-row delete button) and by the asset-class delete route
        (to surface a friendly 409 message before attempting the delete).

        Args:
            asset_class_id: The asset class to check for references.

        Returns:
            Count of configurations whose inputs reference the asset class.
        """
        configurations = await self._configurations.list_all()
        count = 0
        for config in configurations:
            inputs = await self._inputs.list_by_configuration(config.id)
            if any(i.asset_class_id == asset_class_id for i in inputs):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Group 2: write workflows
    # ------------------------------------------------------------------

    async def create_configuration(
        self,
        name: str,
        risk_free_rate: float,
        n_frontier_points: int,
        created_by: UUID,
    ) -> SAAConfigurationDTO:
        """Create a new (initially empty, inactive) configuration.

        The configuration is created with no inputs and no
        correlations; activate it later with
        :meth:`activate_configuration` once the inputs are saved.
        """
        return await self._configurations.create(
            name=name,
            risk_free_rate=risk_free_rate,
            n_frontier_points=n_frontier_points,
            created_by=created_by,
        )

    async def update_configuration_metadata(
        self, config_id: UUID, **fields: Any
    ) -> SAAConfigurationDTO:
        """Update the configuration's name, risk-free rate, or point count.

        Only ``name``, ``risk_free_rate``, and ``n_frontier_points``
        are accepted; passing other keys raises ``TypeError`` from the
        underlying repository.
        """
        return await self._configurations.update(config_id, **fields)

    async def activate_configuration(self, config_id: UUID) -> SAAConfigurationDTO:
        """Make this configuration the tenant's active SAA.

        Atomic with respect to the partial unique index from b005.
        """
        return await self._configurations.set_active(config_id)

    async def delete_configuration(self, config_id: UUID) -> None:
        """Hard-delete a configuration with cascade to inputs / correlations."""
        await self._configurations.delete(config_id)

    async def save_inputs_and_correlations(
        self,
        config_id: UUID,
        inputs: list[SAAAssetClassInputSpec],
        correlations: list[SAACorrelationSpec],
    ) -> None:
        """Atomically replace inputs and correlations for a configuration.

        This is the "Save Configuration" workflow from the web UI:
        the entire SAA state is persisted in one transaction.
        Validation runs in-process before any writes — if validation
        fails, no DB writes occur.

        Args:
            config_id: The configuration to update.
            inputs: The full set of per-asset-class input specs.
            correlations: The full set of correlation specs (upper or
                lower triangle, the repository normalises).

        Raises:
            SAAValidationError: If cross-field validation fails on
                either inputs or correlations. The error carries
                ``field`` and ``row_index`` so the SAA UI can attach
                the message to the offending row.
        """
        validate_inputs(inputs)
        validate_correlations(correlations, inputs)

        await self._inputs.replace_all_for_configuration(
            config_id,
            [
                (
                    spec.asset_class_id,
                    spec.expected_return,
                    spec.volatility,
                    spec.min_weight,
                    spec.max_weight,
                )
                for spec in inputs
            ],
        )
        await self._correlations.replace_all_for_configuration(
            config_id,
            [
                (
                    spec.asset_class_a_id,
                    spec.asset_class_b_id,
                    spec.correlation,
                )
                for spec in correlations
            ],
        )

    async def create_asset_class(
        self,
        code: str,
        display_name: str,
        description: str | None = None,
    ) -> AssetClassDTO:
        """Create a new asset class in the tenant catalogue."""
        return await self._asset_classes.create(
            code=code,
            display_name=display_name,
            description=description,
        )

    async def update_asset_class(self, asset_class_id: UUID, **fields: Any) -> AssetClassDTO:
        """Update mutable fields on an asset class."""
        return await self._asset_classes.update(asset_class_id, **fields)

    async def delete_asset_class(self, asset_class_id: UUID) -> None:
        """Delete an asset class.

        Raises an integrity error if the asset class is referenced by
        any configuration's inputs or correlations (FK ON DELETE
        RESTRICT). Callers must remove references first.
        """
        await self._asset_classes.delete(asset_class_id)

    # ------------------------------------------------------------------
    # Group 3: compute workflows
    # ------------------------------------------------------------------

    async def run_optimization(self, config_id: UUID) -> SAAOptimizationResultDTO:
        """Run mean-variance optimisation for a configuration.

        Loads the configuration aggregate, builds a
        :class:`PortfolioOptimizer` from
        ``analytics.portfolio_optimizer``, and computes the efficient
        frontier, tangency portfolio, minimum-variance portfolio,
        random portfolio cloud, and capital market line.

        Per ADR-0042 §2 results are not persisted — every call
        recomputes from the persisted configuration.

        Args:
            config_id: The configuration to optimise.

        Returns:
            An :class:`SAAOptimizationResultDTO` with all five output
            shapes the SAA detail view consumes.

        Raises:
            SAAValidationError: If the configuration does not exist,
                has fewer than two asset classes, or references asset
                classes that no longer exist in the catalogue.
            ValueError: If the numerical optimisation fails (re-raised
                from the analytics layer).
        """
        detail = await self.get_configuration_full(config_id)
        if detail is None:
            raise SAAValidationError(f"Configuration {config_id} not found in active tenant.")
        if len(detail.inputs) < 2:
            raise SAAValidationError(
                "At least 2 asset classes are required for optimisation "
                f"(configuration has {len(detail.inputs)})."
            )

        asset_class_lookup = {ac.id: ac for ac in await self.list_asset_classes()}
        # Sort inputs by display name so the asset_names list and the
        # weights vector index alignment is stable across calls.
        try:
            inputs_sorted = sorted(
                detail.inputs,
                key=lambda i: asset_class_lookup[i.asset_class_id].display_name,
            )
        except KeyError as exc:  # noqa: BLE001 - convert to typed error
            raise SAAValidationError(
                f"Configuration references asset class {exc} that is no longer in the catalogue."
            ) from exc

        asset_names = [asset_class_lookup[i.asset_class_id].display_name for i in inputs_sorted]
        expected_returns = np.array([i.expected_return for i in inputs_sorted], dtype=float)
        volatilities = np.array([i.volatility for i in inputs_sorted], dtype=float)
        min_weights = np.array([i.min_weight for i in inputs_sorted], dtype=float)
        max_weights = np.array([i.max_weight for i in inputs_sorted], dtype=float)

        n = len(inputs_sorted)
        asset_class_idx = {inp.asset_class_id: idx for idx, inp in enumerate(inputs_sorted)}
        # Initialise with the diagonal at 1.0 (self-correlations are
        # implicit and never stored). Mirror upper-triangle triplets
        # into the lower triangle to feed the optimiser a symmetric
        # matrix.
        corr = np.eye(n)
        for c in detail.correlations:
            i_a = asset_class_idx.get(c.asset_class_a_id)
            i_b = asset_class_idx.get(c.asset_class_b_id)
            if i_a is None or i_b is None:
                # Stored correlation references an asset class that is
                # no longer part of the configuration — skip it. The
                # save-pattern keeps inputs and correlations in sync,
                # but this guard makes the compute path resilient to
                # any future schema-evolution races.
                continue
            corr[i_a, i_b] = c.correlation
            corr[i_b, i_a] = c.correlation

        cov = np.outer(volatilities, volatilities) * corr
        constraints = PortfolioConstraints(
            long_only=True,
            min_weights=min_weights,
            max_weights=max_weights,
        )
        optimizer = PortfolioOptimizer(
            expected_returns=expected_returns,
            cov_matrix=cov,
            asset_names=asset_names,
            risk_free_rate=detail.configuration.risk_free_rate,
            constraints=constraints,
        )

        cloud = optimizer.random_portfolios(n_portfolios=5000)
        frontier = optimizer.efficient_frontier(n_points=detail.configuration.n_frontier_points)
        tangency = optimizer.tangency_portfolio()
        min_var = optimizer.minimum_variance_portfolio()
        cml = optimizer.capital_market_line(n_points=50)

        return SAAOptimizationResultDTO(
            asset_names=asset_names,
            frontier=frontier,
            tangency=tangency,
            min_var=min_var,
            cloud=cloud,
            cml=cml,
        )
