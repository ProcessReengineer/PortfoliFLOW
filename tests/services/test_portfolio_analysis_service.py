# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Integration test for ``PortfolioAnalysisService.compute_frontier``.

Live-DB end-to-end: seeds two investments with overlapping NAV
histories, calls the service method, asserts the
:class:`PortfolioAnalysisBundle` shape, that the analytics-layer
output matches a direct call against the same inputs, and that
cross-tenant isolation holds.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
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
from services.analytics import (
    compute_cashflow_adjusted_return_series,
    compute_total_return_series,
    derive_expected_returns_and_cov,
)
from services.portfolio_analysis import (
    PortfolioAnalysisBundle,
    PortfolioAnalysisService,
)


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
            code="pa_class", display_name="Portfolio Analysis Class"
        )
    return actor, asset_class


async def _create_investment_with_navs(
    session,
    actor_id,
    asset_class_id,
    *,
    name: str,
    nav_values: list[tuple[date, Decimal]],
    is_active: bool = True,
):
    inv = await InvestmentRepository(session).create(
        name=name,
        investment_type="private_equity",
        asset_class_id=asset_class_id,
        currency="EUR",
        created_by=actor_id,
        is_active=is_active,
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


async def _seed_cashflows(
    session,
    actor_id,
    investment_id,
    *,
    flows: list[tuple[datetime, str, Decimal]],
) -> None:
    """Append actual cashflows for an investment.

    ``flows`` are ``(flow_timestamp, flow_type, signed_amount)`` tuples
    — calls negative, distributions positive (ADR-0043 §1).
    """
    cf_repo = InvestmentCashflowRepository(session)
    for flow_timestamp, flow_type, amount in flows:
        await cf_repo.create(
            investment_id=investment_id,
            flow_timestamp=flow_timestamp,
            flow_type=flow_type,
            flow_kind="actual",
            amount=amount,
            currency="EUR",
            description=None,
            created_by=actor_id,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_compute_frontier_returns_bundle(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pa-basic@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Alpha Fund",
            nav_values=[
                (date(2024, 3, 31), Decimal("100")),
                (date(2024, 6, 30), Decimal("104")),
                (date(2024, 9, 30), Decimal("110")),
                (date(2024, 12, 31), Decimal("112")),
                (date(2025, 3, 31), Decimal("118")),
                (date(2025, 6, 30), Decimal("121")),
            ],
        )
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Beta Fund",
            nav_values=[
                (date(2024, 3, 31), Decimal("200")),
                (date(2024, 6, 30), Decimal("198")),
                (date(2024, 9, 30), Decimal("210")),
                (date(2024, 12, 31), Decimal("215")),
                (date(2025, 3, 31), Decimal("220")),
                (date(2025, 6, 30), Decimal("225")),
            ],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _build_service(session)
        bundle = await service.compute_frontier(
            n_points=40,
            risk_free_rate=0.025,
        )

    assert isinstance(bundle, PortfolioAnalysisBundle)
    assert bundle.frontier_result.frontier_returns.size > 0
    assert bundle.frontier_result.frontier_volatilities.size > 0
    assert bundle.frontier_result.frontier_weights.shape[1] == 2
    assert sorted(bundle.frontier_result.asset_names) == [
        "Alpha Fund",
        "Beta Fund",
    ]
    assert math.isfinite(bundle.tangency.expected_return)
    assert math.isfinite(bundle.tangency.volatility)
    assert math.isfinite(bundle.tangency.sharpe_ratio)
    assert math.isfinite(bundle.min_variance.expected_return)
    assert math.isfinite(bundle.min_variance.volatility)
    # Tangency lies right of (or at) the global min-var point.
    assert bundle.tangency.volatility >= (bundle.min_variance.volatility - 1e-8)
    # Investment markers carry exactly the two investments.
    assert set(bundle.investment_points.keys()) == {
        "Alpha Fund",
        "Beta Fund",
    }
    # CML has 50 sample points by default.
    assert len(bundle.capital_market_line.points) == 50
    # The risk-free rate is echoed back.
    assert bundle.risk_free_rate == pytest.approx(0.025)
    # n_points_requested round-trips.
    assert bundle.n_points_requested == 40


async def test_compute_frontier_returns_none_for_single_investment(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pa-single@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Solo Fund",
            nav_values=[
                (date(2025, 3, 31), Decimal("100")),
                (date(2025, 6, 30), Decimal("110")),
            ],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).compute_frontier()

    assert bundle is None


async def test_compute_frontier_returns_none_for_empty_universe(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, _ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pa-empty@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).compute_frontier()

    assert bundle is None


async def test_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    """An investment seeded in tenant B is invisible to a tenant A session."""
    tenant_a = await seed_tenant("A")
    tenant_b = await seed_tenant("B")

    actor_a, _ac_a = await _seed_actor_and_asset_class(
        app_engine, tenant_a, email="pa-a@example.com"
    )
    actor_b, ac_b = await _seed_actor_and_asset_class(
        app_engine, tenant_b, email="pa-b@example.com"
    )

    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await _create_investment_with_navs(
            session,
            actor_b.id,
            ac_b.id,
            name="Tenant-B Alpha",
            nav_values=[
                (date(2024, 6, 30), Decimal("100")),
                (date(2024, 12, 31), Decimal("110")),
                (date(2025, 6, 30), Decimal("120")),
            ],
        )
        await _create_investment_with_navs(
            session,
            actor_b.id,
            ac_b.id,
            name="Tenant-B Beta",
            nav_values=[
                (date(2024, 6, 30), Decimal("200")),
                (date(2024, 12, 31), Decimal("190")),
                (date(2025, 6, 30), Decimal("210")),
            ],
        )

    # Tenant A has no investments; bundle must be None and must not
    # leak any reference to Tenant B's investments.
    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        bundle_a = await _build_service(session).compute_frontier()
    assert bundle_a is None

    # Tenant B's own session sees its investments.
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        bundle_b = await _build_service(session).compute_frontier(n_points=30)
    assert bundle_b is not None
    assert sorted(bundle_b.frontier_result.asset_names) == [
        "Tenant-B Alpha",
        "Tenant-B Beta",
    ]


async def test_inactive_investments_are_excluded(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pa-inactive@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Active A",
            nav_values=[
                (date(2024, 6, 30), Decimal("100")),
                (date(2024, 12, 31), Decimal("110")),
                (date(2025, 6, 30), Decimal("120")),
            ],
        )
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Active B",
            nav_values=[
                (date(2024, 6, 30), Decimal("200")),
                (date(2024, 12, 31), Decimal("190")),
                (date(2025, 6, 30), Decimal("210")),
            ],
        )
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Inactive C",
            nav_values=[
                (date(2024, 6, 30), Decimal("300")),
                (date(2024, 12, 31), Decimal("310")),
                (date(2025, 6, 30), Decimal("320")),
            ],
            is_active=False,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).compute_frontier(n_points=30)

    assert bundle is not None
    assert sorted(bundle.frontier_result.asset_names) == [
        "Active A",
        "Active B",
    ]
    assert "Inactive C" not in bundle.investment_points


