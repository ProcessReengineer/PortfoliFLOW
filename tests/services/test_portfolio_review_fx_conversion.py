# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Seam A of the ADR-0099 §4 conversion boundary — Portfolio Review.

Live-DB tests that exercise the multi-currency behaviour
:class:`~services.portfolio_review.PortfolioReviewService` gained in
Multi-Currency Block 3. All figures the *portfolio* overview reports are
now expressed in the tenant's functional currency (EUR for the seeded
tenants); the *single-investment* review stays in position currency.

Coverage:

* **Zero-read proof.** A functional-currency-only tenant produces its
  figures without a single ``load_rates_frame`` call — asserted with a spy,
  not merely by value equality. This is the ADR-0099 §3 identity guarantee.
* **Golden mixed portfolio.** One EUR fund plus one USD fund with two known
  USD rates; the portfolio NAV, invested-capital and multiples are checked
  against hand-computed values (see the inline arithmetic).
* **FX effect visibility.** A USD fund whose *local* NAV is flat across two
  dates still moves the portfolio NAV because the two dates carry different
  rates — the property that distinguishes point-in-time from period-end
  conversion.
* **Missing-rate surfacing.** An uncovered USD position raises
  :class:`MissingFxRateError` rather than silently summing nominally.
* **Single-review pin.** A USD investment's single review stays in USD even
  when rates exist.
* **Propagation.** The Front-Office Overview ``cash_eur`` residual reflects
  the converted invested book with no code change in the overview service.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import MissingFxRateError
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
            code="fx_class", display_name="FX Class"
        )
    return actor, asset_class


