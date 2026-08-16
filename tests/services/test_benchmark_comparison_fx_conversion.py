# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ADR-0102 conversion boundary — Benchmarks & Attribution (live DB).

Live-DB companions to the ADR-0102 section in
``tests/services/test_benchmark_comparison_service.py``. That file pins the
conversion against fabricated repositories; this one runs the same boundary
against the real ``FxRateRepository`` (real carry-forward over real rows) and
the real ``TenantRepository`` functional currency — the shape its two ADR-0102
siblings (``test_statistics_fx_conversion.py``,
``test_portfolio_analysis_fx_conversion.py``) already use.

Why this exists on top of the mock-based tests: those cover a *single-currency*
book. :class:`~services.benchmark_comparison.BenchmarkComparisonService` has
three independent NAV-assembly sites, each building its own converter, and two
of them — the Stage-b asset-class composite and the Stage-c actual-portfolio
line — are **NAV-weighted blends across investments**. A weighted blend is the
one place where converting the *returns* but not the *weights* (or the reverse)
silently changes the answer, and a book with one currency cannot detect it. The
mixed EUR/USD fixture below can:

    EUR fund   NAV 100 → 130      (return +30 %),  weight 100 EUR
    USD fund   NAV 100 → 100 USD  (flat locally),  weight 100 USD
    USD rate   2.00 → 2.20        (USD appreciates 10 % against EUR)

Converted, the USD fund is 200 → 220 EUR: a +10 % return on a 200 EUR weight.
The four possible implementations land on four distinct numbers, so a single
equality discriminates all of them:

    both converted (correct)   (100×0.30 + 200×0.10) / 300 = 0.166667
    neither converted          (100×0.30 + 100×0.00) / 200 = 0.15
    weights only               (100×0.30 + 200×0.00) / 300 = 0.10
    returns only               (100×0.30 + 100×0.10) / 200 = 0.20

Coverage:

* **Zero-read proof.** An EUR-only book runs both stages without a single
  ``load_rates_frame`` call — asserted with a spy (ADR-0099 §3).
* **Stage b golden blend.** The composite's NAV weights are the converted
  values, not the nominal ones.
* **Stage c golden blend.** The actual-portfolio line — the synthetic
  ``_portfolio`` class — blends the same converted book.
* **Missing-rate surfacing.** An uncovered USD position raises
  :class:`MissingFxRateError` from both stages rather than blending nominal
  series.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import MissingFxRateError
from core.repositories import (
    AssetClassBenchmarkMappingRepository,
    AssetClassRepository,
    BenchmarkObservationRepository,
    BenchmarkRepository,
    FxRateRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    SAAAssetClassInputRepository,
    SAAConfigurationRepository,
    SAACorrelationRepository,
    TenantRepository,
    UserRepository,
    tenant_context,
)
from services.benchmark_comparison import BenchmarkComparisonService
from services.saa import SAAService


# ---------------------------------------------------------------------------
# Wiring / seeding helpers
# ---------------------------------------------------------------------------


def _build_service(session) -> BenchmarkComparisonService:
    """Construct the orchestrator over live repositories.

    Mirrors ``web/routes/benchmarks_attribution.py::_build_service``. No SAA
    configuration is seeded by these tests, so ``_resolve_risk_free_rate``
    falls back to ``0.0`` — the risk-free rate is orthogonal to the currency
    conversion under test.
    """
    saa_service = SAAService(
        configurations=SAAConfigurationRepository(session),
        asset_classes=AssetClassRepository(session),
        inputs=SAAAssetClassInputRepository(session),
        correlations=SAACorrelationRepository(session),
    )
    return BenchmarkComparisonService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        asset_classes=AssetClassRepository(session),
        benchmarks=BenchmarkRepository(session),
        benchmark_observations=BenchmarkObservationRepository(session),
        mappings=AssetClassBenchmarkMappingRepository(session),
        saa_service=saa_service,
        tenants=TenantRepository(session),
        fx_rates=FxRateRepository(session),
    )