async def test_current_portfolio_is_finite_with_navs(app_engine: AsyncEngine, seed_tenant) -> None:
    """Latest NAVs produce a definable current portfolio.

    Uses six NAV observations per investment so the annualised
    statistics stay well-conditioned; the service uses the same
    daily-period annualisation convention as the QT widget, which
    needs more than two return observations to behave well.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pa-current@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Alpha Fund",
            nav_values=[
                (date(2024, 3, 31), Decimal("100")),
                (date(2024, 6, 30), Decimal("104")),
                (date(2024, 9, 30), Decimal("110")),
                (date(2024, 12, 31), Decimal("112")),
                (date(2025, 3, 31), Decimal("118")),
                (date(2025, 6, 30), Decimal("121")),
            ],
        )
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Beta Fund",
            nav_values=[
                (date(2024, 3, 31), Decimal("200")),
                (date(2024, 6, 30), Decimal("198")),
                (date(2024, 9, 30), Decimal("210")),
                (date(2024, 12, 31), Decimal("215")),
                (date(2025, 3, 31), Decimal("220")),
                (date(2025, 6, 30), Decimal("225")),
            ],
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).compute_frontier(n_points=20)

    assert bundle is not None
    cp_vol, cp_ret = bundle.current_portfolio
    assert math.isfinite(cp_vol)
    assert math.isfinite(cp_ret)
    # The normalised per-asset current allocation is present whenever
    # the current-portfolio position is defined, keyed over the
    # frontier asset universe and summing to 1.
    assert bundle.current_weights is not None
    assert set(bundle.current_weights) <= set(bundle.frontier_result.asset_names)
    assert sum(bundle.current_weights.values()) == pytest.approx(1.0)


def test_normalise_current_weights_populated_and_undefined() -> None:
    """The bundle's ``current_weights`` normalisation guard.

    Mirrors the ``(nan, nan)`` guard of
    :func:`compute_current_portfolio_position`: a finite, non-zero
    raw-weight total normalises to a dict that sums to 1 over the
    frontier asset universe (zero-weight assets included); an
    all-zero / empty total yields ``None`` — exactly the case that
    drives ``current_portfolio`` to ``(nan, nan)``.
    """
    asset_names = ["Alpha Fund", "Beta Fund", "Gamma Fund"]

    # Populated: raw NAV-share weights (need not sum to 1) normalise
    # over the full universe; the absent asset gets an implicit zero.
    normalised = PortfolioAnalysisService._normalise_current_weights(
        {"Alpha Fund": 30.0, "Beta Fund": 10.0},
        asset_names=asset_names,
    )
    assert normalised is not None
    assert set(normalised) == set(asset_names)
    assert sum(normalised.values()) == pytest.approx(1.0)
    assert normalised["Alpha Fund"] == pytest.approx(0.75)
    assert normalised["Beta Fund"] == pytest.approx(0.25)
    assert normalised["Gamma Fund"] == pytest.approx(0.0)

    # Undefined: empty / all-zero weights → None (the same guard that
    # makes the current portfolio (nan, nan)).
    assert PortfolioAnalysisService._normalise_current_weights({}, asset_names=asset_names) is None
    assert (
        PortfolioAnalysisService._normalise_current_weights(
            {"Alpha Fund": 0.0, "Beta Fund": 0.0},
            asset_names=asset_names,
        )
        is None
    )


async def test_frontier_marker_uses_cashflow_adjusted_returns(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An investment with flows plots at its cash-flow-adjusted stats.

    Seeds two investments on an identical NAV grid (so the common-
    window restriction is an identity and the comparison is exact).
    The flowed investment carries interior capital calls and a
    distribution. Its frontier marker must equal the cash-flow-
    adjusted annualised stats — and must diverge from what plain
    NAV ``pct_change`` would have produced, proving the ADR-0066 fix
    is active. (ADR-0066)
    """
    from services.analytics.sample_window import restrict_to_common_window

    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pa-cfadj@example.com"
    )

    nav_dates = [
        date(2024, 3, 31),
        date(2024, 6, 30),
        date(2024, 9, 30),
        date(2024, 12, 31),
        date(2025, 3, 31),
        date(2025, 6, 30),
    ]
    # Magnitudes mirror the smooth, well-conditioned data of
    # test_compute_frontier_returns_bundle (max ~6 %/interval) so the
    # optimiser stays solvable under the daily-period annualisation.
    alpha_navs = [
        Decimal("100"),
        Decimal("104"),
        Decimal("110"),
        Decimal("112"),
        Decimal("118"),
        Decimal("121"),
    ]
    beta_navs = [
        Decimal("200"),
        Decimal("198"),
        Decimal("210"),
        Decimal("215"),
        Decimal("220"),
        Decimal("225"),
    ]
    # Interior flows on the flowed investment: two small capital calls
    # and a distribution, each strictly between NAV observation dates.
    # They are small relative to NAV (so returns stay modest) yet large
    # enough to make the adjusted series diverge from plain pct_change.
    alpha_flows = [
        (
            datetime(2024, 5, 15, 12, tzinfo=timezone.utc),
            "capital_call",
            Decimal("-5"),
        ),
        (
            datetime(2024, 11, 15, 12, tzinfo=timezone.utc),
            "capital_call",
            Decimal("-4"),
        ),
        (
            datetime(2025, 5, 15, 12, tzinfo=timezone.utc),
            "distribution",
            Decimal("3"),
        ),
    ]

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        alpha = await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Alpha Flow Fund",
            nav_values=list(zip(nav_dates, alpha_navs, strict=True)),
        )
        await _seed_cashflows(session, actor.id, alpha.id, flows=alpha_flows)
        await _create_investment_with_navs(
            session,
            actor.id,
            ac.id,
            name="Beta Plain Fund",
            nav_values=list(zip(nav_dates, beta_navs, strict=True)),
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).compute_frontier(n_points=30)

    assert bundle is not None
    assert "Alpha Flow Fund" in bundle.investment_points

    # Replicate the service pipeline inline for both return derivations.
    nav_index = pd.to_datetime(nav_dates)
    nav_alpha = pd.Series(
        [float(v) for v in alpha_navs], index=nav_index, dtype="float64"
    ).sort_index()
    nav_beta = pd.Series(
        [float(v) for v in beta_navs], index=nav_index, dtype="float64"
    ).sort_index()
    cf_alpha = pd.DataFrame(
        {
            "flow_timestamp": [f[0] for f in alpha_flows],
            "flow_type": [f[1] for f in alpha_flows],
            "amount": [float(f[2]) for f in alpha_flows],
        }
    )
    empty_cf = pd.DataFrame({"flow_timestamp": [], "flow_type": [], "amount": []})

    def _marker(returns_by_name: dict[str, pd.Series]) -> tuple[float, float]:
        usable = {n: s for n, s in returns_by_name.items() if not s.empty}
        aligned = pd.DataFrame(usable)
        aligned.index = pd.to_datetime(aligned.index)
        df_window, _ = restrict_to_common_window(aligned)
        cols = {c: df_window[c] for c in df_window.columns}
        exp, cov = derive_expected_returns_and_cov(cols)
        i = list(exp.index).index("Alpha Flow Fund")
        vol = float(np.sqrt(np.diag(cov.to_numpy(dtype=float)))[i])
        return vol, float(exp.iloc[i])

    adj_vol, adj_ret = _marker(
        {
            "Alpha Flow Fund": compute_cashflow_adjusted_return_series(nav_alpha, cf_alpha),
            "Beta Plain Fund": compute_cashflow_adjusted_return_series(nav_beta, empty_cf),
        }
    )
    _plain_vol, plain_ret = _marker(
        {
            "Alpha Flow Fund": compute_total_return_series(nav_alpha),
            "Beta Plain Fund": compute_total_return_series(nav_beta),
        }
    )

    marker_vol, marker_ret = bundle.investment_points["Alpha Flow Fund"]
    # The bundle marker matches the cash-flow-adjusted stats.
    assert marker_ret == pytest.approx(adj_ret, rel=1e-9)
    assert marker_vol == pytest.approx(adj_vol, rel=1e-9)
    # And the adjusted stats genuinely diverge from the pct_change ones,
    # proving the fix changed the numbers for the flowed investment.
    assert adj_ret != pytest.approx(plain_ret, rel=1e-6)
    assert marker_ret != pytest.approx(plain_ret, rel=1e-6)


