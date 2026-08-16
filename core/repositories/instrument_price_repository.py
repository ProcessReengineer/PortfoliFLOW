# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InstrumentPriceRepository — persistence for the per-unit price series.

Backs the ``instrument_prices`` table introduced in migration b024 (per
ADR-0097 §3). One row per ``(investment_id, as_of_date)`` — the
``uq_instrument_prices_investment_date`` unique constraint enforces the
natural key. The method surface deliberately **mirrors
:class:`core.repositories.investment_nav_repository.InvestmentNavRepository`**,
because ADR-0097 §3 keys the price series exactly like ``investment_navs``:

* :meth:`upsert` — the **unconditional** write path (book of record and
  manual CRUD; strands S4/S5). Overwrites everything on the natural key.
* :meth:`upsert_live` — the **conditional** live write path, value-identical
  in guard semantics to ADR-0092 (strand S3). A live price refreshes only
  its own prior ``'live'`` rows and never mutates an ``'excel'``/``'manual'``
  price; a guarded no-op returns ``None``.

Strand S2 materialisation reads the series via :meth:`list_by_investment`;
the Watch Desk's ``price`` producer reads many series at once via
:meth:`list_by_investments`, whose window carries the same soft lower
bound the FX repository's does (ADR-0116 §4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Date, delete, func, literal, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from core.models.instrument_price import InstrumentPrice
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class InstrumentPriceDTO:
    """Plain data-only view of an ``instrument_prices`` row."""

    id: UUID
    tenant_id: UUID
    investment_id: UUID
    as_of_date: _date
    price: Decimal
    currency: str
    source: str | None
    ingest_origin: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime


