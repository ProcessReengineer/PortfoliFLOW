# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`LimitsCoverageService` (Kickoff #3b, Sub-Stream 1).

Static helpers (``_build_evaluation_grid``, ``_build_limit_step_lines``,
``_build_kpi_strip``) are exercised as pure unit tests; the
``get_coverage`` orchestration is exercised against the live compose
Postgres via the shared ``app_engine`` / ``seed_tenant`` fixtures —
same pattern as :mod:`tests.services.test_portfolio_review_service`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import LimitSetNotEffective
from core.repositories import (
    AssetClassRepository,
    FxRateRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    LimitsRepository,
    TenantRepository,
    tenant_context,
)
from core.repositories.limits_repository import LimitSetDTO
from services.analytics._dtos import LimitSetWithLimitsDTO
from services.limits import (
    LimitsCoverageBundle,
    LimitsCoverageService,
)


# ---------------------------------------------------------------------------
# DTO fabrication helpers (pure unit tests)
# ---------------------------------------------------------------------------


_TENANT_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")
_USER_ID: UUID = UUID("00000000-0000-0000-0000-0000000000aa")


def _D(value: str | int | float) -> Decimal:
    return Decimal(str(value))


def _set_dto(family: str, eff: date, set_id: UUID | None = None) -> LimitSetDTO:
    from datetime import datetime, timezone

    return LimitSetDTO(
        id=set_id or uuid4(),
        tenant_id=_TENANT_ID,
        family=family,
        effective_from=eff,
        label=f"{family} @ {eff}",
        notes=None,
        created_by=_USER_ID,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _set_with_limits(
    family: str,
    eff: date,
    limits: dict[str, Decimal],
) -> LimitSetWithLimitsDTO:
    return LimitSetWithLimitsDTO(
        set=_set_dto(family, eff),
        limits=limits,
    )


# ---------------------------------------------------------------------------
# Pure unit tests — static helpers
# ---------------------------------------------------------------------------


def test_evaluation_grid_returns_monthly_ends() -> None:
    grid = LimitsCoverageService._build_evaluation_grid(date(2024, 1, 15), date(2024, 4, 15))
    assert grid == [
        date(2024, 1, 31),
        date(2024, 2, 29),
        date(2024, 3, 31),
    ]


def test_evaluation_grid_empty_for_same_month_no_monthend() -> None:
    # Range fully inside one month, neither bound is a month-end.
    grid = LimitsCoverageService._build_evaluation_grid(date(2024, 3, 5), date(2024, 3, 20))
    assert grid == []


def test_build_limit_step_lines_single_set_one_class() -> None:
    sets = [_set_with_limits("saa", date(2022, 1, 1), {"equities": _D("30.0")})]
    step_lines = LimitsCoverageService._build_limit_step_lines(sets)
    assert step_lines == {
        "equities": [(date(2022, 1, 1), _D("30.0"))],
    }


def test_build_limit_step_lines_class_added_in_later_set() -> None:
    # Class "bonds" only appears from set #2 onwards.
    sets = [
        _set_with_limits("saa", date(2022, 1, 1), {"equities": _D("30.0")}),
        _set_with_limits(
            "saa",
            date(2023, 1, 1),
            {"equities": _D("25.0"), "bonds": _D("40.0")},
        ),
    ]
    step_lines = LimitsCoverageService._build_limit_step_lines(sets)
    assert step_lines["equities"] == [
        (date(2022, 1, 1), _D("30.0")),
        (date(2023, 1, 1), _D("25.0")),
    ]
    # "bonds" starts at set #2; no leading None for the earlier set.
    assert step_lines["bonds"] == [(date(2023, 1, 1), _D("40.0"))]


def test_build_limit_step_lines_class_removed_in_later_set() -> None:
    # Class "alts" appears in set #1, vanishes in set #2 → gap marker
    # at set #2's effective_from.
    sets = [
        _set_with_limits(
            "saa",
            date(2022, 1, 1),
            {"equities": _D("30.0"), "alts": _D("10.0")},
        ),
        _set_with_limits("saa", date(2023, 1, 1), {"equities": _D("25.0")}),
    ]
    step_lines = LimitsCoverageService._build_limit_step_lines(sets)
    assert step_lines["alts"] == [
        (date(2022, 1, 1), _D("10.0")),
        (date(2023, 1, 1), None),
    ]


def test_build_kpi_strip_aggregates_across_families() -> None:
    latest = date(2024, 6, 30)
    latest_ts = pd.Timestamp(latest)
    saa_cov = pd.DataFrame(
        [
            {"as_of_date": latest_ts, "class_key": "saa_a", "status": "OK"},
            {"as_of_date": latest_ts, "class_key": "saa_b", "status": "WARN"},
        ]
    )
    anlv_cov = pd.DataFrame(
        [
            {"as_of_date": latest_ts, "class_key": "anlv_a", "status": "OK"},
            {"as_of_date": latest_ts, "class_key": "anlv_b", "status": "BREACH"},
        ]
    )
    aum_used = pd.Series([_D("1000000")], index=pd.DatetimeIndex([latest_ts]))

    strip = LimitsCoverageService._build_kpi_strip(
        saa_coverage=saa_cov,
        anlv_coverage=anlv_cov,
        aum_used=aum_used,
        latest_as_of_date=latest,
    )

    assert strip.aum_eur == _D("1000000")
    assert strip.ok_total_count == 2
    assert strip.warn_count == 1
    assert strip.breach_count == 1
    assert strip.ok_classes_denominator == 4


def test_build_kpi_strip_excludes_no_limit_from_denominator() -> None:
    latest = date(2024, 6, 30)
    latest_ts = pd.Timestamp(latest)
    saa_cov = pd.DataFrame(
        [
            {"as_of_date": latest_ts, "class_key": "saa_a", "status": "OK"},
            {
                "as_of_date": latest_ts,
                "class_key": "rogue",
                "status": "NO_LIMIT",
            },
            {
                "as_of_date": latest_ts,
                "class_key": "unallocated",
                "status": "UNALLOCATED",
            },
        ]
    )
    anlv_cov = pd.DataFrame(columns=["as_of_date", "class_key", "status"])
    aum_used = pd.Series([_D("500000")], index=pd.DatetimeIndex([latest_ts]))

    strip = LimitsCoverageService._build_kpi_strip(
        saa_coverage=saa_cov,
        anlv_coverage=anlv_cov,
        aum_used=aum_used,
        latest_as_of_date=latest,
    )

    assert strip.ok_total_count == 1
    assert strip.warn_count == 0
    assert strip.breach_count == 0
    # NO_LIMIT and UNALLOCATED are NOT counted into the denominator.
    assert strip.ok_classes_denominator == 1


def test_build_kpi_strip_returns_zeros_when_latest_is_none() -> None:
    strip = LimitsCoverageService._build_kpi_strip(
        saa_coverage=pd.DataFrame(),
        anlv_coverage=pd.DataFrame(),
        aum_used=pd.Series([], dtype=object),
        latest_as_of_date=None,
    )
    assert strip.aum_eur is None
    assert strip.ok_total_count == 0
    assert strip.warn_count == 0
    assert strip.breach_count == 0
    assert strip.ok_classes_denominator == 0


# ---------------------------------------------------------------------------
# Live-DB tests — orchestration
# ---------------------------------------------------------------------------


async def _seed_user(superuser_engine: AsyncEngine, tenant_id: UUID) -> UUID:
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


def _build_service(session) -> LimitsCoverageService:
    return LimitsCoverageService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        limits=LimitsRepository(session),
        asset_classes=AssetClassRepository(session),
        tenants=TenantRepository(session),
        fx_rates=FxRateRepository(session),
    )


