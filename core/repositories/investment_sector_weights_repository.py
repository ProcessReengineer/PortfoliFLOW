# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentSectorWeightsRepository — per-investment sector allocation persistence.

Backs the ``investment_sector_weights`` table introduced in migration
b007 (per ADR-0045 §2) and historised by ADR-0080. One row per
``(investment_id, as_of_date, sector_id)`` — the unique constraint
enforces the natural key.

Converged with the region and country repositories on the unified,
snapshot-aware contract of ADR-0080 §3: the full-history readers
(:meth:`list_for_investment` / :meth:`list_by_investments`), the
latest-snapshot readers (:meth:`list_latest_for_investment` /
:meth:`list_latest_by_investments`), and the date-scoped
:meth:`replace_snapshot_for_investment` write path used by the
Excel-import workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import aliased

from core.models.investment_sector_weight import InvestmentSectorWeight
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class SectorWeightDTO:
    """Plain data-only view of an ``investment_sector_weights`` row."""

    id: UUID
    tenant_id: UUID
    investment_id: UUID
    as_of_date: _date
    sector_id: UUID
    weight_pct: Decimal
    basis: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SectorWeightInput:
    """Caller-supplied weight payload for :meth:`replace_snapshot_for_investment`.

    The caller produces these from the Excel extractor (after
    sector-name resolution) or from the web edit surface; the
    repository never constructs them. One snapshot shares one
    ``as_of_date`` and one ``basis``, both passed as call parameters
    rather than per row.
    """

    sector_id: UUID
    weight_pct: Decimal


