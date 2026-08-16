# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SAACorrelationRepository — persistence for SAA pairwise correlations.

Backs the ``saa_correlations`` table introduced in migration b005
(per ADR-0042 §1). Correlations are stored upper-triangle only —
the b005 CHECK ``asset_class_a_id < asset_class_b_id`` enforces it
at the database level. The repository normalises caller-supplied
pairs before persisting so callers do not need to be aware of UUID
ordering: passing ``(B, A, ρ)`` is silently rewritten to
``(A, B, ρ)`` if ``A < B``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, text, update

from core.models.saa_correlation import SAACorrelation
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class SAACorrelationDTO:
    """Plain data-only view of a ``saa_correlations`` row."""

    id: UUID
    tenant_id: UUID
    configuration_id: UUID
    asset_class_a_id: UUID
    asset_class_b_id: UUID
    correlation: float
    created_at: datetime
    updated_at: datetime


def _to_dto(model: SAACorrelation) -> SAACorrelationDTO:
    return SAACorrelationDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        configuration_id=model.configuration_id,
        asset_class_a_id=model.asset_class_a_id,
        asset_class_b_id=model.asset_class_b_id,
        correlation=float(model.correlation),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _ordered_pair(a: UUID, b: UUID) -> tuple[UUID, UUID]:
    """Return the pair sorted so the smaller UUID comes first.

    The b005 CHECK enforces upper-triangle storage by UUID order
    (``asset_class_a_id < asset_class_b_id``); normalising here keeps
    callers free of that concern.
    """
    return (a, b) if a < b else (b, a)


class SAACorrelationRepository(BaseRepository):
    """Read and write SAA pairwise correlations in the active tenant."""

    async def create(
        self,
        configuration_id: UUID,
        asset_class_a_id: UUID,
        asset_class_b_id: UUID,
        correlation: float,
    ) -> SAACorrelationDTO:
        """Insert a single correlation triplet.

        The pair is normalised to upper-triangle order before
        persisting; the row stored always satisfies
        ``asset_class_a_id < asset_class_b_id``.

        Args:
            configuration_id: Owning SAA configuration.
            asset_class_a_id: Either side of the pair (order is
                normalised internally).
            asset_class_b_id: The other side of the pair.
            correlation: Correlation value (must be in ``[-1, 1]`` per
                DB CHECK).

        Returns:
            The newly created :class:`SAACorrelationDTO`.

        Raises:
            ValueError: If both ids are equal — a self-correlation is
                always 1.0 and is not stored.
        """
        if asset_class_a_id == asset_class_b_id:
            raise ValueError(
                "SAACorrelation: self-correlation is implicit (1.0) and must not be stored."
            )

        a_id, b_id = _ordered_pair(asset_class_a_id, asset_class_b_id)

        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = SAACorrelation(
            tenant_id=active_tenant,
            configuration_id=configuration_id,
            asset_class_a_id=a_id,
            asset_class_b_id=b_id,
            correlation=correlation,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def list_by_configuration(self, config_id: UUID) -> list[SAACorrelationDTO]:
        """Return every correlation row for a configuration.

        Sorted by ``(asset_class_a_id, asset_class_b_id)`` for
        deterministic ordering.
        """
        result = await self._session.execute(
            select(SAACorrelation)
            .where(SAACorrelation.configuration_id == config_id)
            .order_by(
                SAACorrelation.asset_class_a_id,
                SAACorrelation.asset_class_b_id,
            )
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def get_correlation(
        self, config_id: UUID, a_id: UUID, b_id: UUID
    ) -> SAACorrelationDTO | None:
        """Return the correlation row for one ordered or unordered pair.

        Looks up the row regardless of which order the caller passes
        the pair in; returns ``None`` for self-correlation queries
        and for missing pairs.
        """
        if a_id == b_id:
            return None
        ord_a, ord_b = _ordered_pair(a_id, b_id)
        result = await self._session.execute(
            select(SAACorrelation).where(
                SAACorrelation.configuration_id == config_id,
                SAACorrelation.asset_class_a_id == ord_a,
                SAACorrelation.asset_class_b_id == ord_b,
            )
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def update(self, correlation_id: UUID, correlation: float) -> SAACorrelationDTO:
        """Update the correlation value on an existing row.

        Args:
            correlation_id: The row to update.
            correlation: New correlation value (must be in ``[-1, 1]``).

        Returns:
            The refreshed :class:`SAACorrelationDTO`.

        Raises:
            ValueError: If no row with this id exists in the active
                tenant context.
        """
        await self._session.execute(
            update(SAACorrelation)
            .where(SAACorrelation.id == correlation_id)
            .values(correlation=correlation, updated_at=text("NOW()"))
        )
        await self._session.flush()

        result = await self._session.execute(
            select(SAACorrelation).where(SAACorrelation.id == correlation_id)
        )
        refreshed = result.scalar_one_or_none()
        if refreshed is None:
            raise ValueError(f"SAACorrelation {correlation_id} not found in active tenant.")
        return _to_dto(refreshed)

    async def delete(self, correlation_id: UUID) -> None:
        """Hard-delete a single correlation row."""
        await self._session.execute(
            delete(SAACorrelation).where(SAACorrelation.id == correlation_id)
        )
        await self._session.flush()

    async def replace_all_for_configuration(
        self,
        config_id: UUID,
        correlations: list[tuple[UUID, UUID, float]],
    ) -> list[SAACorrelationDTO]:
        """Atomically replace all correlations for a configuration.

        DELETE every existing row for ``config_id``, then INSERT the
        provided triplets. Each triplet's pair is normalised to upper-
        triangle order before insertion.

        Args:
            config_id: The owning configuration.
            correlations: A list of ``(asset_class_a_id,
                asset_class_b_id, correlation)`` triplets. Order
                within each pair is irrelevant — the repository
                normalises.

        Returns:
            The created DTOs in insertion order.

        Raises:
            ValueError: If any triplet has equal ids on both sides
                (self-correlation).
        """
        await self._session.execute(
            delete(SAACorrelation).where(SAACorrelation.configuration_id == config_id)
        )

        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        created: list[SAACorrelation] = []
        for a_id, b_id, value in correlations:
            if a_id == b_id:
                raise ValueError(
                    "SAACorrelation: self-correlation is implicit (1.0) and must not be stored."
                )
            ord_a, ord_b = _ordered_pair(a_id, b_id)
            model = SAACorrelation(
                tenant_id=active_tenant,
                configuration_id=config_id,
                asset_class_a_id=ord_a,
                asset_class_b_id=ord_b,
                correlation=value,
            )
            self._session.add(model)
            created.append(model)

        await self._session.flush()
        for model in created:
            await self._session.refresh(model)
        return [_to_dto(model) for model in created]