async def _seed_minimal_universe(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    nav_rows: list[tuple[date, Decimal]] | None = None,
    saa_limits: dict[str, Decimal] | None = None,
    saa_effective_from: date = date(2020, 1, 1),
    anlv_limits: dict[str, Decimal] | None = None,
    anlv_effective_from: date = date(2020, 1, 1),
    asset_class_code: str = "equities",
) -> None:
    """Seed a one-investment / one-class universe with NAVs and limits.

    There is no AUM to seed since ADR-0103 §2: the NAV rows *are* the book,
    and therefore both the denominator and the horizon the range clamps to.
    """
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        ac = await AssetClassRepository(session).create(
            code=asset_class_code, display_name=asset_class_code.title()
        )
        inv = await InvestmentRepository(session).create(
            name="Alpha",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=user_id,
        )

        nav_repo = InvestmentNavRepository(session)
        for as_of, value in nav_rows or []:
            await nav_repo.upsert(
                investment_id=inv.id,
                as_of_date=as_of,
                nav_kind="actual",
                nav_value=value,
                currency="EUR",
                source=None,
                created_by=user_id,
            )

        if saa_limits is not None:
            await LimitsRepository(session).create_set_with_limits(
                family="saa",
                effective_from=saa_effective_from,
                label="SAA test",
                notes=None,
                limits=saa_limits,
                created_by=user_id,
            )
        if anlv_limits is not None:
            await LimitsRepository(session).create_set_with_limits(
                family="anlv",
                effective_from=anlv_effective_from,
                label="AnlV test",
                notes=None,
                limits=anlv_limits,
                created_by=user_id,
            )


