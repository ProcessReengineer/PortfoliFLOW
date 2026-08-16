# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentRegionWeightsRepository — per-investment region allocation.

Backs the ``investment_region_weights`` table introduced in migration
b009 (per ADR-0046) and historised by ADR-0080. One row per
``(investment_id, as_of_date, region_id)`` — the unique constraint
enforces the natural key.

Converged with the sector and country repositories on the unified,
snapshot-aware contract of ADR-0080 §3: the full-history readers
(:meth:`list_for_investment` / :meth:`list_by_investments`), the
latest-snapshot readers (:meth:`list_latest_for_investment` /
:meth:`list_latest_by_investments`), and the date-scoped
:meth:`replace_snapshot_for_investment` write path used by the
Excel-import workflow. The region table lacks an ``updated_at``
column by design (ADR-0080 §Scope boundaries); the block-replace
write needs none, so :class:`RegionWeightDTO` carries no such field.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from core.models.investment_region_weight import InvestmentRegionWeight
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class RegionWeightDTO:
    """Plain data-only view of an ``investment_region_weights`` row."""

    id: UUID
    tenant_id: UUID
    investment_id: UUID
    as_of_date: _date
    region_id: UUID
    weight_pct: Decimal
    basis: str
    created_by: UUID
    created_at: datetime
    # b021 / ADR-0092: the producer that wrote the row. Defaulted so
    # existing direct constructions of the DTO stay valid.
    ingest_origin: str = "excel"


@dataclass(frozen=True)
class RegionWeightInput:
    """Caller-supplied weight payload for :meth:`replace_snapshot_for_investment`.

    The caller produces these from the Excel extractor (after region-
    label resolution) or from the web edit surface; the repository
    never constructs them. One snapshot shares one ``as_of_date`` and
    one ``basis``, both passed as call parameters rather than per row.
    """

    region_id: UUID
    weight_pct: Decimal


def _to_dto(model: InvestmentRegionWeight) -> RegionWeightDTO:
    return RegionWeightDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        investment_id=model.investment_id,
        as_of_date=model.as_of_date,
        region_id=model.region_id,
        weight_pct=model.weight_pct,
        basis=model.basis,
        created_by=model.created_by,
        created_at=model.created_at,
        ingest_origin=model.ingest_origin,
    )


