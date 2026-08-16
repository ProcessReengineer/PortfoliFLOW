# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentMaturityWeightsRepository — per-investment maturity ladder.

Backs the ``investment_maturity_weight`` time-series table introduced
by ADR-0079 §2. One row per ``(investment_id, as_of_date,
maturity_bucket)`` — the
``uq_investment_maturity_weight_investment_date_bucket`` unique
constraint enforces the natural key.

Mirrors :class:`InvestmentRatingWeightsRepository` exactly, differing
only in the ``maturity_bucket`` taxonomy column. :meth:`upsert` is
the single-row natural-key write; :meth:`list_by_investments` is the
batched, N+1-free reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.models.investment_maturity_weight import InvestmentMaturityWeight
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class MaturityWeightDTO:
    """Plain data-only view of an ``investment_maturity_weight`` row."""

    id: UUID
    tenant_id: UUID
    investment_id: UUID
    as_of_date: _date
    maturity_bucket: str
    weight_pct: Decimal
    basis: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime


def _to_dto(model: InvestmentMaturityWeight) -> MaturityWeightDTO:
    return MaturityWeightDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        investment_id=model.investment_id,
        as_of_date=model.as_of_date,
        maturity_bucket=model.maturity_bucket,
        weight_pct=model.weight_pct,
        basis=model.basis,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class InvestmentMaturityWeightsRepository(BaseRepository):
    """Read and write per-investment maturity weights in the active tenant."""

    async def list_for_investment(
        self,
        investment_id: UUID,
        as_of_cutoff: _date | None = None,
    ) -> list[MaturityWeightDTO]:
        """Return every maturity-weight row for an investment.

        Args:
            investment_id: The investment whose weights to load.
            as_of_cutoff: When given, only rows with
                ``as_of_date <= as_of_cutoff`` are returned.

        Returns:
            All matching rows sorted by
            ``(as_of_date, maturity_bucket)`` ascending. Empty list
            for an unknown investment.
        """
        stmt = select(InvestmentMaturityWeight).where(
            InvestmentMaturityWeight.investment_id == investment_id
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentMaturityWeight.as_of_date <= as_of_cutoff)
        stmt = stmt.order_by(
            InvestmentMaturityWeight.as_of_date.asc(),
            InvestmentMaturityWeight.maturity_bucket.asc(),
        )
        result = await self._session.execute(stmt)
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_by_investments(
        self,
        investment_ids: list[UUID],
        as_of_cutoff: _date | None = None,
    ) -> dict[UUID, list[MaturityWeightDTO]]:
        """Return maturity-weight rows for many investments in one query.

        Batch counterpart to :meth:`list_for_investment`. The single
        SQL ``WHERE investment_id = ANY(:ids)`` replaces N
        per-investment SELECTs.

        Args:
            investment_ids: The investments whose weights to load.
                Empty list is valid and returns an empty dict.
            as_of_cutoff: When given, only rows with
                ``as_of_date <= as_of_cutoff`` are returned.

        Returns:
            A dict keyed by ``investment_id``; every id is present
            (empty list for investments with no rows). Within each
            list rows are sorted by ``(as_of_date, maturity_bucket)``
            ascending.
        """
        if not investment_ids:
            return {}
        stmt = select(InvestmentMaturityWeight).where(
            InvestmentMaturityWeight.investment_id.in_(investment_ids)
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentMaturityWeight.as_of_date <= as_of_cutoff)
        stmt = stmt.order_by(
            InvestmentMaturityWeight.investment_id.asc(),
            InvestmentMaturityWeight.as_of_date.asc(),
            InvestmentMaturityWeight.maturity_bucket.asc(),
        )
        result = await self._session.execute(stmt)
        grouped: dict[UUID, list[MaturityWeightDTO]] = {inv_id: [] for inv_id in investment_ids}
        for model in result.scalars().all():
            grouped[model.investment_id].append(_to_dto(model))
        return grouped

    async def upsert(
        self,
        investment_id: UUID,
        as_of_date: _date,
        maturity_bucket: str,
        *,
        weight_pct: Decimal,
        basis: str,
        created_by: UUID,
        ingest_origin: str = "excel",
    ) -> MaturityWeightDTO:
        """Insert or update a maturity-weight row by its natural key.

        Conflicts on ``(investment_id, as_of_date, maturity_bucket)``
        cause an UPDATE of ``weight_pct``, ``basis``,
        ``ingest_origin``, and ``updated_at``. ``created_by`` and
        ``created_at`` are preserved on update.

        Args:
            investment_id: The investment this weight belongs to.
            as_of_date: Statement-day date.
            maturity_bucket: One of the six canonical maturity buckets.
            weight_pct: Percentage in ``[0, 100]``.
            basis: ``'reported'`` or ``'computed'`` (ADR-0079).
            created_by: UUID of the user attributable for the write.
                Used only on INSERT; preserved on UPDATE.
            ingest_origin: The producer writing the row — ``'excel'``
                (default) or ``'manual'`` (ADR-0092).

        Returns:
            The created or updated :class:`MaturityWeightDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        stmt = (
            pg_insert(InvestmentMaturityWeight)
            .values(
                tenant_id=active_tenant,
                investment_id=investment_id,
                as_of_date=as_of_date,
                maturity_bucket=maturity_bucket,
                weight_pct=weight_pct,
                basis=basis,
                ingest_origin=ingest_origin,
                created_by=created_by,
            )
            .on_conflict_do_update(
                constraint=("uq_investment_maturity_weight_investment_date_bucket"),
                set_={
                    "weight_pct": weight_pct,
                    "basis": basis,
                    "ingest_origin": ingest_origin,
                    "updated_at": text("NOW()"),
                },
            )
            .returning(InvestmentMaturityWeight.id)
        )
        result = await self._session.execute(stmt)
        row_id: UUID = result.scalar_one()
        await self._session.flush()

        refreshed = await self._session.execute(
            select(InvestmentMaturityWeight).where(InvestmentMaturityWeight.id == row_id)
        )
        return _to_dto(refreshed.scalar_one())

    async def delete_for_investment(self, investment_id: UUID) -> int:
        """Delete **every** maturity-weight row for an investment.

        The importer clears the prior generation before re-inserting so
        a shrinking time series leaves no orphaned rows behind
        (replace-by-investment idempotency, ADR-0043 §3 / ADR-0081 §D).

        Args:
            investment_id: The investment whose rows to purge.

        Returns:
            The number of rows deleted across all statement days and
            buckets.
        """
        result = await self._session.execute(
            delete(InvestmentMaturityWeight).where(
                InvestmentMaturityWeight.investment_id == investment_id
            )
        )
        await self._session.flush()
        return result.rowcount or 0
