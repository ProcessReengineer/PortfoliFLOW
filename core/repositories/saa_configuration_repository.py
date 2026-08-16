# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SAAConfigurationRepository — persistence for top-level SAA configurations.

Backs the ``saa_configurations`` table introduced in migration b005
(per ADR-0042 §1). The repository owns the ``set_active`` workflow:
the partial unique index ``uq_saa_configurations_active_per_tenant``
on the database side allows at most one active configuration per
tenant, so activation must run as a two-step transaction
(deactivate-peers, activate-target) to avoid violating the index
during the swap.

Phase 3 returns ``risk_free_rate`` as ``float`` from the DTO because
the optimiser consumes ``float``. Conversion from the SQL ``Numeric``
column happens here so callers stay numpy-friendly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, text, update

from core.models.saa_configuration import SAAConfiguration
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class SAAConfigurationDTO:
    """Plain data-only view of a ``saa_configurations`` row."""

    id: UUID
    tenant_id: UUID
    name: str
    risk_free_rate: float
    n_frontier_points: int
    is_active: bool
    created_by: UUID
    created_at: datetime
    updated_at: datetime


def _to_dto(model: SAAConfiguration) -> SAAConfigurationDTO:
    return SAAConfigurationDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        name=model.name,
        risk_free_rate=float(model.risk_free_rate),
        n_frontier_points=model.n_frontier_points,
        is_active=model.is_active,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SAAConfigurationRepository(BaseRepository):
    """Read and write SAA configurations in the active tenant context."""

    async def create(
        self,
        name: str,
        risk_free_rate: float,
        n_frontier_points: int,
        created_by: UUID,
    ) -> SAAConfigurationDTO:
        """Create an inactive configuration in the current tenant context.

        New configurations are created with ``is_active = FALSE`` so
        the partial unique index permits an arbitrary number of them.
        Activation is a separate, atomic step (:meth:`set_active`).

        Args:
            name: Human-readable, tenant-unique configuration name.
            risk_free_rate: Annualised risk-free rate as a decimal
                (e.g. ``0.025`` for 2.5 %).
            n_frontier_points: Number of points to compute on the
                efficient frontier (range checked at the DB level: 20
                ≤ n ≤ 500).
            created_by: UUID of the authenticated user creating the
                configuration. Stored on the row for audit purposes.

        Returns:
            The newly created :class:`SAAConfigurationDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = SAAConfiguration(
            tenant_id=active_tenant,
            name=name,
            risk_free_rate=risk_free_rate,
            n_frontier_points=n_frontier_points,
            is_active=False,
            created_by=created_by,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def get_by_id(self, config_id: UUID) -> SAAConfigurationDTO | None:
        """Return the configuration with the given id, or ``None`` if absent."""
        result = await self._session.execute(
            select(SAAConfiguration).where(SAAConfiguration.id == config_id)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def get_active(self) -> SAAConfigurationDTO | None:
        """Return the active configuration for the tenant, or ``None``.

        At most one row can satisfy ``is_active = TRUE`` per tenant
        (partial unique index from b005), so ``scalar_one_or_none`` is
        the correct retrieval.
        """
        result = await self._session.execute(
            select(SAAConfiguration).where(SAAConfiguration.is_active.is_(True))
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_all(self) -> list[SAAConfigurationDTO]:
        """Return every configuration visible in the active tenant.

        Sorted by ``name`` for stable rendering.
        """
        result = await self._session.execute(
            select(SAAConfiguration).order_by(SAAConfiguration.name)
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def update(
        self,
        config_id: UUID,
        *,
        name: str | None = None,
        risk_free_rate: float | None = None,
        n_frontier_points: int | None = None,
    ) -> SAAConfigurationDTO:
        """Update mutable metadata fields.

        ``is_active`` is *not* updatable here; activation goes through
        :meth:`set_active` so the deactivate-peers step always runs in
        the same transaction.

        Args:
            config_id: The configuration to update.
            name: New configuration name, or ``None`` to keep.
            risk_free_rate: New risk-free rate as a decimal, or ``None``
                to keep.
            n_frontier_points: New frontier-point count, or ``None`` to
                keep.

        Returns:
            The updated :class:`SAAConfigurationDTO`.

        Raises:
            ValueError: If no configuration with this id exists in the
                active tenant context.
        """
        values: dict[str, object] = {}
        if name is not None:
            values["name"] = name
        if risk_free_rate is not None:
            values["risk_free_rate"] = risk_free_rate
        if n_frontier_points is not None:
            values["n_frontier_points"] = n_frontier_points
        if values:
            values["updated_at"] = text("NOW()")
            await self._session.execute(
                update(SAAConfiguration).where(SAAConfiguration.id == config_id).values(**values)
            )
            await self._session.flush()

        refreshed = await self.get_by_id(config_id)
        if refreshed is None:
            raise ValueError(f"SAAConfiguration {config_id} not found in active tenant.")
        return refreshed

    async def set_active(self, config_id: UUID) -> SAAConfigurationDTO:
        """Make this configuration the tenant's single active SAA.

        Two SQL statements in one transaction:

        1. Deactivate every other configuration in the same tenant
           (RLS scopes the update to the active tenant).
        2. Activate the target.

        The ordering matters: if step 2 ran first, the partial unique
        index would reject the second active row before step 1 could
        clean up. Running deactivate-first guarantees the index sees
        at most one active row at any intermediate point.

        Args:
            config_id: The configuration to activate.

        Returns:
            The activated :class:`SAAConfigurationDTO`.

        Raises:
            ValueError: If no configuration with this id exists in the
                active tenant context.
        """
        await self._session.execute(
            update(SAAConfiguration)
            .where(
                SAAConfiguration.id != config_id,
                SAAConfiguration.is_active.is_(True),
            )
            .values(is_active=False, updated_at=text("NOW()"))
        )
        await self._session.execute(
            update(SAAConfiguration)
            .where(SAAConfiguration.id == config_id)
            .values(is_active=True, updated_at=text("NOW()"))
        )
        await self._session.flush()

        refreshed = await self.get_by_id(config_id)
        if refreshed is None:
            raise ValueError(f"SAAConfiguration {config_id} not found in active tenant.")
        return refreshed

    async def delete(self, config_id: UUID) -> None:
        """Hard-delete a configuration.

        ``saa_asset_class_inputs.configuration_id`` and
        ``saa_correlations.configuration_id`` carry ``ON DELETE
        CASCADE``, so child rows disappear automatically. Asset
        classes referenced by the configuration are *not* deleted —
        they remain in the catalogue for use by other configurations.
        """
        await self._session.execute(
            delete(SAAConfiguration).where(SAAConfiguration.id == config_id)
        )
        await self._session.flush()
