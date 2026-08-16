# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""One definition of AUM, everywhere (ADR-0103 §2).

``aum(t) = Σ nav_functional(t)`` over **all** investments, cash rows
included. The claim this suite pins is not that each surface computes
*something* — it is that the three surfaces that state an AUM figure state
the **same** figure, on a book chosen so that every way of getting it wrong
produces a visibly different number:

* the **Front-Office Overview hero** (``FrontOfficeOverviewService``),
* the **limit-coverage denominator** (``LimitsCoverageService`` →
  ``bundle.aum_used``),
* the **AUM-sheet reconciliation control**
  (``InvestmentService.reconcile_aum_sheet``, ADR-0103 §3) — which agrees by
  *staying silent* when the sheet states that same figure.

The book is multi-currency and holds cash in both currencies:

===============  ========  ==========  =====================
Position         Currency  Native NAV  Functional (EUR)
===============  ========  ==========  =====================
Euro Fund        EUR          400,000              400,000
Dollar Fund      USD          250,000  200,000  (× 0.80)
Cash EUR         EUR          300,000              300,000
Cash USD         USD          125,000  100,000  (× 0.80)
===============  ========  ==========  =====================
**AUM**                               **1,000,000**

Every failure mode lands somewhere else: skipping the cash rows gives
600,000; taking the USD legs nominally gives 1,175,000; skipping only the
foreign cash gives 900,000. Only the definition gives 1,000,000.

The split the Overview publishes is pinned too: ``Invested`` (600,000, the
non-cash book) plus ``Cash`` (400,000, both cash rows converted) is the AUM
— which is the retired residual's job, done by reading the book instead of
subtracting from it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    DataUploadRepository,
    FxRateRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    LimitsRepository,
    RegionRepository,
    SectorRepository,
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
from services.front_office_overview import FrontOfficeOverviewService
from services.investments import InvestmentService
from services.limits import LimitsCoverageService
from services.portfolio_review import PortfolioReviewService

#: The Stichtag. A month-end, so the coverage grid lands on it.
AS_OF = date(2024, 12, 31)

#: The one number.
AUM = Decimal("1000000")
INVESTED = Decimal("600000")
CASH = Decimal("400000")


def _review(session) -> PortfolioReviewService:
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


def _overview(session) -> FrontOfficeOverviewService:
    return FrontOfficeOverviewService(
        _review(session),
        investment_repository=InvestmentRepository(session),
        nav_repository=InvestmentNavRepository(session),
        tenant_repository=TenantRepository(session),
        fx_rate_repository=FxRateRepository(session),
    )


def _coverage(session) -> LimitsCoverageService:
    return LimitsCoverageService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        limits=LimitsRepository(session),
        asset_classes=AssetClassRepository(session),
        tenants=TenantRepository(session),
        fx_rates=FxRateRepository(session),
    )


async def _seed_book(app_engine: AsyncEngine, tenant_id: UUID) -> UUID:
    """Seed the four-position, two-currency book above; return the actor id."""
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="one-definition@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac_repo = AssetClassRepository(session)
        equities = await ac_repo.create(code="equities", display_name="Equities")
        cash_class = await ac_repo.create(code="cash", display_name="Cash")

        inv_repo = InvestmentRepository(session)
        nav_repo = InvestmentNavRepository(session)

        for name, inv_type, ac_id, currency, nav in (
            ("Euro Fund", "listed_equity", equities.id, "EUR", "400000"),
            ("Dollar Fund", "listed_equity", equities.id, "USD", "250000"),
            ("Cash EUR", "cash", cash_class.id, "EUR", "300000"),
            ("Cash USD", "cash", cash_class.id, "USD", "125000"),
        ):
            investment = await inv_repo.create(
                name=name,
                investment_type=inv_type,
                asset_class_id=ac_id,
                currency=currency,
                created_by=actor.id,
            )
            await nav_repo.upsert(
                investment_id=investment.id,
                as_of_date=AS_OF,
                nav_kind="actual",
                nav_value=Decimal(nav),
                currency=currency,
                source=None,
                created_by=actor.id,
            )

        await FxRateRepository(session).upsert(
            currency="USD",
            as_of_date=date(2024, 12, 1),
            rate_to_reference=Decimal("0.80"),
            reference_currency="EUR",
            source="excel",
            created_by=actor.id,
        )

        limits_repo = LimitsRepository(session)
        await limits_repo.create_set_with_limits(
            family="saa",
            effective_from=date(2020, 1, 1),
            label="SAA",
            notes=None,
            limits={"equities": Decimal("70.0"), "cash": Decimal("50.0")},
            created_by=actor.id,
        )
        await limits_repo.create_set_with_limits(
            family="anlv",
            effective_from=date(2020, 1, 1),
            label="AnlV",
            notes=None,
            limits={"anlv_1": Decimal("60.0")},
            created_by=actor.id,
        )

    return actor.id


async def test_one_definition_of_aum_across_every_surface(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Hero, coverage denominator and reconciliation control agree on Σ NAV."""
    tenant_id = await seed_tenant()
    actor_id = await _seed_book(app_engine, tenant_id)

    # --- 1. The Overview hero ------------------------------------------
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        result = await _overview(session).get_overview(AS_OF)

    assert result is not None
    hero = result.kpis
    assert hero.aum_eur == pytest.approx(float(AUM))
    # AUM = Invested + Cash — the split that replaced the residual.
    assert hero.invested_eur == pytest.approx(float(INVESTED))
    assert hero.cash_eur == pytest.approx(float(CASH))
    assert hero.aum_eur == pytest.approx(hero.invested_eur + hero.cash_eur)

    # --- 2. The coverage denominator -----------------------------------
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        bundle = await _coverage(session).get_coverage(
            from_date=date(2024, 12, 1),
            to_date=AS_OF,
            cut_over=date(2025, 12, 31),
        )

    assert bundle is not None
    denominator = bundle.aum_used.loc[pd.Timestamp(AS_OF)]
    assert denominator == pytest.approx(AUM)
    # The same figure the hero states — not merely a similar one.
    assert float(denominator) == pytest.approx(hero.aum_eur)
    # And the KPI strip's AUM is that denominator.
    assert bundle.kpi_strip.aum_eur == pytest.approx(AUM)

    # --- 3. The AUM-sheet reconciliation control ------------------------
    # The control agrees by staying silent: a sheet stating the same number
    # deviates from the book by nothing.
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        upload = await DataUploadRepository(session).create_upload(
            uploaded_by=actor_id,
            filename="aum.xlsx",
            file_hash=uuid4().hex.ljust(64, "0"),
            size_bytes=512,
            format_version="v2",
            sheets={
                "attributes": pd.DataFrame(
                    [["listed_equity"], ["equities"], ["EUR"]],
                    index=["Investment Type", "Asset Class", "Währung"],
                    columns=["Ignored"],
                ),
                "aum": pd.DataFrame(
                    {"AUM total": [float(AUM)]},
                    index=pd.to_datetime([AS_OF.isoformat()]),
                ),
            },
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        service = InvestmentService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            cashflows=InvestmentCashflowRepository(session),
        )
        warnings = await service.reconcile_aum_sheet(
            upload.id,
            data_upload_repository=DataUploadRepository(session),
            tenant_repository=TenantRepository(session),
            fx_rate_repository=FxRateRepository(session),
        )

    assert warnings == (), (
        "The reconciliation control disagrees with the book it is supposed to "
        f"check. Stated {AUM}; the control found a deviation: {warnings}"
    )
