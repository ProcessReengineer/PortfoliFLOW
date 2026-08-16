# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""FxRateRepository — persistence for the FX-rate series (ADR-0099 §2).

Backs the ``fx_rates`` table introduced in migration b026. One row per
``(tenant_id, currency, as_of_date)`` — the
``uq_fx_rates_tenant_currency_date`` unique constraint enforces the natural
key. The method surface deliberately **mirrors**
:class:`core.repositories.instrument_price_repository.InstrumentPriceRepository`,
because an FX rate is the same shape of object: a tenant-scoped, dated,
positive value with an ADR-0092 producer marker.

* :meth:`upsert` — the **unconditional** write path (the Excel book of
  record and any manual CRUD surface). Overwrites everything on the natural
  key.
* :meth:`upsert_live` — the **conditional** live write path, value-identical
  in guard semantics to ADR-0092. A live rate refreshes only its own prior
  ``'live'`` rows and never mutates an ``'excel'``/``'manual'`` rate; a
  guarded no-op returns ``None``. No live FX producer exists yet — the ECB
  SDMX adapter is the named successor (ADR-0099 §5) — so the method lands
  dormant, exactly as ``position_transactions`` did in b024.
* :meth:`load_rates_frame` — the hand-off to the pure conversion service
  :class:`services.fx.conversion.FxConverter`. This is the only place the
  Decimal/pandas boundary is crossed.

Quoting convention (normative, ADR-0099 §2): ``rate_to_reference`` is the
price of one unit of ``currency`` in the reference currency. The identity
rate is never stored — ``rate(reference) = 1`` is an application-level
short-circuit, and the ``ck_fx_rates_currency_not_reference`` CHECK enforces
its absence.

Deviation from the Block-1 prompt, following the repository's own idiom: the
date window is expressed as ``from_date`` / ``to_date`` keyword arguments
(as in :class:`~core.repositories.benchmark_observation_repository.BenchmarkObservationRepository`),
not as a ``window`` tuple. No repository in this package takes a ``window``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID

import pandas as pd
from sqlalchemy import Date, delete, func, literal, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from core.models.fx_rate import FxRate
from core.repositories.base import BaseRepository

#: Column contract of the frame :meth:`FxRateRepository.load_rates_frame`
#: returns and :class:`services.fx.conversion.FxConverter` consumes.
RATES_FRAME_COLUMNS: tuple[str, ...] = (
    "as_of_date",
    "currency",
    "rate_to_reference",
    "reference_currency",
)


@dataclass(frozen=True)
class FxRateDTO:
    """Plain data-only view of an ``fx_rates`` row."""

    id: UUID
    tenant_id: UUID
    as_of_date: _date
    currency: str
    rate_to_reference: Decimal
    reference_currency: str
    source: str
    ingest_origin: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime


def _to_dto(model: FxRate) -> FxRateDTO:
    return FxRateDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        as_of_date=model.as_of_date,
        currency=model.currency,
        rate_to_reference=model.rate_to_reference,
        reference_currency=model.reference_currency,
        source=model.source,
        ingest_origin=model.ingest_origin,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _empty_rates_frame() -> pd.DataFrame:
    """Return a correctly typed, zero-row rates frame.

    An EUR-only tenant holds no ``fx_rates`` rows at all, so the empty
    frame is a first-class result, not an edge case: the conversion
    service must be constructible from it (identity short-circuit,
    ADR-0099 §3). Building it with explicit dtypes keeps
    :class:`~services.fx.conversion.FxConverter` free of dtype guards.
    """
    return pd.DataFrame(
        {
            "as_of_date": pd.Series([], dtype="datetime64[ns]"),
            "currency": pd.Series([], dtype="object"),
            "rate_to_reference": pd.Series([], dtype="object"),
            "reference_currency": pd.Series([], dtype="object"),
        }
    )


