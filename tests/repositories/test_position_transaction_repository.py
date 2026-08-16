# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""PositionTransactionRepository tests against the live compose Postgres.

Coverage (ADR-0097 §2):

* ``add`` inserts and ``list_for_investment`` returns rows in the canonical
  total order ``(trade_date, created_at, id)``.
* ``get_opening`` returns the single opening, or ``None`` when absent.
* The partial unique index rejects a second ``opening`` per investment.
* The sign CHECK rejects wrong-signed units per ``txn_type``.
* The price CHECKs reject a priceless buy/sell and a non-positive price.
* The ``ingest_origin`` CHECK rejects an out-of-set producer.
* ``delete`` reports True/False on rowcount.
* RLS isolates ledger rows between tenants (unprivileged app role).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InvestmentRepository,
    PositionTransactionRepository,
    UserRepository,
    tenant_context,
)


async def _seed_investment(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    investment_name: str = "Listed Fund",
    currency: str = "EUR",
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
            currency=currency,
            created_by=actor.id,
        )
    return actor, investment


# ---------------------------------------------------------------------------
# PT-01: add + list_for_investment canonical ordering
# ---------------------------------------------------------------------------


async def test_pt01_add_and_list_in_canonical_order(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="pt01@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = PositionTransactionRepository(session)
        # Insert out of trade_date order; expect sorted result.
        await repo.add(
            investment_id=inv.id,
            txn_type="buy",
            trade_date=date(2025, 6, 1),
            units=Decimal("50"),
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
            price_per_unit=Decimal("12.5"),
        )
        await repo.add(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=date(2025, 1, 1),
            units=Decimal("100"),
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await PositionTransactionRepository(session).list_for_investment(inv.id)
    assert [r.txn_type for r in rows] == ["opening", "buy"]
    assert [r.trade_date for r in rows] == [
        date(2025, 1, 1),
        date(2025, 6, 1),
    ]
    # opening carries no price; buy carries one.
    assert rows[0].price_per_unit is None
    assert rows[1].price_per_unit == Decimal("12.50000000")


# ---------------------------------------------------------------------------
# PT-02: get_opening
# ---------------------------------------------------------------------------


async def test_pt02_get_opening(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="pt02@example.com")

    async with tenant_context(app_engine, tenant_id) as session:
        # No opening yet.
        assert (await PositionTransactionRepository(session).get_opening(inv.id)) is None

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await PositionTransactionRepository(session).add(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=date(2025, 1, 1),
            units=Decimal("100"),
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        opening = await PositionTransactionRepository(session).get_opening(inv.id)
    assert opening is not None
    assert opening.txn_type == "opening"
    assert opening.units == Decimal("100.00000000")


# ---------------------------------------------------------------------------
# PT-03: at most one opening per investment (partial unique index)
# ---------------------------------------------------------------------------


async def test_pt03_second_opening_rejected(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="pt03@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await PositionTransactionRepository(session).add(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=date(2025, 1, 1),
            units=Decimal("100"),
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await PositionTransactionRepository(session).add(
                investment_id=inv.id,
                txn_type="opening",
                trade_date=date(2025, 2, 1),
                units=Decimal("10"),
                currency="EUR",
                ingest_origin="manual",
                created_by=actor.id,
            )


# ---------------------------------------------------------------------------
# PT-04: sign rules (CHECK)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("txn_type", "units"),
    [
        ("opening", "-100"),  # opening must be > 0
        ("buy", "-50"),  # buy must be > 0
        ("sell", "50"),  # sell must be < 0
        ("transfer", "0"),  # transfer must be <> 0
    ],
)
async def test_pt04_sign_rules_rejected(
    app_engine: AsyncEngine, seed_tenant, txn_type: str, units: str
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email=f"pt04-{txn_type}-{units}@example.com"
    )
    # buy/sell require a price; supply one so only the sign CHECK can fire.
    price = Decimal("10") if txn_type in ("buy", "sell") else None

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await PositionTransactionRepository(session).add(
                investment_id=inv.id,
                txn_type=txn_type,
                trade_date=date(2025, 1, 1),
                units=Decimal(units),
                currency="EUR",
                ingest_origin="manual",
                created_by=actor.id,
                price_per_unit=price,
            )


# ---------------------------------------------------------------------------
# PT-05: price rules (CHECK)
# ---------------------------------------------------------------------------


async def test_pt05_buy_without_price_rejected(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="pt05a@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await PositionTransactionRepository(session).add(
                investment_id=inv.id,
                txn_type="buy",
                trade_date=date(2025, 1, 1),
                units=Decimal("10"),
                currency="EUR",
                ingest_origin="manual",
                created_by=actor.id,
                price_per_unit=None,
            )


async def test_pt05_non_positive_price_rejected(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="pt05b@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await PositionTransactionRepository(session).add(
                investment_id=inv.id,
                txn_type="buy",
                trade_date=date(2025, 1, 1),
                units=Decimal("10"),
                currency="EUR",
                ingest_origin="manual",
                created_by=actor.id,
                price_per_unit=Decimal("0"),
            )


# ---------------------------------------------------------------------------
# PT-06: ingest_origin CHECK
# ---------------------------------------------------------------------------


async def test_pt06_invalid_ingest_origin_rejected(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="pt06@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await PositionTransactionRepository(session).add(
                investment_id=inv.id,
                txn_type="opening",
                trade_date=date(2025, 1, 1),
                units=Decimal("100"),
                currency="EUR",
                ingest_origin="bogus",
                created_by=actor.id,
            )


# ---------------------------------------------------------------------------
# PT-07: delete reports True/False
# ---------------------------------------------------------------------------


async def test_pt07_delete_reports_rowcount_signal(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="pt07@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        txn = await PositionTransactionRepository(session).add(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=date(2025, 1, 1),
            units=Decimal("100"),
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        first = await PositionTransactionRepository(session).delete(txn.id)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        second = await PositionTransactionRepository(session).delete(txn.id)

    assert first is True
    assert second is False


# ---------------------------------------------------------------------------
# PT-08: cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_pt08_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    actor_a, inv_a = await _seed_investment(app_engine, tenant_a, email="pta@example.com")
    actor_b, inv_b = await _seed_investment(app_engine, tenant_b, email="ptb@example.com")

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await PositionTransactionRepository(session).add(
            investment_id=inv_a.id,
            txn_type="opening",
            trade_date=date(2025, 1, 1),
            units=Decimal("100"),
            currency="EUR",
            ingest_origin="excel",
            created_by=actor_a.id,
        )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await PositionTransactionRepository(session).add(
            investment_id=inv_b.id,
            txn_type="opening",
            trade_date=date(2025, 1, 1),
            units=Decimal("200"),
            currency="EUR",
            ingest_origin="excel",
            created_by=actor_b.id,
        )

    async with tenant_context(app_engine, tenant_a) as session:
        repo = PositionTransactionRepository(session)
        a_view = await repo.list_for_investment(inv_a.id)
        # Tenant A cannot see Tenant B's ledger.
        a_cross = await repo.list_for_investment(inv_b.id)

    assert [r.units for r in a_view] == [Decimal("100.00000000")]
    assert a_cross == []


# ---------------------------------------------------------------------------
# PT-09: update_opening restates units / trade_date in place
# ---------------------------------------------------------------------------


async def test_pt09_update_opening_in_place(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="pt09@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        opening = await PositionTransactionRepository(session).add(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=date(2025, 1, 1),
            units=Decimal("100"),
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        updated = await PositionTransactionRepository(session).update_opening(
            opening.id,
            units=Decimal("175"),
            trade_date=date(2025, 3, 1),
        )
    assert updated is not None
    # Same row (identity preserved), restated values, price still NULL.
    assert updated.id == opening.id
    assert updated.units == Decimal("175.00000000")
    assert updated.trade_date == date(2025, 3, 1)
    assert updated.price_per_unit is None

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await PositionTransactionRepository(session).list_for_investment(inv.id)
    # Still exactly one opening — updated, not duplicated.
    assert [r.id for r in rows] == [opening.id]
    assert rows[0].units == Decimal("175.00000000")


# ---------------------------------------------------------------------------
# PT-10: update_opening returns None for an unknown id
# ---------------------------------------------------------------------------


async def test_pt10_update_opening_unknown_id_returns_none(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    from uuid import uuid4

    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        result = await PositionTransactionRepository(session).update_opening(
            uuid4(),
            units=Decimal("1"),
            trade_date=date(2025, 1, 1),
        )
    assert result is None
