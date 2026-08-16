# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AssetClassBenchmarkMappingRepository — asset-class → benchmark mapping persistence.

Backs the ``asset_class_benchmark_mapping`` table introduced in
migration b011 (per ADR-0061 §Decision). One row per
``(asset_class_id, benchmark_id, weight)`` triple, tenant-scoped
via the denormalised ``tenant_id`` column.

In Phase 1 each asset class carries at most one mapping with
``weight = 1.0``; the schema permits composite blends
(multiple rows summing to ≤ 1) but the importer does not exercise
that path yet. The repository exposes both per-asset-class and
catalogue-wide lookups so the analytics layer can fetch the full
mapping graph in one query.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete as sa_delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.models.asset_class_benchmark_mapping import (
    AssetClassBenchmarkMapping,
)
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class AssetClassBenchmarkMappingDTO:
    """Plain data-only view of an ``asset_class_benchmark_mapping`` row."""

    id: UUID
    tenant_id: UUID
    asset_class_id: UUID
    benchmark_id: UUID
    weight: Decimal
    created_at: datetime
    updated_at: datetime


def _to_dto(
    model: AssetClassBenchmarkMapping,
) -> AssetClassBenchmarkMappingDTO:
    return AssetClassBenchmarkMappingDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        asset_class_id=model.asset_class_id,
        benchmark_id=model.benchmark_id,
        weight=model.weight,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class AssetClassBenchmarkMappingRepository(BaseRepository):
    """Read and write asset-class → benchmark mappings in the active tenant context."""

    async def upsert_mapping(
        self,
        asset_class_id: UUID,
        benchmark_id: UUID,
        weight: Decimal,
    ) -> AssetClassBenchmarkMappingDTO:
        """Insert or refresh one mapping row.

        On conflict (``(asset_class_id, benchmark_id)`` already
        exists), the ``weight`` column is overwritten with the
        supplied value; ``created_at`` is preserved and
        ``updated_at`` bumps to ``NOW()``.

        Args:
            asset_class_id: The asset class to associate.
            benchmark_id: The benchmark to associate.
            weight: Weight in ``[0, 1]`` (DB CHECK enforces the
                bound; the service layer is expected to validate
                before reaching this path).

        Returns:
            The :class:`AssetClassBenchmarkMappingDTO` after the
            upsert.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        stmt = pg_insert(AssetClassBenchmarkMapping).values(
            tenant_id=active_tenant,
            asset_class_id=asset_class_id,
            benchmark_id=benchmark_id,
            weight=weight,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_acbm_asset_class_benchmark",
            set_={
                "weight": stmt.excluded.weight,
                "updated_at": text("NOW()"),
            },
        )
        await self._session.execute(stmt)
        await self._session.flush()

        result = await self._session.execute(
            select(AssetClassBenchmarkMapping)
            .where(AssetClassBenchmarkMapping.asset_class_id == asset_class_id)
            .where(AssetClassBenchmarkMapping.benchmark_id == benchmark_id)
        )
        model = result.scalar_one()
        return _to_dto(model)

    async def delete_mappings_for_asset_class(self, asset_class_id: UUID) -> int:
        """Delete every mapping for one asset class.

        Used by the idempotent Excel re-import path: before
        inserting the workbook's mapping rows for an asset class,
        the service deletes the existing generation so the final
        state always reflects the workbook.

        Args:
            asset_class_id: The asset class whose mappings to clear.

        Returns:
            The number of rows deleted.
        """
        result = await self._session.execute(
            sa_delete(AssetClassBenchmarkMapping).where(
                AssetClassBenchmarkMapping.asset_class_id == asset_class_id
            )
        )
        await self._session.flush()
        return result.rowcount or 0

    async def list_all(self) -> list[AssetClassBenchmarkMappingDTO]:
        """Return every mapping row in the active tenant context.

        Ordered by ``(asset_class_id, benchmark_id)`` so callers can
        group composite blends by asset class without an extra sort.
        """
        result = await self._session.execute(
            select(AssetClassBenchmarkMapping).order_by(
                AssetClassBenchmarkMapping.asset_class_id,
                AssetClassBenchmarkMapping.benchmark_id,
            )
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_for_asset_class(
        self, asset_class_id: UUID
    ) -> list[AssetClassBenchmarkMappingDTO]:
        """Return every mapping for one asset class.

        Phase 1 produces at most one row per asset class; the plural
        return shape anticipates Phase 2 composite blends.

        Args:
            asset_class_id: The asset class to look up.

        Returns:
            Matching mappings, ordered by ``benchmark_id``.
        """
        result = await self._session.execute(
            select(AssetClassBenchmarkMapping)
            .where(AssetClassBenchmarkMapping.asset_class_id == asset_class_id)
            .order_by(AssetClassBenchmarkMapping.benchmark_id)
        )
        return [_to_dto(model) for model in result.scalars().all()]
