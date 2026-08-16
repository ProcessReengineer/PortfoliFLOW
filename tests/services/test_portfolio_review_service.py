# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Integration test for ``PortfolioReviewService``.

Live-DB end-to-end: seeds investments with NAV / cashflow histories,
calls the two service methods, asserts the bundle shapes, header
metric values, and cross-tenant isolation.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    FxRateRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    TenantRepository,
    UserRepository,
    tenant_context,
)
from core.repositories.investment_region_weights_repository import (
    InvestmentRegionWeightsRepository,
    RegionWeightInput,
)
from core.repositories.investment_sector_weights_repository import (
    InvestmentSectorWeightsRepository,
)
from core.repositories.region_repository import RegionRepository
from core.repositories.sector_repository import SectorRepository
from services.portfolio_review import (
    PortfolioOverviewBundle,
    PortfolioReviewService,
    SingleInvestmentReviewBundle,
)


def _build_service(session) -> PortfolioReviewService:
    return PortfolioReviewService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        region_weights=InvestmentRegionWeightsRepository(session),
        sector_weights=InvestmentSectorWeightsRepository(session),
        regions=RegionRepository(session),
        sectors=SectorRepository(session),
        tenants=TenantRepository(session),
        fx_rates=FxRateRepository(session),
    )


async def _seed_actor_and_asset_class(app_engine: AsyncEngine, tenant_id, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="pr_class", display_name="PR Class"
        )
    return actor, asset_class


async def _create_investment_with_data(
    session,
    actor_id,
    asset_class_id,
    *,
    name: str,
    vintage_year: int | None = 2020,
    nav_values: list[tuple[date, Decimal]] | None = None,
    cashflows: list[tuple[datetime, str, Decimal]] | None = None,
):
    inv = await InvestmentRepository(session).create(
        name=name,
        investment_type="private_equity",
        asset_class_id=asset_class_id,
        currency="EUR",
        created_by=actor_id,
        vintage_year=vintage_year,
    )
    nav_repo = InvestmentNavRepository(session)
    for as_of, value in nav_values or []:
        await nav_repo.upsert(
            investment_id=inv.id,
            as_of_date=as_of,
            nav_kind="actual",
            nav_value=value,
            currency="EUR",
            source=None,
            created_by=actor_id,
        )
    cf_repo = InvestmentCashflowRepository(session)
    for ts, flow_type, amount in cashflows or []:
        await cf_repo.create(
            investment_id=inv.id,
            flow_timestamp=ts,
            flow_type=flow_type,
            flow_kind="actual",
            amount=amount,
            currency="EUR",
            description=None,
            created_by=actor_id,
        )
    return inv


# ---------------------------------------------------------------------------
# Tests — Portfolio Overview
# ---------------------------------------------------------------------------


