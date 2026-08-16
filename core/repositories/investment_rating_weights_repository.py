# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentRatingWeightsRepository — per-investment credit-rating mix.

Backs the ``investment_rating_weight`` time-series table introduced
by ADR-0079 §2. One row per ``(investment_id, as_of_date,
rating_bucket)`` — the
``uq_investment_rating_weight_investment_date_bucket`` unique
constraint enforces the natural key.

Mirrors :class:`InvestmentNavRepository`: :meth:`upsert` is the
single-row write keyed on the natural key, and
:meth:`list_by_investments` is the batched, N+1-free reader. The
``rating_bucket`` dimension makes the natural key three columns
wide, but the time-series shape (``as_of_date``) matches the NAV
sibling rather than the point-in-time ``investment_sector_weights``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.models.investment_rating_weight import InvestmentRatingWeight
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class RatingWeightDTO:
    """Plain data-only view of an ``investment_rating_weight`` row."""

    id: UUID
    tenant_id: UUID
    investment_id: UUID
    as_of_date: _date
    rating_bucket: str
    weight_pct: Decimal
    basis: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime


def _to_dto(model: InvestmentRatingWeight) -> RatingWeightDTO:
    return RatingWeightDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        investment_id=model.investment_id,
        as_of_date=model.as_of_date,
        rating_bucket=model.rating_bucket,
        weight_pct=model.weight_pct,
        basis=model.basis,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class InvestmentRatingWeightsRepository(BaseRepository):
    """Read and write per-investment rating weights in the active tenant."""

    async def list_for_investment(
        self,
        investment_id: UUID,
        as_of_cutoff: _date | None = None,
    ) -> list[RatingWeightDTO]:
        """Return every rating-weight row for an investment.

        Args:
            investment_id: The investment whose weights to load.
            as_of_cutoff: When given, only rows with
                ``as_of_date <= as_of_cutoff`` are returned.

        Returns:
            All matching rows sorted by ``(as_of_date, rating_bucket)``
            ascending for stable rendering. Empty list for an unknown
            investment.
        """
        stmt = select(InvestmentRatingWeight).where(
            InvestmentRatingWeight.investment_id == investment_id
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentRatingWeight.as_of_date <= as_of_cutoff)
        stmt = stmt.order_by(
            InvestmentRatingWeight.as_of_date.asc(),
            InvestmentRatingWeight.rating_bucket.asc(),
        )
        result = await self._session.execute(stmt)
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_by_investments(
        self,
        investment_ids: list[UUID],
        as_of_cutoff: _date | None = None,
    ) -> dict[UUID, list[RatingWeightDTO]]:
        """Return rating-weight rows for many investments in one query.

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
            list rows are sorted by ``(as_of_date, rating_bucket)``
            ascending.
        """
        if not investment_ids:
            return {}
        stmt = select(InvestmentRatingWeight).where(
            InvestmentRatingWeight.investment_id.in_(investment_ids)
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentRatingWeight.as_of_date <= as_of_cutoff)
        stmt = stmt.order_by(
            InvestmentRatingWeight.investment_id.asc(),
            InvestmentRatingWeight.as_of_date.asc(),
            InvestmentRatingWeight.rating_bucket.asc(),
        )
        result = await self._session.execute(stmt)
        grouped: dict[UUID, list[RatingWeightDTO]] = {inv_id: [] for inv_id in investment_ids}
        for model in result.scalars().all():
            grouped[model.investment_id].append(_to_dto(model))
        return grouped

    async def upsert(
        self,
        investment_id: UUID,
        as_of_date: _date,
        rating_bucket: str,
        *,
        weight_pct: Decimal,
        basis: str,
        created_by: UUID,
        ingest_origin: str = "excel",
    ) -> RatingWeightDTO:
        """Insert or update a rating-weight row by its natural key.

        Conflicts on ``(investment_id, as_of_date, rating_bucket)``
        cause an UPDATE of ``weight_pct``, ``basis``,
        ``ingest_origin``, and ``updated_at``. ``created_by`` and
        ``created_at`` are preserved on update.

        Args:
            investment_id: The investment this weight belongs to.
            as_of_date: Statement-day date.
            rating_bucket: One of the eight canonical rating buckets.
            weight_pct: Percentage in ``[0, 100]``.
            basis: ``'reported'`` or ``'computed'`` (ADR-0079).
            created_by: UUID of the user attributable for the write.
                Used only on INSERT; preserved on UPDATE.
            ingest_origin: The producer writing the row — ``'excel'``
                (default) or ``'manual'`` (ADR-0092).

        Returns:
            The created or updated :class:`RatingWeightDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        stmt = (
            pg_insert(InvestmentRatingWeight)
            .values(
                tenant_id=active_tenant,
                investment_id=investment_id,
                as_of_date=as_of_date,
                rating_bucket=rating_bucket,
                weight_pct=weight_pct,
                basis=basis,
                ingest_origin=ingest_origin,
                created_by=created_by,
            )
            .on_conflict_do_update(
                constraint=("uq_investment_rating_weight_investment_date_bucket"),
                set_={
                    "weight_pct": weight_pct,
                    "basis": basis,
                    "ingest_origin": ingest_origin,
                    "updated_at": text("NOW()"),
                },
            )
            .returning(InvestmentRatingWeight.id)
        )
        result = await self._session.execute(stmt)
        row_id: UUID = result.scalar_one()
        await self._session.flush()

        refreshed = await self._session.execute(
            select(InvestmentRatingWeight).where(InvestmentRatingWeight.id == row_id)
        )
        return _to_dto(refreshed.scalar_one())

    async def delete_for_investment(self, investment_id: UUID) -> int:
        """Delete **every** rating-weight row for an investment.

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
            delete(InvestmentRatingWeight).where(
                InvestmentRatingWeight.investment_id == investment_id
            )
        )
        await self._session.flush()
        return result.rowcount or 0
