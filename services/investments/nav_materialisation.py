# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Computed-NAV materialisation service (ADR-0098 §2–3, strand S2).

Turns a unitised investment's ``holdings × price`` into ``investment_navs``
rows so every consumer of the NAV-series contract (analytics, charts,
limits, Irene, reporting — ADR-0098 finding F3) sees computed NAVs as
ordinary ``actual`` rows, with ``basis='computed'`` /
``ingest_origin='system'`` provenance available for UI and audit.

This is a **DB-writing** service. It lives under ``services/investments/``
deliberately — **not** under ``services/analytics/`` (which stays DB-free,
ADR-0045) nor ``services/market_data/`` (provider-only and DB-free,
ADR-0091). The pure value computation — the holdings step function over the
transaction ledger — is delegated to the DB-free
:mod:`services.investments.holdings` (ADR-0097 §4); this service owns only
reading the sources, joining them into per-date NAV values, classifying
each date against the existing book, writing, and deleting stranded rows.

**Semantics (ADR-0098 §2).** For one *unitised* investment, the
materialised set is: for every ``instrument_prices`` date on or after the
first ledger date, with derived holdings ``> 0`` on that date, one
``investment_navs`` row —

* ``nav_value  = holdings(date) × price(date)`` (quantised to the
  ``Numeric(20, 4)`` scale of the column so re-runs compare equal);
* ``currency   = investment currency`` (equal to the price currency by
  ADR-0097 §5);
* ``nav_kind='actual'``, ``basis='computed'``,
  ``ingest_origin='system'``, ``source='computed:units×price'``.

A ``reported``-mode investment is a whole-investment **no-op**: the service
returns an all-zero report without reading or writing any NAV, so
``reported`` behaviour stays byte-identical to the pre-strand state.

**Classify-then-write idempotency**, mirroring the live-ingest write path
(:meth:`services.investments.InvestmentService._ingest_live_instrument_prices`):
the existing ``actual`` NAVs are read once and each target date is classified —
insert where absent; refresh an own ``'system'`` row only when the value
changed (a value-equal target is a counted no-op, ``updated_at``
untouched); skip ``'excel'`` / ``'manual'`` (precedence) and ``'live'``
(with a warning — a ``'live'`` row must not exist for a unitised
investment, ADR-0098 §1). ``'system'`` rows whose date left the
materialised set are **deleted**. Only ``'system'`` rows are ever inserted,
refreshed, or deleted; no other origin's row is mutated on any path.