async def test_portfolio_overview_returns_bundle(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pr-overview@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment_with_data(
            session,
            actor.id,
            ac.id,
            name="Alpha",
            vintage_year=2018,
            nav_values=[
                (date(2023, 12, 31), Decimal("100")),
                (date(2024, 12, 31), Decimal("150")),
            ],
            cashflows=[
                (
                    datetime(2023, 1, 15, tzinfo=timezone.utc),
                    "capital_call",
                    Decimal("-100"),
                ),
                (
                    datetime(2024, 6, 15, tzinfo=timezone.utc),
                    "distribution",
                    Decimal("20"),
                ),
            ],
        )
        await _create_investment_with_data(
            session,
            actor.id,
            ac.id,
            name="Beta",
            vintage_year=2020,
            nav_values=[
                (date(2024, 12, 31), Decimal("200")),
            ],
            cashflows=[
                (
                    datetime(2024, 1, 15, tzinfo=timezone.utc),
                    "capital_call",
                    Decimal("-200"),
                ),
            ],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _build_service(session)
        bundle = await service.get_portfolio_overview(date(2024, 12, 31))

    assert isinstance(bundle, PortfolioOverviewBundle)
    assert bundle.investment_count == 2
    assert bundle.as_of_date == date(2024, 12, 31)
    # Header NAV = 150 + 200 = 350.
    assert bundle.header_metrics.nav_eur == pytest.approx(350.0)
    # TVPI = (150 + 200 + 20) / 300 = 1.2333...
    assert bundle.header_metrics.tvpi == pytest.approx(370.0 / 300.0)
    # DPI = 20 / 300.
    assert bundle.header_metrics.dpi == pytest.approx(20.0 / 300.0)
    # Year range covers 2023..2024.
    assert bundle.invested_capital_nav.years == [2023, 2024]
    # Vintage distribution sees both vintages.
    assert sorted(bundle.vintage_distribution.vintages) == [2018, 2020]


async def test_portfolio_overview_empty_universe_returns_none(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, _ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pr-empty@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).get_portfolio_overview()
    assert bundle is None


async def test_cross_tenant_isolation_overview(app_engine: AsyncEngine, seed_tenant) -> None:
    """Tenant A's session sees no Tenant B investments."""
    tenant_a = await seed_tenant("A")
    tenant_b = await seed_tenant("B")
    actor_a, _ = await _seed_actor_and_asset_class(app_engine, tenant_a, email="pr-a@example.com")
    actor_b, ac_b = await _seed_actor_and_asset_class(
        app_engine, tenant_b, email="pr-b@example.com"
    )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await _create_investment_with_data(
            session,
            actor_b.id,
            ac_b.id,
            name="Tenant-B Inv",
            nav_values=[(date(2024, 12, 31), Decimal("500"))],
            cashflows=[
                (
                    datetime(2024, 1, 1, tzinfo=timezone.utc),
                    "capital_call",
                    Decimal("-400"),
                ),
            ],
        )

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        bundle_a = await _build_service(session).get_portfolio_overview()
    assert bundle_a is None  # Tenant A sees nothing.

    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        bundle_b = await _build_service(session).get_portfolio_overview()
    assert bundle_b is not None
    assert bundle_b.investment_count == 1
    assert bundle_b.header_metrics.nav_eur == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# Tests — Single Investment Review
# ---------------------------------------------------------------------------


async def test_single_investment_review_returns_bundle(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pr-single@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        inv = await _create_investment_with_data(
            session,
            actor.id,
            ac.id,
            name="Solo",
            vintage_year=2018,
            nav_values=[
                (date(2023, 12, 31), Decimal("100")),
                (date(2024, 12, 31), Decimal("150")),
            ],
            cashflows=[
                (
                    datetime(2023, 1, 15, tzinfo=timezone.utc),
                    "capital_call",
                    Decimal("-100"),
                ),
                (
                    datetime(2024, 6, 15, tzinfo=timezone.utc),
                    "distribution",
                    Decimal("20"),
                ),
            ],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _build_service(session)
        bundle = await service.get_single_investment_review(inv.id, date(2024, 12, 31))

    assert isinstance(bundle, SingleInvestmentReviewBundle)
    assert bundle.investment.name == "Solo"
    assert bundle.header_metrics.nav_eur == pytest.approx(150.0)
    assert bundle.header_metrics.dpi == pytest.approx(20.0 / 100.0)
    assert bundle.header_metrics.tvpi == pytest.approx(170.0 / 100.0)
    # Total Return Index has at least one observation
    # (NAV pct_change → one return → one index point).
    assert not bundle.total_return_index.empty


async def test_single_investment_review_unknown_id_returns_none(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    from uuid import uuid4

    tenant_id = await seed_tenant()
    actor, _ = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pr-unknown@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).get_single_investment_review(uuid4())
    assert bundle is None


async def test_single_investment_review_cross_tenant_returns_none(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An investment in tenant B is invisible to tenant A's session."""
    tenant_a = await seed_tenant("A")
    tenant_b = await seed_tenant("B")
    actor_a, _ = await _seed_actor_and_asset_class(
        app_engine, tenant_a, email="pr-iso-a@example.com"
    )
    actor_b, ac_b = await _seed_actor_and_asset_class(
        app_engine, tenant_b, email="pr-iso-b@example.com"
    )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        inv_b = await _create_investment_with_data(
            session,
            actor_b.id,
            ac_b.id,
            name="Isolated",
            nav_values=[(date(2024, 12, 31), Decimal("100"))],
        )
    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        bundle = await _build_service(session).get_single_investment_review(inv_b.id)
    assert bundle is None


# ---------------------------------------------------------------------------
# P6-H batching regression guard
# ---------------------------------------------------------------------------


async def test_get_portfolio_overview_uses_batched_repository_methods(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Per-repository call counts must remain at-most-one per universe load.

    Guards against re-introducing the per-investment N+1 loops removed
    in P6-H. The four target plural methods
    (``list_by_investments_and_kind`` for NAV and cashflows,
    ``list_latest_by_investments`` for both region and sector weights
    per the ADR-0080 §4 latest-snapshot swap) must each fire exactly
    once across an entire ``get_portfolio_overview`` invocation,
    regardless of how many investments the universe contains. If this
    fails, check whether a per-investment loop has re-introduced the
    N+1 from P6-H.
    """
    from unittest import mock

    from core.repositories import (
        InvestmentCashflowRepository,
        InvestmentNavRepository,
        InvestmentRegionWeightsRepository,
        InvestmentSectorWeightsRepository,
    )

    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pr-batched@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        for i, name in enumerate(("Inv-1", "Inv-2", "Inv-3"), start=1):
            await _create_investment_with_data(
                session,
                actor.id,
                ac.id,
                name=name,
                vintage_year=2020 + i,
                nav_values=[(date(2024, 12, 31), Decimal(100 * i))],
                cashflows=[
                    (
                        datetime(2024, 1, 15, tzinfo=timezone.utc),
                        "capital_call",
                        Decimal(-100 * i),
                    ),
                ],
            )

    with (
        mock.patch.object(
            InvestmentNavRepository,
            "list_by_investments_and_kind",
            autospec=True,
            side_effect=InvestmentNavRepository.list_by_investments_and_kind,
        ) as nav_spy,
        mock.patch.object(
            InvestmentCashflowRepository,
            "list_by_investments_and_kind",
            autospec=True,
            side_effect=InvestmentCashflowRepository.list_by_investments_and_kind,
        ) as cf_spy,
        mock.patch.object(
            InvestmentRegionWeightsRepository,
            "list_latest_by_investments",
            autospec=True,
            side_effect=(InvestmentRegionWeightsRepository.list_latest_by_investments),
        ) as rw_spy,
        mock.patch.object(
            InvestmentSectorWeightsRepository,
            "list_latest_by_investments",
            autospec=True,
            side_effect=(InvestmentSectorWeightsRepository.list_latest_by_investments),
        ) as sw_spy,
    ):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            service = _build_service(session)
            bundle = await service.get_portfolio_overview(date(2024, 12, 31))

    assert bundle is not None
    assert bundle.investment_count == 3
    # Each plural method should fire exactly once across the entire
    # universe load. A regression to N+1 would push these counts up to
    # 3 (one call per investment).
    assert nav_spy.call_count == 1, (
        f"Expected 1 batched NAV fetch, got {nav_spy.call_count}. "
        "If this fails, check whether a per-investment loop has "
        "re-introduced the N+1 from P6-H."
    )
    assert cf_spy.call_count == 1, (
        f"Expected 1 batched cashflow fetch, got {cf_spy.call_count}. "
        "If this fails, check whether a per-investment loop has "
        "re-introduced the N+1 from P6-H."
    )
    assert rw_spy.call_count == 1, (
        f"Expected 1 batched region-weights fetch, "
        f"got {rw_spy.call_count}. If this fails, check whether a "
        "per-investment loop has re-introduced the N+1 from P6-H."
    )
    assert sw_spy.call_count == 1, (
        f"Expected 1 batched sector-weights fetch, "
        f"got {sw_spy.call_count}. If this fails, check whether a "
        "per-investment loop has re-introduced the N+1 from P6-H."
    )


# ---------------------------------------------------------------------------
# ADR-0080 §4 — the overview reads the latest composition snapshot
# ---------------------------------------------------------------------------


async def test_portfolio_overview_uses_latest_region_snapshot(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Two region snapshots for one investment → overview shows the later.

    Seeds an investment with a D1 snapshot (100 % DACH) and a later D2
    snapshot (100 % North America — USA). The service loads composition
    via ``list_latest_by_investments`` (ADR-0080 §4), so the region
    breakdown must reflect only the D2 generation.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pr-latest-snapshot@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        inv = await _create_investment_with_data(
            session,
            actor.id,
            ac.id,
            name="Drifter",
            vintage_year=2019,
            nav_values=[(date(2024, 12, 31), Decimal("200"))],
        )
        region_repo = RegionRepository(session)
        region_dach = await region_repo.create(code="dach", display_name="DACH", sort_order=10)
        region_usa = await region_repo.create(
            code="north_america_usa",
            display_name="North America — USA",
            sort_order=60,
        )
        weights_repo = InvestmentRegionWeightsRepository(session)
        await weights_repo.replace_snapshot_for_investment(
            inv.id,
            date(2023, 12, 31),
            [RegionWeightInput(region_id=region_dach.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )
        await weights_repo.replace_snapshot_for_investment(
            inv.id,
            date(2024, 12, 31),
            [RegionWeightInput(region_id=region_usa.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _build_service(session)
        bundle = await service.get_portfolio_overview(date(2024, 12, 31))

    assert bundle is not None
    by_code = {r.region_code: r for r in bundle.region_breakdown.rows}
    # Only the later snapshot (USA) survives; the D1 DACH generation is
    # not part of "the" composition.
    assert set(by_code) == {"north_america_usa"}
    assert by_code["north_america_usa"].weight_pct == pytest.approx(100.0)