async def _create_investment(
    session,
    actor_id,
    asset_class_id,
    *,
    name: str,
    currency: str,
    vintage_year: int | None = 2020,
    nav_values: list[tuple[date, Decimal]] | None = None,
    cashflows: list[tuple[datetime, str, Decimal]] | None = None,
):
    """Create one investment in a given position currency with data."""
    inv = await InvestmentRepository(session).create(
        name=name,
        investment_type="private_equity",
        asset_class_id=asset_class_id,
        currency=currency,
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


async def _seed_fx_rates(
    session,
    actor_id,
    currency: str,
    rows: list[tuple[date, str]],
    *,
    reference: str = "EUR",
) -> None:
    """Seed FX rates for one priced currency against the reference."""
    fx_repo = FxRateRepository(session)
    for as_of, rate in rows:
        await fx_repo.upsert(
            currency=currency,
            as_of_date=as_of,
            rate_to_reference=Decimal(rate),
            reference_currency=reference,
            source="excel",
            created_by=actor_id,
        )


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Zero-read proof (ADR-0099 §3 identity guarantee)
# ---------------------------------------------------------------------------


async def test_functional_only_tenant_reads_no_fx_rows(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-zero-read@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Euro Fund",
            currency="EUR",
            nav_values=[(date(2024, 12, 31), Decimal("100"))],
            cashflows=[(_utc(2024, 1, 15), "capital_call", Decimal("-100"))],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        # Spy on the FX repository: an EUR-only tenant must never load a
        # rate frame (the identity short-circuit fires before any query).
        fx_repo = FxRateRepository(session)
        calls: list[tuple] = []
        original = fx_repo.load_rates_frame

        async def _spy(*args, **kwargs):
            calls.append((args, kwargs))
            return await original(*args, **kwargs)

        fx_repo.load_rates_frame = _spy  # type: ignore[method-assign]

        service = PortfolioReviewService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            cashflows=InvestmentCashflowRepository(session),
            region_weights=InvestmentRegionWeightsRepository(session),
            sector_weights=InvestmentSectorWeightsRepository(session),
            regions=RegionRepository(session),
            sectors=SectorRepository(session),
            tenants=TenantRepository(session),
            fx_rates=fx_repo,
        )
        bundle = await service.get_portfolio_overview(date(2024, 12, 31))

    assert bundle is not None
    # The provable zero-change path: no FX row was ever read ...
    assert calls == []
    # ... and the figure is exactly the pre-ADR-0099 nominal EUR sum.
    assert bundle.header_metrics.nav_eur == pytest.approx(100.0)
    assert bundle.header_metrics.functional_currency == "EUR"


# ---------------------------------------------------------------------------
# Golden mixed portfolio
# ---------------------------------------------------------------------------


async def test_golden_mixed_portfolio_converts_navs_and_flows(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """One EUR fund + one USD fund, hand-computed against known rates.

    Functional currency: EUR. USD rates (price of 1 USD in EUR):
      2024-06-01 → 0.80   2024-12-01 → 0.90   (carry-forward between).

    EUR fund:  NAV 2024-12-31 = 100 EUR;  call 2024-01-15 = -100 EUR.
    USD fund:  NAV 2024-12-31 = 200 USD;  call 2024-06-15 = -200 USD.

    Point-in-time conversion (each date at its own carry-forward rate):
      USD NAV  @2024-12-31 → rate 0.90 → 180 EUR   (nominal would be 200)
      USD call @2024-06-15 → rate 0.80 → -160 EUR  (nominal would be -200)

    Portfolio NAV      = 100 + 180 = 280 EUR   (nominal 300 — FX applied)
    Invested (calls)   = 100 + 160 = 260 EUR
    TVPI = (0 + 280) / 260   DPI = 0 / 260.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-golden@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Euro Fund",
            currency="EUR",
            vintage_year=2024,
            nav_values=[(date(2024, 12, 31), Decimal("100"))],
            cashflows=[(_utc(2024, 1, 15), "capital_call", Decimal("-100"))],
        )
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Dollar Fund",
            currency="USD",
            vintage_year=2024,
            nav_values=[(date(2024, 12, 31), Decimal("200"))],
            cashflows=[(_utc(2024, 6, 15), "capital_call", Decimal("-200"))],
        )
        await _seed_fx_rates(
            session,
            actor.id,
            "USD",
            [(date(2024, 6, 1), "0.80"), (date(2024, 12, 1), "0.90")],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).get_portfolio_overview(date(2024, 12, 31))

    assert bundle is not None
    m = bundle.header_metrics
    assert m.functional_currency == "EUR"
    # Converted NAV (280), NOT the nominal 300.
    assert m.nav_eur == pytest.approx(280.0)
    assert m.tvpi == pytest.approx(280.0 / 260.0)
    assert m.dpi == pytest.approx(0.0)
    # Tile-1 series carries the converted NAV (280) and invested (260).
    icn = bundle.invested_capital_nav
    assert icn.years[-1] == 2024
    assert icn.nav[-1] == pytest.approx(280.0)
    assert icn.invested_capital[-1] == pytest.approx(260.0)


# ---------------------------------------------------------------------------
# FX effect visibility — point-in-time vs period-end
# ---------------------------------------------------------------------------


async def test_fx_effect_moves_portfolio_nav_despite_flat_local_nav(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A flat USD NAV still moves the EUR portfolio NAV across a rate change.

    USD fund NAV is 200 USD at both 2024-06-30 and 2024-12-31 (flat in
    local currency). USD rates: 2024-06-01 → 0.90, 2024-12-01 → 0.80.

      as-of 2024-06-30 → 200 × 0.90 = 180 EUR
      as-of 2024-12-31 → 200 × 0.80 = 160 EUR

    Period-end conversion of the whole history at the latest rate (0.80)
    would report 160 at *both* dates; point-in-time reports 180 then 160.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-visible@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Dollar Fund",
            currency="USD",
            vintage_year=2024,
            nav_values=[
                (date(2024, 6, 30), Decimal("200")),
                (date(2024, 12, 31), Decimal("200")),
            ],
            # Flow dated after the first rate so it is covered; the NAV
            # movement (not the flow) is what this test exercises.
            cashflows=[(_utc(2024, 6, 15), "capital_call", Decimal("-200"))],
        )
        await _seed_fx_rates(
            session,
            actor.id,
            "USD",
            [(date(2024, 6, 1), "0.90"), (date(2024, 12, 1), "0.80")],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _build_service(session)
        mid = await service.get_portfolio_overview(date(2024, 6, 30))
        end = await service.get_portfolio_overview(date(2024, 12, 31))

    assert mid is not None and end is not None
    assert mid.header_metrics.nav_eur == pytest.approx(180.0)
    assert end.header_metrics.nav_eur == pytest.approx(160.0)
    # The distinguishing property: the two differ even though local NAV is
    # flat — point-in-time conversion retained the FX movement.
    assert mid.header_metrics.nav_eur != end.header_metrics.nav_eur


# ---------------------------------------------------------------------------
# Missing-rate surfacing (service level)
# ---------------------------------------------------------------------------


async def test_uncovered_currency_raises_missing_fx_rate(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-missing@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        # USD position but NO USD rates seeded anywhere.
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Dollar Fund",
            currency="USD",
            vintage_year=2024,
            nav_values=[(date(2024, 12, 31), Decimal("200"))],
            cashflows=[(_utc(2024, 1, 15), "capital_call", Decimal("-200"))],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _build_service(session)
        with pytest.raises(MissingFxRateError) as excinfo:
            await service.get_portfolio_overview(date(2024, 12, 31))
    # Loud failure names the currency — never a silent 1:1 fallback.
    assert excinfo.value.currency == "USD"


# ---------------------------------------------------------------------------
# Single-review pin — stays in position currency
# ---------------------------------------------------------------------------


async def test_single_investment_review_stays_in_position_currency(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-single@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        usd = await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Dollar Fund",
            currency="USD",
            vintage_year=2024,
            nav_values=[(date(2024, 12, 31), Decimal("200"))],
            cashflows=[(_utc(2024, 1, 15), "capital_call", Decimal("-100"))],
        )
        # Rates DO exist — the single review must ignore them regardless.
        await _seed_fx_rates(session, actor.id, "USD", [(date(2024, 12, 1), "0.80")])

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).get_single_investment_review(
            usd.id, date(2024, 12, 31)
        )

    assert bundle is not None
    # 200 USD reported as-is — NOT converted to 160 EUR.
    assert bundle.header_metrics.nav_eur == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Propagation — Front-Office Overview cash residual
# ---------------------------------------------------------------------------


async def test_front_office_aum_is_the_converted_sum_of_navs(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """``aum_eur`` = Σ *converted* NAV (ADR-0103 §2), not a persisted row.

    EUR fund NAV 100 + USD fund NAV 200 USD (× 0.80 = 160) → AUM 260. A
    nominal (unconverted) book would read 300 — the test pins the converted
    260, which is what makes this a conversion proof and not just an
    arithmetic one. USD rates cover both the flow and NAV dates.

    Neither fund is cash, so ``cash_eur`` is 0 and ``invested_eur`` carries
    the whole book: ``aum = invested + cash`` holds trivially here, and the
    cash-bearing case is pinned in ``test_portfolio_review_cash_positions``.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-cash@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Euro Fund",
            currency="EUR",
            vintage_year=2024,
            nav_values=[(date(2024, 12, 31), Decimal("100"))],
            cashflows=[(_utc(2024, 1, 15), "capital_call", Decimal("-100"))],
        )
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Dollar Fund",
            currency="USD",
            vintage_year=2024,
            nav_values=[(date(2024, 12, 31), Decimal("200"))],
            cashflows=[(_utc(2024, 6, 15), "capital_call", Decimal("-200"))],
        )
        await _seed_fx_rates(
            session,
            actor.id,
            "USD",
            [(date(2024, 6, 1), "0.80"), (date(2024, 12, 1), "0.80")],
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        overview = FrontOfficeOverviewService(
            _build_service(session),
            investment_repository=InvestmentRepository(session),
            nav_repository=InvestmentNavRepository(session),
            tenant_repository=TenantRepository(session),
            fx_rate_repository=FxRateRepository(session),
        )
        result = await overview.get_overview(date(2024, 12, 31))

    assert result is not None
    assert result.kpis.aum_eur == pytest.approx(260.0)
    assert result.kpis.invested_eur == pytest.approx(260.0)
    assert result.kpis.cash_eur == pytest.approx(0.0)
