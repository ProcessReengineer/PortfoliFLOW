# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SAAAssetClassInputRepository — persistence for per-asset-class SAA inputs.

Backs the ``saa_asset_class_inputs`` table introduced in migration
b005 (per ADR-0042 §1). One row per (configuration, asset class)
pair, carrying the forward-looking expectation (expected return,
volatility) and weight bounds.

The ``replace_all_for_configuration`` workflow is the principal
write path used by the SAA web UI's "Save Configuration" button:
the entire set of inputs for a configuration is replaced atomically.
Cross-field validation runs in :class:`services.saa.SAAService`
before the repository is called, so a violation surfaces as
``SAAValidationError`` rather than as a Postgres ``IntegrityError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, text, update

from core.models.saa_asset_class_input import SAAAssetClassInput
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class SAAAssetClassInputDTO:
    """Plain data-only view of a ``saa_asset_class_inputs`` row."""

    id: UUID
    tenant_id: UUID
    configuration_id: UUID
    asset_class_id: UUID
    expected_return: float
    volatility: float
    min_weight: float
    max_weight: float
    created_at: datetime
    updated_at: datetime


def _to_dto(model: SAAAssetClassInput) -> SAAAssetClassInputDTO:
    return SAAAssetClassInputDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        configuration_id=model.configuration_id,
        asset_class_id=model.asset_class_id,
        expected_return=float(model.expected_return),
        volatility=float(model.volatility),
        min_weight=float(model.min_weight),
        max_weight=float(model.max_weight),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SAAAssetClassInputRepository(BaseRepository):
    """Read and write per-asset-class SAA inputs in the active tenant."""

    async def create(
        self,
        configuration_id: UUID,
        asset_class_id: UUID,
        expected_return: float,
        volatility: float,
        min_weight: float,
        max_weight: float,
    ) -> SAAAssetClassInputDTO:
        """Insert a single per-asset-class input row.

        ``tenant_id`` is read from the active GUC; the unique
        constraint ``(configuration_id, asset_class_id)`` from b005
        prevents duplicate rows for the same pair within one
        configuration.

        Args:
            configuration_id: Owning SAA configuration.
            asset_class_id: Asset class this row belongs to.
            expected_return: Annualised expected return as a decimal.
            volatility: Annualised standard deviation as a decimal
                (must be ≥ 0; DB CHECK).
            min_weight: Lower bound (0 ≤ min ≤ 1; DB CHECK).
            max_weight: Upper bound (0 ≤ max ≤ 1, min ≤ max; DB CHECKs).

        Returns:
            The newly created :class:`SAAAssetClassInputDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = SAAAssetClassInput(
            tenant_id=active_tenant,
            configuration_id=configuration_id,
            asset_class_id=asset_class_id,
            expected_return=expected_return,
            volatility=volatility,
            min_weight=min_weight,
            max_weight=max_weight,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def list_by_configuration(self, config_id: UUID) -> list[SAAAssetClassInputDTO]:
        """Return every input row for a configuration.

        Sorted by ``asset_class_id`` for deterministic ordering. The
        service layer re-sorts by display name when assembling
        optimisation inputs so the UI sees a stable column order.
        """
        result = await self._session.execute(
            select(SAAAssetClassInput)
            .where(SAAAssetClassInput.configuration_id == config_id)
            .order_by(SAAAssetClassInput.asset_class_id)
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def update(
        self,
        input_id: UUID,
        *,
        expected_return: float | None = None,
        volatility: float | None = None,
        min_weight: float | None = None,
        max_weight: float | None = None,
    ) -> SAAAssetClassInputDTO:
        """Update mutable fields on a single input row.

        Only non-``None`` fields are modified. The CHECK constraints
        from b005 fire at flush time if any combined value violates
        the volatility / weight invariants.

        Returns:
            The refreshed :class:`SAAAssetClassInputDTO`.

        Raises:
            ValueError: If no row with this id exists in the active
                tenant context.
        """
        values: dict[str, object] = {}
        if expected_return is not None:
            values["expected_return"] = expected_return
        if volatility is not None:
            values["volatility"] = volatility
        if min_weight is not None:
            values["min_weight"] = min_weight
        if max_weight is not None:
            values["max_weight"] = max_weight
        if values:
            values["updated_at"] = text("NOW()")
            await self._session.execute(
                update(SAAAssetClassInput).where(SAAAssetClassInput.id == input_id).values(**values)
            )
            await self._session.flush()

        result = await self._session.execute(
            select(SAAAssetClassInput).where(SAAAssetClassInput.id == input_id)
        )
        refreshed = result.scalar_one_or_none()
        if refreshed is None:
            raise ValueError(f"SAAAssetClassInput {input_id} not found in active tenant.")
        return _to_dto(refreshed)

    async def delete(self, input_id: UUID) -> None:
        """Hard-delete a single input row."""
        await self._session.execute(
            delete(SAAAssetClassInput).where(SAAAssetClassInput.id == input_id)
        )
        await self._session.flush()

    async def replace_all_for_configuration(
        self,
        config_id: UUID,
        inputs: list[tuple[UUID, float, float, float, float]],
    ) -> list[SAAAssetClassInputDTO]:
        """Atomically replace all inputs for a configuration.

        DELETE every existing row for ``config_id``, then INSERT the
        provided tuples. Both statements run in the caller's
        transaction; an :class:`IntegrityError` rolls back the entire
        operation.

        Args:
            config_id: The owning configuration.
            inputs: A list of ``(asset_class_id, expected_return,
                volatility, min_weight, max_weight)`` tuples.

        Returns:
            The created DTOs in insertion order.
        """
        await self._session.execute(
            delete(SAAAssetClassInput).where(SAAAssetClassInput.configuration_id == config_id)
        )

        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        created: list[SAAAssetClassInput] = []
        for (
            asset_class_id,
            expected_return,
            volatility,
            min_weight,
            max_weight,
        ) in inputs:
            model = SAAAssetClassInput(
                tenant_id=active_tenant,
                configuration_id=config_id,
                asset_class_id=asset_class_id,
                expected_return=expected_return,
                volatility=volatility,
                min_weight=min_weight,
                max_weight=max_weight,
            )
            self._session.add(model)
            created.append(model)

        await self._session.flush()
        for model in created:
            await self._session.refresh(model)
        return [_to_dto(model) for model in created]