async def _seed_actor_and_asset_class(app_engine: AsyncEngine, tenant_id, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="fx_bm_class", display_name="FX Benchmark Class"
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


async def _create_mapped_benchmark(
    session,
    actor_id,
    asset_class_id,
    *,
    observations: list[tuple[date, Decimal]],
):
    """Seed a benchmark, map it to the asset class, and give it observations.

    Stage b only emits a composite row for an asset class that carries a
    benchmark mapping, so the mapping is scaffolding rather than the subject
    of these tests. The observations return exactly zero throughout: the
    benchmark contributes no movement of its own, which keeps every number
    below traceable to the investments' converted NAVs.
    """
    benchmark = await BenchmarkRepository(session).create(
        code="BM_FX",
        display_name="Zero-Return Benchmark",
        description=None,
        provider_hint=None,
        created_by=actor_id,
    )
    await AssetClassBenchmarkMappingRepository(session).upsert_mapping(
        asset_class_id=asset_class_id,
        benchmark_id=benchmark.id,
        weight=Decimal("1.0"),
    )
    await BenchmarkObservationRepository(session).replace_observations_for_benchmark(
        benchmark.id, observations
    )
    return benchmark


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


# The two NAV dates that bracket the single month under test. Two points
# produce exactly one monthly return (the first pct_change is NaN and
# dropped), so every composite below is a one-month blend that can be
# hand-computed in full.
_PRIOR_MONTH_END = date(2024, 1, 31)
_MONTH_END = date(2024, 2, 29)

# The blend the module docstring derives. Distinct from all three
# partial-conversion answers (0.15, 0.10, 0.20).
_CONVERTED_BLEND = (100.0 * 0.30 + 200.0 * 0.10) / 300.0  # = 0.1666…
_NOMINAL_BLEND = (100.0 * 0.30 + 100.0 * 0.00) / 200.0  # = 0.15

_ZERO_OBSERVATIONS = [
    (_PRIOR_MONTH_END, Decimal("0")),
    (_MONTH_END, Decimal("0")),
]


async def _seed_mixed_currency_book(
    app_engine: AsyncEngine,
    tenant_id,
    actor,
    asset_class,
    *,
    with_fx_rates: bool = True,
) -> None:
    """Seed the mixed EUR/USD book the module docstring hand-computes.

    Args:
        with_fx_rates: When False the USD rows are seeded without any USD
            rate — the uncovered-currency case.
    """
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            asset_class.id,
            name="Euro Fund",
            currency="EUR",
            nav_values=[
                (_PRIOR_MONTH_END, Decimal("100")),
                (_MONTH_END, Decimal("130")),
            ],
        )
        # Flat in USD: everything this leg contributes is the currency move.
        await _create_investment(
            session,
            actor.id,
            asset_class.id,
            name="Dollar Fund",
            currency="USD",
            nav_values=[
                (_PRIOR_MONTH_END, Decimal("100")),
                (_MONTH_END, Decimal("100")),
            ],
        )
        await _create_mapped_benchmark(
            session,
            actor.id,
            asset_class.id,
            observations=_ZERO_OBSERVATIONS,
        )
        if with_fx_rates:
            await _seed_fx_rates(
                session,
                actor.id,
                "USD",
                [
                    (_PRIOR_MONTH_END, "2.00"),
                    (_MONTH_END, "2.20"),
                ],
            )


# ---------------------------------------------------------------------------
# Zero-read proof (ADR-0099 §3 identity guarantee)
# ---------------------------------------------------------------------------


async def test_functional_only_universe_reads_no_fx_rows(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An all-EUR book loads no rate frame — neither stage can move.

    Both stages are driven in one session so the spy covers all the
    converters the section builds: Stage a builds one per call, Stage b
    another. The identity short-circuit in ``build_portfolio_fx_converter``
    fires before either issues a query.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-bm-zero@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment(
            session,
            actor.id,
            ac.id,
            name="Euro Fund",
            currency="EUR",
            nav_values=[
                (_PRIOR_MONTH_END, Decimal("100")),
                (_MONTH_END, Decimal("130")),
            ],
        )
        await _create_mapped_benchmark(session, actor.id, ac.id, observations=_ZERO_OBSERVATIONS)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        fx_repo = FxRateRepository(session)
        calls: list[tuple] = []
        original = fx_repo.load_rates_frame

        async def _spy(*args, **kwargs):
            calls.append((args, kwargs))
            return await original(*args, **kwargs)

        fx_repo.load_rates_frame = _spy  # type: ignore[method-assign]

        saa_service = SAAService(
            configurations=SAAConfigurationRepository(session),
            asset_classes=AssetClassRepository(session),
            inputs=SAAAssetClassInputRepository(session),
            correlations=SAACorrelationRepository(session),
        )
        service = BenchmarkComparisonService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            asset_classes=AssetClassRepository(session),
            benchmarks=BenchmarkRepository(session),
            benchmark_observations=BenchmarkObservationRepository(session),
            mappings=AssetClassBenchmarkMappingRepository(session),
            saa_service=saa_service,
            tenants=TenantRepository(session),
            fx_rates=fx_repo,
        )
        comparisons = await service.get_investment_comparisons(as_of_date=_MONTH_END)
        composites = await service.get_asset_class_composites(as_of_date=_MONTH_END)

    # The provable zero-change path: no FX row was ever read.
    assert calls == []
    # ... and both stages still produced their rows against the nominal NAVs.
    assert [r.investment_name for r in comparisons.rows] == ["Euro Fund"]
    assert [r.asset_class_code for r in composites.rows] == ["fx_bm_class"]
    euro_row = composites.rows[0]
    assert euro_row.n_investments == 1
    assert euro_row.composite_cumulative_returns.iloc[-1] == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# Stage b — the composite's NAV weights are the converted ones
