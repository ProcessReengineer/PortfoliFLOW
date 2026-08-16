# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ADR-0102 conversion boundary — Portfolio Analysis (the SAA frontier).

Live-DB tests for the multi-currency behaviour
:class:`~services.portfolio_analysis.PortfolioAnalysisService` gained under
ADR-0102. Both inputs the frontier is built from are now converted into the
tenant's functional currency at the ADR-0099 §4 boundary before the pure
analytics layer sees them:

* the **NAV histories**, which become the cash-flow-adjusted return series
  the covariance matrix is estimated from — a covariance across unconverted
  currencies measures partly co-movement and partly the currencies' own FX
  paths; and
* the **current NAV weights**, which are a share-of-total and therefore
  meaningless unless the numerator and denominator share a currency.

Coverage:

* **Zero-read proof.** A functional-currency-only universe computes its
  frontier without a single ``load_rates_frame`` call — asserted with a spy
  (ADR-0099 §3).
* **Golden weight.** A hand-computed current weight for a mixed EUR/USD
  universe: the USD leg enters at its converted value, not its nominal one.
* **Cashflow conversion.** The flows that adjust the return series convert
  point-in-time alongside the NAVs they are subtracted from.
* **Missing-rate surfacing.** An uncovered USD position raises
  :class:`MissingFxRateError` rather than optimising over nominal series.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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
from services.portfolio_analysis import PortfolioAnalysisService


# ---------------------------------------------------------------------------
# Wiring / seeding helpers
# ---------------------------------------------------------------------------


def _build_service(session) -> PortfolioAnalysisService:
    return PortfolioAnalysisService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        tenants=TenantRepository(session),
        fx_rates=FxRateRepository(session),
    )


async def _seed_actor_and_asset_class(app_engine: AsyncEngine, tenant_id, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="fx_pa_class", display_name="FX PA Class"
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
    cashflows: list[tuple[datetime, str, Decimal]] | None = None,
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


def _monthly_navs(start: date, values: list[str]) -> list[tuple[date, Decimal]]:
    """Month-end-ish NAV points, 30 days apart, from a list of values."""
    return [(start + timedelta(days=30 * i), Decimal(v)) for i, v in enumerate(values)]


# A drifting pair: two investments whose *local* return paths differ enough
# for the covariance matrix to be well-conditioned over a common window.
_EUR_PATH = ["100", "104", "103", "108", "112", "110", "115", "119"]
_USD_PATH = ["200", "198", "205", "203", "210", "215", "212", "220"]
_START = date(2024, 1, 31)


# ---------------------------------------------------------------------------
# Zero-read proof (ADR-0099 §3 identity guarantee)
# ---------------------------------------------------------------------------


async def test_functional_only_universe_reads_no_fx_rows(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An all-EUR universe loads no rate frame — the frontier cannot move."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-pa-zero@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Euro Fund A",
            currency="EUR",
            nav_values=_monthly_navs(_START, _EUR_PATH),
        )
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Euro Fund B",
            currency="EUR",
            nav_values=_monthly_navs(_START, _USD_PATH),
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        fx_repo = FxRateRepository(session)
        calls: list[tuple] = []
        original = fx_repo.load_rates_frame

        async def _spy(*args, **kwargs):
            calls.append((args, kwargs))
            return await original(*args, **kwargs)

        fx_repo.load_rates_frame = _spy  # type: ignore[method-assign]

        service = PortfolioAnalysisService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            cashflows=InvestmentCashflowRepository(session),
            tenants=TenantRepository(session),
            fx_rates=fx_repo,
        )
        bundle = await service.compute_frontier()

    assert bundle is not None
    # The provable zero-change path: no FX row was ever read.
    assert calls == []
    # Current weights are the nominal NAV shares: 119 / (119 + 220).
    weights = bundle.current_weights
    assert weights is not None
    assert weights["Euro Fund A"] == pytest.approx(119.0 / (119.0 + 220.0))


# ---------------------------------------------------------------------------
# Golden weight — the USD leg enters converted
# ---------------------------------------------------------------------------