def _to_dto(model: InvestmentSectorWeight) -> SectorWeightDTO:
    return SectorWeightDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        investment_id=model.investment_id,
        as_of_date=model.as_of_date,
        sector_id=model.sector_id,
        weight_pct=model.weight_pct,
        basis=model.basis,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class InvestmentSectorWeightsRepository(BaseRepository):
    """Read and write per-investment sector weights in the active tenant."""

    async def list_for_investment(
        self,
        investment_id: UUID,
        *,
        as_of_cutoff: _date | None = None,
    ) -> list[SectorWeightDTO]:
        """Return the full history of sector weights for an investment.

        Args:
            investment_id: The investment whose weights to load.
            as_of_cutoff: When given, only rows with
                ``as_of_date <= as_of_cutoff`` are returned.

        Returns:
            Every matching row sorted by ``(as_of_date, sector_id)``
            ascending for stable rendering. Empty list for an unknown
            investment.
        """
        stmt = select(InvestmentSectorWeight).where(
            InvestmentSectorWeight.investment_id == investment_id
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentSectorWeight.as_of_date <= as_of_cutoff)
        stmt = stmt.order_by(
            InvestmentSectorWeight.as_of_date.asc(),
            InvestmentSectorWeight.sector_id.asc(),
        )
        result = await self._session.execute(stmt)
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_by_investments(
        self,
        investment_ids: list[UUID],
        *,
        as_of_cutoff: _date | None = None,
    ) -> dict[UUID, list[SectorWeightDTO]]:
        """Full-history batch counterpart to :meth:`list_for_investment`.

        Args:
            investment_ids: The investments whose weights to load.
                Empty list returns an empty dict.
            as_of_cutoff: When given, only rows with
                ``as_of_date <= as_of_cutoff`` are returned.

        Returns:
            A dict keyed by ``investment_id``; every id is present
            (empty list for investments with no rows). Within each
            list rows are sorted by ``(as_of_date, sector_id)``
            ascending.
        """
        if not investment_ids:
            return {}
        stmt = select(InvestmentSectorWeight).where(
            InvestmentSectorWeight.investment_id.in_(investment_ids)
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentSectorWeight.as_of_date <= as_of_cutoff)
        stmt = stmt.order_by(
            InvestmentSectorWeight.investment_id.asc(),
            InvestmentSectorWeight.as_of_date.asc(),
            InvestmentSectorWeight.sector_id.asc(),
        )
        result = await self._session.execute(stmt)
        grouped: dict[UUID, list[SectorWeightDTO]] = {inv_id: [] for inv_id in investment_ids}
        for model in result.scalars().all():
            grouped[model.investment_id].append(_to_dto(model))
        return grouped

    async def list_latest_for_investment(
        self,
        investment_id: UUID,
        *,
        as_of_cutoff: _date | None = None,
    ) -> list[SectorWeightDTO]:
        """Return the rows of the single most-recent snapshot.

        Args:
            investment_id: The investment whose latest snapshot to load.
            as_of_cutoff: When given, the latest snapshot at or before
                ``as_of_cutoff`` is selected.

        Returns:
            The rows of the ``max(as_of_date)`` snapshot, sorted by
            ``sector_id`` ascending. Empty list when the investment has
            no rows (at or before the cutoff).
        """
        max_stmt = select(func.max(InvestmentSectorWeight.as_of_date)).where(
            InvestmentSectorWeight.investment_id == investment_id
        )
        if as_of_cutoff is not None:
            max_stmt = max_stmt.where(InvestmentSectorWeight.as_of_date <= as_of_cutoff)
        latest = await self._session.scalar(max_stmt)
        if latest is None:
            return []
        return await self._list_on_date(investment_id, latest)

    async def list_latest_by_investments(
        self,
        investment_ids: list[UUID],
        *,
        as_of_cutoff: _date | None = None,
    ) -> dict[UUID, list[SectorWeightDTO]]:
        """Latest snapshot per investment, batched and N+1-free.

        A single query with a correlated ``max(as_of_date)`` subquery
        per ``investment_id`` selects, for each investment, only the
        rows of its most-recent snapshot (at or before the cutoff).

        Args:
            investment_ids: The investments whose latest snapshot to
                load. Empty list returns an empty dict.
            as_of_cutoff: When given, the latest snapshot at or before
                ``as_of_cutoff`` is selected per investment.

        Returns:
            A dict keyed by ``investment_id``; every id is present
            (empty list when the investment has no snapshot). Within
            each list rows are sorted by ``sector_id`` ascending.
        """
        if not investment_ids:
            return {}
        w2 = aliased(InvestmentSectorWeight)
        max_sub = select(func.max(w2.as_of_date)).where(
            w2.investment_id == InvestmentSectorWeight.investment_id
        )
        if as_of_cutoff is not None:
            max_sub = max_sub.where(w2.as_of_date <= as_of_cutoff)
        max_sub = max_sub.scalar_subquery()

        stmt = select(InvestmentSectorWeight).where(
            InvestmentSectorWeight.investment_id.in_(investment_ids)
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentSectorWeight.as_of_date <= as_of_cutoff)
        stmt = stmt.where(InvestmentSectorWeight.as_of_date == max_sub).order_by(
            InvestmentSectorWeight.investment_id.asc(),
            InvestmentSectorWeight.sector_id.asc(),
        )
        result = await self._session.execute(stmt)
        grouped: dict[UUID, list[SectorWeightDTO]] = {inv_id: [] for inv_id in investment_ids}
        for model in result.scalars().all():
            grouped[model.investment_id].append(_to_dto(model))
        return grouped

    async def delete_for_investment(self, investment_id: UUID) -> int:
        """Delete **every** sector-weight snapshot for an investment.

        Args:
            investment_id: The investment whose weights to purge.

        Returns:
            The number of rows deleted across all snapshots.
        """
        result = await self._session.execute(
            delete(InvestmentSectorWeight).where(
                InvestmentSectorWeight.investment_id == investment_id
            )
        )
        await self._session.flush()
        return result.rowcount or 0

    async def replace_snapshot_for_investment(
        self,
        investment_id: UUID,
        as_of_date: _date,
        weights: list[SectorWeightInput],
        *,
        basis: str,
        created_by: UUID,
        ingest_origin: str = "excel",
    ) -> list[SectorWeightDTO]:
        """Atomic, date-scoped replace of one sector-weight snapshot.

        Two SQL statements wrapped in the calling session's
        transaction: a ``DELETE`` of every existing weight for
        ``(investment_id, as_of_date)``, followed by ``INSERT`` of the
        new generation for that same snapshot. **Other snapshots
        (different ``as_of_date``) are left untouched** — this is the
        ADR-0080 historisation guarantee. The caller's surrounding
        transaction guarantees atomicity.

        Repeated invocation with the same ``weights`` is idempotent on
        final state.

        Args:
            investment_id: The investment whose snapshot to replace.
            as_of_date: Statement-day date the snapshot is anchored to.
            weights: New generation of weight payloads. May be empty,
                in which case the method is a pure delete of that
                snapshot.
            basis: ``'reported'`` or ``'computed'`` (ADR-0080), applied
                to every inserted row.
            created_by: UUID of the user attributable for the write.
            ingest_origin: The producer writing the snapshot —
                ``'excel'`` (default) or ``'manual'`` (ADR-0092).

        Returns:
            The persisted :class:`SectorWeightDTO` rows of the
            ``(investment_id, as_of_date)`` snapshot in ``sector_id``
            order.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        await self._session.execute(
            delete(InvestmentSectorWeight).where(
                InvestmentSectorWeight.investment_id == investment_id,
                InvestmentSectorWeight.as_of_date == as_of_date,
            )
        )

        for w in weights:
            self._session.add(
                InvestmentSectorWeight(
                    tenant_id=active_tenant,
                    investment_id=investment_id,
                    as_of_date=as_of_date,
                    sector_id=w.sector_id,
                    weight_pct=w.weight_pct,
                    basis=basis,
                    ingest_origin=ingest_origin,
                    created_by=created_by,
                )
            )
        await self._session.flush()

        return await self._list_on_date(investment_id, as_of_date)

    async def _list_on_date(self, investment_id: UUID, as_of_date: _date) -> list[SectorWeightDTO]:
        """Return the rows of one ``(investment_id, as_of_date)`` snapshot."""
        result = await self._session.execute(
            select(InvestmentSectorWeight)
            .where(
                InvestmentSectorWeight.investment_id == investment_id,
                InvestmentSectorWeight.as_of_date == as_of_date,
            )
            .order_by(InvestmentSectorWeight.sector_id.asc())
        )
        return [_to_dto(model) for model in result.scalars().all()]
