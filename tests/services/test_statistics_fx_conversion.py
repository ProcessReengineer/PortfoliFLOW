# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ADR-0102 conversion boundary — Statistics.

Live-DB tests for the multi-currency behaviour
:class:`~services.statistics.StatisticsService` gained under ADR-0102.
Every *return-derived* statistic — annualised return, Sharpe, distribution,
risk, and the cross-investment correlation matrix — is now computed from
NAV histories converted into the tenant's functional currency at the
ADR-0099 §4 boundary.

The KPI card's ``latest_nav`` deliberately stays in the **position**
currency: it is published as a ``(value, currency)`` pair, so it is
self-describing rather than aggregated. Converting it while keeping the
``currency`` label would produce a EUR number labelled "USD", which is the
one outcome worse than not converting at all.

Coverage:

* **Zero-read proof.** A functional-currency-only tenant computes its
  statistics without a single ``load_rates_frame`` call — asserted with a
  spy, not by value equality (ADR-0099 §3).
* **Golden conversion.** A USD investment whose NAV is flat *in USD* still
  shows a non-zero return once converted, and the value is hand-computed.
* **Native NAV pin.** That same investment's KPI card still reports its USD
  balance under a "USD" label.
* **Missing-rate surfacing.** An uncovered USD position raises
  :class:`MissingFxRateError` rather than silently correlating nominal
  series.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import MissingFxRateError
from core.repositories import (
    AssetClassRepository,
    FxRateRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    TenantRepository,
    UserRepository,
    tenant_context,
)
from services.statistics import StatisticsService


# ---------------------------------------------------------------------------
# Wiring / seeding helpers
# ---------------------------------------------------------------------------


def _build_service(session) -> StatisticsService:
    return StatisticsService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        tenants=TenantRepository(session),
        fx_rates=FxRateRepository(session),
    )


async def _seed_actor_and_asset_class(app_engine: AsyncEngine, tenant_id, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="fx_stats_class", display_name="FX Stats Class"
        )
    return actor, asset_class