# ---------------------------------------------------------------------------


async def test_composite_weights_use_converted_navs(app_engine: AsyncEngine, seed_tenant) -> None:
    """One asset class, two currencies: the blend uses converted weights.

    Per ADR-0061 the composite for a month is
    ``sum(w_i × r_i) / sum(w_i)`` with ``w_i`` the constituent's NAV at the
    *previous* month-end. Here the USD fund's month-start NAV is 100 USD,
    which enters the weight vector at ``100 × 2.00 = 200`` EUR — double the
    EUR fund's 100. Its converted return over the month is +10 % (the rate
    moving 2.00 → 2.20 on a locally flat NAV).

    Hand-computed: ``(100 × 0.30 + 200 × 0.10) / 300 = 0.1667``. The nominal
    answer — 100/100 weights and a 0 % USD return — is ``0.15``, and both
    half-converted answers (0.10 and 0.20, see the module docstring) sit
    further away still. The equality below therefore discriminates every one
    of them.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-bm-composite@example.com"
    )
    await _seed_mixed_currency_book(app_engine, tenant_id, actor, ac)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).get_asset_class_composites(as_of_date=_MONTH_END)

    assert len(bundle.rows) == 1
    row = bundle.rows[0]
    assert row.n_investments == 2
    # A single aligned month, so the cumulative endpoint *is* the blend.
    composite = row.composite_cumulative_returns
    assert len(composite) == 1
    assert composite.iloc[-1] == pytest.approx(_CONVERTED_BLEND)
    # And the nominal blend is decisively not what we got.
    assert composite.iloc[-1] != pytest.approx(_NOMINAL_BLEND)
    # The benchmark returned zero throughout, so the excess is the blend.
    assert row.benchmark_cumulative_returns.iloc[-1] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Stage c — the actual-portfolio line blends the converted book
# ---------------------------------------------------------------------------


async def test_actual_portfolio_line_is_converted(app_engine: AsyncEngine, seed_tenant) -> None:
    """The synthetic ``_portfolio`` class is a converted NAV-weighted blend.

    ``_build_actual_portfolio_returns`` is the Actual line of the Stage-c
    SAA-hypothetical chart, and it is the third of the service's three
    conversion sites — it assembles its NAVs and builds its converter
    independently of Stage b, so Stage b passing says nothing about it. It is
    exercised directly here rather than through ``get_saa_hypothetical``: the
    full Stage-c path additionally needs an optimizable SAA configuration,
    which is orthogonal to the currency blend and is already covered
    end-to-end by the mock-based ``test_stage_c_actual_portfolio_returns_are
    _converted``.

    The whole book is one asset class here, so the expected value coincides
    with the Stage-b blend — the same ``0.1667`` the module docstring derives.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-bm-actual@example.com"
    )
    await _seed_mixed_currency_book(app_engine, tenant_id, actor, ac)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _build_service(session)
        actual = await service._build_actual_portfolio_returns(_MONTH_END)

    assert len(actual) == 1
    assert actual.iloc[-1] == pytest.approx(_CONVERTED_BLEND)
    assert actual.iloc[-1] != pytest.approx(_NOMINAL_BLEND)


# ---------------------------------------------------------------------------
# Missing-rate surfacing
# ---------------------------------------------------------------------------


async def test_uncovered_currency_raises_missing_fx_rate(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A USD position with no USD rate fails loudly — never a 1:1 fallback.

    Asserted on both stages: each assembles its NAVs and builds its own
    converter, so neither inherits the other's failure.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="fx-bm-missing@example.com"
    )
    await _seed_mixed_currency_book(app_engine, tenant_id, actor, ac, with_fx_rates=False)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _build_service(session)

        with pytest.raises(MissingFxRateError) as excinfo:
            await service.get_investment_comparisons(as_of_date=_MONTH_END)
        assert excinfo.value.currency == "USD"

        with pytest.raises(MissingFxRateError) as excinfo:
            await service.get_asset_class_composites(as_of_date=_MONTH_END)
        assert excinfo.value.currency == "USD"
