# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Seam B of the ADR-0099 §4 conversion boundary — Investment Limits.

Live-DB tests for the multi-currency behaviour
:class:`~services.limits.LimitsCoverageService` gained in Multi-Currency
Block 3. Per-investment NAV series are converted from their position
currency into the functional currency before the coverage engine sees them,
so every asset-class quota compares like-for-like.

Since ADR-0103 §2 the denominator is the converted book itself
(``Σ nav_functional``, cash rows included) rather than a separately
persisted AUM series — numerator and denominator now come out of the same
conversion pass, which is one fewer thing that can disagree.

Coverage:

* **Converted quota.** A USD fund in a book whose remainder is EUR cash:
  the coverage percentage and ``nav_sum_eur`` reflect the converted NAV,
  not the nominal one.
* **Plan-year frozen rate.** A plan NAV dated a year past the last stored
  FX rate converts at that last, frozen rate (the defined ADR-0060-style
  carry-forward — not a defect).
* **Zero-read proof.** A functional-currency-only tenant reads no FX rows
  (spy-asserted).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

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


def _D(value: str | int) -> Decimal:
    return Decimal(str(value))


async def _seed_user(superuser_engine: AsyncEngine, tenant_id: UUID) -> UUID:
    from uuid import uuid4

    from sqlalchemy import text

    user_id = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, email, password_hash, roles, is_active) "
                "VALUES (:uid, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)"
            ),
            {
                "uid": str(user_id),
                "tid": str(tenant_id),
                "email": f"u-{user_id}@example.com",
                "hash": "$2b$04$placeholder_hash_for_service_tests_only",
            },
        )
    return user_id


async def _seed_usd_universe(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    eur_cash: list[tuple[date, Decimal]] | None = None,
    actual_navs: list[tuple[date, Decimal]],
    plan_navs: list[tuple[date, Decimal]] | None = None,
    usd_rates: list[tuple[date, str]],
    saa_limits: dict[str, Decimal],
) -> None:
    """Seed a one-USD-investment universe, optionally plus an EUR cash row.

    ``eur_cash`` is what the ``portfolio_aum`` rows used to be: the part of
    the book that is not the fund. Under ADR-0103 §2 it has to be *held*
    rather than asserted, so a test that wants the fund at 40 % of a
    1,000,000 book holds 600,000 in cash.
    """
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        ac = await AssetClassRepository(session).create(code="equities", display_name="Equities")
        inv = await InvestmentRepository(session).create(
            name="Dollar Fund",
            investment_type="listed_equity",
            asset_class_id=ac.id,
            currency="USD",
            created_by=user_id,
        )
        if eur_cash:
            cash_ac = await AssetClassRepository(session).create(code="cash", display_name="Cash")
            cash_inv = await InvestmentRepository(session).create(
                name="Cash EUR",
                investment_type="cash",
                asset_class_id=cash_ac.id,
                currency="EUR",
                created_by=user_id,
            )
            for as_of, value in eur_cash:
                await InvestmentNavRepository(session).upsert(
                    investment_id=cash_inv.id,
                    as_of_date=as_of,
                    nav_kind="actual",
                    nav_value=value,
                    currency="EUR",
                    source=None,
                    created_by=user_id,
                )

        nav_repo = InvestmentNavRepository(session)
        for as_of, value in actual_navs:
            await nav_repo.upsert(
                investment_id=inv.id,
                as_of_date=as_of,
                nav_kind="actual",
                nav_value=value,
                currency="USD",
                source=None,
                created_by=user_id,
            )
        for as_of, value in plan_navs or []:
            await nav_repo.upsert(
                investment_id=inv.id,
                as_of_date=as_of,
                nav_kind="plan",
                nav_value=value,
                currency="USD",
                source=None,
                created_by=user_id,
            )

        fx_repo = FxRateRepository(session)
        for as_of, rate in usd_rates:
            await fx_repo.upsert(
                currency="USD",
                as_of_date=as_of,
                rate_to_reference=Decimal(rate),
                reference_currency="EUR",
                source="excel",
                created_by=user_id,
            )

        limits_repo = LimitsRepository(session)
        await limits_repo.create_set_with_limits(
            family="saa",
            effective_from=date(2020, 1, 1),
            label="SAA test",
            notes=None,
            limits=saa_limits,
            created_by=user_id,
        )
        # The engine evaluates both families; an AnlV set must be in force
        # too (the USD fund carries no anlv_code, so it lands UNALLOCATED
        # there — the set just needs to exist).
        await limits_repo.create_set_with_limits(
            family="anlv",
            effective_from=date(2020, 1, 1),
            label="AnlV test",
            notes=None,
            limits={"anlv_1": _D("50.0")},
            created_by=user_id,
        )


def _row_at(coverage: pd.DataFrame, stichtag: date, class_key: str) -> dict:
    """Return the single coverage row for a class at a Stichtag as a dict."""
    slice_df = coverage[
        (coverage["as_of_date"] == pd.Timestamp(stichtag)) & (coverage["class_key"] == class_key)
    ]
    assert len(slice_df) == 1, (
        f"expected exactly one {class_key} row at {stichtag}, got {len(slice_df)}"
    )
    return slice_df.iloc[0].to_dict()


# ---------------------------------------------------------------------------
# Converted quota + residual
# ---------------------------------------------------------------------------


