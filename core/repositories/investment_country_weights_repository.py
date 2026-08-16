# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentCountryWeightsRepository — per-investment country allocation persistence.

Backs the ``investment_country_weights`` table introduced in
migration b007 (per ADR-0045 §2) and historised by ADR-0080. One row
per ``(investment_id, as_of_date, country_iso_code)`` — the unique
constraint enforces the natural key.

Converged with the sector and region repositories on the unified,
snapshot-aware contract of ADR-0080 §3: the full-history readers
(:meth:`list_for_investment` / :meth:`list_by_investments`), the
latest-snapshot readers (:meth:`list_latest_for_investment` /
:meth:`list_latest_by_investments`), and the date-scoped
:meth:`replace_snapshot_for_investment` write path. The table is
reserved for ISO-granular data sources (GP report scrapers — roadmap
A2/A3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import aliased

from core.models.investment_country_weight import InvestmentCountryWeight
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class CountryWeightDTO:
    """Plain data-only view of an ``investment_country_weights`` row."""

    id: UUID
    tenant_id: UUID
    investment_id: UUID
    as_of_date: _date
    country_iso_code: str
    weight_pct: Decimal
    basis: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CountryWeightInput:
    """Caller-supplied weight payload for :meth:`replace_snapshot_for_investment`.

    The caller produces these from the Excel extractor or from the
    web edit surface; the repository never constructs them. One
    snapshot shares one ``as_of_date`` and one ``basis``, both passed
    as call parameters rather than per row.
    """

    country_iso_code: str
    weight_pct: Decimal


def _to_dto(model: InvestmentCountryWeight) -> CountryWeightDTO:
    return CountryWeightDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        investment_id=model.investment_id,
        as_of_date=model.as_of_date,
        country_iso_code=model.country_iso_code,
        weight_pct=model.weight_pct,
        basis=model.basis,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class InvestmentCountryWeightsRepository(BaseRepository):
    """Read and write per-investment country weights in the active tenant."""

    async def list_for_investment(
        self,
        investment_id: UUID,
        *,
        as_of_cutoff: _date | None = None,
    ) -> list[CountryWeightDTO]:
        """Return the full history of country weights for an investment.

        Args:
            investment_id: The investment whose weights to load.
            as_of_cutoff: When given, only rows with
                ``as_of_date <= as_of_cutoff`` are returned.

        Returns:
            Every matching row sorted by ``(as_of_date,
            country_iso_code)`` ascending for stable rendering. Empty
            list for an unknown investment.
        """
        stmt = select(InvestmentCountryWeight).where(
            InvestmentCountryWeight.investment_id == investment_id
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentCountryWeight.as_of_date <= as_of_cutoff)
        stmt = stmt.order_by(
            InvestmentCountryWeight.as_of_date.asc(),
            InvestmentCountryWeight.country_iso_code.asc(),
        )
        result = await self._session.execute(stmt)
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_by_investments(
        self,
        investment_ids: list[UUID],
        *,
        as_of_cutoff: _date | None = None,
    ) -> dict[UUID, list[CountryWeightDTO]]:
        """Full-history batch counterpart to :meth:`list_for_investment`.

        Args:
            investment_ids: The investments whose weights to load.
                Empty list returns an empty dict.
            as_of_cutoff: When given, only rows with
                ``as_of_date <= as_of_cutoff`` are returned.

        Returns:
            A dict keyed by ``investment_id``; every id is present
            (empty list for investments with no rows). Within each
            list rows are sorted by ``(as_of_date, country_iso_code)``
            ascending.
        """
        if not investment_ids:
            return {}
        stmt = select(InvestmentCountryWeight).where(
            InvestmentCountryWeight.investment_id.in_(investment_ids)
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentCountryWeight.as_of_date <= as_of_cutoff)
        stmt = stmt.order_by(
            InvestmentCountryWeight.investment_id.asc(),
            InvestmentCountryWeight.as_of_date.asc(),
            InvestmentCountryWeight.country_iso_code.asc(),
        )
        result = await self._session.execute(stmt)
        grouped: dict[UUID, list[CountryWeightDTO]] = {inv_id: [] for inv_id in investment_ids}
        for model in result.scalars().all():
            grouped[model.investment_id].append(_to_dto(model))
        return grouped

    async def list_latest_for_investment(
        self,
        investment_id: UUID,
        *,
        as_of_cutoff: _date | None = None,
    ) -> list[CountryWeightDTO]:
        """Return the rows of the single most-recent snapshot.

        Args:
            investment_id: The investment whose latest snapshot to load.
            as_of_cutoff: When given, the latest snapshot at or before
                ``as_of_cutoff`` is selected.

        Returns:
            The rows of the ``max(as_of_date)`` snapshot, sorted by
            ``country_iso_code`` ascending. Empty list when the
            investment has no rows (at or before the cutoff).
        """
        max_stmt = select(func.max(InvestmentCountryWeight.as_of_date)).where(
            InvestmentCountryWeight.investment_id == investment_id
        )
        if as_of_cutoff is not None:
            max_stmt = max_stmt.where(InvestmentCountryWeight.as_of_date <= as_of_cutoff)
        latest = await self._session.scalar(max_stmt)
        if latest is None:
            return []
        return await self._list_on_date(investment_id, latest)

    async def list_latest_by_investments(
        self,
        investment_ids: list[UUID],
        *,
        as_of_cutoff: _date | None = None,
    ) -> dict[UUID, list[CountryWeightDTO]]:
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
            each list rows are sorted by ``country_iso_code`` ascending.
        """
        if not investment_ids:
            return {}
        w2 = aliased(InvestmentCountryWeight)
        max_sub = select(func.max(w2.as_of_date)).where(
            w2.investment_id == InvestmentCountryWeight.investment_id
        )
        if as_of_cutoff is not None:
            max_sub = max_sub.where(w2.as_of_date <= as_of_cutoff)
        max_sub = max_sub.scalar_subquery()

        stmt = select(InvestmentCountryWeight).where(
            InvestmentCountryWeight.investment_id.in_(investment_ids)
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentCountryWeight.as_of_date <= as_of_cutoff)
        stmt = stmt.where(InvestmentCountryWeight.as_of_date == max_sub).order_by(
            InvestmentCountryWeight.investment_id.asc(),
            InvestmentCountryWeight.country_iso_code.asc(),
        )
        result = await self._session.execute(stmt)
        grouped: dict[UUID, list[CountryWeightDTO]] = {inv_id: [] for inv_id in investment_ids}
        for model in result.scalars().all():
            grouped[model.investment_id].append(_to_dto(model))
        return grouped

    async def delete_for_investment(self, investment_id: UUID) -> int:
        """Delete **every** country-weight snapshot for an investment.

        Args:
            investment_id: The investment whose weights to purge.

        Returns:
            The number of rows deleted across all snapshots.
        """
        result = await self._session.execute(
            delete(InvestmentCountryWeight).where(
                InvestmentCountryWeight.investment_id == investment_id
            )
        )
        await self._session.flush()
        return result.rowcount or 0

    async def replace_snapshot_for_investment(
        self,
        investment_id: UUID,
        as_of_date: _date,
        weights: list[CountryWeightInput],
        *,
        basis: str,
        created_by: UUID,
        ingest_origin: str = "excel",
    ) -> list[CountryWeightDTO]:
        """Atomic, date-scoped replace of one country-weight snapshot.

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
            The persisted :class:`CountryWeightDTO` rows of the
            ``(investment_id, as_of_date)`` snapshot in
            ``country_iso_code`` order.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        await self._session.execute(
            delete(InvestmentCountryWeight).where(
                InvestmentCountryWeight.investment_id == investment_id,
                InvestmentCountryWeight.as_of_date == as_of_date,
            )
        )

        for w in weights:
            self._session.add(
                InvestmentCountryWeight(
                    tenant_id=active_tenant,
                    investment_id=investment_id,
                    as_of_date=as_of_date,
                    country_iso_code=w.country_iso_code.upper(),
                    weight_pct=w.weight_pct,
                    basis=basis,
                    ingest_origin=ingest_origin,
                    created_by=created_by,
                )
            )
        await self._session.flush()

        return await self._list_on_date(investment_id, as_of_date)

    async def _list_on_date(self, investment_id: UUID, as_of_date: _date) -> list[CountryWeightDTO]:
        """Return the rows of one ``(investment_id, as_of_date)`` snapshot."""
        result = await self._session.execute(
            select(InvestmentCountryWeight)
            .where(
                InvestmentCountryWeight.investment_id == investment_id,
                InvestmentCountryWeight.as_of_date == as_of_date,
            )
            .order_by(InvestmentCountryWeight.country_iso_code.asc())
        )
        return [_to_dto(model) for model in result.scalars().all()]
