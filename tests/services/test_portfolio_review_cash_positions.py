# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ADR-0100 §4 — explicit cash positions at the Portfolio Review seam.

Live-DB tests for the performance/full universe split the
:class:`~services.portfolio_review.PortfolioReviewService` gained in
Multi-Currency Block 4. A ``investment_type='cash'`` row is a first-class
investment (converted at the ADR-0099 §4 boundary, inside Σ NAV) but is
excluded from the private-markets performance metrics.

Coverage:

* **Residual shrinkage / headline growth (ADR-0100 §3).** Adding a Cash USD
  position (converted NAV ``x``) grows the headline ``nav_eur`` by exactly
  ``x`` and shrinks the front-office cash residual by exactly ``x`` while
  the authoritative AUM is unchanged — value moves from residual into
  Σ NAV, total conserved.
* **Performance invariance (ADR-0100 §4, migration-day no-jump).** Portfolio
  IRR / TVPI / DPI and the invested-capital series are identical with and
  without the cash row present.
* **Composition inclusion / vintage exclusion.** Cash (positive NAV) is a
  fund-composition row (full universe); cash (NULL vintage) drops out of
  the vintage distribution automatically.
* **Persistence pin.** A cash investment inserts under the b027 CHECK and a
  blank vintage persists as ``vintage_year IS NULL``.
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
)
from core.repositories.investment_sector_weights_repository import (
    InvestmentSectorWeightsRepository,
)
from core.repositories.region_repository import RegionRepository
from core.repositories.sector_repository import SectorRepository
from services.front_office_overview import FrontOfficeOverviewService
from services.portfolio_review import PortfolioReviewService


# ---------------------------------------------------------------------------
# Wiring / seeding helpers
# ---------------------------------------------------------------------------


def _build_overview(session) -> FrontOfficeOverviewService:
    """Compose the Overview service on its post-ADR-0103 collaborators."""
    return FrontOfficeOverviewService(
        _build_service(session),
        investment_repository=InvestmentRepository(session),
        nav_repository=InvestmentNavRepository(session),
        tenant_repository=TenantRepository(session),
        fx_rate_repository=FxRateRepository(session),
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


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc)


async def _seed_actor_and_classes(app_engine: AsyncEngine, tenant_id, *, email):
    """Create an owner plus a private-markets and a cash asset class."""
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        acr = AssetClassRepository(session)
        pe_class = await acr.create(code="pe_class", display_name="Private Eq")
        cash_class = await acr.create(code="cash", display_name="Cash")
    return actor, pe_class, cash_class


async def _create_investment(
    session,
    actor_id,
    asset_class_id,
    *,
    name: str,
    investment_type: str = "private_equity",
    currency: str = "EUR",
    vintage_year: int | None = 2020,
    anlv_code: str | None = None,
    nav_values: list[tuple[date, Decimal]] | None = None,
    cashflows: list[tuple[datetime, str, Decimal]] | None = None,
):
    inv = await InvestmentRepository(session).create(
        name=name,
        investment_type=investment_type,
        asset_class_id=asset_class_id,
        currency=currency,
        created_by=actor_id,
        vintage_year=vintage_year,
        anlv_code=anlv_code,
    )
    nav_repo = InvestmentNavRepository(session)
    for as_of, value in nav_values or []:
        await nav_repo.upsert(
            investment_id=inv.id,
            as_of_date=as_of,
            nav_kind="actual",
            nav_value=value,
            currency=currency,
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
            currency=currency,
            description=None,
            created_by=actor_id,
        )
    return inv


async def _seed_usd_rate(session, actor_id, as_of: date, rate: str) -> None:
    await FxRateRepository(session).upsert(
        currency="USD",
        as_of_date=as_of,
        rate_to_reference=Decimal(rate),
        reference_currency="EUR",
        source="excel",
        created_by=actor_id,
    )


# ---------------------------------------------------------------------------
# AUM = Invested + Cash (ADR-0103 §2, superseding the ADR-0100 §3 residual)
# ---------------------------------------------------------------------------