async def test_usd_fund_quota_and_residual_use_converted_nav(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    """USD NAV 500k × 0.80 = 400k EUR against 1,000k EUR AUM → 40% (not 50%).

    The book is the USD fund plus 600,000 of EUR cash, so the denominator is
    400,000 + 600,000 = 1,000,000 (ADR-0103 §2 — the same 1,000,000 the
    retired ``portfolio_aum`` row used to assert, now *held* rather than
    stated). A nominal (unconverted) 500,000 would read 500,000 nav_sum
    against a 1,100,000 book — the test pins the converted figures.
    """
    tenant_id = await seed_tenant("fx-limits")
    user_id = await _seed_user(superuser_engine, tenant_id)

    await _seed_usd_universe(
        app_engine,
        tenant_id,
        user_id,
        eur_cash=[(date(2024, 12, 31), _D("600000"))],
        actual_navs=[(date(2024, 12, 31), _D("500000"))],
        usd_rates=[(date(2024, 12, 1), "0.80")],
        saa_limits={"equities": _D("50.0")},
    )

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        bundle = await _build_service(session).get_coverage(
            from_date=date(2024, 12, 1),
            to_date=date(2024, 12, 31),
            cut_over=date(2025, 12, 31),
        )

    assert bundle is not None
    stichtag = date(2024, 12, 31)
    assert bundle.latest_as_of_date == stichtag

    row = _row_at(bundle.saa.coverage, stichtag, "equities")
    assert row["nav_sum_eur"] == pytest.approx(Decimal("400000"))
    assert row["coverage_pct"] == pytest.approx(Decimal("40"))
    assert row["status"] == "OK"

    # The denominator is the converted book — fund plus cash — not a
    # separately persisted AUM row.
    assert bundle.aum_used.loc[pd.Timestamp(stichtag)] == pytest.approx(Decimal("1000000"))


# ---------------------------------------------------------------------------
# Plan-year frozen rate
# ---------------------------------------------------------------------------


async def test_plan_year_converts_at_frozen_carry_forward_rate(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    """A plan NAV a year past the last FX rate converts at the frozen rate.

    Last USD rate is 2024-12-01 = 0.80. The plan NAV at 2025-12-31 (a full
    year later, past cut_over so the plan stream is used) converts at the
    carry-forward 0.80 → 400,000 EUR, not a 1:1 default (500,000) and not
    an error. That frozen-rate behaviour is the defined ADR-0060 semantics.
    """
    tenant_id = await seed_tenant("fx-plan")
    user_id = await _seed_user(superuser_engine, tenant_id)

    await _seed_usd_universe(
        app_engine,
        tenant_id,
        user_id,
        eur_cash=[(date(2024, 12, 31), _D("600000"))],
        actual_navs=[(date(2024, 12, 31), _D("500000"))],
        plan_navs=[(date(2025, 12, 31), _D("500000"))],
        usd_rates=[(date(2024, 12, 1), "0.80")],
        saa_limits={"equities": _D("50.0")},
    )

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        bundle = await _build_service(session).get_coverage(
            from_date=date(2024, 12, 1),
            to_date=date(2025, 12, 31),
            cut_over=date(2024, 12, 31),  # 2025 Stichtage use the plan stream
        )

    assert bundle is not None
    plan_stichtag = date(2025, 12, 31)
    assert bundle.latest_as_of_date == plan_stichtag

    row = _row_at(bundle.saa.coverage, plan_stichtag, "equities")
    # Frozen 0.80 applied to the 2025 plan NAV → 400k, not 500k, not error.
    assert row["nav_sum_eur"] == pytest.approx(Decimal("400000"))
    assert row["coverage_pct"] == pytest.approx(Decimal("40"))


# ---------------------------------------------------------------------------
# Zero-read proof (identity guarantee for Seam B)
# ---------------------------------------------------------------------------


async def test_functional_only_tenant_reads_no_fx_rows(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant("fx-limits-zero")
    user_id = await _seed_user(superuser_engine, tenant_id)

    # An all-EUR universe: seed via the USD helper but with EUR everywhere
    # is awkward, so seed directly.
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        ac = await AssetClassRepository(session).create(code="equities", display_name="Equities")
        inv = await InvestmentRepository(session).create(
            name="Euro Fund",
            investment_type="listed_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=user_id,
        )
        await InvestmentNavRepository(session).upsert(
            investment_id=inv.id,
            as_of_date=date(2024, 12, 31),
            nav_kind="actual",
            nav_value=_D("400000"),
            currency="EUR",
            source=None,
            created_by=user_id,
        )
        limits_repo = LimitsRepository(session)
        await limits_repo.create_set_with_limits(
            family="saa",
            effective_from=date(2020, 1, 1),
            label="SAA test",
            notes=None,
            limits={"equities": _D("50.0")},
            created_by=user_id,
        )
        await limits_repo.create_set_with_limits(
            family="anlv",
            effective_from=date(2020, 1, 1),
            label="AnlV test",
            notes=None,
            limits={"anlv_1": _D("50.0")},
            created_by=user_id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        fx_repo = FxRateRepository(session)
        calls: list[tuple] = []
        original = fx_repo.load_rates_frame

        async def _spy(*args, **kwargs):
            calls.append((args, kwargs))
            return await original(*args, **kwargs)

        fx_repo.load_rates_frame = _spy  # type: ignore[method-assign]

        service = LimitsCoverageService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            limits=LimitsRepository(session),
            asset_classes=AssetClassRepository(session),
            tenants=TenantRepository(session),
            fx_rates=fx_repo,
        )
        bundle = await service.get_coverage(
            from_date=date(2024, 12, 1),
            to_date=date(2024, 12, 31),
            cut_over=date(2025, 12, 31),
        )

    assert bundle is not None
    assert calls == []
    row = _row_at(bundle.saa.coverage, date(2024, 12, 31), "equities")
    assert row["nav_sum_eur"] == pytest.approx(Decimal("400000"))
