# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentCashflowRepository tests against the live compose Postgres.

Coverage:

* ``create`` appends rows; repeated calls with identical args produce
  multiple rows (no UNIQUE constraint on the table).
* ``list_by_investment`` orders ascending by ``flow_timestamp``.
* ``list_by_investment_and_kind`` filters on ``flow_kind``.
* ``update`` modifies only the requested fields.
* ``delete_by_investment`` reports rowcount.
* ``delete`` reports True/False.
* RLS isolates cashflows between tenants.
* Invalid ``flow_type`` and ``flow_kind`` rejected by CHECK.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InvestmentCashflowRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)


async def _seed_investment(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    investment_name: str = "Test Fund",
):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="default_class", display_name="Default Class"
        )
        investment = await InvestmentRepository(session).create(
            name=investment_name,
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
    return actor, investment


# ---------------------------------------------------------------------------
# IC-01: create appends; identical create-args produce two distinct rows
# ---------------------------------------------------------------------------


async def test_ic01_create_appends_no_upsert(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="ic01@example.com")

    ts = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentCashflowRepository(session)
        first = await repo.create(
            investment_id=investment.id,
            flow_timestamp=ts,
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-1000000.0000"),
            currency="EUR",
            description="Q2 2025 call",
            created_by=actor.id,
        )
        second = await repo.create(
            investment_id=investment.id,
            flow_timestamp=ts,
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-1000000.0000"),
            currency="EUR",
            description="Q2 2025 call",
            created_by=actor.id,
        )

    assert first.id != second.id

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentCashflowRepository(session).list_by_investment(investment.id)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# IC-02: list orders by flow_timestamp ascending
# ---------------------------------------------------------------------------


async def test_ic02_list_orders_by_timestamp_ascending(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="ic02@example.com")

    timestamps = [
        datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
        datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc),
        datetime(2025, 12, 1, 12, 0, tzinfo=timezone.utc),
    ]

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentCashflowRepository(session)
        for ts in timestamps:
            await repo.create(
                investment_id=investment.id,
                flow_timestamp=ts,
                flow_type="capital_call",
                flow_kind="actual",
                amount=Decimal("-100"),
                currency="EUR",
                description=None,
                created_by=actor.id,
            )

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentCashflowRepository(session).list_by_investment(investment.id)

    assert [r.flow_timestamp for r in rows] == sorted(timestamps)


# ---------------------------------------------------------------------------
# IC-03: list_by_investment_and_kind filters on flow_kind
# ---------------------------------------------------------------------------


