# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentNavRepository tests against the live compose Postgres.

Coverage:

* ``upsert`` is INSERT-then-UPDATE on ``(investment_id, as_of_date,
  nav_kind)``: re-applying the same key updates the existing row.
* Plan and Actual NAVs on the same date coexist as two distinct
  rows.
* ``list_by_investment`` orders ascending by ``as_of_date``.
* ``list_by_investment_and_kind`` filters on ``nav_kind``.
* ``list_by_investments`` / ``list_by_investments_and_kind``: batched
  counterparts return one query for many investments (P6-H).
* ``get_latest_actual`` returns the most recent ``actual`` NAV.
* ``latest_actual_as_of_date`` aggregates the universe as-of (ADR-0113
  §1): max over ``actual`` rows only, ``None`` for an empty id list.
* ``delete_by_investment`` reports the number of deleted rows.
* ``delete`` reports True/False on rowcount.
* RLS isolates NAVs between tenants.
* Invalid ``nav_kind`` rejected by CHECK constraint.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
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
    investment_name: str = "Test Fund",
):
    """Create one user, one asset class, and one investment for setup."""
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
# IN-01: upsert inserts on first call, updates on second call
# ---------------------------------------------------------------------------


async def test_in01_upsert_inserts_then_updates(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="in01@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentNavRepository(session)
        first = await repo.upsert(
            investment_id=investment.id,
            as_of_date=date(2025, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("1500000.0000"),
            currency="EUR",
            source="initial",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentNavRepository(session)
        second = await repo.upsert(
            investment_id=investment.id,
            as_of_date=date(2025, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("1600000.0000"),
            currency="EUR",
            source="corrected",
            created_by=actor.id,
        )

    # Same id ⇒ second was an UPDATE, not an INSERT.
    assert first.id == second.id
    assert second.nav_value == Decimal("1600000.0000")
    assert second.source == "corrected"

    async with tenant_context(app_engine, tenant_id) as session:
        all_navs = await InvestmentNavRepository(session).list_by_investment(investment.id)
    assert len(all_navs) == 1
    assert all_navs[0].nav_value == Decimal("1600000.0000")


# ---------------------------------------------------------------------------
# IN-02: plan and actual on same date coexist
# ---------------------------------------------------------------------------


async def test_in02_plan_and_actual_coexist_same_date(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="in02@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentNavRepository(session)
        await repo.upsert(
            investment_id=investment.id,
            as_of_date=date(2026, 6, 30),
            nav_kind="plan",
            nav_value=Decimal("2000000.0000"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        await repo.upsert(
            investment_id=investment.id,
            as_of_date=date(2026, 6, 30),
            nav_kind="actual",
            nav_value=Decimal("2100000.0000"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentNavRepository(session)
        plans = await repo.list_by_investment_and_kind(investment.id, "plan")
        actuals = await repo.list_by_investment_and_kind(investment.id, "actual")

    assert [n.nav_value for n in plans] == [Decimal("2000000.0000")]
    assert [n.nav_value for n in actuals] == [Decimal("2100000.0000")]


# ---------------------------------------------------------------------------
# IN-03: list_by_investment orders ascending by as_of_date
# ---------------------------------------------------------------------------


async def test_in03_list_orders_by_date_ascending(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="in03@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentNavRepository(session)
        # Insert out of order; expect sorted result.
        for d in (date(2025, 6, 30), date(2024, 12, 31), date(2025, 12, 31)):
            await repo.upsert(
                investment_id=investment.id,
                as_of_date=d,
                nav_kind="actual",
                nav_value=Decimal("1000000"),
                currency="EUR",
                source=None,
                created_by=actor.id,
            )

    async with tenant_context(app_engine, tenant_id) as session:
        all_navs = await InvestmentNavRepository(session).list_by_investment(investment.id)
    assert [n.as_of_date for n in all_navs] == [
        date(2024, 12, 31),
        date(2025, 6, 30),
        date(2025, 12, 31),
    ]


# ---------------------------------------------------------------------------
# IN-04: get_latest_actual returns the most recent actual NAV
# ---------------------------------------------------------------------------


async def test_in04_get_latest_actual(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="in04@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentNavRepository(session)
        # Two actuals plus a plan after the latest actual.
        await repo.upsert(
            investment_id=investment.id,
            as_of_date=date(2025, 6, 30),
            nav_kind="actual",
            nav_value=Decimal("1000"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        await repo.upsert(
            investment_id=investment.id,
            as_of_date=date(2025, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("2000"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        await repo.upsert(
            investment_id=investment.id,
            as_of_date=date(2026, 6, 30),
            nav_kind="plan",
            nav_value=Decimal("9999"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        latest = await InvestmentNavRepository(session).get_latest_actual(investment.id)
    assert latest is not None
    assert latest.as_of_date == date(2025, 12, 31)
    assert latest.nav_value == Decimal("2000")


# ---------------------------------------------------------------------------
# IN-05: get_latest_actual returns None when no actuals exist
# ---------------------------------------------------------------------------


async def test_in05_get_latest_actual_returns_none(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="in05@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentNavRepository(session).upsert(
            investment_id=investment.id,
            as_of_date=date(2026, 6, 30),
            nav_kind="plan",
            nav_value=Decimal("1000"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        latest = await InvestmentNavRepository(session).get_latest_actual(investment.id)
    assert latest is None


# ---------------------------------------------------------------------------
# IN-05b: latest_actual_as_of_date aggregates over the investment universe
# ---------------------------------------------------------------------------


async def test_in05b_latest_actual_as_of_date_over_universe(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The ADR-0113 §1 universe as-of: max actual date, plan rows ignored."""
    tenant_id = await seed_tenant()
    actor, first = await _seed_investment(
        app_engine, tenant_id, email="in05b@example.com", investment_name="Fund One"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        second = await InvestmentRepository(session).create(
            name="Fund Two",
            investment_type="private_equity",
            asset_class_id=first.asset_class_id,
            currency="EUR",
            created_by=actor.id,
        )
        repo = InvestmentNavRepository(session)
        # The stale investment stops in June; the fresh one runs to
        # September; a plan row sits beyond both and must not win.
        await repo.upsert(
            investment_id=first.id,
            as_of_date=date(2025, 6, 30),
            nav_kind="actual",
            nav_value=Decimal("1000"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        await repo.upsert(
            investment_id=second.id,
            as_of_date=date(2025, 9, 30),
            nav_kind="actual",
            nav_value=Decimal("2000"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        await repo.upsert(
            investment_id=second.id,
            as_of_date=date(2026, 12, 31),
            nav_kind="plan",
            nav_value=Decimal("9999"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentNavRepository(session)
        assert await repo.latest_actual_as_of_date([first.id, second.id]) == date(2025, 9, 30)
        # Restricting the universe moves the as-of back to that subset.
        assert await repo.latest_actual_as_of_date([first.id]) == date(2025, 6, 30)


# ---------------------------------------------------------------------------
# IN-05c: latest_actual_as_of_date returns None for empty / actual-free input
# ---------------------------------------------------------------------------


async def test_in05c_latest_actual_as_of_date_returns_none(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="in05c@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentNavRepository(session).upsert(
            investment_id=investment.id,
            as_of_date=date(2026, 6, 30),
            nav_kind="plan",
            nav_value=Decimal("1000"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentNavRepository(session)
        # An empty universe short-circuits before the query.
        assert await repo.latest_actual_as_of_date([]) is None
        # A universe carrying plan rows only has no actual frontier.
        assert await repo.latest_actual_as_of_date([investment.id]) is None


# ---------------------------------------------------------------------------
# IN-06: delete_by_investment reports rowcount
# ---------------------------------------------------------------------------


async def test_in06_delete_by_investment_reports_rowcount(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="in06@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentNavRepository(session)
        for d in (date(2025, 1, 1), date(2025, 2, 1), date(2025, 3, 1)):
            await repo.upsert(
                investment_id=investment.id,
                as_of_date=d,
                nav_kind="actual",
                nav_value=Decimal("1"),
                currency="EUR",
                source=None,
                created_by=actor.id,
            )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        deleted = await InvestmentNavRepository(session).delete_by_investment(investment.id)
    assert deleted == 3

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentNavRepository(session).list_by_investment(investment.id)
    assert rows == []


# ---------------------------------------------------------------------------
# IN-07: delete reports True/False on rowcount
# ---------------------------------------------------------------------------


async def test_in07_delete_reports_rowcount_signal(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="in07@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        nav = await InvestmentNavRepository(session).upsert(
            investment_id=investment.id,
            as_of_date=date(2025, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("1"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        first = await InvestmentNavRepository(session).delete(nav.id)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        second = await InvestmentNavRepository(session).delete(nav.id)

    assert first is True
    assert second is False


# ---------------------------------------------------------------------------
# IN-08: cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_in08_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    actor_a, inv_a = await _seed_investment(app_engine, tenant_a, email="a@example.com")
    actor_b, inv_b = await _seed_investment(app_engine, tenant_b, email="b@example.com")

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await InvestmentNavRepository(session).upsert(
            investment_id=inv_a.id,
            as_of_date=date(2025, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("100"),
            currency="EUR",
            source=None,
            created_by=actor_a.id,
        )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await InvestmentNavRepository(session).upsert(
            investment_id=inv_b.id,
            as_of_date=date(2025, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("200"),
            currency="EUR",
            source=None,
            created_by=actor_b.id,
        )

    async with tenant_context(app_engine, tenant_a) as session:
        a_view = await InvestmentNavRepository(session).list_by_investment(inv_a.id)
        # Tenant A cannot see Tenant B's NAVs.
        a_cross = await InvestmentNavRepository(session).list_by_investment(inv_b.id)
    async with tenant_context(app_engine, tenant_b) as session:
        b_view = await InvestmentNavRepository(session).list_by_investment(inv_b.id)

    assert [n.nav_value for n in a_view] == [Decimal("100")]
    assert a_cross == []
    assert [n.nav_value for n in b_view] == [Decimal("200")]


# ---------------------------------------------------------------------------
# IN-09: invalid nav_kind rejected by CHECK
# ---------------------------------------------------------------------------


async def test_in09_invalid_nav_kind_raises(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="in09@example.com")

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await InvestmentNavRepository(session).upsert(
                investment_id=investment.id,
                as_of_date=date(2025, 12, 31),
                nav_kind="bogus",
                nav_value=Decimal("1"),
                currency="EUR",
                source=None,
                created_by=actor.id,
            )


# ---------------------------------------------------------------------------
# Batched plural methods (P6-H)
# ---------------------------------------------------------------------------


async def _seed_three_investments_with_navs(app_engine: AsyncEngine, tenant_id, *, email: str):
    """Seed one actor, one asset class and three investments with NAV rows.

    Returns ``(actor, [inv_a, inv_b, inv_c])`` so callers can drive
    the plural methods against an explicit id list.
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
        nav_repo = InvestmentNavRepository(session)
        # Inv A: two actuals + one plan.
        await nav_repo.upsert(
            investment_id=inv_a.id,
            as_of_date=date(2024, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("100"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        await nav_repo.upsert(
            investment_id=inv_a.id,
            as_of_date=date(2025, 6, 30),
            nav_kind="actual",
            nav_value=Decimal("110"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        await nav_repo.upsert(
            investment_id=inv_a.id,
            as_of_date=date(2026, 6, 30),
            nav_kind="plan",
            nav_value=Decimal("130"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        # Inv B: one actual.
        await nav_repo.upsert(
            investment_id=inv_b.id,
            as_of_date=date(2025, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("200"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        # Inv C: empty (no NAV rows at all).
    return actor, [inv_a, inv_b, inv_c]


async def test_in10_list_by_investments_matches_singular(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Batched fetch returns the same per-investment rows as singular calls."""
    tenant_id = await seed_tenant()
    _actor, invs = await _seed_three_investments_with_navs(
        app_engine, tenant_id, email="in10@example.com"
    )
    inv_a, inv_b, inv_c = invs

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentNavRepository(session)
        singular = {
            inv_a.id: await repo.list_by_investment(inv_a.id),
            inv_b.id: await repo.list_by_investment(inv_b.id),
            inv_c.id: await repo.list_by_investment(inv_c.id),
        }
        batched = await repo.list_by_investments([inv_a.id, inv_b.id, inv_c.id])

    assert set(batched.keys()) == {inv_a.id, inv_b.id, inv_c.id}
    for inv_id, rows in singular.items():
        assert batched[inv_id] == rows
    # Inv A contract: rows sorted by as_of_date ascending within the
    # per-investment list (covers plan + actual interleaved).
    assert [n.as_of_date for n in batched[inv_a.id]] == [
        date(2024, 12, 31),
        date(2025, 6, 30),
        date(2026, 6, 30),
    ]


async def test_in11_list_by_investments_empty_input_returns_empty_dict(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        result = await InvestmentNavRepository(session).list_by_investments([])
    assert result == {}


async def test_in12_list_by_investments_missing_id_maps_to_empty_list(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An id with no rows is still present in the result mapping."""
    tenant_id = await seed_tenant()
    _actor, invs = await _seed_three_investments_with_navs(
        app_engine, tenant_id, email="in12@example.com"
    )
    inv_a, inv_b, _inv_c = invs
    fresh_id = uuid4()

    async with tenant_context(app_engine, tenant_id) as session:
        result = await InvestmentNavRepository(session).list_by_investments(
            [inv_a.id, inv_b.id, fresh_id]
        )

    assert set(result.keys()) == {inv_a.id, inv_b.id, fresh_id}
    assert result[fresh_id] == []
    assert len(result[inv_a.id]) == 3
    assert len(result[inv_b.id]) == 1


async def test_in13_list_by_investments_and_kind_filters(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Kind-filtered batched fetch returns only rows of the requested kind."""
    tenant_id = await seed_tenant()
    _actor, invs = await _seed_three_investments_with_navs(
        app_engine, tenant_id, email="in13@example.com"
    )
    inv_a, inv_b, inv_c = invs

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentNavRepository(session)
        actuals = await repo.list_by_investments_and_kind([inv_a.id, inv_b.id, inv_c.id], "actual")
        plans = await repo.list_by_investments_and_kind([inv_a.id, inv_b.id, inv_c.id], "plan")

    # Inv A has two actuals, Inv B has one, Inv C has none.
    assert [n.nav_value for n in actuals[inv_a.id]] == [
        Decimal("100.0000"),
        Decimal("110.0000"),
    ]
    assert [n.nav_value for n in actuals[inv_b.id]] == [Decimal("200.0000")]
    assert actuals[inv_c.id] == []
    # Inv A has one plan, others none.
    assert [n.nav_value for n in plans[inv_a.id]] == [Decimal("130.0000")]
    assert plans[inv_b.id] == []
    assert plans[inv_c.id] == []
    # Empty list still returns empty dict.
    async with tenant_context(app_engine, tenant_id) as session:
        empty = await InvestmentNavRepository(session).list_by_investments_and_kind([], "actual")
    assert empty == {}


# ---------------------------------------------------------------------------
# IN-14: delete_live_navs removes only this investment's 'live'-origin rows
# ---------------------------------------------------------------------------


async def test_in14_delete_live_navs_is_origin_and_investment_scoped(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The mode-flip cleanup (ADR-0097 §6) never reaches another origin.

    ``'excel'`` and ``'manual'`` rows carry precedence over any computed or
    provider write, and ``'system'`` rows are the materialisation's own. Only
    the flipped investment's ``'live'`` rows — the F1 defect artifacts — are
    deletion candidates. Runs under the unprivileged ``portfoliflow_app``
    role, so RLS is live for every statement.
    """
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="in14@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        other = await InvestmentRepository(session).create(
            name="Second Fund",
            investment_type="listed_equity",
            asset_class_id=investment.asset_class_id,
            currency="EUR",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentNavRepository(session)
        for offset, origin in enumerate(("excel", "live", "manual", "system")):
            await repo.upsert(
                investment_id=investment.id,
                as_of_date=date(2026, 1, 1 + offset),
                nav_kind="actual",
                nav_value=Decimal("100"),
                currency="EUR",
                source=None,
                created_by=actor.id,
                ingest_origin=origin,
            )
        # A 'live' row on a *different* investment must survive.
        await repo.upsert(
            investment_id=other.id,
            as_of_date=date(2026, 1, 2),
            nav_kind="actual",
            nav_value=Decimal("55"),
            currency="EUR",
            source=None,
            created_by=actor.id,
            ingest_origin="live",
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        deleted = await InvestmentNavRepository(session).delete_live_navs(investment.id)
    assert deleted == 1

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentNavRepository(session)
        survivors = {row.ingest_origin for row in await repo.list_by_investment(investment.id)}
        assert survivors == {"excel", "manual", "system"}
        assert len(await repo.list_by_investment(other.id)) == 1


async def test_in15_delete_live_navs_on_clean_investment_is_zero(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A flip on an investment the live ingest never touched deletes nothing."""
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="in15@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentNavRepository(session).upsert(
            investment_id=investment.id,
            as_of_date=date(2026, 1, 1),
            nav_kind="actual",
            nav_value=Decimal("100"),
            currency="EUR",
            source=None,
            created_by=actor.id,
            ingest_origin="excel",
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        assert await InvestmentNavRepository(session).delete_live_navs(investment.id) == 0
