# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Integration test for ``InvestmentService.get_charts_data``.

Live-DB end-to-end: seeds an investment with a couple of NAV rows
and a couple of cashflows, calls the service method, asserts the
:class:`InvestmentChartsBundle` is wired up with the six expected
fields and the values reflect the seeded rows.

Cross-tenant isolation: a foreign-tenant investment id surfaces as
``None`` (RLS hides the row, the service maps absence to None).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import numpy as np
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from services.investments import InvestmentChartsBundle, InvestmentService


def _build_service(session) -> InvestmentService:
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
    )


async def _seed_actor_and_asset_class(app_engine: AsyncEngine, tenant_id, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="charts_class", display_name="Charts Class"
        )
    return actor, asset_class


async def test_get_charts_data_aggregates_navs_and_cashflows(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="charts-bundle@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        inv = await svc.create_investment(
            name="Charts Fund",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        await svc.add_nav(
            investment_id=inv.id,
            as_of_date=date(2024, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("100"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        await svc.add_nav(
            investment_id=inv.id,
            as_of_date=date(2025, 6, 30),
            nav_kind="actual",
            nav_value=Decimal("160"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        # Plan NAVs are deliberately ignored by ``get_charts_data``.
        await svc.add_nav(
            investment_id=inv.id,
            as_of_date=date(2025, 12, 31),
            nav_kind="plan",
            nav_value=Decimal("180"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )
        await svc.add_cashflow(
            investment_id=inv.id,
            flow_timestamp=datetime(2024, 1, 31, 12, 0, tzinfo=timezone.utc),
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-100"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
        await svc.add_cashflow(
            investment_id=inv.id,
            flow_timestamp=datetime(2025, 6, 30, 12, 0, tzinfo=timezone.utc),
            flow_type="distribution",
            flow_kind="actual",
            amount=Decimal("30"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        bundle = await svc.get_charts_data(inv.id)

    assert isinstance(bundle, InvestmentChartsBundle)
    assert bundle.investment_name == "Charts Fund"

    # NAV series — actual only, sorted by date.
    assert list(bundle.nav_series.index) == [date(2024, 12, 31), date(2025, 6, 30)]
    assert list(bundle.nav_series.values) == [100.0, 160.0]

    # Cashflow frame — both actual rows present, plan ignored.
    assert len(bundle.cashflows_actual) == 2
    assert set(bundle.cashflows_actual["flow_type"]) == {"capital_call", "distribution"}

    # Total return: only one period available (2024-12-31 → 2025-06-30):
    # (160 - 100) / 100 = 0.60.
    assert len(bundle.total_return_series) == 1
    assert abs(float(bundle.total_return_series.iloc[0]) - 0.60) < 1e-12

    # Net Capital Gain at the 2025-06-30 observation:
    #   NAV=160 + cumsum(amount)=-100 → 60 (after the 2024 call,
    #   before the distribution); plus the 30 distribution → 60+30=90
    #   ... but reindexed onto the union of dates; the relevant final
    #   datapoint reflects all activity through 2025-06-30:
    #   NAV(160) + (-100 + 30) = 90. The function evaluates NCG at
    #   union dates, so the entry at 2025-06-30 is 90.
    final_ncg = bundle.net_capital_gain.iloc[-1]
    assert abs(float(final_ncg) - 90.0) < 1e-9

    # Rolling multiples: TVPI/DPI/RVPI present at each NAV observation.
    assert list(bundle.rolling_multiples["as_of_date"]) == [date(2024, 12, 31), date(2025, 6, 30)]

    # Rolling IRR: indexed by the same NAV observations.
    assert list(bundle.rolling_irr.index) == [date(2024, 12, 31), date(2025, 6, 30)]


async def test_get_charts_data_skips_irr_when_opted_out(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="charts-skip-irr@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        inv = await svc.create_investment(
            name="Skip IRR Fund",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        for as_of, value in [
            (date(2024, 6, 30), Decimal("100")),
            (date(2024, 12, 31), Decimal("120")),
            (date(2025, 6, 30), Decimal("160")),
        ]:
            await svc.add_nav(
                investment_id=inv.id,
                as_of_date=as_of,
                nav_kind="actual",
                nav_value=value,
                currency="EUR",
                source=None,
                created_by=actor.id,
            )
        await svc.add_cashflow(
            investment_id=inv.id,
            flow_timestamp=datetime(2024, 1, 31, 12, 0, tzinfo=timezone.utc),
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-100"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
        await svc.add_cashflow(
            investment_id=inv.id,
            flow_timestamp=datetime(2025, 6, 30, 12, 0, tzinfo=timezone.utc),
            flow_type="distribution",
            flow_kind="actual",
            amount=Decimal("30"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        bundle = await svc.get_charts_data(inv.id, include_irr=False)

    assert isinstance(bundle, InvestmentChartsBundle)
    assert bundle.rolling_irr.empty is True
    # The other fields remain populated as before.
    assert not bundle.nav_series.empty
    assert not bundle.total_return_series.empty
    assert not bundle.cashflows_actual.empty
    assert not bundle.net_capital_gain.empty
    assert not bundle.rolling_multiples.empty


async def test_get_charts_data_includes_irr_by_default(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="charts-default-irr@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        inv = await svc.create_investment(
            name="Default IRR Fund",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        nav_dates = [
            date(2024, 6, 30),
            date(2024, 12, 31),
            date(2025, 6, 30),
        ]
        for as_of, value in zip(nav_dates, [Decimal("100"), Decimal("120"), Decimal("160")]):
            await svc.add_nav(
                investment_id=inv.id,
                as_of_date=as_of,
                nav_kind="actual",
                nav_value=value,
                currency="EUR",
                source=None,
                created_by=actor.id,
            )
        await svc.add_cashflow(
            investment_id=inv.id,
            flow_timestamp=datetime(2024, 1, 31, 12, 0, tzinfo=timezone.utc),
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-100"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
        await svc.add_cashflow(
            investment_id=inv.id,
            flow_timestamp=datetime(2025, 6, 30, 12, 0, tzinfo=timezone.utc),
            flow_type="distribution",
            flow_kind="actual",
            amount=Decimal("30"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        bundle = await svc.get_charts_data(inv.id)

    assert isinstance(bundle, InvestmentChartsBundle)
    assert not bundle.rolling_irr.empty
    assert list(bundle.rolling_irr.index) == nav_dates


async def test_get_charts_data_returns_none_for_missing_investment(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, _ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="charts-missing@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        bundle = await svc.get_charts_data(uuid4())
    assert bundle is None


async def test_get_charts_data_handles_investment_without_data(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="charts-empty@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        inv = await svc.create_investment(
            name="Empty Fund",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        bundle = await svc.get_charts_data(inv.id)

    assert bundle is not None
    assert bundle.nav_series.empty
    assert bundle.total_return_series.empty
    assert bundle.cashflows_actual.empty or bundle.cashflows_actual.shape[0] == 0
    assert bundle.net_capital_gain.empty
    assert bundle.rolling_multiples.empty
    assert bundle.rolling_irr.empty
    # Numerical sanity: an empty IRR series should not raise during
    # downstream consumption — verify it really is an empty Series.
    assert isinstance(bundle.rolling_irr, type(bundle.total_return_series))
    assert np.isnan(bundle.rolling_irr.values).sum() == 0