async def test_current_weights_use_converted_navs(app_engine: AsyncEngine, seed_tenant) -> None:
    """The current NAV weight of a USD fund is its *converted* share.

    Latest NAVs: EUR fund 119 EUR, USD fund 220 USD. The USD rate carried
    forward to the last NAV date is 0.90, so the USD leg enters the weight
    vector at ``220 × 0.90 = 198`` EUR — not at its nominal 220.

    Hand-computed:
        weight(EUR fund) = 119 / (119 + 198) = 119 / 317 ≈ 0.37539
        weight(USD fund) = 198 / 317         ≈ 0.62461

    The nominal (unconverted) answer would be 119 / 339 ≈ 0.35103 — well
    outside the tolerance below, so this test discriminates directly.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-pa-golden@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Euro Fund",
            currency="EUR",
            nav_values=_monthly_navs(_START, _EUR_PATH),
        )
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Dollar Fund",
            currency="USD",
            nav_values=_monthly_navs(_START, _USD_PATH),
        )
        # One rate before the first NAV date; carry-forward covers the rest.
        await _seed_fx_rates(session, actor.id, "USD", [(date(2024, 1, 1), "0.90")])

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).compute_frontier()

    assert bundle is not None
    weights = bundle.current_weights
    assert weights is not None
    converted_total = 119.0 + 220.0 * 0.90  # = 317.0
    assert weights["Euro Fund"] == pytest.approx(119.0 / converted_total)
    assert weights["Dollar Fund"] == pytest.approx(220.0 * 0.90 / converted_total)
    # And the nominal answer is *not* what we got.
    assert weights["Euro Fund"] != pytest.approx(119.0 / (119.0 + 220.0))


async def test_flat_rate_conversion_leaves_returns_unchanged(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A constant FX rate scales NAVs but must not alter *returns*.

    Scaling a NAV series by a constant leaves its ``pct_change`` untouched,
    so a USD fund converted at a flat 0.90 must produce exactly the same
    per-investment ``(volatility, expected return)`` marker as the identical
    series read as EUR. This pins the conversion as a currency restatement
    rather than an accidental transformation of the return series — the FX
    *effect* only appears when the rate itself moves (the sibling test in
    ``test_statistics_fx_conversion.py`` pins that direction).
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-pa-flat@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Euro Fund",
            currency="EUR",
            nav_values=_monthly_navs(_START, _EUR_PATH),
        )
        # Same NAV path as the EUR fund, but denominated in USD.
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Dollar Fund",
            currency="USD",
            nav_values=_monthly_navs(_START, _EUR_PATH),
        )
        await _seed_fx_rates(session, actor.id, "USD", [(date(2024, 1, 1), "0.90")])

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).compute_frontier()

    assert bundle is not None
    eur_point = bundle.investment_points["Euro Fund"]
    usd_point = bundle.investment_points["Dollar Fund"]
    # Identical return series → identical (vol, expected return) markers.
    assert usd_point[0] == pytest.approx(eur_point[0])
    assert usd_point[1] == pytest.approx(eur_point[1])
    # ... while the weights still differ, because the *levels* were scaled.
    weights = bundle.current_weights
    assert weights is not None
    assert weights["Dollar Fund"] == pytest.approx(119.0 * 0.90 / (119.0 + 119.0 * 0.90))


# ---------------------------------------------------------------------------
# Cashflow conversion — the ADR-0066 adjustment stays currency-consistent
# ---------------------------------------------------------------------------


async def test_cashflows_are_converted_with_the_navs_they_adjust(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A USD call converts point-in-time before it adjusts the USD returns.

    ``compute_cashflow_adjusted_return_series`` subtracts the flow from the
    NAV movement (ADR-0066). If the NAV were converted and the flow were
    not, the adjustment would subtract dollars from euros and the resulting
    return series — hence the whole frontier — would be silently wrong.

    Here the USD fund receives a large capital call. Converted at 0.90 the
    call is 0.90 × its nominal size, so the adjusted return differs from
    both the unconverted-flow case and the no-flow case. The assertion is
    the invariant that survives: a *rate-flat* conversion scales NAVs and
    flows by the same constant, and the cashflow-adjusted return series is
    scale-invariant — so it must equal the EUR twin's exactly.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-pa-cashflow@example.com"
    )
    call_ts = datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Euro Fund",
            currency="EUR",
            nav_values=_monthly_navs(_START, _EUR_PATH),
            cashflows=[(call_ts, "capital_call", Decimal("-3"))],
        )
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Dollar Fund",
            currency="USD",
            nav_values=_monthly_navs(_START, _EUR_PATH),
            cashflows=[(call_ts, "capital_call", Decimal("-3"))],
        )
        await _seed_fx_rates(session, actor.id, "USD", [(date(2024, 1, 1), "0.90")])

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).compute_frontier()

    assert bundle is not None
    eur_point = bundle.investment_points["Euro Fund"]
    usd_point = bundle.investment_points["Dollar Fund"]
    # Same NAV path, same flow, uniform 0.90 scaling on both → the
    # cashflow-adjusted return series must coincide. A converted NAV against
    # an unconverted flow would break this equality.
    assert usd_point[0] == pytest.approx(eur_point[0])
    assert usd_point[1] == pytest.approx(eur_point[1])


# ---------------------------------------------------------------------------
# Missing-rate surfacing
# ---------------------------------------------------------------------------


async def test_uncovered_currency_raises_missing_fx_rate(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A USD position with no USD rate fails loudly — never a 1:1 fallback."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-pa-missing@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Euro Fund",
            currency="EUR",
            nav_values=_monthly_navs(_START, _EUR_PATH),
        )
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Dollar Fund",
            currency="USD",
            nav_values=_monthly_navs(_START, _USD_PATH),
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _build_service(session)
        with pytest.raises(MissingFxRateError) as excinfo:
            await service.compute_frontier()
    assert excinfo.value.currency == "USD"
