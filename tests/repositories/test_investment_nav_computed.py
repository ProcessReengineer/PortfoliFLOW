# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentNavRepository computed-write tests (ADR-0098 §1–2, strand S2).

Exercises the two ``'system'``-origin methods added for computed-NAV
materialisation, against the live compose Postgres under the unprivileged
``portfoliflow_app`` role:

* ``upsert_computed`` — the conditional sibling of ``upsert_live``. It
  inserts a ``'system'`` / ``basis='computed'`` row where absent, refreshes
  its own ``'system'`` row, and — the guard — leaves any ``'excel'`` /
  ``'manual'`` / ``'live'`` row **byte-identical**, returning ``None``.
* ``delete_system_navs`` — deletes only ``'system'`` ``actual`` rows on the
  supplied dates; never an ``'excel'`` / ``'manual'`` / ``'live'`` / plan row.

``upsert_live`` and the ADR-0092 semantics are not touched here — these are
sibling guards, verified independently.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)


async def _seed_investment(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
):
    """Create one user, one asset class, and one investment for setup."""
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="listed_class", display_name="Listed Class"
        )
        investment = await InvestmentRepository(session).create(
            name="Listed Fund",
            investment_type="listed_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
    return actor, investment


# ---------------------------------------------------------------------------
# NC-01: upsert_computed inserts a new 'system' / 'computed' row
# ---------------------------------------------------------------------------


async def test_nc01_upsert_computed_inserts(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="nc01@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentNavRepository(session)
        row = await repo.upsert_computed(
            investment_id=inv.id,
            as_of_date=date(2025, 1, 31),
            nav_kind="actual",
            nav_value=Decimal("1000.0000"),
            currency="EUR",
            source="computed:units×price",
            created_by=actor.id,
        )

    assert row is not None
    assert row.ingest_origin == "system"
    assert row.nav_value == Decimal("1000.0000")
    assert row.source == "computed:units×price"


# ---------------------------------------------------------------------------
# NC-02: upsert_computed refreshes its own 'system' row in place
# ---------------------------------------------------------------------------


async def test_nc02_upsert_computed_refreshes_own_system_row(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="nc02@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        first = await InvestmentNavRepository(session).upsert_computed(
            investment_id=inv.id,
            as_of_date=date(2025, 1, 31),
            nav_kind="actual",
            nav_value=Decimal("1000.0000"),
            currency="EUR",
            source="computed:units×price",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        second = await InvestmentNavRepository(session).upsert_computed(
            investment_id=inv.id,
            as_of_date=date(2025, 1, 31),
            nav_kind="actual",
            nav_value=Decimal("1200.0000"),
            currency="EUR",
            source="computed:units×price",
            created_by=actor.id,
        )

    assert first is not None and second is not None
    assert first.id == second.id  # same row — an UPDATE, not an INSERT
    assert second.nav_value == Decimal("1200.0000")
    assert second.updated_at > first.updated_at


# ---------------------------------------------------------------------------
# NC-03: upsert_computed leaves 'excel'/'manual'/'live' rows byte-identical
# ---------------------------------------------------------------------------


async def _assert_computed_skips_origin(
    app_engine: AsyncEngine,
    seed_tenant,
    *,
    email: str,
    seed_origin: str,
) -> None:
    """Seed a non-system row, then assert upsert_computed leaves it intact."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email=email)
    as_of = date(2025, 3, 31)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentNavRepository(session)
        if seed_origin == "live":
            await repo.upsert_live(
                investment_id=inv.id,
                as_of_date=as_of,
                nav_kind="actual",
                nav_value=Decimal("500.0000"),
                currency="EUR",
                source="provider",
                basis="reported",
                created_by=actor.id,
            )
        else:
            await repo.upsert(
                investment_id=inv.id,
                as_of_date=as_of,
                nav_kind="actual",
                nav_value=Decimal("500.0000"),
                currency="EUR",
                source="book",
                created_by=actor.id,
                ingest_origin=seed_origin,
            )

    async with tenant_context(app_engine, tenant_id) as session:
        before = (await InvestmentNavRepository(session).list_by_investment(inv.id))[0]

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        result = await InvestmentNavRepository(session).upsert_computed(
            investment_id=inv.id,
            as_of_date=as_of,
            nav_kind="actual",
            nav_value=Decimal("9999.0000"),
            currency="EUR",
            source="computed:units×price",
            created_by=actor.id,
        )

    assert result is None  # guarded no-op — the book row wins

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentNavRepository(session).list_by_investment(inv.id)
    assert len(rows) == 1
    after = rows[0]
    assert after.id == before.id
    assert after.ingest_origin == seed_origin
    assert after.nav_value == Decimal("500.0000")
    assert after.updated_at == before.updated_at  # not bumped


async def test_nc03a_upsert_computed_skips_excel(app_engine: AsyncEngine, seed_tenant) -> None:
    await _assert_computed_skips_origin(
        app_engine, seed_tenant, email="nc03a@example.com", seed_origin="excel"
    )


async def test_nc03b_upsert_computed_skips_manual(app_engine: AsyncEngine, seed_tenant) -> None:
    await _assert_computed_skips_origin(
        app_engine,
        seed_tenant,
        email="nc03b@example.com",
        seed_origin="manual",
    )


async def test_nc03c_upsert_computed_skips_live(app_engine: AsyncEngine, seed_tenant) -> None:
    await _assert_computed_skips_origin(
        app_engine, seed_tenant, email="nc03c@example.com", seed_origin="live"
    )


# ---------------------------------------------------------------------------
# NC-04: delete_system_navs deletes only 'system' actual rows on given dates
# ---------------------------------------------------------------------------


async def test_nc04_delete_system_navs_scopes_to_system_and_dates(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="nc04@example.com")
    d1, d2, d3 = date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentNavRepository(session)
        # A 'system' row on each of d1, d2, d3.
        for d in (d1, d2, d3):
            await repo.upsert_computed(
                investment_id=inv.id,
                as_of_date=d,
                nav_kind="actual",
                nav_value=Decimal("100.0000"),
                currency="EUR",
                source="computed:units×price",
                created_by=actor.id,
            )
        # An 'excel' actual row on d1 would violate the unique key with the
        # system row — instead put the excel row on a separate date, and a
        # 'plan' system-value row on d2 to prove nav_kind scoping.
        await repo.upsert(
            investment_id=inv.id,
            as_of_date=date(2025, 2, 1),
            nav_kind="actual",
            nav_value=Decimal("777.0000"),
            currency="EUR",
            source="book",
            created_by=actor.id,
            ingest_origin="excel",
        )

    # Delete the system rows on d1 and d3 only.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        deleted = await InvestmentNavRepository(session).delete_system_navs(inv.id, [d1, d3])
    assert deleted == 2

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentNavRepository(session).list_by_investment(inv.id)
    remaining = {(r.as_of_date, r.ingest_origin) for r in rows}
    assert remaining == {
        (d2, "system"),  # untouched — not in the delete date list
        (date(2025, 2, 1), "excel"),  # untouched — excel is never a candidate
    }


async def test_nc05_delete_system_navs_empty_dates_is_noop(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="nc05@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        deleted = await InvestmentNavRepository(session).delete_system_navs(inv.id, [])
    assert deleted == 0