**Trigger (ADR-0098 §3).** The service runs synchronously inside the
caller's tenant-scoped transaction at the write choke-points — it takes the
same repositories (hence the same :class:`~sqlalchemy.ext.asyncio.AsyncSession`)
the caller already holds, so prices/ledger and computed NAVs can never be
observed disagreeing, and no advisory lock is needed (materialisation does
no provider I/O). Callers that know the earliest affected date pass
``since`` to bound the recompute to dates on or after it.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from core.repositories.instrument_price_repository import (
    InstrumentPriceRepository,
)
from core.repositories.investment_nav_repository import (
    InvestmentNavRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.position_transaction_repository import (
    PositionTransactionRepository,
)
from services.investments.holdings import derive_holdings

_LOG = logging.getLogger("portfoliflow.services.investments.nav_materialisation")

#: The ``nav_kind`` materialised rows carry. Plan rows are never
#: materialised (ADR-0098 §2); only ``'actual'`` is ever written.
_NAV_KIND: str = "actual"

#: The pinned free-text provenance of a materialised row (ADR-0098 §2).
#: Distinct from ``basis='computed'`` (analytics semantics) and
#: ``ingest_origin='system'`` (writer channel).
_COMPUTED_SOURCE: str = "computed:units×price"

#: The ``Numeric(20, 4)`` scale of ``investment_navs.nav_value``. Computed
#: values are quantised to it (half-away-from-zero, matching Postgres
#: ``numeric`` rounding) so a stored row and a freshly recomputed value
#: compare equal — the load-bearing condition for the value-equal no-op.
_NAV_SCALE: Decimal = Decimal("0.0001")


@dataclass(frozen=True)
class NavMaterialisationReport:
    """Per-outcome counts for one materialisation run (ADR-0098 §2).

    Idempotency is a property, not a mode: re-running with an unchanged
    ledger and price series produces all-zero change counts on the second
    run — only ``noop`` (target dates already present as identical
    ``'system'`` rows) and the ``skipped_*`` counters move, and no
    ``updated_at`` is bumped.

    Attributes:
        inserted: Target dates with no existing row — a fresh ``'system'``
            row written.
        updated: Own ``'system'`` rows whose computed value changed and were
            refreshed in place.
        noop: Target dates already present as an identical ``'system'`` row —
            no write issued (the idempotency signal).
        skipped_excel: Target dates whose existing row is ``'excel'`` — left
            byte-identical (book of record is authoritative).
        skipped_manual: Target dates whose existing row is ``'manual'`` —
            left byte-identical (operator edits are precedence-protected).
        skipped_live: Target dates whose existing row is ``'live'`` — left
            byte-identical and **logged as a warning**: a ``'live'`` row must
            not exist for a unitised investment (ADR-0098 §1). Never deleted
            or overwritten.
        deleted: ``'system'`` rows whose date left the materialised set — a
            backdated edit, a price deletion, or a holdings-to-zero sale can
            strand them. Only ``'system'`` rows are ever deletion candidates.
    """

    inserted: int = 0
    updated: int = 0
    noop: int = 0
    skipped_excel: int = 0
    skipped_manual: int = 0
    skipped_live: int = 0
    deleted: int = 0

    @property
    def total_written(self) -> int:
        """Rows the run created or refreshed (``inserted + updated``)."""
        return self.inserted + self.updated


class NavMaterialisationService:
    """Materialise ``holdings × price`` into ``investment_navs`` rows.

    Constructed from the four tenant-scoped repositories it needs; they must
    share one session so the whole run is a single in-transaction unit
    (ADR-0098 §3). The service holds no session itself.
    """

    def __init__(
        self,
        investments: InvestmentRepository,
        navs: InvestmentNavRepository,
        prices: InstrumentPriceRepository,
        transactions: PositionTransactionRepository,
    ) -> None:
        self._investments = investments
        self._navs = navs
        self._prices = prices
        self._transactions = transactions

    async def materialise(
        self,
        investment_id: UUID,
        *,
        acting_user: UUID,
        since: _date | None = None,
    ) -> NavMaterialisationReport:
        """Recompute the computed-NAV rows for one unitised investment.

        Reads the ledger, the price series, and the existing ``actual``
        NAVs; forms the target ``{date: nav_value}`` set; classifies each
        target date against the book; writes ``'system'`` inserts/updates;
        and deletes stranded ``'system'`` rows. A ``reported``-mode
        investment (or an unknown one) is an all-zero no-op.

        Args:
            investment_id: The investment to materialise. RLS scopes every
                read and write to the active tenant; a foreign-tenant id
                simply matches no rows.
            acting_user: ``created_by`` for inserted rows (preserved on
                update). The market-data system actor arrives here when the
                trigger is live ingest (ADR-0093, strand S3).
            since: The earliest affected statement day, if the caller knows
                it (e.g. a transaction's ``trade_date``). When given, the
                recompute is bounded to dates on or after it — a change at
                ``since`` cannot alter holdings, prices, or the target set on
                any earlier date, so earlier ``'system'`` rows are left
                untouched. ``None`` recomputes the whole series (the initial
                full materialisation on a mode flip, strand S5).

        Returns:
            A :class:`NavMaterialisationReport` with per-outcome counts.
        """
        investment = await self._investments.get_by_id(investment_id)
        if investment is None or investment.valuation_mode != "unitised":
            # Reported-mode (and unknown) investments are byte-identical:
            # no NAV is read or written. This is the regression guard that
            # keeps every non-unitised investment untouched (ADR-0098 §5).
            return NavMaterialisationReport()

        currency = investment.currency
        transactions = await self._transactions.list_for_investment(investment_id)
        prices = await self._prices.list_by_investment(investment_id)
        existing = await self._navs.list_by_investment_and_kind(investment_id, _NAV_KIND)

        target = self._target_navs(
            transactions=transactions,
            prices=prices,
            currency_scale=_NAV_SCALE,
            since=since,
        )

        existing_by_date = {n.as_of_date: n for n in existing}
        inserted = updated = noop = 0
        skipped_excel = skipped_manual = skipped_live = 0

        for as_of_date, nav_value in target.items():
            current = existing_by_date.get(as_of_date)
            if current is None:
                await self._navs.upsert_computed(
                    investment_id=investment_id,
                    as_of_date=as_of_date,
                    nav_kind=_NAV_KIND,
                    nav_value=nav_value,
                    currency=currency,
                    source=_COMPUTED_SOURCE,
                    created_by=acting_user,
                )
                inserted += 1
            elif current.ingest_origin == "system":
                if current.nav_value == nav_value and current.currency == currency:
                    # Identical computed row already present — no write, so
                    # updated_at is untouched (idempotency).
                    noop += 1
                else:
                    await self._navs.upsert_computed(
                        investment_id=investment_id,
                        as_of_date=as_of_date,
                        nav_kind=_NAV_KIND,
                        nav_value=nav_value,
                        currency=currency,
                        source=_COMPUTED_SOURCE,
                        created_by=acting_user,
                    )
                    updated += 1
            elif current.ingest_origin == "manual":
                skipped_manual += 1
            elif current.ingest_origin == "live":
                # A 'live' row cannot legitimately exist for a unitised
                # investment (the mode flip deletes them, the re-routing
                # prevents new ones — ADR-0098 §1). Never overwrite or
                # delete it; skip loudly so a provisioning fault surfaces.
                _LOG.warning(
                    "nav_materialisation: unexpected 'live' NAV row for "
                    "unitised investment=%s on %s — skipped, not overwritten "
                    "(ADR-0098 §1).",
                    investment_id,
                    as_of_date,
                )
                skipped_live += 1
            else:
                # 'excel' — book of record; also the safe default for any
                # unexpected origin (never overwrite what is not 'system').
                skipped_excel += 1

        deleted = await self._delete_stranded(
            investment_id=investment_id,
            existing=existing,
            target_dates=target.keys(),
            since=since,
        )

        report = NavMaterialisationReport(
            inserted=inserted,
            updated=updated,
            noop=noop,
            skipped_excel=skipped_excel,
            skipped_manual=skipped_manual,
            skipped_live=skipped_live,
            deleted=deleted,
        )
        _LOG.info(
            "nav_materialisation: investment=%s since=%s targets=%d "
            "inserted=%d updated=%d noop=%d skipped_excel=%d "
            "skipped_manual=%d skipped_live=%d deleted=%d",
            investment_id,
            since,
            len(target),
            report.inserted,
            report.updated,
            report.noop,
            report.skipped_excel,
            report.skipped_manual,
            report.skipped_live,
            report.deleted,
        )
        return report

    @staticmethod
    def _target_navs(
        *,
        transactions,
        prices,
        currency_scale: Decimal,
        since: _date | None,
    ) -> dict[_date, Decimal]:
        """Form the ``{date: nav_value}`` materialised set (ADR-0098 §2).

        One entry per ``instrument_prices`` date on or after the first
        ledger date (and on or after ``since`` when given) with derived
        holdings ``> 0``. Empty when the ledger is empty (no holdings) or no
        price falls in range.
        """
        points = derive_holdings(transactions)
        if not points:
            # No ledger → holdings are zero everywhere → nothing to
            # materialise. Any existing 'system' rows are stranded and are
            # deleted by the caller.
            return {}

        first_ledger_date = points[0].as_of_date
        lower_bound = first_ledger_date if since is None else max(first_ledger_date, since)

        point_dates = [p.as_of_date for p in points]

        def holdings_on(on: _date) -> Decimal:
            # The step function's value on `on`: cumulative units of the
            # last point with as_of_date <= on (zero before the first).
            idx = bisect_right(point_dates, on)
            return points[idx - 1].units if idx else Decimal(0)

        target: dict[_date, Decimal] = {}
        for price in prices:
            if price.as_of_date < lower_bound:
                continue
            held = holdings_on(price.as_of_date)
            if held > 0:
                target[price.as_of_date] = (held * price.price).quantize(
                    currency_scale, rounding=ROUND_HALF_UP
                )
        return target

    async def _delete_stranded(
        self,
        *,
        investment_id: UUID,
        existing,
        target_dates,
        since: _date | None,
    ) -> int:
        """Delete ``'system'`` rows whose date left the materialised set.

        Only ``'system'``-origin ``actual`` rows are candidates. When
        ``since`` is given, rows before it are outside the recompute window
        (their target membership is unchanged) and are never stranded.
        """
        target = set(target_dates)
        stranded = [
            n.as_of_date
            for n in existing
            if n.ingest_origin == "system"
            and n.as_of_date not in target
            and (since is None or n.as_of_date >= since)
        ]
        if not stranded:
            return 0
        return await self._navs.delete_system_navs(investment_id, stranded)


__all__ = [
    "NavMaterialisationReport",
    "NavMaterialisationService",
]