async def test_get_coverage_returns_none_for_empty_universe(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    """No investments ⇒ ``None`` — the only ``None`` case left (ADR-0103 §2).

    The old ``None`` case was "the tenant has no AUM series". That question
    no longer exists: a book with NAVs always has a denominator, because the
    denominator *is* the book. What remains is the empty universe.
    """
    tenant_id = await seed_tenant("svc-empty")
    user_id = await _seed_user(superuser_engine, tenant_id)
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = _build_service(session)
        bundle = await service.get_coverage()
    assert bundle is None


async def test_get_coverage_resolves_default_from_date_to_12_months_back(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant("svc-default-from")
    user_id = await _seed_user(superuser_engine, tenant_id)

    # The book carries NAVs on Jan 2023 through Dec 2024 month-ends. The
    # service should default from_date to (to_date - 12 months).
    book_dates = [
        date(2023, m, d)
        for m, d in [
            (1, 31),
            (2, 28),
            (3, 31),
            (4, 30),
            (5, 31),
            (6, 30),
            (7, 31),
            (8, 31),
            (9, 30),
            (10, 31),
            (11, 30),
            (12, 31),
        ]
    ] + [
        date(2024, m, d)
        for m, d in [
            (1, 31),
            (2, 29),
            (3, 31),
            (4, 30),
            (5, 31),
            (6, 30),
            (7, 31),
            (8, 31),
            (9, 30),
            (10, 31),
            (11, 30),
            (12, 31),
        ]
    ]
    nav_rows = [(d, _D("100000")) for d in book_dates]

    await _seed_minimal_universe(
        app_engine,
        tenant_id,
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": _D("50.0")},
        anlv_limits={"anlv_1": _D("50.0")},
    )

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = _build_service(session)
        bundle = await service.get_coverage(
            cut_over=date(2025, 12, 31),
        )

    assert bundle is not None
    # Default to_date = the book's NAV horizon = 2024-12-31.
    assert bundle.to_date == date(2024, 12, 31)
    # Default from_date = to_date - 12 months = 2023-12-31.
    assert bundle.from_date == date(2023, 12, 31)


async def test_get_coverage_clamps_to_date_to_book_horizon(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant("svc-clamp")
    user_id = await _seed_user(superuser_engine, tenant_id)

    nav_rows = [
        (date(2024, 1, 31), _D("100000")),
        (date(2024, 2, 29), _D("100000")),
        (date(2024, 3, 31), _D("100000")),
    ]
    await _seed_minimal_universe(
        app_engine,
        tenant_id,
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": _D("50.0")},
        anlv_limits={"anlv_1": _D("50.0")},
    )

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = _build_service(session)
        bundle = await service.get_coverage(
            from_date=date(2024, 1, 1),
            to_date=date(2030, 12, 31),  # past the book's horizon
            cut_over=date(2025, 12, 31),
        )

    assert bundle is not None
    # to_date is silently clamped to the book's last NAV date.
    assert bundle.to_date == date(2024, 3, 31)


async def test_get_coverage_swaps_when_from_after_to(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant("svc-swap")
    user_id = await _seed_user(superuser_engine, tenant_id)

    nav_rows = [
        (date(2024, 1, 31), _D("100000")),
        (date(2024, 2, 29), _D("100000")),
        (date(2024, 3, 31), _D("100000")),
    ]
    await _seed_minimal_universe(
        app_engine,
        tenant_id,
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": _D("50.0")},
        anlv_limits={"anlv_1": _D("50.0")},
    )

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = _build_service(session)
        bundle = await service.get_coverage(
            from_date=date(2024, 3, 31),
            to_date=date(2024, 1, 31),
            cut_over=date(2025, 12, 31),
        )

    assert bundle is not None
    # Swap restores monotonic order.
    assert bundle.from_date == date(2024, 1, 31)
    assert bundle.to_date == date(2024, 3, 31)


async def test_engine_exception_propagates(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    """When no SAA limit set is effective at the Stichtag the engine
    raises ``LimitSetNotEffective`` — the service must let it propagate."""
    tenant_id = await seed_tenant("svc-no-set")
    user_id = await _seed_user(superuser_engine, tenant_id)

    nav_rows = [
        (date(2024, 1, 31), _D("100000")),
        (date(2024, 2, 29), _D("100000")),
    ]
    # No SAA set seeded — the engine has nothing to evaluate against.
    await _seed_minimal_universe(
        app_engine,
        tenant_id,
        user_id,
        nav_rows=nav_rows,
        saa_limits=None,
        anlv_limits={"anlv_1": _D("50.0")},
    )

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = _build_service(session)
        with pytest.raises(LimitSetNotEffective):
            await service.get_coverage(
                from_date=date(2024, 1, 1),
                to_date=date(2024, 2, 29),
                cut_over=date(2025, 12, 31),
            )


async def test_get_coverage_returns_bundle_when_range_has_no_monthend(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    """A same-month range without a month-end Stichtag returns an
    empty-grid bundle (with ``latest_as_of_date=None``), not None — the
    tenant has a book, only the range is unhelpful.

    This is the distinction ADR-0103 §2 sharpened: ``None`` now means "no
    book", and an unhelpful *range* over a real book is still a bundle."""
    tenant_id = await seed_tenant("svc-empty-grid")
    user_id = await _seed_user(superuser_engine, tenant_id)

    await _seed_minimal_universe(
        app_engine,
        tenant_id,
        user_id,
        nav_rows=[(date(2024, 3, 31), _D("100000"))],
        saa_limits={"equities": _D("50.0")},
        anlv_limits={"anlv_1": _D("50.0")},
    )

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = _build_service(session)
        bundle = await service.get_coverage(
            from_date=date(2024, 3, 5),
            to_date=date(2024, 3, 20),
            cut_over=date(2025, 12, 31),
        )

    assert isinstance(bundle, LimitsCoverageBundle)
    assert bundle.latest_as_of_date is None
    assert bundle.evaluation_dates == []
    assert bundle.kpi_strip.aum_eur is None
    assert bundle.kpi_strip.ok_total_count == 0
