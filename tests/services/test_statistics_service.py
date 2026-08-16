# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Integration test for ``StatisticsService.get_universe_statistics``.

Live-DB end-to-end: seeds two investments with NAV histories, calls
the service method, asserts the :class:`UniverseStatisticsBundle`
shape and numerics agree with the analytics primitives.

Cross-tenant isolation: an investment in tenant B is never visible
to a session bound to tenant A — RLS hides the row, the bundle
omits it.
"""

from __future__ import annotations

import math
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
    TenantRepository,
    UserRepository,
    tenant_context,
)
from services.analytics import (
    compute_correlation_matrix,
    compute_full_distribution_stats,
    compute_risk_metrics,
    compute_total_return_series,
)
from services.statistics import StatisticsService


def _approx_or_nan(actual: float, expected: float, abs_tol: float = 1e-12) -> bool:
    """``pytest.approx`` does not treat NaN == NaN; this helper does."""
    if math.isnan(actual) and math.isnan(expected):
        return True
    return actual == pytest.approx(expected, abs=abs_tol)


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
            code="stats_class", display_name="Stats Class"
        )
    return actor, asset_class


async def _create_investment_with_navs(
    session,
    actor_id,
    asset_class_id,
    *,
    name: str,
    nav_values: list[tuple[date, Decimal]],
):
    inv = await InvestmentRepository(session).create(
        name=name,
        investment_type="private_equity",
        asset_class_id=asset_class_id,
        currency="EUR",
        created_by=actor_id,
    )
    nav_repo = InvestmentNavRepository(session)
    for as_of, value in nav_values:
        await nav_repo.upsert(
            investment_id=inv.id,
            as_of_date=as_of,
            nav_kind="actual",
            nav_value=value,
            currency="EUR",
            source=None,
            created_by=actor_id,
        )
    return inv


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_universe_statistics_basic_shape(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="stats-basic@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Alpha Fund",
            nav_values=[
                (date(2024, 12, 31), Decimal("100")),
                (date(2025, 3, 31), Decimal("110")),
                (date(2025, 6, 30), Decimal("121")),
                (date(2025, 9, 30), Decimal("100")),
            ],
        )
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Beta Fund",
            nav_values=[
                (date(2024, 12, 31), Decimal("200")),
                (date(2025, 3, 31), Decimal("180")),
                (date(2025, 6, 30), Decimal("220")),
                (date(2025, 9, 30), Decimal("210")),
            ],
        )

        svc = _build_service(session)
        bundle = await svc.get_universe_statistics()

    assert bundle.investment_names == ["Alpha Fund", "Beta Fund"]
    assert "Alpha Fund" in bundle.key_metrics
    assert "Beta Fund" in bundle.key_metrics
    assert "Alpha Fund" in bundle.distribution_stats
    assert "Beta Fund" in bundle.distribution_stats
    assert "Alpha Fund" in bundle.risk_metrics
    assert "Beta Fund" in bundle.risk_metrics

    assert bundle.correlation_matrix.shape == (2, 2)
    assert list(bundle.correlation_matrix.index) == ["Alpha Fund", "Beta Fund"]


async def test_get_universe_statistics_numerics_match_primitives(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="stats-numerics@example.com"
    )

    nav_values = [
        (date(2024, 12, 31), Decimal("100")),
        (date(2025, 3, 31), Decimal("110")),
        (date(2025, 6, 30), Decimal("121")),
        (date(2025, 9, 30), Decimal("100")),
    ]
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Solo Fund",
            nav_values=nav_values,
        )

        svc = _build_service(session)
        bundle = await svc.get_universe_statistics()

    nav_series = pd.Series(
        [float(v) for _, v in nav_values],
        index=[d for d, _ in nav_values],
        dtype="float64",
    )
    return_series = compute_total_return_series(nav_series)
    expected_dist = compute_full_distribution_stats(return_series)
    expected_risk = compute_risk_metrics(return_series, nav_series, risk_free_rate_annual=0.0)

    actual_dist = bundle.distribution_stats["Solo Fund"]
    actual_risk = bundle.risk_metrics["Solo Fund"]

    assert _approx_or_nan(actual_dist.mean_daily, expected_dist.mean_daily)
    assert _approx_or_nan(actual_dist.mean_annualised, expected_dist.mean_annualised)
    assert _approx_or_nan(actual_dist.std_daily, expected_dist.std_daily)
    assert _approx_or_nan(actual_dist.skewness, expected_dist.skewness)
    assert _approx_or_nan(actual_dist.kurtosis_excess, expected_dist.kurtosis_excess)
    assert _approx_or_nan(actual_risk.max_drawdown, expected_risk.max_drawdown)
    assert _approx_or_nan(actual_risk.sharpe_ratio, expected_risk.sharpe_ratio)
    assert _approx_or_nan(actual_risk.lag_1_autocorrelation, expected_risk.lag_1_autocorrelation)

    card = bundle.key_metrics["Solo Fund"]
    assert card.investment_name == "Solo Fund"
    assert card.latest_nav == pytest.approx(100.0, abs=1e-12)
    assert card.currency == "EUR"
    assert _approx_or_nan(card.annualised_return, expected_dist.mean_annualised)
    assert _approx_or_nan(card.sharpe_ratio, expected_risk.sharpe_ratio)
    assert len(card.sparkline_values) == len(return_series)


async def test_get_universe_statistics_correlation_matches_primitive(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="stats-corr@example.com"
    )

    a_navs = [
        (date(2024, 12, 31), Decimal("100")),
        (date(2025, 3, 31), Decimal("110")),
        (date(2025, 6, 30), Decimal("121")),
        (date(2025, 9, 30), Decimal("100")),
    ]
    b_navs = [
        (date(2024, 12, 31), Decimal("200")),
        (date(2025, 3, 31), Decimal("210")),
        (date(2025, 6, 30), Decimal("231")),
        (date(2025, 9, 30), Decimal("190")),
    ]

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment_with_navs(
            session, actor.id, ac.id, name="A Fund", nav_values=a_navs
        )
        await _create_investment_with_navs(
            session, actor.id, ac.id, name="B Fund", nav_values=b_navs
        )

        svc = _build_service(session)
        bundle = await svc.get_universe_statistics()

    a_series = compute_total_return_series(
        pd.Series([float(v) for _, v in a_navs], index=[d for d, _ in a_navs])
    )
    b_series = compute_total_return_series(
        pd.Series([float(v) for _, v in b_navs], index=[d for d, _ in b_navs])
    )
    expected = compute_correlation_matrix({"A Fund": a_series, "B Fund": b_series})

    assert bundle.correlation_matrix.loc["A Fund", "B Fund"] == pytest.approx(
        float(expected.loc["A Fund", "B Fund"]), abs=1e-12
    )


async def test_get_universe_statistics_filter_by_investment_id(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="stats-filter@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        a = await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Keep Me",
            nav_values=[
                (date(2024, 12, 31), Decimal("100")),
                (date(2025, 6, 30), Decimal("110")),
            ],
        )
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Drop Me",
            nav_values=[
                (date(2024, 12, 31), Decimal("200")),
                (date(2025, 6, 30), Decimal("180")),
            ],
        )

        svc = _build_service(session)
        bundle = await svc.get_universe_statistics(investment_ids=[a.id])

    assert bundle.investment_names == ["Keep Me"]
    assert "Keep Me" in bundle.key_metrics
    assert "Drop Me" not in bundle.key_metrics


async def test_get_universe_statistics_empty_universe(app_engine: AsyncEngine, seed_tenant) -> None:
    """No active investments → empty bundle (route renders empty state)."""
    tenant_id = await seed_tenant()
    actor, _ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="stats-empty@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        bundle = await svc.get_universe_statistics()

    assert bundle.investment_names == []
    assert bundle.key_metrics == {}
    assert bundle.distribution_stats == {}
    assert bundle.risk_metrics == {}
    assert bundle.correlation_matrix.empty


async def test_get_universe_statistics_cross_tenant_isolation(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Tenant A only sees its own investments, never B's."""
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    actor_a, _ac_a = await _seed_actor_and_asset_class(app_engine, tenant_a, email="a@example.com")
    actor_b, ac_b = await _seed_actor_and_asset_class(app_engine, tenant_b, email="b@example.com")

    # Seed an investment in tenant B.
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await _create_investment_with_navs(
            session,
            actor_b.id,
            ac_b.id,
            name="Tenant B Fund",
            nav_values=[
                (date(2024, 12, 31), Decimal("100")),
                (date(2025, 6, 30), Decimal("110")),
            ],
        )

    # Tenant A's view: zero investments.
    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        svc = _build_service(session)
        bundle = await svc.get_universe_statistics()

    assert bundle.investment_names == []


