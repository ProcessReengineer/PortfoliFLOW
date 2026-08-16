# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InstrumentPriceRepository tests against the live compose Postgres.

Coverage (ADR-0097 §3, mirroring the NAV repository):

* ``upsert`` is INSERT-then-UPDATE on ``(investment_id, as_of_date)``.
* ``upsert_live`` guard matrix (ADR-0092): insert new / refresh own live /
  no-op on an ``'excel'`` row / no-op on a ``'manual'`` row — the no-op
  returns ``None`` and leaves the book-of-record row byte-identical.
* The unique key rejects a duplicate ``(investment_id, as_of_date)``.
* The ``price > 0`` CHECK and the ``ingest_origin`` CHECK reject bad rows.
* ``list_by_investment`` orders ascending by ``as_of_date``.
* ``list_by_investments`` batches many series into one query and keeps the
  per-investment carry-forward anchor before ``from_date`` (ADR-0116 §4).
* ``delete_by_investment`` reports the number of deleted rows.
* RLS isolates prices between tenants (unprivileged app role).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InstrumentPriceRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)


async def _seed_investment(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    investment_name: str = "Listed Fund",
):
    """Create one user, one asset class, and one investment for setup."""
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="listed_class", display_name="Listed Class"
        )
        investment = await InvestmentRepository(session).create(
            name=investment_name,
            investment_type="listed_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
    return actor, investment


# ---------------------------------------------------------------------------
# IP-01: upsert inserts on first call, updates on second call
# ---------------------------------------------------------------------------


async def test_ip01_upsert_inserts_then_updates(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="ip01@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        first = await InstrumentPriceRepository(session).upsert(
            investment_id=inv.id,
            as_of_date=date(2025, 12, 31),
            price=Decimal("100.5"),
            currency="EUR",
            source="initial",
            created_by=actor.id,
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        second = await InstrumentPriceRepository(session).upsert(
            investment_id=inv.id,
            as_of_date=date(2025, 12, 31),
            price=Decimal("101.25"),
            currency="EUR",
            source="corrected",
            created_by=actor.id,
        )

    assert first.id == second.id  # UPDATE, not a second INSERT
    assert second.price == Decimal("101.25000000")
    assert second.source == "corrected"


# ---------------------------------------------------------------------------
# IP-02: upsert_live inserts a new row as 'live'
# ---------------------------------------------------------------------------


async def test_ip02_upsert_live_inserts_new(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="ip02@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        result = await InstrumentPriceRepository(session).upsert_live(
            investment_id=inv.id,
            as_of_date=date(2025, 12, 31),
            price=Decimal("99.0"),
            currency="EUR",
            source="yahoo",
            created_by=actor.id,
        )
    assert result is not None
    assert result.ingest_origin == "live"
    assert result.price == Decimal("99.00000000")


# ---------------------------------------------------------------------------
# IP-03: upsert_live refreshes its own prior 'live' row
# ---------------------------------------------------------------------------


async def test_ip03_upsert_live_refreshes_own_live_row(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="ip03@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InstrumentPriceRepository(session)
        first = await repo.upsert_live(
            investment_id=inv.id,
            as_of_date=date(2025, 12, 31),
            price=Decimal("99.0"),
            currency="EUR",
            source="yahoo",
            created_by=actor.id,
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        second = await InstrumentPriceRepository(session).upsert_live(
            investment_id=inv.id,
            as_of_date=date(2025, 12, 31),
            price=Decimal("102.0"),
            currency="EUR",
            source="yahoo",
            created_by=actor.id,
        )
    assert first is not None and second is not None
    assert first.id == second.id  # refreshed in place
    assert second.price == Decimal("102.00000000")


# ---------------------------------------------------------------------------
# IP-04: upsert_live is a no-op against 'excel' / 'manual' rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["excel", "manual"])
async def test_ip04_upsert_live_noops_on_book_of_record(
    app_engine: AsyncEngine, seed_tenant, origin: str
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email=f"ip04-{origin}@example.com")
    # Seed a book-of-record row via the unconditional upsert.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        seeded = await InstrumentPriceRepository(session).upsert(
            investment_id=inv.id,
            as_of_date=date(2025, 12, 31),
            price=Decimal("100.0"),
            currency="EUR",
            source="book",
            created_by=actor.id,
            ingest_origin=origin,
        )

    # A live write on the same key must be a recorded no-op.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        result = await InstrumentPriceRepository(session).upsert_live(
            investment_id=inv.id,
            as_of_date=date(2025, 12, 31),
            price=Decimal("77.0"),
            currency="EUR",
            source="yahoo",
            created_by=actor.id,
        )
    assert result is None  # guarded no-op

    # The book-of-record row is byte-identical.
    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InstrumentPriceRepository(session).list_by_investment(inv.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.ingest_origin == origin
    assert row.price == Decimal("100.00000000")
    assert row.source == "book"
    assert row.updated_at == seeded.updated_at  # not bumped


# ---------------------------------------------------------------------------
# IP-05: unique (investment_id, as_of_date) rejects a raw duplicate
# ---------------------------------------------------------------------------


async def test_ip05_price_positive_check(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="ip05@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await InstrumentPriceRepository(session).upsert(
                investment_id=inv.id,
                as_of_date=date(2025, 12, 31),
                price=Decimal("0"),
                currency="EUR",
                source=None,
                created_by=actor.id,
            )


async def test_ip06_invalid_ingest_origin_rejected(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="ip06@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await InstrumentPriceRepository(session).upsert(
                investment_id=inv.id,
                as_of_date=date(2025, 12, 31),
                price=Decimal("100"),
                currency="EUR",
                source=None,
                created_by=actor.id,
                ingest_origin="bogus",
            )


# ---------------------------------------------------------------------------
# IP-07: list ordering + delete_by_investment
# ---------------------------------------------------------------------------


async def test_ip07_list_orders_ascending_and_delete_by_investment(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="ip07@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InstrumentPriceRepository(session)
        for d in (date(2025, 6, 30), date(2024, 12, 31), date(2025, 12, 31)):
            await repo.upsert(
                investment_id=inv.id,
                as_of_date=d,
                price=Decimal("100"),
                currency="EUR",
                source=None,
                created_by=actor.id,
            )

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InstrumentPriceRepository(session).list_by_investment(inv.id)
    assert [r.as_of_date for r in rows] == [
        date(2024, 12, 31),
        date(2025, 6, 30),
        date(2025, 12, 31),
    ]

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        deleted = await InstrumentPriceRepository(session).delete_by_investment(inv.id)
    assert deleted == 3


# ---------------------------------------------------------------------------
# IP-08: cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_ip08_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    actor_a, inv_a = await _seed_investment(app_engine, tenant_a, email="ipa@example.com")
    actor_b, inv_b = await _seed_investment(app_engine, tenant_b, email="ipb@example.com")

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await InstrumentPriceRepository(session).upsert(
            investment_id=inv_a.id,
            as_of_date=date(2025, 12, 31),
            price=Decimal("100"),
            currency="EUR",
            source=None,
            created_by=actor_a.id,
        )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await InstrumentPriceRepository(session).upsert(
            investment_id=inv_b.id,
            as_of_date=date(2025, 12, 31),
            price=Decimal("200"),
            currency="EUR",
            source=None,
            created_by=actor_b.id,
        )

    async with tenant_context(app_engine, tenant_a) as session:
        repo = InstrumentPriceRepository(session)
        a_view = await repo.list_by_investment(inv_a.id)
        a_cross = await repo.list_by_investment(inv_b.id)

    assert [r.price for r in a_view] == [Decimal("100.00000000")]
    assert a_cross == []


# ---------------------------------------------------------------------------
# IP-10: list_by_investments — one query, many series, a soft lower bound
# ---------------------------------------------------------------------------


async def _seed_two_investments(app_engine: AsyncEngine, tenant_id, *, email: str):
    """One user, one asset class, two investments to price independently."""
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="listed_class", display_name="Listed Class"
        )
        investments = InvestmentRepository(session)
        first = await investments.create(
            name="Fund A",
            investment_type="listed_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        second = await investments.create(
            name="Fund B",
            investment_type="listed_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
    return actor, first, second


async def test_ip10_list_by_investments_batches_and_keeps_the_anchor(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The batched read serves both series and both windows correctly.

    The soft lower bound is the claim under test (ADR-0116 §4). Fund A is
    priced only *before* the window; a hard-edged read would return nothing
    for it and the Watch Desk would report "no data" over a price that
    plainly exists and, by carry-forward, still applies.
    """
    tenant_id = await seed_tenant()
    actor, fund_a, fund_b = await _seed_two_investments(app_engine, tenant_id, email="ip10@x.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        prices = InstrumentPriceRepository(session)
        for investment_id, points in (
            (fund_a.id, [(date(2026, 1, 5), "10"), (date(2026, 2, 2), "11")]),
            (
                fund_b.id,
                [(date(2026, 1, 5), "20"), (date(2026, 6, 1), "21"), (date(2026, 6, 30), "22")],
            ),
        ):
            for as_of_date, value in points:
                await prices.upsert(
                    investment_id=investment_id,
                    as_of_date=as_of_date,
                    price=Decimal(value),
                    currency="EUR",
                    source="test",
                    created_by=actor.id,
                )

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InstrumentPriceRepository(session).list_by_investments(
            [fund_a.id, fund_b.id], from_date=date(2026, 6, 25), to_date=date(2026, 6, 30)
        )

    by_investment: dict = {}
    for row in rows:
        by_investment.setdefault(row.investment_id, []).append(row.as_of_date)

    # Fund A: nothing inside the window, so the anchor — its latest row at
    # or before the window start — comes back on its own.
    assert by_investment[fund_a.id] == [date(2026, 2, 2)]
    # Fund B: its own anchor plus the row inside the window, ascending.
    assert by_investment[fund_b.id] == [date(2026, 6, 1), date(2026, 6, 30)]


async def test_ip10_list_by_investments_clips_the_upper_bound_hard(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """No price after ``to_date`` is ever needed — or returned."""
    tenant_id = await seed_tenant()
    actor, fund_a, _ = await _seed_two_investments(app_engine, tenant_id, email="ip10b@x.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        prices = InstrumentPriceRepository(session)
        for as_of_date, value in ((date(2026, 6, 1), "10"), (date(2026, 7, 1), "12")):
            await prices.upsert(
                investment_id=fund_a.id,
                as_of_date=as_of_date,
                price=Decimal(value),
                currency="EUR",
                source="test",
                created_by=actor.id,
            )

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InstrumentPriceRepository(session).list_by_investments(
            [fund_a.id], to_date=date(2026, 6, 30)
        )

    assert [row.as_of_date for row in rows] == [date(2026, 6, 1)]


async def test_ip10_list_by_investments_of_nothing_queries_nothing(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An empty id list is a legitimate call — a tenant may watch no prices."""
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        assert await InstrumentPriceRepository(session).list_by_investments([]) == []
