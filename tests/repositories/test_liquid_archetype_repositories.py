# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Liquid-archetype data-layer tests against the live compose Postgres.

Covers the per-investment schema introduced by ADR-0079 §2 — the
three new time-series tables (``investment_bond_analytics``,
``investment_rating_weight``, ``investment_maturity_weight``) and the
additive ``investment_navs.basis`` column — across the test groups
the implementation prompt enumerates:

1. Taxonomy constraints: out-of-taxonomy ``rating_bucket`` /
   ``maturity_bucket`` and out-of-range ``weight_pct`` raise
   ``IntegrityError``.
2. ``basis`` constraint: ``basis='bogus'`` is rejected on all four
   tables; ``basis=NULL`` is accepted on ``investment_navs`` and
   rejected (NOT NULL) on the three new tables.
3. Negative-yield acceptance: ``investment_bond_analytics`` accepts
   ``ytm = -0.005`` with ``oas`` / ``convexity`` NULL.
4. Natural-key uniqueness: a duplicate natural key raises
   ``IntegrityError``.
5. RLS isolation: rows written under one tenant are invisible under
   another.
6. Repository round-trip: ``list_by_investments`` returns batched,
   ordered, ``as_of_cutoff``-truncated results across investments.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.models.investment_bond_analytics import InvestmentBondAnalytics
from core.models.investment_maturity_weight import InvestmentMaturityWeight
from core.models.investment_nav import InvestmentNav
from core.models.investment_rating_weight import InvestmentRatingWeight
from core.repositories import (
    AssetClassRepository,
    InvestmentBondAnalyticsRepository,
    InvestmentMaturityWeightsRepository,
    InvestmentRatingWeightsRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)


