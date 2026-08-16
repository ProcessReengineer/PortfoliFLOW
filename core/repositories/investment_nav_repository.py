# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentNavRepository — persistence for date-stamped NAV rows.

Backs the ``investment_navs`` table introduced in migration b006
(per ADR-0043 §1). One row per ``(investment_id, as_of_date,
nav_kind)`` triple — the ``uq_investment_navs_investment_date_kind``
unique constraint enforces the natural key. Plan and actual NAV
series coexist as two distinct rows on the same statement day.

The :meth:`upsert` workflow is the principal write path used by both
the web CRUD surface (sub-stream 4b) for individual NAV corrections
and the Excel-import re-loader (sub-stream 4c) for bulk replacement
inside the per-investment replace-by-investment loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.models.investment_nav import InvestmentNav
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class InvestmentNavDTO:
    """Plain data-only view of an ``investment_navs`` row."""

    id: UUID
    tenant_id: UUID
    investment_id: UUID
    as_of_date: _date
    nav_value: Decimal
    currency: str
    nav_kind: str
    source: str | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    # b021 / ADR-0092: the producer that wrote the row
    # ('excel' | 'live' | 'manual' | 'system'). Defaulted so existing direct
    # constructions of the DTO stay valid.
    ingest_origin: str = "excel"
    # ADR-0079 / ADR-0098 §1: how the number was formed — 'reported' (carried
    # from a statement) or 'computed' (holdings × price). Orthogonal to
    # ``ingest_origin``, which names the writer channel; the two are never
    # read as one another. NULL means 'reported' (b016 landed the column
    # nullable with no backfill). Defaulted for the same reason as above.
    basis: str | None = None


def _to_dto(model: InvestmentNav) -> InvestmentNavDTO:
    return InvestmentNavDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        investment_id=model.investment_id,
        as_of_date=model.as_of_date,
        nav_value=model.nav_value,
        currency=model.currency,
        nav_kind=model.nav_kind,
        source=model.source,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
        ingest_origin=model.ingest_origin,
        basis=model.basis,
    )