class InvestmentRegionWeightsRepository(BaseRepository):
    """Read and write per-investment region weights in the active tenant."""

    async def list_for_investment(
        self,
        investment_id: UUID,
        *,
        as_of_cutoff: _date | None = None,
    ) -> list[RegionWeightDTO]:
        """Return the full history of region weights for an investment.

        Args:
            investment_id: The investment whose weights to load.
            as_of_cutoff: When given, only rows with
                ``as_of_date <= as_of_cutoff`` are returned.

        Returns:
            Every matching row sorted by ``(as_of_date, region_id)``
            ascending for stable rendering. Empty list for an unknown
            investment.
        """
        stmt = select(InvestmentRegionWeight).where(
            InvestmentRegionWeight.investment_id == investment_id
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentRegionWeight.as_of_date <= as_of_cutoff)
        stmt = stmt.order_by(
            InvestmentRegionWeight.as_of_date.asc(),
            InvestmentRegionWeight.region_id.asc(),
        )
        result = await self._session.execute(stmt)
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_by_investments(
        self,
        investment_ids: list[UUID],
        *,
        as_of_cutoff: _date | None = None,
    ) -> dict[UUID, list[RegionWeightDTO]]:
        """Full-history batch counterpart to :meth:`list_for_investment`.

        Args:
            investment_ids: The investments whose weights to load.
                Empty list returns an empty dict.
            as_of_cutoff: When given, only rows with
                ``as_of_date <= as_of_cutoff`` are returned.

        Returns:
            A dict keyed by ``investment_id``; every id is present
            (empty list for investments with no rows). Within each
            list rows are sorted by ``(as_of_date, region_id)``
            ascending.
        """
        if not investment_ids:
            return {}
        stmt = select(InvestmentRegionWeight).where(
            InvestmentRegionWeight.investment_id.in_(investment_ids)
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentRegionWeight.as_of_date <= as_of_cutoff)
        stmt = stmt.order_by(
            InvestmentRegionWeight.investment_id.asc(),
            InvestmentRegionWeight.as_of_date.asc(),
            InvestmentRegionWeight.region_id.asc(),
        )
        result = await self._session.execute(stmt)
        grouped: dict[UUID, list[RegionWeightDTO]] = {inv_id: [] for inv_id in investment_ids}
        for model in result.scalars().all():
            grouped[model.investment_id].append(_to_dto(model))
        return grouped

    async def list_latest_for_investment(
        self,
        investment_id: UUID,
        *,
        as_of_cutoff: _date | None = None,
    ) -> list[RegionWeightDTO]:
        """Return the rows of the single most-recent snapshot.

        Args:
            investment_id: The investment whose latest snapshot to load.
            as_of_cutoff: When given, the latest snapshot at or before
                ``as_of_cutoff`` is selected.

        Returns:
            The rows of the ``max(as_of_date)`` snapshot, sorted by
            ``region_id`` ascending. Empty list when the investment has
            no rows (at or before the cutoff).
        """
        max_stmt = select(func.max(InvestmentRegionWeight.as_of_date)).where(
            InvestmentRegionWeight.investment_id == investment_id
        )
        if as_of_cutoff is not None:
            max_stmt = max_stmt.where(InvestmentRegionWeight.as_of_date <= as_of_cutoff)
        latest = await self._session.scalar(max_stmt)
        if latest is None:
            return []
        return await self._list_on_date(investment_id, latest)

    async def list_latest_by_investments(
        self,
        investment_ids: list[UUID],
        *,
        as_of_cutoff: _date | None = None,
    ) -> dict[UUID, list[RegionWeightDTO]]:
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
            each list rows are sorted by ``region_id`` ascending.
        """
        if not investment_ids:
            return {}
        w2 = aliased(InvestmentRegionWeight)
        max_sub = select(func.max(w2.as_of_date)).where(
            w2.investment_id == InvestmentRegionWeight.investment_id
        )
        if as_of_cutoff is not None:
            max_sub = max_sub.where(w2.as_of_date <= as_of_cutoff)
        max_sub = max_sub.scalar_subquery()

        stmt = select(InvestmentRegionWeight).where(
            InvestmentRegionWeight.investment_id.in_(investment_ids)
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentRegionWeight.as_of_date <= as_of_cutoff)
        stmt = stmt.where(InvestmentRegionWeight.as_of_date == max_sub).order_by(
            InvestmentRegionWeight.investment_id.asc(),
            InvestmentRegionWeight.region_id.asc(),
        )
        result = await self._session.execute(stmt)
        grouped: dict[UUID, list[RegionWeightDTO]] = {inv_id: [] for inv_id in investment_ids}
        for model in result.scalars().all():
            grouped[model.investment_id].append(_to_dto(model))
        return grouped

    async def delete_for_investment(self, investment_id: UUID) -> int:
        """Delete **every** region-weight snapshot for an investment.

        Args:
            investment_id: The investment whose weights to purge.

        Returns:
            The number of rows deleted across all snapshots.
        """
        result = await self._session.execute(
            delete(InvestmentRegionWeight).where(
                InvestmentRegionWeight.investment_id == investment_id
            )
        )
        await self._session.flush()
        return result.rowcount or 0

    async def replace_snapshot_for_investment(
        self,
        investment_id: UUID,
        as_of_date: _date,
        weights: list[RegionWeightInput],
        *,
        basis: str,
        created_by: UUID,
        ingest_origin: str = "excel",
    ) -> list[RegionWeightDTO]:
        """Atomic, date-scoped replace of one region-weight snapshot.

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
                ``'excel'`` (default, the Excel importer) or
                ``'manual'`` (ADR-0092). The live producer uses
                :meth:`upsert_live` per row instead.

        Returns:
            The persisted :class:`RegionWeightDTO` rows of the
            ``(investment_id, as_of_date)`` snapshot in ``region_id``
            order.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        await self._session.execute(
            delete(InvestmentRegionWeight).where(
                InvestmentRegionWeight.investment_id == investment_id,
                InvestmentRegionWeight.as_of_date == as_of_date,
            )
        )

        for w in weights:
            self._session.add(
                InvestmentRegionWeight(
                    tenant_id=active_tenant,
                    investment_id=investment_id,
                    as_of_date=as_of_date,
                    region_id=w.region_id,
                    weight_pct=w.weight_pct,
                    basis=basis,
                    ingest_origin=ingest_origin,
                    created_by=created_by,
                )
            )
        await self._session.flush()

        return await self._list_on_date(investment_id, as_of_date)

    async def upsert_live(
        self,
        investment_id: UUID,
        as_of_date: _date,
        region_id: UUID,
        *,
        weight_pct: Decimal,
        basis: str,
        created_by: UUID,
    ) -> RegionWeightDTO | None:
        """Row-level conditional upsert for the **live** producer (ADR-0092).

        The Excel-precedence guard on the historised natural key
        ``(investment_id, as_of_date, region_id)``, identical in shape to
        :meth:`InvestmentNavRepository.upsert_live`:

        - No row → **INSERT** as ``ingest_origin = 'live'``.
        - A prior ``'live'`` row → **UPDATE in place**.
        - An ``'excel'`` / ``'manual'`` row → the ``WHERE`` guard skips
          the ``DO UPDATE``, the row is left **byte-identical**, and the
          method returns ``None`` (recorded no-op).

        This is the representative composition-weight guard. The four
        sibling weight families share the identical natural-key shape and
        gain the same seam when a bucketed-weight provider (and the DTO
        dimension it needs) lands (ADR-0091 / ADR-0092). The region table
        has no ``updated_at`` column (ADR-0080), so none is bumped.

        Args:
            investment_id: The investment this weight belongs to.
            as_of_date: Statement-day date the snapshot is anchored to.
            region_id: The region bucket.
            weight_pct: Percentage in ``[0, 100]``.
            basis: ``'reported'`` or ``'computed'`` (ADR-0079 / ADR-0080).
            created_by: UUID of the acting user.

        Returns:
            The inserted / updated :class:`RegionWeightDTO`, or ``None``
            when an ``'excel'`` / ``'manual'`` row was left untouched.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        stmt = (
            pg_insert(InvestmentRegionWeight)
            .values(
                tenant_id=active_tenant,
                investment_id=investment_id,
                as_of_date=as_of_date,
                region_id=region_id,
                weight_pct=weight_pct,
                basis=basis,
                ingest_origin="live",
                created_by=created_by,
            )
            .on_conflict_do_update(
                constraint=("uq_investment_region_weights_investment_date_region"),
                set_={
                    "weight_pct": weight_pct,
                    "basis": basis,
                    "ingest_origin": "live",
                },
                where=InvestmentRegionWeight.ingest_origin == "live",
            )
            .returning(InvestmentRegionWeight.id)
        )
        result = await self._session.execute(stmt)
        row_id: UUID | None = result.scalar_one_or_none()
        await self._session.flush()
        if row_id is None:
            return None

        refreshed = await self._session.execute(
            select(InvestmentRegionWeight).where(InvestmentRegionWeight.id == row_id)
        )
        return _to_dto(refreshed.scalar_one())

    async def _list_on_date(self, investment_id: UUID, as_of_date: _date) -> list[RegionWeightDTO]:
        """Return the rows of one ``(investment_id, as_of_date)`` snapshot."""
        result = await self._session.execute(
            select(InvestmentRegionWeight)
            .where(
                InvestmentRegionWeight.investment_id == investment_id,
                InvestmentRegionWeight.as_of_date == as_of_date,
            )
            .order_by(InvestmentRegionWeight.region_id.asc())
        )
        return [_to_dto(model) for model in result.scalars().all()]