async def _seed_investment(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    investment_name: str = "IG Credit Fund",
):
    """Create one user, one asset class, and one listed-bonds investment."""
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(code="credit", display_name="Credit")
        investment = await InvestmentRepository(session).create(
            name=investment_name,
            investment_type="listed_bonds",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
    return actor, investment


# ---------------------------------------------------------------------------
# Group 1: taxonomy constraints
# ---------------------------------------------------------------------------


async def test_la01_invalid_rating_bucket_raises(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la01@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            session.add(
                InvestmentRatingWeight(
                    tenant_id=tenant_id,
                    investment_id=inv.id,
                    as_of_date=date(2025, 12, 31),
                    rating_bucket="AA+",  # not in the taxonomy
                    weight_pct=Decimal("10.0000"),
                    basis="reported",
                    ingest_origin="excel",
                    created_by=actor.id,
                )
            )
            await session.flush()


async def test_la02_invalid_maturity_bucket_raises(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la02@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            session.add(
                InvestmentMaturityWeight(
                    tenant_id=tenant_id,
                    investment_id=inv.id,
                    as_of_date=date(2025, 12, 31),
                    maturity_bucket="2-4y",  # not in the taxonomy
                    weight_pct=Decimal("10.0000"),
                    basis="reported",
                    ingest_origin="excel",
                    created_by=actor.id,
                )
            )
            await session.flush()


@pytest.mark.parametrize("bad_weight", [Decimal("-0.0001"), Decimal("100.0001")])
async def test_la03_rating_weight_out_of_range_raises(
    app_engine: AsyncEngine, seed_tenant, bad_weight: Decimal
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email=f"la03-{bad_weight}@example.com"
    )
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            session.add(
                InvestmentRatingWeight(
                    tenant_id=tenant_id,
                    investment_id=inv.id,
                    as_of_date=date(2025, 12, 31),
                    rating_bucket="A",
                    weight_pct=bad_weight,
                    basis="reported",
                    ingest_origin="excel",
                    created_by=actor.id,
                )
            )
            await session.flush()


async def test_la04_maturity_weight_out_of_range_raises(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la04@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            session.add(
                InvestmentMaturityWeight(
                    tenant_id=tenant_id,
                    investment_id=inv.id,
                    as_of_date=date(2025, 12, 31),
                    maturity_bucket="3-5y",
                    weight_pct=Decimal("150.0000"),
                    basis="reported",
                    ingest_origin="excel",
                    created_by=actor.id,
                )
            )
            await session.flush()


# ---------------------------------------------------------------------------
# Group 2: basis constraint
# ---------------------------------------------------------------------------


async def test_la05_bogus_basis_rejected_on_bond_analytics(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la05@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            session.add(
                InvestmentBondAnalytics(
                    tenant_id=tenant_id,
                    investment_id=inv.id,
                    as_of_date=date(2025, 12, 31),
                    ytm=Decimal("0.042000"),
                    eff_duration=Decimal("4.500"),
                    oas=None,
                    convexity=None,
                    basis="bogus",
                    created_by=actor.id,
                )
            )
            await session.flush()


async def test_la06_bogus_basis_rejected_on_rating_weight(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la06@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            session.add(
                InvestmentRatingWeight(
                    tenant_id=tenant_id,
                    investment_id=inv.id,
                    as_of_date=date(2025, 12, 31),
                    rating_bucket="A",
                    weight_pct=Decimal("10.0000"),
                    basis="bogus",
                    ingest_origin="excel",
                    created_by=actor.id,
                )
            )
            await session.flush()


async def test_la07_bogus_basis_rejected_on_maturity_weight(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la07@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            session.add(
                InvestmentMaturityWeight(
                    tenant_id=tenant_id,
                    investment_id=inv.id,
                    as_of_date=date(2025, 12, 31),
                    maturity_bucket="3-5y",
                    weight_pct=Decimal("10.0000"),
                    basis="bogus",
                    ingest_origin="excel",
                    created_by=actor.id,
                )
            )
            await session.flush()


async def test_la08_bogus_basis_rejected_on_investment_navs(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la08@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            session.add(
                InvestmentNav(
                    tenant_id=tenant_id,
                    investment_id=inv.id,
                    as_of_date=date(2025, 12, 31),
                    nav_value=Decimal("100.0000"),
                    currency="EUR",
                    nav_kind="actual",
                    source=None,
                    basis="bogus",
                    ingest_origin="excel",
                    created_by=actor.id,
                )
            )
            await session.flush()


async def test_la09_null_basis_accepted_on_investment_navs(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """NULL basis ⇒ treated as 'reported' downstream; persisted as NULL."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la09@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        nav = InvestmentNav(
            tenant_id=tenant_id,
            investment_id=inv.id,
            as_of_date=date(2025, 12, 31),
            nav_value=Decimal("100.0000"),
            currency="EUR",
            nav_kind="actual",
            source=None,
            basis=None,
            ingest_origin="excel",
            created_by=actor.id,
        )
        session.add(nav)
        await session.flush()
        assert nav.basis is None


@pytest.mark.parametrize(
    "model_factory",
    [
        lambda tid, inv, actor: InvestmentBondAnalytics(
            tenant_id=tid,
            investment_id=inv,
            as_of_date=date(2025, 12, 31),
            ytm=Decimal("0.042000"),
            eff_duration=Decimal("4.500"),
            oas=None,
            convexity=None,
            basis=None,
            created_by=actor,
        ),
        lambda tid, inv, actor: InvestmentRatingWeight(
            tenant_id=tid,
            investment_id=inv,
            as_of_date=date(2025, 12, 31),
            rating_bucket="A",
            weight_pct=Decimal("10.0000"),
            basis=None,
            ingest_origin="excel",
            created_by=actor,
        ),
        lambda tid, inv, actor: InvestmentMaturityWeight(
            tenant_id=tid,
            investment_id=inv,
            as_of_date=date(2025, 12, 31),
            maturity_bucket="3-5y",
            weight_pct=Decimal("10.0000"),
            basis=None,
            ingest_origin="excel",
            created_by=actor,
        ),
    ],
    ids=["bond_analytics", "rating_weight", "maturity_weight"],
)
async def test_la10_null_basis_rejected_on_new_tables(
    app_engine: AsyncEngine, seed_tenant, model_factory
) -> None:
    """``basis`` is NOT NULL on the three new tables (unlike navs)."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la10@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            session.add(model_factory(tenant_id, inv.id, actor.id))
            await session.flush()


# ---------------------------------------------------------------------------
# Group 3: negative-yield acceptance
# ---------------------------------------------------------------------------


async def test_la11_negative_yield_and_null_optionals_accepted(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A negative YTM with NULL oas/convexity is valid (EUR govvies)."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la11@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        dto = await InvestmentBondAnalyticsRepository(session).upsert(
            investment_id=inv.id,
            as_of_date=date(2025, 12, 31),
            ytm=Decimal("-0.005000"),
            eff_duration=Decimal("6.250"),
            oas=None,
            convexity=None,
            basis="reported",
            created_by=actor.id,
        )
    assert dto.ytm == Decimal("-0.005000")
    assert dto.oas is None
    assert dto.convexity is None


# ---------------------------------------------------------------------------
# Group 4: natural-key uniqueness
# ---------------------------------------------------------------------------


async def test_la12_bond_analytics_duplicate_natural_key_raises(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la12@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            for _ in range(2):
                session.add(
                    InvestmentBondAnalytics(
                        tenant_id=tenant_id,
                        investment_id=inv.id,
                        as_of_date=date(2025, 12, 31),
                        ytm=Decimal("0.042000"),
                        eff_duration=Decimal("4.500"),
                        oas=None,
                        convexity=None,
                        basis="reported",
                        created_by=actor.id,
                    )
                )
            await session.flush()


async def test_la13_rating_weight_duplicate_natural_key_raises(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la13@example.com")
    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            for _ in range(2):
                session.add(
                    InvestmentRatingWeight(
                        tenant_id=tenant_id,
                        investment_id=inv.id,
                        as_of_date=date(2025, 12, 31),
                        rating_bucket="BBB",
                        weight_pct=Decimal("25.0000"),
                        basis="reported",
                        ingest_origin="excel",
                        created_by=actor.id,
                    )
                )
            await session.flush()


async def test_la14_rating_weight_distinct_buckets_coexist(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Same investment + date, different bucket ⇒ two distinct rows."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la14@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRatingWeightsRepository(session)
        await repo.upsert(
            inv.id,
            date(2025, 12, 31),
            "AAA",
            weight_pct=Decimal("30.0000"),
            basis="reported",
            created_by=actor.id,
        )
        await repo.upsert(
            inv.id,
            date(2025, 12, 31),
            "AA",
            weight_pct=Decimal("70.0000"),
            basis="reported",
            created_by=actor.id,
        )
    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentRatingWeightsRepository(session).list_for_investment(inv.id)
    assert [(r.rating_bucket, r.weight_pct) for r in rows] == [
        ("AA", Decimal("70.0000")),
        ("AAA", Decimal("30.0000")),
    ]


# ---------------------------------------------------------------------------
# Group 5: RLS isolation
# ---------------------------------------------------------------------------


async def test_la15_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")
    actor_a, inv_a = await _seed_investment(app_engine, tenant_a, email="la15a@example.com")
    actor_b, inv_b = await _seed_investment(app_engine, tenant_b, email="la15b@example.com")

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await InvestmentBondAnalyticsRepository(session).upsert(
            investment_id=inv_a.id,
            as_of_date=date(2025, 12, 31),
            ytm=Decimal("0.040000"),
            eff_duration=Decimal("3.000"),
            oas=Decimal("0.012000"),
            convexity=Decimal("0.150"),
            basis="reported",
            created_by=actor_a.id,
        )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await InvestmentBondAnalyticsRepository(session).upsert(
            investment_id=inv_b.id,
            as_of_date=date(2025, 12, 31),
            ytm=Decimal("0.050000"),
            eff_duration=Decimal("5.000"),
            oas=None,
            convexity=None,
            basis="reported",
            created_by=actor_b.id,
        )

    async with tenant_context(app_engine, tenant_a) as session:
        repo = InvestmentBondAnalyticsRepository(session)
        a_view = await repo.list_for_investment(inv_a.id)
        a_cross = await repo.list_for_investment(inv_b.id)
    async with tenant_context(app_engine, tenant_b) as session:
        b_view = await InvestmentBondAnalyticsRepository(session).list_for_investment(inv_b.id)

    assert [r.ytm for r in a_view] == [Decimal("0.040000")]
    assert a_cross == []  # Tenant A cannot see Tenant B's row.
    assert [r.ytm for r in b_view] == [Decimal("0.050000")]


# ---------------------------------------------------------------------------
# Group 6: repository round-trip (batched, ordered, cutoff-truncated)
# ---------------------------------------------------------------------------


async def _seed_two_investments(app_engine: AsyncEngine, tenant_id, *, email):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(code="credit", display_name="Credit")
        inv_repo = InvestmentRepository(session)
        inv_a = await inv_repo.create(
            name="Alpha Bond Fund",
            investment_type="listed_bonds",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        inv_b = await inv_repo.create(
            name="Beta Bond Fund",
            investment_type="listed_bonds",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
    return actor, inv_a, inv_b


async def test_la16_bond_analytics_list_by_investments_batched_ordered(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv_a, inv_b = await _seed_two_investments(
        app_engine, tenant_id, email="la16@example.com"
    )
    dates = [date(2024, 12, 31), date(2025, 6, 30), date(2025, 12, 31)]

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentBondAnalyticsRepository(session)
        # Insert out of order for inv_a; inv_b gets a single row.
        for d in (dates[2], dates[0], dates[1]):
            await repo.upsert(
                investment_id=inv_a.id,
                as_of_date=d,
                ytm=Decimal("0.040000"),
                eff_duration=Decimal("4.000"),
                oas=None,
                convexity=None,
                basis="reported",
                created_by=actor.id,
            )
        await repo.upsert(
            investment_id=inv_b.id,
            as_of_date=dates[1],
            ytm=Decimal("0.030000"),
            eff_duration=Decimal("2.000"),
            oas=None,
            convexity=None,
            basis="reported",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentBondAnalyticsRepository(session)
        batched = await repo.list_by_investments([inv_a.id, inv_b.id])
        truncated = await repo.list_by_investments(
            [inv_a.id, inv_b.id], as_of_cutoff=date(2025, 6, 30)
        )
        empty = await repo.list_by_investments([])

    # Both ids present; inv_a rows sorted ascending by as_of_date.
    assert set(batched.keys()) == {inv_a.id, inv_b.id}
    assert [r.as_of_date for r in batched[inv_a.id]] == dates
    assert [r.as_of_date for r in batched[inv_b.id]] == [dates[1]]
    # Cutoff truncates inv_a to the two on-or-before 2025-06-30; inv_b
    # keeps its single row, which is exactly on the cutoff.
    assert [r.as_of_date for r in truncated[inv_a.id]] == dates[:2]
    assert [r.as_of_date for r in truncated[inv_b.id]] == [dates[1]]
    assert empty == {}


async def test_la17_rating_weight_round_trip_cutoff(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv_a, inv_b = await _seed_two_investments(
        app_engine, tenant_id, email="la17@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRatingWeightsRepository(session)
        # inv_a: two snapshots, each two buckets.
        for d in (date(2024, 12, 31), date(2025, 12, 31)):
            await repo.upsert(
                inv_a.id,
                d,
                "AAA",
                weight_pct=Decimal("40.0000"),
                basis="reported",
                created_by=actor.id,
            )
            await repo.upsert(
                inv_a.id,
                d,
                "AA",
                weight_pct=Decimal("60.0000"),
                basis="reported",
                created_by=actor.id,
            )
        # inv_b: one snapshot, one bucket.
        await repo.upsert(
            inv_b.id,
            date(2025, 12, 31),
            "BBB",
            weight_pct=Decimal("100.0000"),
            basis="reported",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentRatingWeightsRepository(session)
        batched = await repo.list_by_investments([inv_a.id, inv_b.id])
        truncated = await repo.list_by_investments(
            [inv_a.id, inv_b.id], as_of_cutoff=date(2024, 12, 31)
        )

    # inv_a: ordered by (as_of_date, rating_bucket); four rows total.
    assert [(r.as_of_date, r.rating_bucket) for r in batched[inv_a.id]] == [
        (date(2024, 12, 31), "AA"),
        (date(2024, 12, 31), "AAA"),
        (date(2025, 12, 31), "AA"),
        (date(2025, 12, 31), "AAA"),
    ]
    assert [r.rating_bucket for r in batched[inv_b.id]] == ["BBB"]
    # Cutoff keeps only the 2024 snapshot for inv_a and drops inv_b.
    assert [(r.as_of_date, r.rating_bucket) for r in truncated[inv_a.id]] == [
        (date(2024, 12, 31), "AA"),
        (date(2024, 12, 31), "AAA"),
    ]
    assert truncated[inv_b.id] == []


async def test_la18_upsert_updates_in_place(app_engine: AsyncEngine, seed_tenant) -> None:
    """Re-upserting the same natural key updates rather than inserts."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la18@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentMaturityWeightsRepository(session)
        first = await repo.upsert(
            inv.id,
            date(2025, 12, 31),
            "3-5y",
            weight_pct=Decimal("20.0000"),
            basis="reported",
            created_by=actor.id,
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentMaturityWeightsRepository(session)
        second = await repo.upsert(
            inv.id,
            date(2025, 12, 31),
            "3-5y",
            weight_pct=Decimal("35.0000"),
            basis="computed",
            created_by=actor.id,
        )
    assert first.id == second.id
    assert second.weight_pct == Decimal("35.0000")
    assert second.basis == "computed"

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentMaturityWeightsRepository(session).list_for_investment(inv.id)
    assert len(rows) == 1
    assert rows[0].weight_pct == Decimal("35.0000")


async def test_la19_delete_for_investment_clears_all_dates(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """``delete_for_investment`` purges every row of one investment only.

    Mirrors the replace-by-investment idempotency the importer relies on
    (ADR-0081 §D): a second investment's rows are untouched.
    """
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="la19@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        other = await InvestmentRepository(session).create(
            name="Other Credit Fund",
            investment_type="listed_bonds",
            asset_class_id=inv.asset_class_id,
            currency="EUR",
            created_by=actor.id,
        )
        ba_repo = InvestmentBondAnalyticsRepository(session)
        rw_repo = InvestmentRatingWeightsRepository(session)
        mw_repo = InvestmentMaturityWeightsRepository(session)
        # Two statement days for the target investment across all three
        # tables, plus a row for the other investment as a guard.
        for d in (date(2025, 1, 31), date(2025, 2, 28)):
            await ba_repo.upsert(
                inv.id,
                d,
                ytm=Decimal("0.04"),
                eff_duration=Decimal("3.0"),
                oas=None,
                convexity=None,
                basis="reported",
                created_by=actor.id,
            )
            await rw_repo.upsert(
                inv.id,
                d,
                "AAA",
                weight_pct=Decimal("100.0000"),
                basis="reported",
                created_by=actor.id,
            )
            await mw_repo.upsert(
                inv.id,
                d,
                "1-3y",
                weight_pct=Decimal("100.0000"),
                basis="reported",
                created_by=actor.id,
            )
        await ba_repo.upsert(
            other.id,
            date(2025, 1, 31),
            ytm=Decimal("0.03"),
            eff_duration=Decimal("2.0"),
            oas=None,
            convexity=None,
            basis="reported",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        n_ba = await InvestmentBondAnalyticsRepository(session).delete_for_investment(inv.id)
        n_rw = await InvestmentRatingWeightsRepository(session).delete_for_investment(inv.id)
        n_mw = await InvestmentMaturityWeightsRepository(session).delete_for_investment(inv.id)
    assert (n_ba, n_rw, n_mw) == (2, 2, 2)

    async with tenant_context(app_engine, tenant_id) as session:
        assert await InvestmentBondAnalyticsRepository(session).list_for_investment(inv.id) == []
        assert await InvestmentRatingWeightsRepository(session).list_for_investment(inv.id) == []
        assert await InvestmentMaturityWeightsRepository(session).list_for_investment(inv.id) == []
        # The other investment's row survives.
        other_ba = await InvestmentBondAnalyticsRepository(session).list_for_investment(other.id)
        assert len(other_ba) == 1