def _to_dto(model: InstrumentPrice) -> InstrumentPriceDTO:
    return InstrumentPriceDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        investment_id=model.investment_id,
        as_of_date=model.as_of_date,
        price=model.price,
        currency=model.currency,
        source=model.source,
        ingest_origin=model.ingest_origin,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class InstrumentPriceRepository(BaseRepository):
    """Read and write instrument price rows in the active tenant context."""

    async def get_by_id(self, price_id: UUID) -> InstrumentPriceDTO | None:
        """Return the price row with the given id, or ``None`` if absent.

        Args:
            price_id: The price row to look up.

        Returns:
            The matching :class:`InstrumentPriceDTO`, or ``None`` if no
            price row with this id exists in the active tenant context.
        """
        result = await self._session.execute(
            select(InstrumentPrice).where(InstrumentPrice.id == price_id)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_by_investment(self, investment_id: UUID) -> list[InstrumentPriceDTO]:
        """Return every price row for an investment, ascending by date.

        The price series strand S2 materialisation samples on statement
        days.

        Args:
            investment_id: The investment whose price history to load.

        Returns:
            All price rows sorted by ``as_of_date`` ascending. Empty list
            for an unknown investment.
        """
        result = await self._session.execute(
            select(InstrumentPrice)
            .where(InstrumentPrice.investment_id == investment_id)
            .order_by(InstrumentPrice.as_of_date.asc())
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_by_investments(
        self,
        investment_ids: Sequence[UUID],
        *,
        from_date: _date | None = None,
        to_date: _date | None = None,
    ) -> list[InstrumentPriceDTO]:
        """Return the windowed price rows for many investments, in one query.

        The batched sibling of :meth:`list_by_investment`, for callers that
        must not issue one query per instrument — the Irene beat evaluates
        every ``price`` watchpoint on one tick (ADR-0116 §4), and a query
        per watchpoint would make the beat's cost linear in how much a
        tenant chose to watch.

        **The window's lower bound is soft, and that is load-bearing.**
        Prices carry forward: the value applying on a date is the latest at
        or before it, so a Sunday, a holiday, or a monthly series is served
        from an earlier row. A frame clipped hard at ``from_date`` would
        leave a sparse series with no value at the start of the window and
        report "no data" over data that exists. So for each requested
        investment this method also pulls the single latest row **at or
        before** ``from_date`` — the carry-forward anchor — in addition to
        every row inside the window. Mirrors
        :meth:`core.repositories.fx_rate_repository.FxRateRepository.load_rates_frame`,
        whose window has the same property for the same reason. The upper
        bound ``to_date`` is hard: no price after it is ever needed.

        Args:
            investment_ids: The investments whose price history to load. An
                empty sequence returns an empty list without querying.
            from_date: Inclusive lower bound of the window, softened by the
                anchor row described above; ``None`` means unbounded.
            to_date: Inclusive upper bound; ``None`` means unbounded.

        Returns:
            The matching price rows, ordered by ``(investment_id,
            as_of_date)`` — a stable grouping order for the caller.
        """
        if not investment_ids:
            return []

        stmt = select(InstrumentPrice).where(
            InstrumentPrice.investment_id.in_(list(investment_ids))
        )
        if to_date is not None:
            stmt = stmt.where(InstrumentPrice.as_of_date <= to_date)
        if from_date is not None:
            # Per-investment carry-forward anchor: the latest date at or
            # before `from_date`. When an investment has no earlier row the
            # COALESCE degrades the bound to `from_date` itself, i.e. the
            # plain windowed read.
            anchor_row = aliased(InstrumentPrice)
            anchor = (
                select(func.max(anchor_row.as_of_date))
                .where(anchor_row.investment_id == InstrumentPrice.investment_id)
                .where(anchor_row.as_of_date <= from_date)
                .correlate(InstrumentPrice)
                .scalar_subquery()
            )
            stmt = stmt.where(
                InstrumentPrice.as_of_date >= func.coalesce(anchor, literal(from_date, Date))
            )

        stmt = stmt.order_by(InstrumentPrice.investment_id.asc(), InstrumentPrice.as_of_date.asc())
        result = await self._session.execute(stmt)
        return [_to_dto(model) for model in result.scalars().all()]

    async def upsert(
        self,
        investment_id: UUID,
        as_of_date: _date,
        price: Decimal,
        currency: str,
        source: str | None,
        created_by: UUID,
        *,
        ingest_origin: str = "excel",
    ) -> InstrumentPriceDTO:
        """Insert or update a price row by its natural key (unconditional).

        Conflicts on ``(investment_id, as_of_date)`` cause an UPDATE of
        ``price``, ``currency``, ``source``, ``ingest_origin`` and
        ``updated_at``. ``created_by`` and ``created_at`` are preserved on
        update (the row's original author stays attributable in the audit
        log).

        This is the **unconditional** write path used by the Excel importer
        (book of record) and the manual CRUD surface. The live producer
        must use :meth:`upsert_live`, which never overwrites an
        ``'excel'``/``'manual'`` row (ADR-0092).

        Args:
            investment_id: The investment this price belongs to.
            as_of_date: Statement-day date.
            price: Positive per-unit price (``> 0`` CHECK-enforced).
            currency: ISO 4217 currency code; must equal the investment's
                currency (validated in the service layer, not here).
            source: Optional free-form provenance label.
            created_by: UUID attributable for the write. Used only on
                INSERT; preserved on UPDATE.
            ingest_origin: The producer writing the row — ``'excel'``
                (default, book of record) or ``'manual'`` (a CRUD edit).

        Returns:
            The created or updated :class:`InstrumentPriceDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        stmt = (
            pg_insert(InstrumentPrice)
            .values(
                tenant_id=active_tenant,
                investment_id=investment_id,
                as_of_date=as_of_date,
                price=price,
                currency=currency,
                source=source,
                ingest_origin=ingest_origin,
                created_by=created_by,
            )
            .on_conflict_do_update(
                constraint="uq_instrument_prices_investment_date",
                set_={
                    "price": price,
                    "currency": currency,
                    "source": source,
                    "ingest_origin": ingest_origin,
                    "updated_at": text("NOW()"),
                },
            )
            .returning(InstrumentPrice.id)
        )
        result = await self._session.execute(stmt)
        price_id: UUID = result.scalar_one()
        await self._session.flush()

        refreshed = await self._session.execute(
            select(InstrumentPrice).where(InstrumentPrice.id == price_id)
        )
        return _to_dto(refreshed.scalar_one())

    async def upsert_live(
        self,
        investment_id: UUID,
        as_of_date: _date,
        price: Decimal,
        currency: str,
        source: str | None,
        created_by: UUID,
    ) -> InstrumentPriceDTO | None:
        """Conditional upsert for the **live** producer (ADR-0092 guard).

        The Excel-precedence invariant as a single statement:
        ``INSERT ... ON CONFLICT (investment_id, as_of_date) DO UPDATE ...
        WHERE the existing row's ingest_origin = 'live'``. Consequently:

        - No row exists → **INSERT** as ``ingest_origin = 'live'``.
        - A prior ``'live'`` row exists → **UPDATE in place** (the live
          producer refreshes its own price).
        - An ``'excel'``/``'manual'`` row exists → the ``WHERE`` fails, the
          ``DO UPDATE`` fires on zero rows, ``updated_at`` is **not**
          bumped, the row is left **byte-identical**, and the method
          returns ``None`` (a recorded no-op, never an error).

        A live write can therefore never corrupt book-of-record (Excel) or
        operator-edited (manual) price data. This mirrors
        :meth:`core.repositories.investment_nav_repository.InvestmentNavRepository.upsert_live`
        exactly (strand S3 is the named consumer).

        Args:
            investment_id: The investment this price belongs to.
            as_of_date: Statement-day date.
            price: Positive per-unit price (``> 0`` CHECK-enforced).
            currency: ISO 4217 currency code.
            source: Free-text provenance — the provider name.
            created_by: UUID of the acting user (the system actor arrives
                with the tick slice).

        Returns:
            The inserted / updated :class:`InstrumentPriceDTO`, or ``None``
            when an ``'excel'``/``'manual'`` row was left untouched.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        stmt = (
            pg_insert(InstrumentPrice)
            .values(
                tenant_id=active_tenant,
                investment_id=investment_id,
                as_of_date=as_of_date,
                price=price,
                currency=currency,
                source=source,
                ingest_origin="live",
                created_by=created_by,
            )
            .on_conflict_do_update(
                constraint="uq_instrument_prices_investment_date",
                set_={
                    "price": price,
                    "currency": currency,
                    "source": source,
                    "ingest_origin": "live",
                    "updated_at": text("NOW()"),
                },
                where=InstrumentPrice.ingest_origin == "live",
            )
            .returning(InstrumentPrice.id)
        )
        result = await self._session.execute(stmt)
        price_id: UUID | None = result.scalar_one_or_none()
        await self._session.flush()
        if price_id is None:
            # Conflict with an 'excel'/'manual' row: the WHERE guard skipped
            # the UPDATE. The book-of-record row is untouched.
            return None

        refreshed = await self._session.execute(
            select(InstrumentPrice).where(InstrumentPrice.id == price_id)
        )
        return _to_dto(refreshed.scalar_one())

    async def delete_by_investment(self, investment_id: UUID) -> int:
        """Delete every price row for an investment.

        The Excel-import replace-by-investment workflow (strand S4) uses
        this to clear a price series before re-inserting.

        Args:
            investment_id: The investment whose prices to delete.

        Returns:
            The number of rows that were deleted.
        """
        result = await self._session.execute(
            delete(InstrumentPrice).where(InstrumentPrice.investment_id == investment_id)
        )
        await self._session.flush()
        return result.rowcount or 0

    async def delete(self, price_id: UUID) -> bool:
        """Hard-delete a single price row.

        Args:
            price_id: The price row to delete.

        Returns:
            ``True`` if a row was deleted, ``False`` if no price with this
            id existed in the active tenant context.
        """
        result = await self._session.execute(
            delete(InstrumentPrice).where(InstrumentPrice.id == price_id)
        )
        await self._session.flush()
        return (result.rowcount or 0) > 0