async def test_ic03_list_by_kind_filters(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="ic03@example.com")

    ts = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentCashflowRepository(session)
        await repo.create(
            investment_id=investment.id,
            flow_timestamp=ts,
            flow_type="capital_call",
            flow_kind="plan",
            amount=Decimal("-1000"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
        await repo.create(
            investment_id=investment.id,
            flow_timestamp=ts,
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-900"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentCashflowRepository(session)
        plans = await repo.list_by_investment_and_kind(investment.id, "plan")
        actuals = await repo.list_by_investment_and_kind(investment.id, "actual")

    assert [c.amount for c in plans] == [Decimal("-1000.0000")]
    assert [c.amount for c in actuals] == [Decimal("-900.0000")]


# ---------------------------------------------------------------------------
# IC-04: update modifies only the requested fields
# ---------------------------------------------------------------------------


async def test_ic04_update_modifies_requested_fields(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="ic04@example.com")

    ts = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentCashflowRepository(session)
        created = await repo.create(
            investment_id=investment.id,
            flow_timestamp=ts,
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-1000"),
            currency="EUR",
            description="Original",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        updated = await InvestmentCashflowRepository(session).update(
            created.id, amount=Decimal("-950"), description="Corrected"
        )

    assert updated is not None
    assert updated.amount == Decimal("-950.0000")
    assert updated.description == "Corrected"
    assert updated.flow_type == "capital_call"
    assert updated.flow_kind == "actual"
    assert updated.flow_timestamp == ts


# ---------------------------------------------------------------------------
# IC-05: delete_by_investment reports rowcount
# ---------------------------------------------------------------------------


async def test_ic05_delete_by_investment_reports_rowcount(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="ic05@example.com")

    ts = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentCashflowRepository(session)
        for _ in range(4):
            await repo.create(
                investment_id=investment.id,
                flow_timestamp=ts,
                flow_type="capital_call",
                flow_kind="actual",
                amount=Decimal("-100"),
                currency="EUR",
                description=None,
                created_by=actor.id,
            )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        deleted = await InvestmentCashflowRepository(session).delete_by_investment(investment.id)
    assert deleted == 4

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentCashflowRepository(session).list_by_investment(investment.id)
    assert rows == []


# ---------------------------------------------------------------------------
# IC-06: delete reports rowcount signal
# ---------------------------------------------------------------------------


async def test_ic06_delete_reports_rowcount_signal(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="ic06@example.com")

    ts = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        cashflow = await InvestmentCashflowRepository(session).create(
            investment_id=investment.id,
            flow_timestamp=ts,
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-100"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        first = await InvestmentCashflowRepository(session).delete(cashflow.id)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        second = await InvestmentCashflowRepository(session).delete(cashflow.id)

    assert first is True
    assert second is False


# ---------------------------------------------------------------------------
# IC-07: cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_ic07_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    actor_a, inv_a = await _seed_investment(app_engine, tenant_a, email="a@example.com")
    actor_b, inv_b = await _seed_investment(app_engine, tenant_b, email="b@example.com")

    ts = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await InvestmentCashflowRepository(session).create(
            investment_id=inv_a.id,
            flow_timestamp=ts,
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-1"),
            currency="EUR",
            description="A",
            created_by=actor_a.id,
        )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await InvestmentCashflowRepository(session).create(
            investment_id=inv_b.id,
            flow_timestamp=ts,
            flow_type="distribution",
            flow_kind="actual",
            amount=Decimal("2"),
            currency="USD",
            description="B",
            created_by=actor_b.id,
        )

    async with tenant_context(app_engine, tenant_a) as session:
        a_view = await InvestmentCashflowRepository(session).list_by_investment(inv_a.id)
        a_cross = await InvestmentCashflowRepository(session).list_by_investment(inv_b.id)
    async with tenant_context(app_engine, tenant_b) as session:
        b_view = await InvestmentCashflowRepository(session).list_by_investment(inv_b.id)

    assert [c.description for c in a_view] == ["A"]
    assert a_cross == []
    assert [c.description for c in b_view] == ["B"]


# ---------------------------------------------------------------------------
# IC-08: invalid flow_type rejected by CHECK
# ---------------------------------------------------------------------------


async def test_ic08_invalid_flow_type_raises(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="ic08@example.com")

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await InvestmentCashflowRepository(session).create(
                investment_id=investment.id,
                flow_timestamp=datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
                flow_type="bogus_type",
                flow_kind="actual",
                amount=Decimal("-1"),
                currency="EUR",
                description=None,
                created_by=actor.id,
            )


# ---------------------------------------------------------------------------
# IC-09: invalid flow_kind rejected by CHECK
# ---------------------------------------------------------------------------


async def test_ic09_invalid_flow_kind_raises(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="ic09@example.com")

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await InvestmentCashflowRepository(session).create(
                investment_id=investment.id,
                flow_timestamp=datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
                flow_type="capital_call",
                flow_kind="bogus_kind",
                amount=Decimal("-1"),
                currency="EUR",
                description=None,
                created_by=actor.id,
            )


# ---------------------------------------------------------------------------
# Batched plural methods (P6-H)
# ---------------------------------------------------------------------------


async def _seed_three_investments_with_cashflows(app_engine: AsyncEngine, tenant_id, *, email: str):
    """Seed actor, asset class, and three investments with cashflow rows.

    Returns ``(actor, [inv_a, inv_b, inv_c])`` for use against the
    plural cashflow methods.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="batched_class", display_name="Batched Class"
        )
        inv_repo = InvestmentRepository(session)
        inv_a = await inv_repo.create(
            name="Alpha",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        inv_b = await inv_repo.create(
            name="Beta",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        inv_c = await inv_repo.create(
            name="Gamma",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        cf_repo = InvestmentCashflowRepository(session)
        # Inv A: two actuals + one plan.
        await cf_repo.create(
            investment_id=inv_a.id,
            flow_timestamp=datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc),
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-100"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
        await cf_repo.create(
            investment_id=inv_a.id,
            flow_timestamp=datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc),
            flow_type="distribution",
            flow_kind="actual",
            amount=Decimal("20"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
        await cf_repo.create(
            investment_id=inv_a.id,
            flow_timestamp=datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
            flow_type="capital_call",
            flow_kind="plan",
            amount=Decimal("-50"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
        # Inv B: one actual.
        await cf_repo.create(
            investment_id=inv_b.id,
            flow_timestamp=datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc),
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-200"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
        # Inv C: no rows.
    return actor, [inv_a, inv_b, inv_c]


async def test_ic10_list_by_investments_matches_singular(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    _actor, invs = await _seed_three_investments_with_cashflows(
        app_engine, tenant_id, email="ic10@example.com"
    )
    inv_a, inv_b, inv_c = invs

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentCashflowRepository(session)
        singular = {
            inv_a.id: await repo.list_by_investment(inv_a.id),
            inv_b.id: await repo.list_by_investment(inv_b.id),
            inv_c.id: await repo.list_by_investment(inv_c.id),
        }
        batched = await repo.list_by_investments([inv_a.id, inv_b.id, inv_c.id])

    assert set(batched.keys()) == {inv_a.id, inv_b.id, inv_c.id}
    for inv_id, rows in singular.items():
        assert batched[inv_id] == rows
    # Inv A rows must be sorted by flow_timestamp ascending.
    timestamps_a = [c.flow_timestamp for c in batched[inv_a.id]]
    assert timestamps_a == sorted(timestamps_a)


async def test_ic11_list_by_investments_empty_input_returns_empty_dict(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        result = await InvestmentCashflowRepository(session).list_by_investments([])
    assert result == {}


async def test_ic12_list_by_investments_missing_id_maps_to_empty_list(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    _actor, invs = await _seed_three_investments_with_cashflows(
        app_engine, tenant_id, email="ic12@example.com"
    )
    inv_a, inv_b, _inv_c = invs
    fresh_id = uuid4()

    async with tenant_context(app_engine, tenant_id) as session:
        result = await InvestmentCashflowRepository(session).list_by_investments(
            [inv_a.id, inv_b.id, fresh_id]
        )

    assert set(result.keys()) == {inv_a.id, inv_b.id, fresh_id}
    assert result[fresh_id] == []
    assert len(result[inv_a.id]) == 3
    assert len(result[inv_b.id]) == 1


async def test_ic13_list_by_investments_and_kind_filters(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    _actor, invs = await _seed_three_investments_with_cashflows(
        app_engine, tenant_id, email="ic13@example.com"
    )
    inv_a, inv_b, inv_c = invs

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentCashflowRepository(session)
        actuals = await repo.list_by_investments_and_kind([inv_a.id, inv_b.id, inv_c.id], "actual")
        plans = await repo.list_by_investments_and_kind([inv_a.id, inv_b.id, inv_c.id], "plan")

    assert len(actuals[inv_a.id]) == 2
    assert len(actuals[inv_b.id]) == 1
    assert actuals[inv_c.id] == []
    assert len(plans[inv_a.id]) == 1
    assert plans[inv_b.id] == []
    assert plans[inv_c.id] == []
    # Empty list still returns empty dict.
    async with tenant_context(app_engine, tenant_id) as session:
        empty = await InvestmentCashflowRepository(session).list_by_investments_and_kind(
            [], "actual"
        )
    assert empty == {}