class InvestmentNavRepository(BaseRepository):
    """Read and write investment NAV rows in the active tenant context."""

    async def get_by_id(self, nav_id: UUID) -> InvestmentNavDTO | None:
        """Return the NAV row with the given id, or ``None`` if absent.

        The natural key for a NAV row is
        ``(investment_id, as_of_date, nav_kind)`` and bulk paths use
        :meth:`upsert` against that key. ``get_by_id`` exists to
        support the web CRUD surface (sub-stream 4b) where the route
        layer references rows by surrogate id.

        Args:
            nav_id: The NAV row to look up.

        Returns:
            The matching :class:`InvestmentNavDTO`, or ``None`` if no
            NAV row with this id exists in the active tenant context.
        """
        result = await self._session.execute(
            select(InvestmentNav).where(InvestmentNav.id == nav_id)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_by_investment(self, investment_id: UUID) -> list[InvestmentNavDTO]:
        """Return every NAV row for an investment.

        Args:
            investment_id: The investment whose NAV history to load.

        Returns:
            All NAV rows (plan and actual interleaved) sorted by
            ``as_of_date`` ascending. Empty list for an unknown
            investment.
        """
        result = await self._session.execute(
            select(InvestmentNav)
            .where(InvestmentNav.investment_id == investment_id)
            .order_by(InvestmentNav.as_of_date.asc())
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_by_investment_and_kind(
        self, investment_id: UUID, nav_kind: str
    ) -> list[InvestmentNavDTO]:
        """Return NAV rows of a single ``nav_kind`` for an investment.

        Args:
            investment_id: The investment whose NAV history to load.
            nav_kind: ``"plan"`` or ``"actual"``.

        Returns:
            Matching NAV rows sorted by ``as_of_date`` ascending.
        """
        result = await self._session.execute(
            select(InvestmentNav)
            .where(
                InvestmentNav.investment_id == investment_id,
                InvestmentNav.nav_kind == nav_kind,
            )
            .order_by(InvestmentNav.as_of_date.asc())
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_by_investments(
        self, investment_ids: list[UUID]
    ) -> dict[UUID, list[InvestmentNavDTO]]:
        """Return every NAV row for a list of investments in one query.

        Batch counterpart to :meth:`list_by_investment`. The single
        SQL ``WHERE investment_id = ANY(:ids)`` replaces N
        per-investment SELECTs and is the recommended call site for
        universe-wide services (Portfolio Review, Statistics) per
        Phase-5 follow-up P6-H.

        Args:
            investment_ids: The investments whose NAV history to load.
                Empty list is valid and returns an empty dict.

        Returns:
            A dict keyed by ``investment_id``; every id from
            ``investment_ids`` is present in the result even when
            the investment has no NAV rows (its value is an empty
            list). Within each list, rows are sorted by
            ``as_of_date`` ascending — matching the singular
            :meth:`list_by_investment` contract.
        """
        if not investment_ids:
            return {}
        result = await self._session.execute(
            select(InvestmentNav)
            .where(InvestmentNav.investment_id.in_(investment_ids))
            .order_by(
                InvestmentNav.investment_id.asc(),
                InvestmentNav.as_of_date.asc(),
            )
        )
        grouped: dict[UUID, list[InvestmentNavDTO]] = {inv_id: [] for inv_id in investment_ids}
        for model in result.scalars().all():
            grouped[model.investment_id].append(_to_dto(model))
        return grouped

    async def list_by_investments_and_kind(
        self, investment_ids: list[UUID], nav_kind: str
    ) -> dict[UUID, list[InvestmentNavDTO]]:
        """Return NAV rows of a single ``nav_kind`` for a list of investments.

        Args:
            investment_ids: The investments whose NAV history to load.
                Empty list is valid and returns an empty dict.
            nav_kind: ``"plan"`` or ``"actual"``.

        Returns:
            A dict keyed by ``investment_id``; every id from
            ``investment_ids`` is present in the result with rows
            sorted by ``as_of_date`` ascending. Empty list value for
            ids with no NAV rows of this kind.
        """
        if not investment_ids:
            return {}
        result = await self._session.execute(
            select(InvestmentNav)
            .where(
                InvestmentNav.investment_id.in_(investment_ids),
                InvestmentNav.nav_kind == nav_kind,
            )
            .order_by(
                InvestmentNav.investment_id.asc(),
                InvestmentNav.as_of_date.asc(),
            )
        )
        grouped: dict[UUID, list[InvestmentNavDTO]] = {inv_id: [] for inv_id in investment_ids}
        for model in result.scalars().all():
            grouped[model.investment_id].append(_to_dto(model))
        return grouped

    async def get_latest_actual(self, investment_id: UUID) -> InvestmentNavDTO | None:
        """Return the most recent ``actual`` NAV for an investment.

        The descending index on ``(investment_id, as_of_date DESC)``
        from b006 makes this a single index seek.

        Args:
            investment_id: The investment to query.

        Returns:
            The most recent ``actual`` NAV row, or ``None`` if the
            investment has no actual NAVs yet.
        """
        result = await self._session.execute(
            select(InvestmentNav)
            .where(
                InvestmentNav.investment_id == investment_id,
                InvestmentNav.nav_kind == "actual",
            )
            .order_by(InvestmentNav.as_of_date.desc())
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def latest_actual_by_investment(
        self, investment_ids: list[UUID]
    ) -> dict[UUID, InvestmentNavDTO]:
        """Return each investment's most recent ``actual`` NAV, in one pass.

        The batch counterpart to :meth:`get_latest_actual`: one
        ``DISTINCT ON`` seek over the b006 descending index instead of N
        per-investment queries, and it returns the whole row rather than
        just its date, so a caller that needs the level as well as the
        recency does not go back for it.

        Written for the Watch Desk's ``freshness`` family (ADR-0116 §4),
        whose beat enumerates one subject per active investment and must
        not scale its query count with the size of the book. ``'plan'``
        rows are excluded: a projection is not an observation, and dating
        staleness against one would make an unmaintained plan look like a
        fresh book.

        Args:
            investment_ids: The investments to look up. An empty list is
                valid and returns an empty dict.

        Returns:
            A dict keyed by ``investment_id``, carrying only the
            investments that have at least one actual NAV row. A missing
            key means "no actual NAV at all" — deliberately distinct from
            a present key with an old date.
        """
        if not investment_ids:
            return {}
        result = await self._session.execute(
            select(InvestmentNav)
            .where(
                InvestmentNav.investment_id.in_(investment_ids),
                InvestmentNav.nav_kind == "actual",
            )
            .distinct(InvestmentNav.investment_id)
            .order_by(
                InvestmentNav.investment_id.asc(),
                InvestmentNav.as_of_date.desc(),
            )
        )
        return {model.investment_id: _to_dto(model) for model in result.scalars().all()}

    async def latest_actual_as_of_date(self, investment_ids: list[UUID]) -> _date | None:
        """Return the latest ``actual`` NAV date across several investments.

        The universe-level counterpart to :meth:`get_latest_actual`: one
        ``max(as_of_date)`` aggregate instead of N per-investment seeks.
        Feeds the ADR-0113 §1 "universe as-of" — the shared right-hand
        x-axis end of the Front-Office chart tiles. ``'plan'`` rows are
        excluded: the frontier is what has actually been observed.

        Args:
            investment_ids: The investments to aggregate over. An empty
                list is valid and returns ``None``.

        Returns:
            The most recent ``actual`` NAV date over ``investment_ids``,
            or ``None`` when the list is empty or none of those
            investments carries an actual NAV row.
        """
        if not investment_ids:
            return None
        result = await self._session.execute(
            select(func.max(InvestmentNav.as_of_date)).where(
                InvestmentNav.investment_id.in_(investment_ids),
                InvestmentNav.nav_kind == "actual",
            )
        )
        return result.scalar_one_or_none()

    async def upsert(
        self,
        investment_id: UUID,
        as_of_date: _date,
        nav_kind: str,
        nav_value: Decimal,
        currency: str,
        source: str | None,
        created_by: UUID,
        *,
        ingest_origin: str = "excel",
    ) -> InvestmentNavDTO:
        """Insert or update a NAV row by its natural key (unconditional).

        Conflicts on ``(investment_id, as_of_date, nav_kind)`` cause
        an UPDATE of ``nav_value``, ``currency``, ``source``,
        ``ingest_origin``, and ``updated_at``. ``created_by`` and
        ``created_at`` are preserved on update (the row's original
        author stays attributable in the audit log).

        This is the **unconditional** write path used by the Excel
        importer (book of record — overwrites everything on its keys)
        and the manual CRUD surface. The live producer must use
        :meth:`upsert_live` instead, which never overwrites an
        ``'excel'`` or ``'manual'`` row (ADR-0092).

        Args:
            investment_id: The investment this NAV belongs to.
            as_of_date: Statement-day date.
            nav_kind: ``"plan"`` or ``"actual"``.
            nav_value: Numeric NAV value.
            currency: ISO 4217 currency code.
            source: Optional free-form provenance label (e.g. the
                Excel sheet name).
            created_by: UUID of the user attributable for the write.
                Used only on INSERT; preserved on UPDATE.
            ingest_origin: The producer writing the row — ``'excel'``
                (default, book of record) or ``'manual'`` (a CRUD
                edit). Stated on both INSERT and UPDATE so a re-import
                reclaims the origin (ADR-0092).

        Returns:
            The created or updated :class:`InvestmentNavDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        stmt = (
            pg_insert(InvestmentNav)
            .values(
                tenant_id=active_tenant,
                investment_id=investment_id,
                as_of_date=as_of_date,
                nav_value=nav_value,
                currency=currency,
                nav_kind=nav_kind,
                source=source,
                ingest_origin=ingest_origin,
                created_by=created_by,
            )
            .on_conflict_do_update(
                constraint="uq_investment_navs_investment_date_kind",
                set_={
                    "nav_value": nav_value,
                    "currency": currency,
                    "source": source,
                    "ingest_origin": ingest_origin,
                    "updated_at": text("NOW()"),
                },
            )
            .returning(InvestmentNav.id)
        )
        result = await self._session.execute(stmt)
        nav_id: UUID = result.scalar_one()
        await self._session.flush()

        refreshed = await self._session.execute(
            select(InvestmentNav).where(InvestmentNav.id == nav_id)
        )
        return _to_dto(refreshed.scalar_one())

    async def upsert_live(
        self,
        investment_id: UUID,
        as_of_date: _date,
        nav_kind: str,
        nav_value: Decimal,
        currency: str,
        source: str | None,
        basis: str | None,
        created_by: UUID,
    ) -> InvestmentNavDTO | None:
        """Conditional upsert for the **live** producer (ADR-0092 guard).

        The Excel-precedence invariant as a single statement:
        ``INSERT ... ON CONFLICT (investment_id, as_of_date, nav_kind)
        DO UPDATE ... WHERE the existing row's ingest_origin = 'live'``.
        Consequently:

        - No row exists → **INSERT** as ``ingest_origin = 'live'``.
        - A prior ``'live'`` row exists → **UPDATE in place** (the live
          producer refreshes its own value).
        - An ``'excel'`` or ``'manual'`` row exists → the ``WHERE`` fails,
          the ``DO UPDATE`` fires on zero rows, ``updated_at`` is **not**
          bumped, the row is left **byte-identical**, and the method
          returns ``None`` (a recorded no-op, never an error).

        A live write can therefore never corrupt book-of-record (Excel)
        or operator-edited (manual) data. Unlike :meth:`upsert`, ``basis``
        is written (``'reported'`` for a provider-reported price,
        ADR-0079).

        Args:
            investment_id: The investment this NAV belongs to.
            as_of_date: Statement-day date.
            nav_kind: ``"plan"`` or ``"actual"`` (live prices are
                ``"actual"``).
            nav_value: Numeric NAV value.
            currency: ISO 4217 currency code.
            source: Free-text provenance — the DTO's ``provider`` value.
            basis: ``'reported'`` | ``'computed'`` | ``None``.
            created_by: UUID of the acting user (system actor arrives
                with the tick slice, ADR-0093).

        Returns:
            The inserted / updated :class:`InvestmentNavDTO`, or ``None``
            when an ``'excel'`` / ``'manual'`` row was left untouched.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        stmt = (
            pg_insert(InvestmentNav)
            .values(
                tenant_id=active_tenant,
                investment_id=investment_id,
                as_of_date=as_of_date,
                nav_value=nav_value,
                currency=currency,
                nav_kind=nav_kind,
                source=source,
                basis=basis,
                ingest_origin="live",
                created_by=created_by,
            )
            .on_conflict_do_update(
                constraint="uq_investment_navs_investment_date_kind",
                set_={
                    "nav_value": nav_value,
                    "currency": currency,
                    "source": source,
                    "basis": basis,
                    "ingest_origin": "live",
                    "updated_at": text("NOW()"),
                },
                where=InvestmentNav.ingest_origin == "live",
            )
            .returning(InvestmentNav.id)
        )
        result = await self._session.execute(stmt)
        nav_id: UUID | None = result.scalar_one_or_none()
        await self._session.flush()
        if nav_id is None:
            # Conflict with an 'excel' / 'manual' row: the WHERE guard
            # skipped the UPDATE. The book-of-record row is untouched.
            return None

        refreshed = await self._session.execute(
            select(InvestmentNav).where(InvestmentNav.id == nav_id)
        )
        return _to_dto(refreshed.scalar_one())

    async def upsert_computed(
        self,
        investment_id: UUID,
        as_of_date: _date,
        nav_kind: str,
        nav_value: Decimal,
        currency: str,
        source: str | None,
        created_by: UUID,
    ) -> InvestmentNavDTO | None:
        """Conditional upsert for the **materialisation** producer (ADR-0098).

        The ``'system'`` sibling of :meth:`upsert_live`: it writes a
        computed-NAV row (``holdings × price``) and, on conflict, refreshes
        **only its own ``'system'`` row**. The guard is structurally
        identical to :meth:`upsert_live`, one origin over:
        ``INSERT ... ON CONFLICT (investment_id, as_of_date, nav_kind)
        DO UPDATE ... WHERE the existing row's ingest_origin = 'system'``.
        Consequently, with precedence ``'excel'`` > ``'manual'`` >
        ``'system'`` (ADR-0098 §1):

        - No row exists → **INSERT** as ``ingest_origin = 'system'``,
          ``basis = 'computed'``.
        - A prior ``'system'`` row exists → **UPDATE in place** (the
          materialisation refreshes its own computed value).
        - An ``'excel'`` / ``'manual'`` / ``'live'`` row exists → the
          ``WHERE`` fails, the ``DO UPDATE`` fires on zero rows,
          ``updated_at`` is **not** bumped, the row is left
          **byte-identical**, and the method returns ``None`` (a recorded
          no-op, never an error).

        The row is always written with ``basis = 'computed'`` (ADR-0079):
        the number was formed by holdings aggregation, orthogonally to the
        ``'system'`` writer channel. This method never mutates
        book-of-record (``'excel'``), operator-edited (``'manual'``) or
        provider-delivered (``'live'``) rows — the caller
        (:class:`services.investments.nav_materialisation.NavMaterialisationService`)
        classifies each date first and only reaches this method for dates it
        intends to insert or refresh, so ``None`` here is a defence-in-depth
        signal, not the primary skip path.

        Args:
            investment_id: The investment this NAV belongs to.
            as_of_date: Statement-day date.
            nav_kind: ``"actual"`` (plan rows are never materialised —
                ADR-0098 §2). Accepted as a parameter for symmetry with the
                sibling upserts; the service always passes ``"actual"``.
            nav_value: The computed NAV value (``holdings × price``).
            currency: ISO 4217 currency code — the investment's currency
                (equal to the price currency by ADR-0097 §5).
            source: Free-text provenance — ``'computed:units×price'``.
            created_by: UUID of the acting user; the market-data system
                actor when triggered by live ingest (ADR-0093, strand S3).

        Returns:
            The inserted / updated :class:`InvestmentNavDTO`, or ``None``
            when a non-``'system'`` row was left untouched.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        stmt = (
            pg_insert(InvestmentNav)
            .values(
                tenant_id=active_tenant,
                investment_id=investment_id,
                as_of_date=as_of_date,
                nav_value=nav_value,
                currency=currency,
                nav_kind=nav_kind,
                source=source,
                basis="computed",
                ingest_origin="system",
                created_by=created_by,
            )
            .on_conflict_do_update(
                constraint="uq_investment_navs_investment_date_kind",
                set_={
                    "nav_value": nav_value,
                    "currency": currency,
                    "source": source,
                    "basis": "computed",
                    "ingest_origin": "system",
                    "updated_at": text("NOW()"),
                },
                where=InvestmentNav.ingest_origin == "system",
            )
            .returning(InvestmentNav.id)
        )
        result = await self._session.execute(stmt)
        nav_id: UUID | None = result.scalar_one_or_none()
        await self._session.flush()
        if nav_id is None:
            # Conflict with a higher-precedence row ('excel'/'manual') or a
            # 'live' row: the WHERE guard skipped the UPDATE. Left untouched.
            return None

        refreshed = await self._session.execute(
            select(InvestmentNav).where(InvestmentNav.id == nav_id)
        )
        return _to_dto(refreshed.scalar_one())

    async def delete_system_navs(self, investment_id: UUID, as_of_dates: list[_date]) -> int:
        """Delete computed (``'system'``) ``actual`` NAV rows on given dates.

        The stranded-row cleanup for the materialisation service (ADR-0098
        §2): when a backdated transaction edit, a price deletion, or a
        holdings-to-zero sale shrinks the materialised set, the ``'system'``
        rows whose dates left the set are deleted here. The predicate is
        deliberately narrow — ``nav_kind = 'actual'`` **and**
        ``ingest_origin = 'system'`` **and** ``as_of_date`` in the supplied
        dates — so it can never touch an ``'excel'`` / ``'manual'`` /
        ``'live'`` row or a ``'plan'`` row, whatever the caller passes.

        Args:
            investment_id: The investment whose stranded computed rows to
                delete.
            as_of_dates: The exact statement days to delete computed rows
                on. Empty list is valid and deletes nothing.

        Returns:
            The number of rows deleted (only ``'system'`` ``actual`` rows
            are ever counted).
        """
        if not as_of_dates:
            return 0
        result = await self._session.execute(
            delete(InvestmentNav).where(
                InvestmentNav.investment_id == investment_id,
                InvestmentNav.nav_kind == "actual",
                InvestmentNav.ingest_origin == "system",
                InvestmentNav.as_of_date.in_(list(as_of_dates)),
            )
        )
        await self._session.flush()
        return result.rowcount or 0

    async def delete_system_plan_navs(
        self, investment_id: UUID, as_of_dates: list[_date], *, source: str
    ) -> int:
        """Delete computed (``'system'``) ``plan`` NAV rows on given dates.

        The stranded-row cleanup for the cash plan path (ADR-0103 §6): when a
        plan flow is removed or re-dated, or a new statement moves the anchor
        ``t₀`` past a projected date, the rows whose dates left the event set
        are deleted here.

        The ``plan`` sibling of :meth:`delete_system_navs`, which pins
        ``nav_kind='actual'`` and therefore cannot serve this path — the two
        producers write different kinds and must not be able to reach each
        other's rows even by mistake. This predicate is narrower still, by
        one column: ``nav_kind='plan'`` **and** ``ingest_origin='system'``
        **and** ``source`` **and** ``as_of_date`` in the supplied dates. The
        ``source`` term is what makes the write sets of the ADR-0103 §6
        projection and the future ADR-0104 overlay producers — both of which
        write ``'system'`` plan rows — provably disjoint.

        Args:
            investment_id: The investment whose stranded plan rows to delete.
            as_of_dates: The exact dates to delete projected rows on. Empty
                list is valid and deletes nothing.
            source: The producer's own ``source`` marker (for the cash plan
                path, ``'computed:cash-plan'``). A row of any other source is
                another writer's and is never touched.

        Returns:
            The number of rows deleted (only ``'system'`` ``plan`` rows of the
            given ``source`` are ever counted).
        """
        if not as_of_dates:
            return 0
        result = await self._session.execute(
            delete(InvestmentNav).where(
                InvestmentNav.investment_id == investment_id,
                InvestmentNav.nav_kind == "plan",
                InvestmentNav.ingest_origin == "system",
                InvestmentNav.source == source,
                InvestmentNav.as_of_date.in_(list(as_of_dates)),
            )
        )
        await self._session.flush()
        return result.rowcount or 0

    async def delete_live_navs(self, investment_id: UUID) -> int:
        """Delete every ``'live'``-origin NAV row of one investment.

        The valuation-mode flip's cleanup (ADR-0097 §6): the rows a live
        market-data ingest wrote into this investment's NAV series before it
        was unitised are **per-share prices in a position-value column** —
        the F1 defect artifacts. Their information content is re-ingested
        correctly into ``instrument_prices``, so the flip deletes them and
        materialisation replaces them with ``'system'`` rows.

        The predicate is narrow by construction — ``investment_id`` **and**
        ``ingest_origin = 'live'`` — so it can never touch an ``'excel'`` /
        ``'manual'`` / ``'system'`` row, nor a ``'live'`` row of any other
        investment, whatever the caller passes. Both ``nav_kind`` values are
        in range: a ``'live'`` producer never writes ``'plan'`` rows
        (ADR-0092), so the kind carries no additional discrimination here,
        and constraining it would silently strand a malformed row.

        Args:
            investment_id: The investment whose live-origin NAV rows to
                delete.

        Returns:
            The number of rows deleted (only ``'live'``-origin rows of this
            investment are ever counted).
        """
        result = await self._session.execute(
            delete(InvestmentNav).where(
                InvestmentNav.investment_id == investment_id,
                InvestmentNav.ingest_origin == "live",
            )
        )
        await self._session.flush()
        return result.rowcount or 0

    async def delete_by_investment(self, investment_id: UUID) -> int:
        """Delete every NAV row for an investment.

        The Excel-import (B1.1) replace-by-investment workflow uses
        this to clear an investment's NAV history before re-inserting
        the Excel-derived rows.

        Args:
            investment_id: The investment whose NAVs to delete.

        Returns:
            The number of rows that were deleted.
        """
        result = await self._session.execute(
            delete(InvestmentNav).where(InvestmentNav.investment_id == investment_id)
        )
        await self._session.flush()
        return result.rowcount or 0

    async def delete(self, nav_id: UUID) -> bool:
        """Hard-delete a single NAV row.

        Args:
            nav_id: The NAV row to delete.

        Returns:
            ``True`` if a row was deleted, ``False`` if no NAV with
            this id existed in the active tenant context.
        """
        result = await self._session.execute(
            delete(InvestmentNav).where(InvestmentNav.id == nav_id)
        )
        await self._session.flush()
        return (result.rowcount or 0) > 0