class FxRateRepository(BaseRepository):
    """Read and write FX-rate rows in the active tenant context."""

    async def get_by_id(self, rate_id: UUID) -> FxRateDTO | None:
        """Return the FX-rate row with the given id, or ``None`` if absent.

        Args:
            rate_id: The rate row to look up.

        Returns:
            The matching :class:`FxRateDTO`, or ``None`` if no rate row
            with this id exists in the active tenant context.
        """
        result = await self._session.execute(select(FxRate).where(FxRate.id == rate_id))
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_by_currency(
        self,
        currency: str,
        from_date: _date | None = None,
        to_date: _date | None = None,
    ) -> list[FxRateDTO]:
        """Return every rate row for one currency, ascending by date.

        Args:
            currency: The priced currency (ISO 4217).
            from_date: Inclusive lower bound; ``None`` means unbounded.
            to_date: Inclusive upper bound; ``None`` means unbounded.

        Returns:
            The matching rate rows sorted by ``as_of_date`` ascending.
            Empty list for an uncovered currency.

        Note:
            This is the plain windowed read. It does **not** pull the
            carry-forward anchor row that precedes ``from_date`` — that is
            :meth:`load_rates_frame`'s job, and the distinction matters.
        """
        stmt = select(FxRate).where(FxRate.currency == currency)
        if from_date is not None:
            stmt = stmt.where(FxRate.as_of_date >= from_date)
        if to_date is not None:
            stmt = stmt.where(FxRate.as_of_date <= to_date)
        stmt = stmt.order_by(FxRate.as_of_date.asc())
        result = await self._session.execute(stmt)
        return [_to_dto(model) for model in result.scalars().all()]

    async def upsert(
        self,
        currency: str,
        as_of_date: _date,
        rate_to_reference: Decimal,
        reference_currency: str,
        source: str,
        created_by: UUID,
        *,
        ingest_origin: str = "excel",
    ) -> FxRateDTO:
        """Insert or update a rate row by its natural key (unconditional).

        Conflicts on ``(tenant_id, currency, as_of_date)`` cause an UPDATE
        of ``rate_to_reference``, ``reference_currency``, ``source``,
        ``ingest_origin`` and ``updated_at``. ``created_by`` and
        ``created_at`` are preserved on update (the row's original author
        stays attributable in the audit log).

        This is the **unconditional** write path used by the Excel importer
        (book of record) and any manual CRUD surface. A live producer must
        use :meth:`upsert_live`, which never overwrites an
        ``'excel'``/``'manual'`` row (ADR-0092).

        Args:
            currency: The priced currency (ISO 4217). Must differ from
                ``reference_currency`` — the identity rate is never stored
                (``ck_fx_rates_currency_not_reference``).
            as_of_date: The rate date.
            rate_to_reference: Price of one unit of ``currency`` in
                ``reference_currency``. Strictly positive (CHECK-enforced).
            reference_currency: The base the rate is quoted against; stored
                per row so every rate is self-describing for audit.
            source: Free-form provenance label — ``'excel'``, ``'ecb'``,
                ``'yahoo'``. Mandatory (``NOT NULL``), unlike
                ``instrument_prices.source``.
            created_by: UUID attributable for the write. Used only on
                INSERT; preserved on UPDATE.
            ingest_origin: The producer writing the row — ``'excel'``
                (default, book of record) or ``'manual'`` (a CRUD edit).

        Returns:
            The created or updated :class:`FxRateDTO`.
        """
        active_tenant = await self._active_tenant_id()

        stmt = (
            pg_insert(FxRate)
            .values(
                tenant_id=active_tenant,
                currency=currency,
                as_of_date=as_of_date,
                rate_to_reference=rate_to_reference,
                reference_currency=reference_currency,
                source=source,
                ingest_origin=ingest_origin,
                created_by=created_by,
            )
            .on_conflict_do_update(
                constraint="uq_fx_rates_tenant_currency_date",
                set_={
                    "rate_to_reference": rate_to_reference,
                    "reference_currency": reference_currency,
                    "source": source,
                    "ingest_origin": ingest_origin,
                    "updated_at": text("NOW()"),
                },
            )
            .returning(FxRate.id)
        )
        result = await self._session.execute(stmt)
        rate_id: UUID = result.scalar_one()
        await self._session.flush()

        refreshed = await self._session.execute(select(FxRate).where(FxRate.id == rate_id))
        return _to_dto(refreshed.scalar_one())

    async def upsert_live(
        self,
        currency: str,
        as_of_date: _date,
        rate_to_reference: Decimal,
        reference_currency: str,
        source: str,
        created_by: UUID,
    ) -> FxRateDTO | None:
        """Conditional upsert for the **live** producer (ADR-0092 guard).

        The Excel-precedence invariant as a single statement:
        ``INSERT ... ON CONFLICT (tenant_id, currency, as_of_date) DO UPDATE
        ... WHERE the existing row's ingest_origin = 'live'``. Consequently:

        - No row exists → **INSERT** as ``ingest_origin = 'live'``.
        - A prior ``'live'`` row exists → **UPDATE in place** (the live
          producer refreshes its own rate).
        - An ``'excel'``/``'manual'`` row exists → the ``WHERE`` fails, the
          ``DO UPDATE`` fires on zero rows, ``updated_at`` is **not**
          bumped, the row is left **byte-identical**, and the method returns
          ``None`` (a recorded no-op, never an error).

        A live write can therefore never corrupt book-of-record (Excel) or
        operator-edited (manual) rate data. This mirrors
        :meth:`core.repositories.instrument_price_repository.InstrumentPriceRepository.upsert_live`
        exactly. No live FX producer exists yet (ADR-0099 §5); the method is
        the seam an ECB SDMX adapter lands on.

        Args:
            currency: The priced currency (ISO 4217).
            as_of_date: The rate date.
            rate_to_reference: Price of one unit of ``currency`` in
                ``reference_currency``. Strictly positive (CHECK-enforced).
            reference_currency: The base the rate is quoted against.
            source: Free-text provenance — the provider name.
            created_by: UUID of the acting user (the system actor arrives
                with the tick slice).

        Returns:
            The inserted / updated :class:`FxRateDTO`, or ``None`` when an
            ``'excel'``/``'manual'`` row was left untouched.
        """
        active_tenant = await self._active_tenant_id()

        stmt = (
            pg_insert(FxRate)
            .values(
                tenant_id=active_tenant,
                currency=currency,
                as_of_date=as_of_date,
                rate_to_reference=rate_to_reference,
                reference_currency=reference_currency,
                source=source,
                ingest_origin="live",
                created_by=created_by,
            )
            .on_conflict_do_update(
                constraint="uq_fx_rates_tenant_currency_date",
                set_={
                    "rate_to_reference": rate_to_reference,
                    "reference_currency": reference_currency,
                    "source": source,
                    "ingest_origin": "live",
                    "updated_at": text("NOW()"),
                },
                where=FxRate.ingest_origin == "live",
            )
            .returning(FxRate.id)
        )
        result = await self._session.execute(stmt)
        rate_id: UUID | None = result.scalar_one_or_none()
        await self._session.flush()
        if rate_id is None:
            # Conflict with an 'excel'/'manual' row: the WHERE guard skipped
            # the UPDATE. The book-of-record row is untouched.
            return None

        refreshed = await self._session.execute(select(FxRate).where(FxRate.id == rate_id))
        return _to_dto(refreshed.scalar_one())

    async def load_rates_frame(
        self,
        currencies: list[str],
        from_date: _date | None = None,
        to_date: _date | None = None,
    ) -> pd.DataFrame:
        """Load a tidy rates frame for the pure conversion service.

        This is the hand-off :class:`services.fx.conversion.FxConverter`
        consumes: the service is pure (ADR-0013) and never touches a
        session, so the repository materialises the whole rate set it needs
        in one query.

        **The window's lower bound is soft, and that is load-bearing.** FX
        series have gaps (weekends, holidays) and conversion carries the
        latest rate at or before each date forward (ADR-0099 §3). A frame
        clipped hard at ``from_date`` would leave the first days of the
        window without an anchor and raise
        :class:`~core.exceptions.MissingFxRateError` on data that exists.
        So for each requested currency this method also pulls the single
        latest row **at or before** ``from_date`` — the carry-forward
        anchor — in addition to every row inside the window. The upper bound
        ``to_date`` is hard: no rate after it is ever needed.

        Args:
            currencies: The priced currencies to load. The reference
                currency need not (and cannot) appear — its rate is the
                identity short-circuit. An empty list returns an empty
                frame without querying.
            from_date: Inclusive lower bound of the window, softened by the
                anchor row described above; ``None`` means unbounded.
            to_date: Inclusive upper bound; ``None`` means unbounded.

        Returns:
            A tidy frame with columns :data:`RATES_FRAME_COLUMNS` —
            ``as_of_date`` (``datetime64[ns]``), ``currency`` (``str``),
            ``rate_to_reference`` (:class:`~decimal.Decimal`, so DB truth
            survives the hand-off), ``reference_currency`` (``str``) —
            ordered by ``(currency, as_of_date)``. Zero rows when the
            tenant holds no matching rates; the column contract and dtypes
            hold either way.
        """
        if not currencies:
            return _empty_rates_frame()

        stmt = select(
            FxRate.as_of_date,
            FxRate.currency,
            FxRate.rate_to_reference,
            FxRate.reference_currency,
        ).where(FxRate.currency.in_(currencies))

        if to_date is not None:
            stmt = stmt.where(FxRate.as_of_date <= to_date)
        if from_date is not None:
            # Per-currency carry-forward anchor: the latest date at or
            # before `from_date`. When a currency has no earlier row the
            # COALESCE degrades the bound to `from_date` itself, i.e. the
            # plain windowed read.
            anchor_row = aliased(FxRate)
            anchor = (
                select(func.max(anchor_row.as_of_date))
                .where(anchor_row.currency == FxRate.currency)
                .where(anchor_row.as_of_date <= from_date)
                .correlate(FxRate)
                .scalar_subquery()
            )
            stmt = stmt.where(FxRate.as_of_date >= func.coalesce(anchor, literal(from_date, Date)))

        stmt = stmt.order_by(FxRate.currency.asc(), FxRate.as_of_date.asc())
        result = await self._session.execute(stmt)
        rows = result.all()
        if not rows:
            return _empty_rates_frame()

        frame = pd.DataFrame(rows, columns=list(RATES_FRAME_COLUMNS))
        frame["as_of_date"] = pd.to_datetime(frame["as_of_date"])
        return frame

    async def delete(self, rate_id: UUID) -> bool:
        """Hard-delete a single rate row.

        Args:
            rate_id: The rate row to delete.

        Returns:
            ``True`` if a row was deleted, ``False`` if no rate with this id
            existed in the active tenant context.
        """
        result = await self._session.execute(delete(FxRate).where(FxRate.id == rate_id))
        await self._session.flush()
        return (result.rowcount or 0) > 0

    async def delete_by_currency(self, currency: str) -> int:
        """Delete every rate row for one currency.

        The replace-by-currency import workflow uses this to clear a series
        before re-inserting it.

        Args:
            currency: The priced currency whose rates to delete.

        Returns:
            The number of rows that were deleted.
        """
        result = await self._session.execute(delete(FxRate).where(FxRate.currency == currency))
        await self._session.flush()
        return result.rowcount or 0

    async def _active_tenant_id(self) -> UUID:
        """Resolve the session's tenant from the ``app.tenant_id`` GUC."""
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        return tenant_row.scalar_one()