async def _create_investment(
    session,
    actor_id,
    asset_class_id,
    *,
    name: str,
    currency: str,
    nav_values: list[tuple[date, Decimal]],
):
    inv = await InvestmentRepository(session).create(
        name=name,
        investment_type="private_equity",
        asset_class_id=asset_class_id,
        currency=currency,
        created_by=actor_id,
        vintage_year=2020,
    )
    nav_repo = InvestmentNavRepository(session)
    for as_of, value in nav_values:
        await nav_repo.upsert(
            investment_id=inv.id,
            as_of_date=as_of,
            nav_kind="actual",
            nav_value=value,
            currency=currency,
            source=None,
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


# ---------------------------------------------------------------------------
# Zero-read proof (ADR-0099 §3 identity guarantee)
# ---------------------------------------------------------------------------


async def test_functional_only_tenant_reads_no_fx_rows(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An EUR-only universe loads no rate frame — the numbers cannot move."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-stats-zero@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Euro Fund",
            currency="EUR",
            nav_values=[
                (date(2024, 1, 31), Decimal("100")),
                (date(2024, 2, 29), Decimal("110")),
                (date(2024, 3, 31), Decimal("121")),
            ],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        fx_repo = FxRateRepository(session)
        calls: list[tuple] = []
        original = fx_repo.load_rates_frame

        async def _spy(*args, **kwargs):
            calls.append((args, kwargs))
            return await original(*args, **kwargs)

        fx_repo.load_rates_frame = _spy  # type: ignore[method-assign]

        service = StatisticsService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            tenants=TenantRepository(session),
            fx_rates=fx_repo,
        )
        bundle = await service.get_universe_statistics(as_of_date=date(2024, 3, 31))

    # The provable zero-change path: no FX row was ever read ...
    assert calls == []
    # ... and the returns are the pre-ADR-0102 nominal ones (two +10 % steps).
    card = bundle.key_metrics["Euro Fund"]
    assert card.latest_nav == pytest.approx(121.0)
    assert card.currency == "EUR"
    assert card.sparkline_values == [pytest.approx(1.10), pytest.approx(1.21)]


# ---------------------------------------------------------------------------
# Golden conversion — the FX effect enters the return series
# ---------------------------------------------------------------------------


async def test_flat_usd_nav_yields_converted_return(app_engine: AsyncEngine, seed_tenant) -> None:
    """A NAV flat in USD still returns +10 % per step in EUR as the rate moves.

    USD rates (price of 1 USD in EUR): 2024-01-31 → 1.00, 2024-02-29 → 1.10,
    2024-03-31 → 1.21. The NAV is 100 USD on all three dates — perfectly flat
    in local currency.

    Converted point-in-time: 100 → 110 → 121 EUR, i.e. two returns of
    ``110 / 100 - 1 = 0.10`` and ``121 / 110 - 1 = 0.10``.

    Without the conversion both returns would be exactly ``0.0`` and the
    sparkline flat at 1.0 — so these assertions are direct discriminators.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-stats-golden@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Dollar Fund",
            currency="USD",
            nav_values=[
                (date(2024, 1, 31), Decimal("100")),
                (date(2024, 2, 29), Decimal("100")),
                (date(2024, 3, 31), Decimal("100")),
            ],
        )
        await _seed_fx_rates(
            session,
            actor.id,
            "USD",
            [
                (date(2024, 1, 31), "1.00"),
                (date(2024, 2, 29), "1.10"),
                (date(2024, 3, 31), "1.21"),
            ],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).get_universe_statistics(as_of_date=date(2024, 3, 31))

    card = bundle.key_metrics["Dollar Fund"]
    # The returns are pure FX: two +10 % steps compounded to 1.10 then 1.21.
    assert card.sparkline_values == [pytest.approx(1.10), pytest.approx(1.21)]
    stats = bundle.distribution_stats["Dollar Fund"]
    assert stats.mean_daily == pytest.approx(0.10)
    assert stats.median == pytest.approx(0.10)

    # The KPI card's NAV stays native: 100 USD, labelled USD — not the
    # converted 121 EUR under a "USD" label.
    assert card.latest_nav == pytest.approx(100.0)
    assert card.currency == "USD"


async def test_correlation_matrix_is_computed_on_converted_returns(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Two funds flat in their own currency correlate through the FX path.

    Both funds hold a NAV that never moves locally. The EUR fund therefore
    has no return at all; the USD fund's returns are entirely the currency's
    movement. A correlation matrix built on *unconverted* series could not
    exist here — neither series would vary — so the matrix's presence and
    the USD fund's non-zero volatility together prove the conversion ran
    before the analytics layer.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-stats-corr@example.com"
    )
    dates = [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31)]
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Euro Fund",
            currency="EUR",
            nav_values=[(d, Decimal("100")) for d in dates],
        )
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Dollar Fund",
            currency="USD",
            nav_values=[(d, Decimal("100")) for d in dates],
        )
        await _seed_fx_rates(
            session,
            actor.id,
            "USD",
            [
                (date(2024, 1, 31), "1.00"),
                (date(2024, 2, 29), "1.10"),
                (date(2024, 3, 31), "1.21"),
            ],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).get_universe_statistics(as_of_date=date(2024, 3, 31))

    # Two +10 % steps for the USD fund (100 → 110 → 121 EUR), zero for EUR.
    usd = bundle.key_metrics["Dollar Fund"]
    assert usd.sparkline_values == [pytest.approx(1.10), pytest.approx(1.21)]
    assert bundle.key_metrics["Euro Fund"].sparkline_values == [
        pytest.approx(1.0),
        pytest.approx(1.0),
    ]
    # Both series are non-empty, so the matrix is square over both names.
    assert list(bundle.correlation_matrix.columns) == ["Dollar Fund", "Euro Fund"]


# ---------------------------------------------------------------------------
# Missing-rate surfacing
# ---------------------------------------------------------------------------


async def test_uncovered_currency_raises_missing_fx_rate(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A USD position with no USD rate fails loudly — never a 1:1 fallback."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-stats-missing@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Dollar Fund",
            currency="USD",
            nav_values=[
                (date(2024, 1, 31), Decimal("100")),
                (date(2024, 2, 29), Decimal("100")),
            ],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _build_service(session)
        with pytest.raises(MissingFxRateError) as excinfo:
            await service.get_universe_statistics(as_of_date=date(2024, 2, 29))
    assert excinfo.value.currency == "USD"