async def test_get_universe_statistics_as_of_date_truncates_navs(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An as-of date drops NAV rows after the cutoff before deriving returns."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="stats-asof@example.com"
    )

    nav_values = [
        (date(2024, 12, 31), Decimal("100")),
        (date(2025, 3, 31), Decimal("110")),
        (date(2025, 6, 30), Decimal("121")),
        (date(2025, 9, 30), Decimal("80")),  # post-cutoff drop, must NOT
        # affect MDD when truncated.
    ]
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Truncated Fund",
            nav_values=nav_values,
        )

        svc = _build_service(session)
        bundle = await svc.get_universe_statistics(as_of_date=date(2025, 6, 30))

    # Truncated histories rise monotonically → MDD == 0.
    risk = bundle.risk_metrics["Truncated Fund"]
    assert risk.max_drawdown == pytest.approx(0.0, abs=1e-12)
    # Latest NAV must be the cutoff value, not the post-cutoff drop.
    assert bundle.key_metrics["Truncated Fund"].latest_nav == pytest.approx(121.0, abs=1e-12)


async def test_get_universe_statistics_uses_batched_nav_fetch(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Per-repository call count must stay at one per universe load.

    Guards against re-introducing the per-investment N+1 loop removed
    in P6-H. If this fails, check whether a per-investment loop has
    re-introduced the N+1 from P6-H.
    """
    from unittest import mock

    from core.repositories import InvestmentNavRepository

    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="stats-batched@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        for name in ("Alpha Fund", "Beta Fund", "Gamma Fund"):
            await _create_investment_with_navs(
                session,
                actor.id,
                ac.id,
                name=name,
                nav_values=[
                    (date(2024, 12, 31), Decimal("100")),
                    (date(2025, 3, 31), Decimal("110")),
                    (date(2025, 6, 30), Decimal("121")),
                ],
            )

    with mock.patch.object(
        InvestmentNavRepository,
        "list_by_investments_and_kind",
        autospec=True,
        side_effect=InvestmentNavRepository.list_by_investments_and_kind,
    ) as nav_spy:
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            svc = _build_service(session)
            bundle = await svc.get_universe_statistics()

    assert len(bundle.investment_names) == 3
    assert nav_spy.call_count == 1, (
        f"Expected 1 batched NAV fetch, got {nav_spy.call_count}. "
        "If this fails, check whether a per-investment loop has "
        "re-introduced the N+1 from P6-H."
    )


async def test_get_universe_statistics_carries_all_extended_risk_metrics(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """RiskMetrics packs 13 fields per investment (sub-stream 6F-3b-Plus)."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="stats-extended@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Alpha Fund",
            nav_values=[
                (date(2024, 12, 31), Decimal("100")),
                (date(2025, 3, 31), Decimal("110")),
                (date(2025, 6, 30), Decimal("121")),
                (date(2025, 9, 30), Decimal("100")),
            ],
        )
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Beta Fund",
            nav_values=[
                (date(2024, 12, 31), Decimal("200")),
                (date(2025, 3, 31), Decimal("180")),
                (date(2025, 6, 30), Decimal("220")),
                (date(2025, 9, 30), Decimal("210")),
            ],
        )

        svc = _build_service(session)
        bundle = await svc.get_universe_statistics()

    expected_fields = [
        "var_90_daily",
        "var_95_daily",
        "var_99_daily",
        "cvar_95_daily",
        "max_drawdown",
        "ulcer_index",
        "downside_deviation",
        "sharpe_ratio",
        "sortino_ratio",
        "lag_1_autocorrelation",
        "lag_2_autocorrelation",
        "lag_3_autocorrelation",
        "lag_4_autocorrelation",
    ]
    for name, metrics in bundle.risk_metrics.items():
        for field in expected_fields:
            assert hasattr(metrics, field), f"RiskMetrics for {name} missing field {field!r}"
            value = getattr(metrics, field)
            assert isinstance(value, float), (
                f"RiskMetrics.{field} for {name} is {type(value).__name__}, expected float"
            )


async def test_get_universe_statistics_inactive_excluded_by_default(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="stats-active@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        active_inv = await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Active Fund",
            nav_values=[
                (date(2024, 12, 31), Decimal("100")),
                (date(2025, 6, 30), Decimal("110")),
            ],
        )
        inactive_inv = await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Inactive Fund",
            nav_values=[
                (date(2024, 12, 31), Decimal("100")),
                (date(2025, 6, 30), Decimal("90")),
            ],
        )
        await InvestmentRepository(session).set_active(inactive_inv.id, False)

        svc = _build_service(session)
        bundle = await svc.get_universe_statistics()

    assert bundle.investment_names == ["Active Fund"]
    assert "Inactive Fund" not in bundle.key_metrics

    # active_only=False brings the inactive one back.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        bundle_with_inactive = await svc.get_universe_statistics(active_only=False)
    assert "Inactive Fund" in bundle_with_inactive.key_metrics
    _ = active_inv  # silence linters
