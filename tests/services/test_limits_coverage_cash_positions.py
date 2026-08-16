# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ADR-0100 §4 / ADR-0103 §8 — explicit cash positions at the Limits seam.

Live-DB test that an ``investment_type='cash'`` row flows through the
coverage engine like any other holding, with **no engine change**: its
(converted) NAV enters both the SAA asset-class quota (via
``asset_class_code``) and the AnlV quota (via ``anlv_code``).

ADR-0103 §2 finished the job the cash row started. There is no residual left
for cash to shrink: the denominator *is* ``Σ nav`` — cash included (§8,
"coverage/limits: cash rows remain included") — so the book divides by
itself and every quota is a share of what is actually held.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    FxRateRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    LimitsRepository,
    TenantRepository,
    UserRepository,
    tenant_context,
)
from services.limits import LimitsCoverageService


def _build_service(session) -> LimitsCoverageService:
    return LimitsCoverageService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        limits=LimitsRepository(session),
        asset_classes=AssetClassRepository(session),
        tenants=TenantRepository(session),
        fx_rates=FxRateRepository(session),
    )


def _row_at(coverage: pd.DataFrame, stichtag: date, class_key: str) -> dict:
    slice_df = coverage[
        (coverage["as_of_date"] == pd.Timestamp(stichtag)) & (coverage["class_key"] == class_key)
    ]
    assert len(slice_df) == 1, (
        f"expected exactly one {class_key} row at {stichtag}, got {len(slice_df)}"
    )
    return slice_df.iloc[0].to_dict()


async def test_cash_row_enters_asset_class_and_anlv_quotas(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A USD cash row (400k EUR converted) counts in SAA 'cash' and AnlV.

    Universe at 2024-12-31:
      Equity Fund  400,000 EUR  (asset class 'equities', no AnlV)
      Cash USD     500,000 USD × 0.80 = 400,000 EUR
                   (asset class 'cash', anlv_code 'anlv_13')
      AUM        1,000,000 EUR

    Σ nav = 800,000 → residual = 200,000 (would be 600,000 without the
    cash row: the residual shrinks by exactly the 400,000 cash NAV).
    """
    stichtag = date(2024, 12, 31)
    tenant_id = await seed_tenant()

    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="cash-coverage@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        acr = AssetClassRepository(session)
        equities = await acr.create(code="equities", display_name="Equities")
        cash_ac = await acr.create(code="cash", display_name="Cash")

        inv_repo = InvestmentRepository(session)
        equity = await inv_repo.create(
            name="Equity Fund",
            investment_type="listed_equity",
            asset_class_id=equities.id,
            currency="EUR",
            created_by=actor.id,
        )
        cash = await inv_repo.create(
            name="Cash USD",
            investment_type="cash",
            asset_class_id=cash_ac.id,
            currency="USD",
            created_by=actor.id,
            vintage_year=None,
            anlv_code="anlv_13",
        )

        nav_repo = InvestmentNavRepository(session)
        await nav_repo.upsert(
            investment_id=equity.id,
            as_of_date=stichtag,
            nav_kind="actual",
            nav_value=Decimal("400000"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        await nav_repo.upsert(
            investment_id=cash.id,
            as_of_date=stichtag,
            nav_kind="actual",
            nav_value=Decimal("500000"),
            currency="USD",
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
            limits={"equities": Decimal("50.0"), "cash": Decimal("50.0")},
            created_by=actor.id,
        )
        await limits_repo.create_set_with_limits(
            family="anlv",
            effective_from=date(2020, 1, 1),
            label="AnlV",
            notes=None,
            limits={"anlv_13": Decimal("60.0")},
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).get_coverage(
            from_date=date(2024, 12, 1),
            to_date=stichtag,
            cut_over=date(2025, 12, 31),
        )

    assert bundle is not None

    # The book is 400,000 of equity plus 500,000 USD of cash converted at
    # 0.80 → 400,000. The denominator is that book: 800,000 (ADR-0103 §2).
    assert bundle.aum_used.loc[pd.Timestamp(stichtag)] == pytest.approx(Decimal("800000"))

    # SAA: the explicit cash row enters the 'cash' asset-class quota with its
    # converted NAV — half the book.
    saa_cash = _row_at(bundle.saa.coverage, stichtag, "cash")
    assert saa_cash["nav_sum_eur"] == pytest.approx(Decimal("400000"))
    assert saa_cash["coverage_pct"] == pytest.approx(Decimal("50"))

    # AnlV: the cash row's anlv_code carries the same converted NAV.
    anlv_row = _row_at(bundle.anlv.coverage, stichtag, "anlv_13")
    assert anlv_row["nav_sum_eur"] == pytest.approx(Decimal("400000"))
    assert anlv_row["coverage_pct"] == pytest.approx(Decimal("50"))

    # The two quotas exhaust the book: there is no residual bucket left over,
    # because there is no unmodelled float to put in one (ADR-0103 §2).
    assert not hasattr(bundle, "cash_residual")
