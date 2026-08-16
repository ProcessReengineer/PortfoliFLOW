# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentBondAnalyticsRepository — per-investment FI characteristics.

Backs the ``investment_bond_analytics`` time-series table introduced
by ADR-0079 §2. One row per ``(investment_id, as_of_date)`` —
the ``uq_investment_bond_analytics_investment_date`` unique
constraint enforces the natural key.

Mirrors :class:`InvestmentNavRepository`: :meth:`upsert` is the
single-row write keyed on the natural key, and
:meth:`list_by_investments` is the batched, N+1-free reader used by
universe-wide services (the Fixed-Income archetype loads many
investments at once).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.models.investment_bond_analytics import InvestmentBondAnalytics
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class BondAnalyticsDTO:
    """Plain data-only view of an ``investment_bond_analytics`` row."""

    id: UUID
    tenant_id: UUID
    investment_id: UUID
    as_of_date: _date
    ytm: Decimal
    eff_duration: Decimal
    oas: Decimal | None
    convexity: Decimal | None
    basis: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime


def _to_dto(model: InvestmentBondAnalytics) -> BondAnalyticsDTO:
    return BondAnalyticsDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        investment_id=model.investment_id,
        as_of_date=model.as_of_date,
        ytm=model.ytm,
        eff_duration=model.eff_duration,
        oas=model.oas,
        convexity=model.convexity,
        basis=model.basis,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class InvestmentBondAnalyticsRepository(BaseRepository):
    """Read and write FI-characteristics rows in the active tenant."""

    async def list_for_investment(
        self,
        investment_id: UUID,
        as_of_cutoff: _date | None = None,
    ) -> list[BondAnalyticsDTO]:
        """Return every FI-characteristics row for an investment.

        Args:
            investment_id: The investment whose characteristics to load.
            as_of_cutoff: When given, only rows with
                ``as_of_date <= as_of_cutoff`` are returned (the same
                truncation NAV-driven services apply for as-of-date
                reporting).

        Returns:
            All matching rows sorted by ``as_of_date`` ascending. Empty
            list for an unknown investment.
        """
        stmt = select(InvestmentBondAnalytics).where(
            InvestmentBondAnalytics.investment_id == investment_id
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentBondAnalytics.as_of_date <= as_of_cutoff)
        stmt = stmt.order_by(InvestmentBondAnalytics.as_of_date.asc())
        result = await self._session.execute(stmt)
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_by_investments(
        self,
        investment_ids: list[UUID],
        as_of_cutoff: _date | None = None,
    ) -> dict[UUID, list[BondAnalyticsDTO]]:
        """Return FI-characteristics rows for many investments in one query.

        Batch counterpart to :meth:`list_for_investment`. The single
        SQL ``WHERE investment_id = ANY(:ids)`` replaces N
        per-investment SELECTs and is the recommended call site for
        universe-wide services.

        Args:
            investment_ids: The investments whose characteristics to
                load. Empty list is valid and returns an empty dict.
            as_of_cutoff: When given, only rows with
                ``as_of_date <= as_of_cutoff`` are returned.

        Returns:
            A dict keyed by ``investment_id``; every id from
            ``investment_ids`` is present (empty list for investments
            with no rows). Within each list rows are sorted by
            ``as_of_date`` ascending.
        """
        if not investment_ids:
            return {}
        stmt = select(InvestmentBondAnalytics).where(
            InvestmentBondAnalytics.investment_id.in_(investment_ids)
        )
        if as_of_cutoff is not None:
            stmt = stmt.where(InvestmentBondAnalytics.as_of_date <= as_of_cutoff)
        stmt = stmt.order_by(
            InvestmentBondAnalytics.investment_id.asc(),
            InvestmentBondAnalytics.as_of_date.asc(),
        )
        result = await self._session.execute(stmt)
        grouped: dict[UUID, list[BondAnalyticsDTO]] = {inv_id: [] for inv_id in investment_ids}
        for model in result.scalars().all():
            grouped[model.investment_id].append(_to_dto(model))
        return grouped

    async def upsert(
        self,
        investment_id: UUID,
        as_of_date: _date,
        *,
        ytm: Decimal,
        eff_duration: Decimal,
        oas: Decimal | None,
        convexity: Decimal | None,
        basis: str,
        created_by: UUID,
    ) -> BondAnalyticsDTO:
        """Insert or update an FI-characteristics row by its natural key.

        Conflicts on ``(investment_id, as_of_date)`` cause an UPDATE of
        the characteristic columns, ``basis``, and ``updated_at``.
        ``created_by`` and ``created_at`` are preserved on update so
        the row's original author stays attributable.

        Args:
            investment_id: The investment this row belongs to.
            as_of_date: Statement-day date.
            ytm: Yield-to-maturity (decimal fraction; may be negative).
            eff_duration: Effective duration in years.
            oas: Option-adjusted spread, or ``None`` if not reported.
            convexity: Convexity, or ``None`` if not reported.
            basis: ``'reported'`` or ``'computed'`` (ADR-0079).
            created_by: UUID of the user attributable for the write.
                Used only on INSERT; preserved on UPDATE.

        Returns:
            The created or updated :class:`BondAnalyticsDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        stmt = (
            pg_insert(InvestmentBondAnalytics)
            .values(
                tenant_id=active_tenant,
                investment_id=investment_id,
                as_of_date=as_of_date,
                ytm=ytm,
                eff_duration=eff_duration,
                oas=oas,
                convexity=convexity,
                basis=basis,
                created_by=created_by,
            )
            .on_conflict_do_update(
                constraint="uq_investment_bond_analytics_investment_date",
                set_={
                    "ytm": ytm,
                    "eff_duration": eff_duration,
                    "oas": oas,
                    "convexity": convexity,
                    "basis": basis,
                    "updated_at": text("NOW()"),
                },
            )
            .returning(InvestmentBondAnalytics.id)
        )
        result = await self._session.execute(stmt)
        row_id: UUID = result.scalar_one()
        await self._session.flush()

        refreshed = await self._session.execute(
            select(InvestmentBondAnalytics).where(InvestmentBondAnalytics.id == row_id)
        )
        return _to_dto(refreshed.scalar_one())

    async def delete_for_investment(self, investment_id: UUID) -> int:
        """Delete **every** FI-characteristics row for an investment.

        The importer clears the prior generation before re-inserting so
        a shrinking time series leaves no orphaned rows behind
        (replace-by-investment idempotency, ADR-0043 §3 / ADR-0081 §D).

        Args:
            investment_id: The investment whose rows to purge.

        Returns:
            The number of rows deleted across all statement days.
        """
        result = await self._session.execute(
            delete(InvestmentBondAnalytics).where(
                InvestmentBondAnalytics.investment_id == investment_id
            )
        )
        await self._session.flush()
        return result.rowcount or 0