async def test_cash_row_grows_aum_and_lands_in_the_cash_figure(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A cash position adds to AUM and shows up as Cash, not as a residual.

    ADR-0103 §2 retired ``aum − invested``. AUM is ``Σ nav_functional`` over
    the book, so a cash balance does not *move value between buckets* — it
    **is** value, and the book grows by exactly what it holds.

    Base: EUR PE fund NAV 1000, no cash position:
        aum 1000 = invested 1000 + cash 0
    After adding Cash USD 500 × 0.90 = 450 EUR:
        aum 1450 = invested 1000 + cash 450

    Note ``invested`` is unchanged: it is the non-cash book now (the Overview
    narrowing), while the Review's headline ``nav_eur`` stays the full
    universe and grows to 1450 (ADR-0100 §4, untouched here).
    """
    as_of = date(2024, 12, 31)
    tenant_id = await seed_tenant()
    actor, pe_class, cash_class = await _seed_actor_and_classes(
        app_engine, tenant_id, email="cash-residual@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            pe_class.id,
            name="Euro Fund",
            investment_type="private_equity",
            currency="EUR",
            vintage_year=2024,
            nav_values=[(as_of, Decimal("1000"))],
            cashflows=[(_utc(2024, 1, 15), "capital_call", Decimal("-1000"))],
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        base = await _build_overview(session).get_overview(as_of)

    assert base is not None
    assert base.kpis.aum_eur == pytest.approx(1000.0)
    assert base.kpis.invested_eur == pytest.approx(1000.0)
    assert base.kpis.cash_eur == pytest.approx(0.0)

    # Add the explicit foreign-currency cash position + its rate.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            cash_class.id,
            name="Cash USD",
            investment_type="cash",
            currency="USD",
            vintage_year=None,
            nav_values=[(as_of, Decimal("500"))],
        )
        await _seed_usd_rate(session, actor.id, date(2024, 12, 1), "0.90")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        after = await _build_overview(session).get_overview(as_of)

    assert after is not None
    # AUM grows by exactly the converted cash NAV (450).
    assert after.kpis.aum_eur == pytest.approx(1450.0)
    assert after.kpis.aum_eur - base.kpis.aum_eur == pytest.approx(450.0)
    # The invested book is untouched; the cash lands in the Cash figure.
    assert after.kpis.invested_eur == pytest.approx(1000.0)
    assert after.kpis.cash_eur == pytest.approx(450.0)
    # The identity the strip states.
    assert after.kpis.aum_eur == pytest.approx(after.kpis.invested_eur + after.kpis.cash_eur)


# ---------------------------------------------------------------------------
# Performance invariance (ADR-0100 §4 — migration-day no-jump)
# ---------------------------------------------------------------------------


async def test_performance_metrics_invariant_to_cash_rows(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """IRR / TVPI / DPI and the invested-capital series ignore cash entirely."""
    as_of = date(2023, 12, 31)
    tenant_id = await seed_tenant()
    actor, pe_class, cash_class = await _seed_actor_and_classes(
        app_engine, tenant_id, email="cash-perf@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            pe_class.id,
            name="Euro Fund",
            investment_type="private_equity",
            currency="EUR",
            vintage_year=2022,
            nav_values=[
                (date(2022, 12, 31), Decimal("1000")),
                (date(2023, 12, 31), Decimal("1200")),
            ],
            cashflows=[
                (_utc(2022, 6, 15), "capital_call", Decimal("-1000")),
                (_utc(2023, 6, 15), "distribution", Decimal("200")),
            ],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        before = await _build_service(session).get_portfolio_overview(as_of)

    assert before is not None

    # Add a cash position (with its rate) that has NO cashflows.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            cash_class.id,
            name="Cash USD",
            investment_type="cash",
            currency="USD",
            vintage_year=None,
            nav_values=[(date(2023, 12, 31), Decimal("500"))],
        )
        await _seed_usd_rate(session, actor.id, date(2023, 12, 1), "0.90")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        after = await _build_service(session).get_portfolio_overview(as_of)

    assert after is not None

    # The four private-markets scalars are byte-for-byte unchanged.
    assert after.header_metrics.irr == pytest.approx(before.header_metrics.irr)
    assert after.header_metrics.tvpi == pytest.approx(before.header_metrics.tvpi)
    assert after.header_metrics.dpi == pytest.approx(before.header_metrics.dpi)
    # The headline NAV, by contrast, DID grow (full universe includes cash).
    assert after.header_metrics.nav_eur > before.header_metrics.nav_eur

    # The invested-capital / NAV series is identical too.
    b_icn, a_icn = before.invested_capital_nav, after.invested_capital_nav
    assert a_icn.years == b_icn.years
    assert a_icn.invested_capital == pytest.approx(b_icn.invested_capital)
    assert a_icn.nav == pytest.approx(b_icn.nav)


# ---------------------------------------------------------------------------
# Composition inclusion / vintage exclusion
# ---------------------------------------------------------------------------


async def test_cash_in_fund_composition_but_not_vintage(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Cash is a fund-composition row (positive NAV) yet absent from vintages."""
    as_of = date(2024, 12, 31)
    tenant_id = await seed_tenant()
    actor, pe_class, cash_class = await _seed_actor_and_classes(
        app_engine, tenant_id, email="cash-composition@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            pe_class.id,
            name="Euro Fund",
            investment_type="private_equity",
            currency="EUR",
            vintage_year=2020,
            nav_values=[(as_of, Decimal("1000"))],
            cashflows=[(_utc(2020, 1, 15), "capital_call", Decimal("-1000"))],
        )
        await _create_investment(
            session,
            actor.id,
            cash_class.id,
            name="Cash USD",
            investment_type="cash",
            currency="USD",
            vintage_year=None,
            nav_values=[(as_of, Decimal("500"))],
        )
        await _seed_usd_rate(session, actor.id, date(2024, 12, 1), "0.90")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).get_portfolio_overview(as_of)

    assert bundle is not None
    # Full universe: the cash row is a composition row alongside the fund.
    comp_names = {r.name for r in bundle.fund_composition.rows}
    assert comp_names == {"Euro Fund", "Cash USD"}
    # Vintage distribution excludes the NULL-vintage cash row automatically.
    assert bundle.vintage_distribution.vintages == [2020]
    assert bundle.vintage_distribution.count == [1]


# ---------------------------------------------------------------------------
# Persistence pin — cash inserts under b027, blank vintage stays NULL
# ---------------------------------------------------------------------------


async def test_cash_investment_persists_with_null_vintage(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, _pe, cash_class = await _seed_actor_and_classes(
        app_engine, tenant_id, email="cash-persist@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        created = await _create_investment(
            session,
            actor.id,
            cash_class.id,
            name="Cash USD",
            investment_type="cash",
            currency="USD",
            vintage_year=None,
            anlv_code=None,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        fetched = await InvestmentRepository(session).get_by_id(created.id)

    assert fetched is not None
    assert fetched.investment_type == "cash"
    assert fetched.vintage_year is None
    assert fetched.currency == "USD"