# ---------------------------------------------------------------------------
# P6-H batching regression guard
# ---------------------------------------------------------------------------


async def test_compute_frontier_uses_batched_repository_methods(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The batched NAV fetch fires once regardless of universe size.

    Guards against re-introducing the per-investment N+1 loop removed
    in sub-stream 6F-3c. ``list_by_investments_and_kind`` must fire
    exactly once across the entire ``compute_frontier`` invocation,
    and the singular ``list_by_investment_and_kind`` must not fire at
    all. If this fails, check whether a per-investment loop has
    re-introduced the N+1 (see P6-H / sub-stream 6F-3c).
    """
    from unittest import mock

    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pa-batched@example.com"
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        for i, name in enumerate(("Inv-1", "Inv-2", "Inv-3"), start=1):
            await _create_investment_with_navs(
                session,
                actor.id,
                ac.id,
                name=name,
                nav_values=[
                    (date(2024, 3, 31), Decimal(100 * i)),
                    (date(2024, 6, 30), Decimal(104 * i)),
                    (date(2024, 9, 30), Decimal(110 * i)),
                    (date(2024, 12, 31), Decimal(112 * i)),
                    (date(2025, 3, 31), Decimal(118 * i)),
                    (date(2025, 6, 30), Decimal(121 * i)),
                ],
            )

    with (
        mock.patch.object(
            InvestmentNavRepository,
            "list_by_investments_and_kind",
            autospec=True,
            side_effect=InvestmentNavRepository.list_by_investments_and_kind,
        ) as batched_spy,
        mock.patch.object(
            InvestmentNavRepository,
            "list_by_investment_and_kind",
            autospec=True,
            side_effect=InvestmentNavRepository.list_by_investment_and_kind,
        ) as singular_spy,
    ):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            bundle = await _build_service(session).compute_frontier(n_points=20)

    assert bundle is not None
    assert batched_spy.call_count == 1, (
        f"Expected 1 batched NAV fetch, got {batched_spy.call_count}. "
        "If this fails, check whether a per-investment loop has "
        "re-introduced the N+1 from sub-stream 6F-3c."
    )
    assert singular_spy.call_count == 0, (
        f"Singular per-investment NAV fetch must not fire, got "
        f"{singular_spy.call_count} call(s). The service must use the "
        "batched plural method exclusively."
    )


# ---------------------------------------------------------------------------
# ADR-0103 §8 — cash is never a frontier asset
# ---------------------------------------------------------------------------


async def _create_unitised_cash_with_dense_navs(
    session,
    actor_id,
    asset_class_id,
    *,
    name: str = "USD Cash",
):
    """Seed a unitised cash position with a dense actual NAV series.

    Mirrors the shape a v32 Cash-sheet import materialises (ADR-0103
    §3): ``investment_type='cash'``, ``valuation_mode='unitised'``, and
    a long, regular NAV series — month-ends spanning *wider* than the
    quarterly funds seeded alongside it. Both properties matter to the
    pin: were the seam filter to regress, the dense index would enter
    the alignment frame and the common-window restriction would visibly
    move.
    """
    inv = await InvestmentRepository(session).create(
        name=name,
        investment_type="cash",
        asset_class_id=asset_class_id,
        currency="EUR",
        created_by=actor_id,
        valuation_mode="unitised",
    )
    nav_repo = InvestmentNavRepository(session)
    month_ends = pd.date_range("2024-01-31", "2025-07-31", freq="ME")
    for i, timestamp in enumerate(month_ends):
        await nav_repo.upsert(
            investment_id=inv.id,
            as_of_date=timestamp.date(),
            nav_kind="actual",
            nav_value=Decimal("1000") + Decimal(i) * Decimal("7"),
            currency="EUR",
            source=None,
            created_by=actor_id,
        )
    return inv


async def _seed_two_quarterly_funds(session, actor_id, asset_class_id):
    """Seed the two non-cash funds the frontier tests optimise over."""
    alpha = await _create_investment_with_navs(
        session,
        actor_id,
        asset_class_id,
        name="Alpha Fund",
        nav_values=[
            (date(2024, 3, 31), Decimal("100")),
            (date(2024, 6, 30), Decimal("104")),
            (date(2024, 9, 30), Decimal("110")),
            (date(2024, 12, 31), Decimal("112")),
            (date(2025, 3, 31), Decimal("118")),
            (date(2025, 6, 30), Decimal("121")),
        ],
    )
    beta = await _create_investment_with_navs(
        session,
        actor_id,
        asset_class_id,
        name="Beta Fund",
        nav_values=[
            (date(2024, 3, 31), Decimal("200")),
            (date(2024, 6, 30), Decimal("198")),
            (date(2024, 9, 30), Decimal("210")),
            (date(2024, 12, 31), Decimal("215")),
            (date(2025, 3, 31), Decimal("220")),
            (date(2025, 6, 30), Decimal("225")),
        ],
    )
    return alpha, beta


def _assert_bundles_identical(
    before: PortfolioAnalysisBundle, after: PortfolioAnalysisBundle
) -> None:
    """Assert two bundles are numerically identical, field by field.

    Exact equality, not tolerance: the optimiser is deterministic, so
    identical inputs must produce an identical payload. Anything short
    of exact equality would mean the cash row perturbed the inputs.
    """
    fr_before = before.frontier_result
    fr_after = after.frontier_result
    assert fr_after.asset_names == fr_before.asset_names
    for field in (
        "frontier_returns",
        "frontier_volatilities",
        "frontier_weights",
        "expected_returns",
        "cov_matrix",
    ):
        np.testing.assert_array_equal(
            getattr(fr_after, field),
            getattr(fr_before, field),
            err_msg=f"frontier_result.{field} changed when cash was added",
        )

    for portfolio_field in ("tangency", "min_variance"):
        p_before = getattr(before, portfolio_field)
        p_after = getattr(after, portfolio_field)
        assert p_after.asset_names == p_before.asset_names
        np.testing.assert_array_equal(p_after.weights, p_before.weights)
        assert p_after.expected_return == p_before.expected_return
        assert p_after.volatility == p_before.volatility

    assert after.tangency.sharpe_ratio == before.tangency.sharpe_ratio
    assert after.current_portfolio == before.current_portfolio
    assert after.current_weights == before.current_weights
    assert after.investment_points == before.investment_points
    assert after.risk_free_rate == before.risk_free_rate


async def test_cash_position_never_enters_the_frontier_universe(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A unitised cash position is invisible to the optimiser (ADR-0103 §8).

    The headline pin. Computes the frontier over two funds, adds a
    unitised cash position with a dense actual NAV series to the same
    book, and re-computes: the cash row appears in no part of the
    payload, and the two-fund frontier is *numerically identical* —
    cash's presence in the book cannot be observed from the result.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pa-cash-excluded@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _seed_two_quarterly_funds(session, actor.id, ac.id)

    # Frontier over the book *without* any cash row.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle_before = await _build_service(session).compute_frontier(
            n_points=40, risk_free_rate=0.025
        )
    assert bundle_before is not None

    # Same book, now holding a dense unitised cash position.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _create_unitised_cash_with_dense_navs(session, actor.id, ac.id, name="USD Cash")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle_after = await _build_service(session).compute_frontier(
            n_points=40, risk_free_rate=0.025
        )
    assert bundle_after is not None

    # The cash row appears nowhere in the payload.
    assert sorted(bundle_after.frontier_result.asset_names) == [
        "Alpha Fund",
        "Beta Fund",
    ]
    assert "USD Cash" not in bundle_after.frontier_result.asset_names
    assert "USD Cash" not in bundle_after.tangency.asset_names
    assert "USD Cash" not in bundle_after.min_variance.asset_names
    assert "USD Cash" not in bundle_after.investment_points
    assert bundle_after.current_weights is not None
    assert "USD Cash" not in bundle_after.current_weights
    # Weights carry one column per frontier asset — cash added none.
    assert bundle_after.frontier_result.frontier_weights.shape[1] == 2

    # And the frontier is byte-identical to the cash-free run.
    _assert_bundles_identical(bundle_before, bundle_after)

    # The CML is the risk-free anchor's only channel into the result
    # (ADR-0103 §8): it is a function of risk_free_rate and the
    # tangency, so it too is unmoved by a cash row in the book.
    assert bundle_after.capital_market_line.points == bundle_before.capital_market_line.points


async def test_explicitly_selected_cash_id_is_filtered_not_honoured(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A cash id passed in ``investment_ids`` is dropped (ADR-0103 §8).

    §8 admits no exception: cash is never a frontier asset, so a
    user-selected cash id yields the frontier over the remaining
    non-cash selection — not a three-asset frontier.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pa-cash-selected@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        alpha, beta = await _seed_two_quarterly_funds(session, actor.id, ac.id)
        cash = await _create_unitised_cash_with_dense_navs(
            session, actor.id, ac.id, name="USD Cash"
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).compute_frontier(
            n_points=30,
            investment_ids=[cash.id, alpha.id, beta.id],
        )

    assert bundle is not None
    assert sorted(bundle.frontier_result.asset_names) == [
        "Alpha Fund",
        "Beta Fund",
    ]
    assert bundle.frontier_result.frontier_weights.shape[1] == 2
    assert set(bundle.investment_points) == {"Alpha Fund", "Beta Fund"}


async def test_only_cash_selection_yields_insufficient_universe(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An all-cash selection hits the existing insufficient-universe path.

    The filter leaves an empty universe, so ``compute_frontier``
    returns ``None`` — the same empty-state signal an under-populated
    book produces (see
    ``test_compute_frontier_returns_none_for_empty_universe``). Not a
    new error type, and emphatically not a silent empty payload.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed_actor_and_asset_class(
        app_engine, tenant_id, email="pa-cash-only@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _seed_two_quarterly_funds(session, actor.id, ac.id)
        usd_cash = await _create_unitised_cash_with_dense_navs(
            session, actor.id, ac.id, name="USD Cash"
        )
        chf_cash = await _create_unitised_cash_with_dense_navs(
            session, actor.id, ac.id, name="CHF Cash"
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        bundle = await _build_service(session).compute_frontier(
            n_points=30,
            investment_ids=[usd_cash.id, chf_cash.id],
        )

    assert bundle is None
