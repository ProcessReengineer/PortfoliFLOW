# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for :class:`BenchmarkComparisonService` (Roadmap A12).

Pure-mock tests covering the service orchestration: how the service
threads repository data through the analytics layer, how empty cases
surface, and how SAA-optimization failures are mapped to the
``WeightSetOptionDTO.unavailable_hint`` channel.

The pure analytics layer is exercised separately in
``tests/services/analytics/test_benchmark_comparison.py``; here the
focus is on the orchestrator's data plumbing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
import pytest

from core.exceptions import MissingFxRateError
from core.repositories.asset_class_benchmark_mapping_repository import (
    AssetClassBenchmarkMappingDTO,
)
from core.repositories.asset_class_repository import AssetClassDTO
from core.repositories.benchmark_observation_repository import (
    BenchmarkObservationDTO,
)
from core.repositories.benchmark_repository import BenchmarkDTO
from core.repositories.investment_nav_repository import InvestmentNavDTO
from core.repositories.investment_repository import InvestmentDTO
from core.repositories.saa_configuration_repository import (
    SAAConfigurationDTO,
)
from services.benchmark_comparison import BenchmarkComparisonService
from services.saa import SAAValidationError


_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = UUID("00000000-0000-0000-0000-0000000000aa")


# ---------------------------------------------------------------------------
# Fabrication helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ac_dto(code: str, display_name: str | None = None) -> AssetClassDTO:
    return AssetClassDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        code=code,
        display_name=display_name or code.replace("_", " ").title(),
        description=None,
        created_at=_now(),
        updated_at=_now(),
    )


def _benchmark_dto(code: str) -> BenchmarkDTO:
    return BenchmarkDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        code=code,
        display_name=f"{code} display",
        description=None,
        provider_hint=None,
        created_by=_USER_ID,
        created_at=_now(),
        updated_at=_now(),
    )


def _mapping_dto(ac_id: UUID, benchmark_id: UUID) -> AssetClassBenchmarkMappingDTO:
    return AssetClassBenchmarkMappingDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        asset_class_id=ac_id,
        benchmark_id=benchmark_id,
        weight=Decimal("1.0"),
        created_at=_now(),
        updated_at=_now(),
    )


def _investment_dto(name: str, ac_id: UUID, currency: str = "EUR") -> InvestmentDTO:
    return InvestmentDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        name=name,
        investment_type="private_equity",
        asset_class_id=ac_id,
        manager_name=None,
        region=None,
        currency=currency,
        vintage_year=2020,
        commitment_amount=None,
        is_active=True,
        type_specific_data=None,
        created_by=_USER_ID,
        created_at=_now(),
        updated_at=_now(),
        anlv_code=None,
    )


def _nav_dto(inv_id: UUID, as_of: date, value: Decimal | float) -> InvestmentNavDTO:
    return InvestmentNavDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        investment_id=inv_id,
        as_of_date=as_of,
        nav_value=Decimal(str(value)),
        currency="EUR",
        nav_kind="actual",
        source=None,
        created_by=_USER_ID,
        created_at=_now(),
        updated_at=_now(),
    )


def _obs_dto(benchmark_id: UUID, as_of: date, period_return: float) -> BenchmarkObservationDTO:
    return BenchmarkObservationDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        benchmark_id=benchmark_id,
        as_of_date=as_of,
        period_return=Decimal(str(period_return)),
        created_at=_now(),
    )


def _saa_config(name: str, is_active: bool = True) -> SAAConfigurationDTO:
    return SAAConfigurationDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        name=name,
        risk_free_rate=Decimal("0.02"),
        n_frontier_points=100,
        is_active=is_active,
        created_by=_USER_ID,
        created_at=_now(),
        updated_at=_now(),
    )


@dataclass
class _PortfolioResultMock:
    weights: Any
    expected_return: float = 0.05
    volatility: float = 0.10
    sharpe_ratio: float = 0.30
    asset_names: list[str] | None = None


@dataclass
class _OptimizationResultMock:
    asset_names: list[str]
    tangency: _PortfolioResultMock
    min_var: _PortfolioResultMock
    frontier: list = None  # type: ignore[assignment]
    cloud: list = None  # type: ignore[assignment]
    cml: list = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.frontier is None:
            self.frontier = []
        if self.cloud is None:
            self.cloud = []
        if self.cml is None:
            self.cml = []


def _daily_nav_series(inv_id: UUID, start: date, days: int) -> list[InvestmentNavDTO]:
    """Build a synthetic daily NAV series with a slight upward drift.

    The drift produces non-trivial daily returns so the analytics
    layer has something to compound into monthly returns.
    """
    from datetime import timedelta

    rows: list[InvestmentNavDTO] = []
    value = 100.0
    current = start
    for _i in range(days):
        rows.append(_nav_dto(inv_id, current, Decimal(str(round(value, 4)))))
        value *= 1.0005  # ~13% annualised
        current = current + timedelta(days=1)
    return rows


def _daily_obs_series(
    benchmark_id: UUID, start: date, days: int, daily_return: float = 0.0004
) -> list[BenchmarkObservationDTO]:
    from datetime import timedelta

    rows: list[BenchmarkObservationDTO] = []
    current = start
    for _ in range(days):
        rows.append(_obs_dto(benchmark_id, current, daily_return))
        current = current + timedelta(days=1)
    return rows


# ---------------------------------------------------------------------------
# Service mock fabrication
# ---------------------------------------------------------------------------


def _build_service_with_mocks(
    *,
    investments: list[InvestmentDTO],
    asset_classes: list[AssetClassDTO],
    benchmarks: list[BenchmarkDTO],
    mappings: list[AssetClassBenchmarkMappingDTO],
    nav_rows_by_inv: dict[UUID, list[InvestmentNavDTO]] | None = None,
    observations_by_benchmark: (dict[UUID, list[BenchmarkObservationDTO]] | None) = None,
    active_saa_config: SAAConfigurationDTO | None = None,
    saa_configurations: list[SAAConfigurationDTO] | None = None,
    optimization_result: _OptimizationResultMock | Exception | None = None,
    functional_currency: str = "EUR",
    fx_rows: list[tuple[str, date, str]] | None = None,
    fx_load_calls: list[list[str]] | None = None,
) -> BenchmarkComparisonService:
    """Fabricate the service over mock repositories.

    Args (ADR-0102 FX additions):
        functional_currency: What ``TenantRepository`` reports.
        fx_rows: ``(currency, as_of_date, rate_to_reference)`` triples the
            fake ``FxRateRepository`` serves, quoted against
            ``functional_currency`` as the reference. ``None`` → no rates,
            which is what an uncovered foreign position must trip over.
        fx_load_calls: Optional spy list; each ``load_rates_frame`` call
            appends its requested currencies. An empty list after a run is
            the zero-read proof (ADR-0099 §3).
    """
    investments_repo = MagicMock()
    investments_repo.list_active = AsyncMock(return_value=list(investments))
    investments_lookup = {inv.id: inv for inv in investments}
    investments_repo.get_by_id = AsyncMock(
        side_effect=lambda inv_id: investments_lookup.get(inv_id)
    )

    navs_repo = MagicMock()
    nav_rows_by_inv = nav_rows_by_inv or {}

    async def _list_by_inv_kind(inv_id: UUID, kind: str):
        return nav_rows_by_inv.get(inv_id, [])

    async def _list_by_investments_and_kind(
        inv_ids: list[UUID], kind: str
    ) -> dict[UUID, list[InvestmentNavDTO]]:
        return {inv_id: nav_rows_by_inv.get(inv_id, []) for inv_id in inv_ids}

    navs_repo.list_by_investment_and_kind = AsyncMock(side_effect=_list_by_inv_kind)
    navs_repo.list_by_investments_and_kind = AsyncMock(side_effect=_list_by_investments_and_kind)

    asset_classes_repo = MagicMock()
    asset_classes_repo.list_all = AsyncMock(return_value=list(asset_classes))

    benchmarks_repo = MagicMock()
    benchmarks_repo.list_all = AsyncMock(return_value=list(benchmarks))

    observations_repo = MagicMock()
    observations_by_benchmark = observations_by_benchmark or {}

    async def _list_for_benchmark(benchmark_id: UUID, from_date=None, to_date=None):
        rows = observations_by_benchmark.get(benchmark_id, [])
        if to_date is not None:
            rows = [r for r in rows if r.as_of_date <= to_date]
        return rows

    observations_repo.list_for_benchmark = AsyncMock(side_effect=_list_for_benchmark)

    mappings_repo = MagicMock()
    mappings_repo.list_all = AsyncMock(return_value=list(mappings))

    saa_service = MagicMock()
    saa_service.get_active_configuration = AsyncMock(return_value=active_saa_config)
    saa_service.list_configurations = AsyncMock(return_value=list(saa_configurations or []))

    async def _run_optimization(config_id: UUID):
        if isinstance(optimization_result, Exception):
            raise optimization_result
        if optimization_result is None:
            raise SAAValidationError("no optimisation configured")
        return optimization_result

    saa_service.run_optimization = AsyncMock(side_effect=_run_optimization)

    tenants_repo = MagicMock()
    tenants_repo.get_current_functional_currency = AsyncMock(return_value=functional_currency)

    fx_repo = MagicMock()
    all_fx_rows = fx_rows or []

    async def _load_rates_frame(
        currencies: list[str], from_date=None, to_date=None
    ) -> pd.DataFrame:
        if fx_load_calls is not None:
            fx_load_calls.append(list(currencies))
        wanted = set(currencies)
        rows = [r for r in all_fx_rows if r[0] in wanted]
        return pd.DataFrame(
            {
                "as_of_date": pd.to_datetime([r[1] for r in rows]),
                "currency": [r[0] for r in rows],
                "rate_to_reference": [Decimal(r[2]) for r in rows],
                "reference_currency": [functional_currency] * len(rows),
            }
        )

    fx_repo.load_rates_frame = AsyncMock(side_effect=_load_rates_frame)

    return BenchmarkComparisonService(
        investments=investments_repo,
        navs=navs_repo,
        asset_classes=asset_classes_repo,
        benchmarks=benchmarks_repo,
        benchmark_observations=observations_repo,
        mappings=mappings_repo,
        saa_service=saa_service,
        tenants=tenants_repo,
        fx_rates=fx_repo,
    )


# ---------------------------------------------------------------------------
# get_investment_comparisons
# ---------------------------------------------------------------------------


async def test_get_investment_comparisons_classifies_by_mapping() -> None:
    """Investments with mapping go into rows; without mapping into the side list."""
    ac_mapped = _ac_dto("equities")
    ac_unmapped = _ac_dto("cash")
    benchmark = _benchmark_dto("BM_EQ")
    mapping = _mapping_dto(ac_mapped.id, benchmark.id)
    inv_a = _investment_dto("Alpha", ac_mapped.id)
    inv_b = _investment_dto("Bravo", ac_unmapped.id)

    start = date(2023, 1, 1)
    service = _build_service_with_mocks(
        investments=[inv_a, inv_b],
        asset_classes=[ac_mapped, ac_unmapped],
        benchmarks=[benchmark],
        mappings=[mapping],
        nav_rows_by_inv={
            inv_a.id: _daily_nav_series(inv_a.id, start, 400),
            inv_b.id: _daily_nav_series(inv_b.id, start, 400),
        },
        observations_by_benchmark={
            benchmark.id: _daily_obs_series(benchmark.id, start, 400),
        },
    )

    bundle = await service.get_investment_comparisons(as_of_date=date(2024, 2, 5))

    assert [row.investment_name for row in bundle.rows] == ["Alpha"]
    assert bundle.investments_without_benchmark == ["Bravo"]
    row = bundle.rows[0]
    assert row.asset_class_code == "equities"
    assert row.benchmark_id == benchmark.id
    # 13-month run produces ratio-based metrics (n >= 12).
    assert row.n_observations >= 12
    assert not math.isnan(row.beta)


async def test_get_investment_comparisons_returns_nan_for_short_series() -> None:
    """Investments with fewer than 12 aligned months get NaN ratio metrics."""
    ac = _ac_dto("equities")
    benchmark = _benchmark_dto("BM_EQ")
    mapping = _mapping_dto(ac.id, benchmark.id)
    inv = _investment_dto("Alpha", ac.id)

    start = date(2023, 1, 1)
    # 60 days → ~2 monthly observations → < 12.
    service = _build_service_with_mocks(
        investments=[inv],
        asset_classes=[ac],
        benchmarks=[benchmark],
        mappings=[mapping],
        nav_rows_by_inv={
            inv.id: _daily_nav_series(inv.id, start, 60),
        },
        observations_by_benchmark={
            benchmark.id: _daily_obs_series(benchmark.id, start, 60),
        },
    )

    bundle = await service.get_investment_comparisons(as_of_date=date(2023, 3, 15))
    assert len(bundle.rows) == 1
    row = bundle.rows[0]
    assert row.n_observations < 12
    assert math.isnan(row.information_ratio)
    assert math.isnan(row.r_squared)


# ---------------------------------------------------------------------------
# get_investment_comparison_detail
# ---------------------------------------------------------------------------


async def test_get_investment_comparison_detail_returns_none_for_unmapped() -> None:
    ac = _ac_dto("cash")
    inv = _investment_dto("CashFund", ac.id)
    service = _build_service_with_mocks(
        investments=[inv],
        asset_classes=[ac],
        benchmarks=[],
        mappings=[],
    )
    result = await service.get_investment_comparison_detail(inv.id)
    assert result is None


async def test_get_investment_comparison_detail_returns_series() -> None:
    ac = _ac_dto("equities")
    benchmark = _benchmark_dto("BM_EQ")
    mapping = _mapping_dto(ac.id, benchmark.id)
    inv = _investment_dto("Alpha", ac.id)
    start = date(2023, 1, 1)

    service = _build_service_with_mocks(
        investments=[inv],
        asset_classes=[ac],
        benchmarks=[benchmark],
        mappings=[mapping],
        nav_rows_by_inv={
            inv.id: _daily_nav_series(inv.id, start, 400),
        },
        observations_by_benchmark={
            benchmark.id: _daily_obs_series(benchmark.id, start, 400),
        },
    )
    detail = await service.get_investment_comparison_detail(inv.id, as_of_date=date(2024, 2, 5))
    assert detail is not None
    assert detail.investment_name == "Alpha"
    assert detail.benchmark_display_name == benchmark.display_name
    assert not detail.investment_cumulative_returns.empty
    assert not detail.benchmark_cumulative_returns.empty
    assert not detail.excess_cumulative_returns.empty


# ---------------------------------------------------------------------------
# get_asset_class_composites
# ---------------------------------------------------------------------------


async def test_get_asset_class_composites_splits_mapped_and_unmapped() -> None:
    ac_mapped = _ac_dto("equities", display_name="Equities")
    ac_unmapped = _ac_dto("cash", display_name="Cash")
    benchmark = _benchmark_dto("BM_EQ")
    mapping = _mapping_dto(ac_mapped.id, benchmark.id)
    inv_a = _investment_dto("Alpha", ac_mapped.id)
    inv_b = _investment_dto("Bravo", ac_mapped.id)

    start = date(2023, 1, 1)
    service = _build_service_with_mocks(
        investments=[inv_a, inv_b],
        asset_classes=[ac_mapped, ac_unmapped],
        benchmarks=[benchmark],
        mappings=[mapping],
        nav_rows_by_inv={
            inv_a.id: _daily_nav_series(inv_a.id, start, 400),
            inv_b.id: _daily_nav_series(inv_b.id, start, 400),
        },
        observations_by_benchmark={
            benchmark.id: _daily_obs_series(benchmark.id, start, 400),
        },
    )
    bundle = await service.get_asset_class_composites(as_of_date=date(2024, 2, 5))
    codes = [row.asset_class_code for row in bundle.rows]
    assert "equities" in codes
    assert "cash" not in codes
    assert "Cash" in bundle.asset_classes_without_benchmark

    equities_row = next(row for row in bundle.rows if row.asset_class_code == "equities")
    assert equities_row.n_investments == 2


async def test_get_asset_class_composites_mapped_but_empty() -> None:
    ac = _ac_dto("equities", display_name="Equities")
    benchmark = _benchmark_dto("BM_EQ")
    mapping = _mapping_dto(ac.id, benchmark.id)
    start = date(2023, 1, 1)

    service = _build_service_with_mocks(
        investments=[],
        asset_classes=[ac],
        benchmarks=[benchmark],
        mappings=[mapping],
        observations_by_benchmark={
            benchmark.id: _daily_obs_series(benchmark.id, start, 60),
        },
    )
    bundle = await service.get_asset_class_composites(as_of_date=date(2023, 3, 15))
    assert len(bundle.rows) == 1
    row = bundle.rows[0]
    assert row.n_investments == 0
    assert row.composite_cumulative_returns.empty


# ---------------------------------------------------------------------------
# get_saa_hypothetical
# ---------------------------------------------------------------------------


async def test_get_saa_hypothetical_empty_when_no_configurations() -> None:
    service = _build_service_with_mocks(
        investments=[],
        asset_classes=[],
        benchmarks=[],
        mappings=[],
        saa_configurations=[],
    )
    bundle = await service.get_saa_hypothetical()
    assert bundle.selected_configuration_id is None
    assert bundle.series is None
    assert bundle.saa_configuration_options == []
    assert bundle.weight_set_options == []


async def test_get_saa_hypothetical_surfaces_validation_failure() -> None:
    saa_config = _saa_config("Test")
    service = _build_service_with_mocks(
        investments=[],
        asset_classes=[],
        benchmarks=[],
        mappings=[],
        saa_configurations=[saa_config],
        optimization_result=SAAValidationError("Only 1 asset class; need at least 2."),
    )
    bundle = await service.get_saa_hypothetical()
    assert bundle.selected_configuration_id == saa_config.id
    assert bundle.series is None
    assert len(bundle.weight_set_options) == 2
    for opt in bundle.weight_set_options:
        assert opt.available is False
        assert opt.unavailable_hint is not None
        assert "at least 2" in opt.unavailable_hint.lower() or (
            "asset class" in opt.unavailable_hint.lower()
        )


async def test_get_saa_hypothetical_runs_when_optimizer_succeeds() -> None:
    ac_a = _ac_dto("equities", display_name="Equities")
    ac_b = _ac_dto("bonds", display_name="Bonds")
    benchmark_a = _benchmark_dto("BM_EQ")
    benchmark_b = _benchmark_dto("BM_BND")
    mappings = [
        _mapping_dto(ac_a.id, benchmark_a.id),
        _mapping_dto(ac_b.id, benchmark_b.id),
    ]
    inv_a = _investment_dto("Alpha", ac_a.id)
    inv_b = _investment_dto("Bravo", ac_b.id)
    start = date(2023, 1, 1)
    saa_config = _saa_config("Test")

    # ``run_optimization`` produces asset names in display-name sort
    # order — alphabetical: "Bonds", "Equities".
    optimization = _OptimizationResultMock(
        asset_names=["Bonds", "Equities"],
        tangency=_PortfolioResultMock(weights=np.array([0.3, 0.7])),
        min_var=_PortfolioResultMock(weights=np.array([0.7, 0.3])),
    )
    service = _build_service_with_mocks(
        investments=[inv_a, inv_b],
        asset_classes=[ac_a, ac_b],
        benchmarks=[benchmark_a, benchmark_b],
        mappings=mappings,
        nav_rows_by_inv={
            inv_a.id: _daily_nav_series(inv_a.id, start, 400),
            inv_b.id: _daily_nav_series(inv_b.id, start, 400),
        },
        observations_by_benchmark={
            benchmark_a.id: _daily_obs_series(benchmark_a.id, start, 400),
            benchmark_b.id: _daily_obs_series(benchmark_b.id, start, 400, daily_return=0.0001),
        },
        saa_configurations=[saa_config],
        optimization_result=optimization,
    )
    bundle = await service.get_saa_hypothetical(
        weight_set="tangency",
        as_of_date=date(2024, 2, 5),
    )
    assert bundle.selected_configuration_id == saa_config.id
    assert bundle.series is not None
    assert all(opt.available for opt in bundle.weight_set_options)
    assert bundle.selected_weight_set == "tangency"
    # The label is composed from the chosen weight set and the
    # configuration name.
    assert "Tangency" in bundle.series.saa_label
    assert "Test" in bundle.series.saa_label


async def test_get_saa_hypothetical_min_var_uses_min_var_weights() -> None:
    ac_a = _ac_dto("equities", display_name="Equities")
    ac_b = _ac_dto("bonds", display_name="Bonds")
    benchmark_a = _benchmark_dto("BM_EQ")
    benchmark_b = _benchmark_dto("BM_BND")
    mappings = [
        _mapping_dto(ac_a.id, benchmark_a.id),
        _mapping_dto(ac_b.id, benchmark_b.id),
    ]
    inv_a = _investment_dto("Alpha", ac_a.id)
    inv_b = _investment_dto("Bravo", ac_b.id)
    start = date(2023, 1, 1)
    saa_config = _saa_config("Test")

    optimization = _OptimizationResultMock(
        asset_names=["Bonds", "Equities"],
        tangency=_PortfolioResultMock(weights=np.array([0.3, 0.7])),
        min_var=_PortfolioResultMock(weights=np.array([0.85, 0.15])),
    )
    service = _build_service_with_mocks(
        investments=[inv_a, inv_b],
        asset_classes=[ac_a, ac_b],
        benchmarks=[benchmark_a, benchmark_b],
        mappings=mappings,
        nav_rows_by_inv={
            inv_a.id: _daily_nav_series(inv_a.id, start, 400),
            inv_b.id: _daily_nav_series(inv_b.id, start, 400),
        },
        observations_by_benchmark={
            benchmark_a.id: _daily_obs_series(benchmark_a.id, start, 400),
            benchmark_b.id: _daily_obs_series(benchmark_b.id, start, 400, daily_return=0.0001),
        },
        saa_configurations=[saa_config],
        optimization_result=optimization,
    )
    bundle = await service.get_saa_hypothetical(weight_set="min_var", as_of_date=date(2024, 2, 5))
    assert bundle.selected_weight_set == "min_var"
    assert bundle.series is not None
    # Weights stored on the series come from min_var (0.85 / 0.15).
    weights = bundle.series.saa_weights
    assert pytest.approx(weights["bonds"], abs=1e-9) == 0.85
    assert pytest.approx(weights["equities"], abs=1e-9) == 0.15


async def test_get_saa_hypothetical_propagates_value_error_from_optimizer() -> None:
    """A ValueError from the numerical optimizer is NOT swallowed.

    Per the process spec step 4: only SAAValidationError is caught;
    other exceptions are real bugs and propagate to the route's
    standard error handler.
    """
    saa_config = _saa_config("Test")
    service = _build_service_with_mocks(
        investments=[],
        asset_classes=[],
        benchmarks=[],
        mappings=[],
        saa_configurations=[saa_config],
        optimization_result=ValueError("matrix is singular"),
    )
    with pytest.raises(ValueError, match="singular"):
        await service.get_saa_hypothetical()


# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------


def test_resolve_selected_configuration_prefers_requested_then_active() -> None:
    a = _saa_config("A", is_active=False)
    b = _saa_config("B", is_active=True)
    c = _saa_config("C", is_active=False)

    # Requested id wins.
    chosen = BenchmarkComparisonService._resolve_selected_configuration([a, b, c], c.id)
    assert chosen.id == c.id

    # Otherwise active.
    chosen = BenchmarkComparisonService._resolve_selected_configuration([a, b, c], None)
    assert chosen.id == b.id

    # Otherwise first.
    no_active = [
        _saa_config("X", is_active=False),
        _saa_config("Y", is_active=False),
    ]
    chosen = BenchmarkComparisonService._resolve_selected_configuration(no_active, None)
    assert chosen.name == "X"


def test_saa_label_format() -> None:
    assert (
        BenchmarkComparisonService._build_saa_label("Standard 2026", "tangency")
        == "Tangency — Standard 2026"
    )
    assert (
        BenchmarkComparisonService._build_saa_label("Standard 2026", "min_var")
        == "Minimum Variance — Standard 2026"
    )


# The async tests above carry an explicit ``@pytest.mark.asyncio``
# at top of the file via the project's pytest config (asyncio_mode =
# auto) — no module-level pytestmark needed.


# ---------------------------------------------------------------------------
# get_portfolio_summary_kpis
# ---------------------------------------------------------------------------


async def test_get_portfolio_summary_kpis_empty_portfolio() -> None:
    """No investments → zero counts and None medians."""
    service = _build_service_with_mocks(
        investments=[],
        asset_classes=[],
        benchmarks=[],
        mappings=[],
    )
    kpis = await service.get_portfolio_summary_kpis()
    assert kpis.active_investments_count == 0
    assert kpis.investments_with_benchmark_count == 0
    assert kpis.investments_without_benchmark_count == 0
    assert kpis.median_excess_return_annualised is None
    assert kpis.hit_rate is None
    assert kpis.median_information_ratio is None


async def test_get_portfolio_summary_kpis_all_mapped() -> None:
    """All investments mapped → without_benchmark_count is zero."""
    ac = _ac_dto("equities")
    benchmark = _benchmark_dto("BM_EQ")
    mapping = _mapping_dto(ac.id, benchmark.id)
    inv_a = _investment_dto("Alpha", ac.id)
    inv_b = _investment_dto("Bravo", ac.id)

    start = date(2023, 1, 1)
    service = _build_service_with_mocks(
        investments=[inv_a, inv_b],
        asset_classes=[ac],
        benchmarks=[benchmark],
        mappings=[mapping],
        nav_rows_by_inv={
            inv_a.id: _daily_nav_series(inv_a.id, start, 400),
            inv_b.id: _daily_nav_series(inv_b.id, start, 400),
        },
        observations_by_benchmark={
            benchmark.id: _daily_obs_series(benchmark.id, start, 400),
        },
    )
    kpis = await service.get_portfolio_summary_kpis(as_of_date=date(2024, 2, 5))
    assert kpis.active_investments_count == 2
    assert kpis.investments_with_benchmark_count == 2
    assert kpis.investments_without_benchmark_count == 0
    # Medians require eligible rows (n >= 12); 13-month run satisfies.
    assert kpis.median_excess_return_annualised is not None
    assert kpis.hit_rate is not None
    assert kpis.median_information_ratio is not None
    assert 0.0 <= kpis.hit_rate <= 1.0


async def test_get_portfolio_summary_kpis_mixed_mapped_unmapped() -> None:
    """Mapped + unmapped → counts split, medians cover only mapped."""
    ac_mapped = _ac_dto("equities")
    ac_unmapped = _ac_dto("cash")
    benchmark = _benchmark_dto("BM_EQ")
    mapping = _mapping_dto(ac_mapped.id, benchmark.id)
    inv_a = _investment_dto("Alpha", ac_mapped.id)
    inv_b = _investment_dto("Bravo", ac_unmapped.id)

    start = date(2023, 1, 1)
    service = _build_service_with_mocks(
        investments=[inv_a, inv_b],
        asset_classes=[ac_mapped, ac_unmapped],
        benchmarks=[benchmark],
        mappings=[mapping],
        nav_rows_by_inv={
            inv_a.id: _daily_nav_series(inv_a.id, start, 400),
            inv_b.id: _daily_nav_series(inv_b.id, start, 400),
        },
        observations_by_benchmark={
            benchmark.id: _daily_obs_series(benchmark.id, start, 400),
        },
    )
    kpis = await service.get_portfolio_summary_kpis(as_of_date=date(2024, 2, 5))
    assert kpis.active_investments_count == 2
    assert kpis.investments_with_benchmark_count == 1
    assert kpis.investments_without_benchmark_count == 1
    assert kpis.median_excess_return_annualised is not None


async def test_get_portfolio_summary_kpis_filters_short_history() -> None:
    """Stub-length runs (n < 12) excluded from medians."""
    ac = _ac_dto("equities")
    benchmark = _benchmark_dto("BM_EQ")
    mapping = _mapping_dto(ac.id, benchmark.id)
    inv_short = _investment_dto("Stub", ac.id)

    start = date(2023, 1, 1)
    # 60 days → ~2 monthly observations only.
    service = _build_service_with_mocks(
        investments=[inv_short],
        asset_classes=[ac],
        benchmarks=[benchmark],
        mappings=[mapping],
        nav_rows_by_inv={
            inv_short.id: _daily_nav_series(inv_short.id, start, 60),
        },
        observations_by_benchmark={
            benchmark.id: _daily_obs_series(benchmark.id, start, 60),
        },
    )
    kpis = await service.get_portfolio_summary_kpis(as_of_date=date(2023, 3, 15))
    assert kpis.active_investments_count == 1
    assert kpis.investments_with_benchmark_count == 1
    # n_observations is < 12, so the medians and hit rate are None.
    assert kpis.median_excess_return_annualised is None
    assert kpis.hit_rate is None
    assert kpis.median_information_ratio is None


async def test_get_portfolio_summary_kpis_hit_rate_computation() -> None:
    """Hit rate is the share of eligible rows with positive excess."""
    ac = _ac_dto("equities")
    benchmark = _benchmark_dto("BM_EQ")
    mapping = _mapping_dto(ac.id, benchmark.id)
    # Two investments: identical NAV history → identical excess; the
    # hit rate is a clean 100% or 0% depending on the synthetic drift
    # relative to the benchmark daily-return constant. The synthetic
    # NAV series in _daily_nav_series uses 1.0005 daily compounding
    # (~13% annualised) and the benchmark _daily_obs_series default
    # is 0.0004 (~10.5% annualised) → investment beats benchmark.
    inv_a = _investment_dto("Alpha", ac.id)
    inv_b = _investment_dto("Bravo", ac.id)

    start = date(2023, 1, 1)
    service = _build_service_with_mocks(
        investments=[inv_a, inv_b],
        asset_classes=[ac],
        benchmarks=[benchmark],
        mappings=[mapping],
        nav_rows_by_inv={
            inv_a.id: _daily_nav_series(inv_a.id, start, 400),
            inv_b.id: _daily_nav_series(inv_b.id, start, 400),
        },
        observations_by_benchmark={
            benchmark.id: _daily_obs_series(benchmark.id, start, 400),
        },
    )
    kpis = await service.get_portfolio_summary_kpis(as_of_date=date(2024, 2, 5))
    assert kpis.hit_rate == 1.0
    assert kpis.median_excess_return_annualised is not None
    assert kpis.median_excess_return_annualised > 0


# ---------------------------------------------------------------------------
# _compute_saa_effects + SAAHypotheticalBundle.effects
# ---------------------------------------------------------------------------


def test_saa_effects_endpoints_match_compound_of_monthly() -> None:
    """The endpoints equal ``(1+r).cumprod().iloc[-1] - 1`` per series."""
    import pandas as pd

    from services.analytics.benchmark_comparison import SAAHypotheticalSeries
    from services.benchmark_comparison.benchmark_comparison_service import (
        _compute_saa_effects,
    )

    idx = pd.date_range("2023-01-31", periods=3, freq="ME")
    actual = pd.Series([0.02, 0.01, -0.005], index=idx)
    bench = pd.Series([0.015, 0.005, 0.0], index=idx)
    comp = pd.Series([0.018, 0.008, -0.002], index=idx)
    series = SAAHypotheticalSeries(
        saa_label="X",
        saa_weights={"equities": 1.0},
        saa_x_benchmark=bench,
        saa_x_composite=comp,
        actual_portfolio_returns=actual,
        period_start=date(2023, 1, 31),
        period_end=date(2023, 3, 31),
    )
    effects = _compute_saa_effects(series)

    expected_actual = (1.02 * 1.01 * 0.995) - 1.0
    expected_bench = (1.015 * 1.005 * 1.0) - 1.0
    expected_comp = (1.018 * 1.008 * 0.998) - 1.0
    assert effects.actual_cumulative_endpoint is not None
    assert effects.saa_x_benchmark_cumulative_endpoint is not None
    assert effects.saa_x_composite_cumulative_endpoint is not None
    assert effects.actual_cumulative_endpoint == pytest.approx(expected_actual, abs=1e-12)
    assert effects.saa_x_benchmark_cumulative_endpoint == pytest.approx(expected_bench, abs=1e-12)
    assert effects.saa_x_composite_cumulative_endpoint == pytest.approx(expected_comp, abs=1e-12)


def test_saa_effects_allocation_pp_is_arithmetic_difference() -> None:
    """``allocation_effect_pp = (actual - saa_x_benchmark) * 100``."""
    import pandas as pd

    from services.analytics.benchmark_comparison import SAAHypotheticalSeries
    from services.benchmark_comparison.benchmark_comparison_service import (
        _compute_saa_effects,
    )

    idx = pd.date_range("2023-01-31", periods=2, freq="ME")
    actual = pd.Series([0.10, 0.10], index=idx)  # cumulative 0.21
    bench = pd.Series([0.05, 0.05], index=idx)  # cumulative 0.1025
    comp = pd.Series([0.06, 0.06], index=idx)  # cumulative 0.1236
    series = SAAHypotheticalSeries(
        saa_label="X",
        saa_weights={"equities": 1.0},
        saa_x_benchmark=bench,
        saa_x_composite=comp,
        actual_portfolio_returns=actual,
        period_start=date(2023, 1, 31),
        period_end=date(2023, 2, 28),
    )
    effects = _compute_saa_effects(series)
    assert effects.allocation_effect_pp == pytest.approx((0.21 - 0.1025) * 100.0, abs=1e-9)
    assert effects.selection_effect_pp == pytest.approx((0.21 - 0.1236) * 100.0, abs=1e-9)


def test_saa_effects_handles_empty_series() -> None:
    """All-empty series → all endpoints and effects are ``None``."""
    import pandas as pd

    from services.analytics.benchmark_comparison import SAAHypotheticalSeries
    from services.benchmark_comparison.benchmark_comparison_service import (
        _compute_saa_effects,
    )

    empty = pd.Series(dtype="float64")
    series = SAAHypotheticalSeries(
        saa_label="X",
        saa_weights={},
        saa_x_benchmark=empty,
        saa_x_composite=empty,
        actual_portfolio_returns=empty,
        period_start=date(2023, 1, 31),
        period_end=date(2023, 1, 31),
    )
    effects = _compute_saa_effects(series)
    assert effects.actual_cumulative_endpoint is None
    assert effects.saa_x_benchmark_cumulative_endpoint is None
    assert effects.saa_x_composite_cumulative_endpoint is None
    assert effects.allocation_effect_pp is None
    assert effects.selection_effect_pp is None


def test_saa_effects_handles_partial_series() -> None:
    """Actual present, Composite empty → selection effect ``None``."""
    import pandas as pd

    from services.analytics.benchmark_comparison import SAAHypotheticalSeries
    from services.benchmark_comparison.benchmark_comparison_service import (
        _compute_saa_effects,
    )

    idx = pd.date_range("2023-01-31", periods=2, freq="ME")
    actual = pd.Series([0.02, 0.01], index=idx)
    bench = pd.Series([0.015, 0.005], index=idx)
    comp_empty = pd.Series(dtype="float64")
    series = SAAHypotheticalSeries(
        saa_label="X",
        saa_weights={"equities": 1.0},
        saa_x_benchmark=bench,
        saa_x_composite=comp_empty,
        actual_portfolio_returns=actual,
        period_start=date(2023, 1, 31),
        period_end=date(2023, 2, 28),
    )
    effects = _compute_saa_effects(series)
    assert effects.actual_cumulative_endpoint is not None
    assert effects.saa_x_benchmark_cumulative_endpoint is not None
    assert effects.saa_x_composite_cumulative_endpoint is None
    assert effects.allocation_effect_pp is not None
    assert effects.selection_effect_pp is None


async def test_saa_bundle_effects_field_is_none_when_series_is_none() -> None:
    """No SAA configurations → effects on the bundle is ``None``."""
    service = _build_service_with_mocks(
        investments=[],
        asset_classes=[],
        benchmarks=[],
        mappings=[],
        saa_configurations=[],
    )
    bundle = await service.get_saa_hypothetical()
    assert bundle.series is None
    assert bundle.effects is None


async def test_saa_bundle_effects_populated_when_series_present() -> None:
    """A successful optimisation populates ``bundle.effects`` alongside series."""
    ac_a = _ac_dto("equities", display_name="Equities")
    ac_b = _ac_dto("bonds", display_name="Bonds")
    benchmark_a = _benchmark_dto("BM_EQ")
    benchmark_b = _benchmark_dto("BM_BND")
    mappings = [
        _mapping_dto(ac_a.id, benchmark_a.id),
        _mapping_dto(ac_b.id, benchmark_b.id),
    ]
    inv_a = _investment_dto("Alpha", ac_a.id)
    inv_b = _investment_dto("Bravo", ac_b.id)
    start = date(2023, 1, 1)
    saa_config = _saa_config("Test")
    optimization = _OptimizationResultMock(
        asset_names=["Bonds", "Equities"],
        tangency=_PortfolioResultMock(weights=np.array([0.3, 0.7])),
        min_var=_PortfolioResultMock(weights=np.array([0.7, 0.3])),
    )
    service = _build_service_with_mocks(
        investments=[inv_a, inv_b],
        asset_classes=[ac_a, ac_b],
        benchmarks=[benchmark_a, benchmark_b],
        mappings=mappings,
        nav_rows_by_inv={
            inv_a.id: _daily_nav_series(inv_a.id, start, 400),
            inv_b.id: _daily_nav_series(inv_b.id, start, 400),
        },
        observations_by_benchmark={
            benchmark_a.id: _daily_obs_series(benchmark_a.id, start, 400),
            benchmark_b.id: _daily_obs_series(benchmark_b.id, start, 400, daily_return=0.0001),
        },
        saa_configurations=[saa_config],
        optimization_result=optimization,
    )
    bundle = await service.get_saa_hypothetical(
        weight_set="tangency",
        as_of_date=date(2024, 2, 5),
    )
    assert bundle.series is not None
    assert bundle.effects is not None
    assert bundle.effects.actual_cumulative_endpoint is not None
    assert bundle.effects.saa_x_benchmark_cumulative_endpoint is not None
    assert bundle.effects.allocation_effect_pp is not None


# ---------------------------------------------------------------------------
# get_investment_benchmark_inputs (ADR-0082 — reused by ArchetypeChartsService)
# ---------------------------------------------------------------------------


async def test_get_investment_benchmark_inputs_returns_tuple_for_mapped() -> None:
    """A mapped investment yields (display_name, monthly series, rf)."""
    ac = _ac_dto("equities")
    benchmark = _benchmark_dto("BM_EQ")
    mapping = _mapping_dto(ac.id, benchmark.id)
    inv = _investment_dto("Alpha", ac.id)

    start = date(2023, 1, 1)
    service = _build_service_with_mocks(
        investments=[inv],
        asset_classes=[ac],
        benchmarks=[benchmark],
        mappings=[mapping],
        nav_rows_by_inv={inv.id: _daily_nav_series(inv.id, start, 400)},
        observations_by_benchmark={
            benchmark.id: _daily_obs_series(benchmark.id, start, 400),
        },
        active_saa_config=_saa_config("Test"),
    )

    inputs = await service.get_investment_benchmark_inputs(inv.id, as_of_date=date(2024, 2, 5))
    assert inputs is not None
    display_name, monthly, risk_free_rate = inputs
    assert display_name == benchmark.display_name
    # The benchmark monthly series is non-empty over a 13-month run.
    assert not monthly.empty
    # Risk-free rate is sourced from the active SAA config (0.02).
    assert risk_free_rate == pytest.approx(0.02)


async def test_get_investment_benchmark_inputs_none_for_unmapped() -> None:
    """An investment whose asset class has no mapping returns None."""
    ac = _ac_dto("cash")
    inv = _investment_dto("Alpha", ac.id)

    service = _build_service_with_mocks(
        investments=[inv],
        asset_classes=[ac],
        benchmarks=[],
        mappings=[],
    )

    inputs = await service.get_investment_benchmark_inputs(inv.id, as_of_date=date(2024, 2, 5))
    assert inputs is None


async def test_get_investment_benchmark_inputs_none_for_unknown_id() -> None:
    """An unknown / cross-tenant id returns None (RLS hides the row)."""
    ac = _ac_dto("equities")
    benchmark = _benchmark_dto("BM_EQ")
    mapping = _mapping_dto(ac.id, benchmark.id)
    inv = _investment_dto("Alpha", ac.id)

    service = _build_service_with_mocks(
        investments=[inv],
        asset_classes=[ac],
        benchmarks=[benchmark],
        mappings=[mapping],
    )

    inputs = await service.get_investment_benchmark_inputs(uuid4(), as_of_date=date(2024, 2, 5))
    assert inputs is None


# ---------------------------------------------------------------------------
# ADR-0102 — conversion into the functional currency
# ---------------------------------------------------------------------------
#
# The three raw-NAV assembly sites of this service now convert at the
# ADR-0099 §4 boundary before the pure analytics layer sees a number:
# Stage a (per-investment), Stage b (asset-class composites) and Stage c
# (the synthetic "_portfolio" class). What that buys is stated once here:
# a benchmark's observations are already in one currency, so comparing an
# unconverted foreign investment against it would book the currency's drift
# as manager skill.


def _flat_usd_fixture() -> tuple:
    """A USD investment whose NAV is *flat in USD* across one month.

    Its EUR return is therefore pure FX: rate 1.00 → 1.10 over the month.
    Everything the conversion does is visible precisely because the local
    series does nothing.
    """
    ac = _ac_dto("equities")
    benchmark = _benchmark_dto("BM_EQ")
    mapping = _mapping_dto(ac.id, benchmark.id)
    inv = _investment_dto("Dollar Fund", ac.id, currency="USD")
    nav_rows = {
        inv.id: [
            _nav_dto(inv.id, date(2024, 1, 31), Decimal("100")),
            _nav_dto(inv.id, date(2024, 2, 29), Decimal("100")),
        ]
    }
    # A benchmark that returns exactly zero every day, so the excess is the
    # investment's own (converted) return and nothing else.
    observations = {
        benchmark.id: _daily_obs_series(benchmark.id, date(2024, 1, 1), 60, daily_return=0.0)
    }
    return ac, benchmark, mapping, inv, nav_rows, observations


async def test_functional_currency_universe_reads_no_fx_rows() -> None:
    """ADR-0099 §3 zero-read guarantee, asserted with a spy — not by value.

    An all-EUR universe under an EUR functional currency must not load a
    single FX row: the identity short-circuit fires in
    ``build_portfolio_fx_converter`` before any query. This is what makes a
    single-currency tenant's numbers provably unchanged.
    """
    ac = _ac_dto("equities")
    benchmark = _benchmark_dto("BM_EQ")
    mapping = _mapping_dto(ac.id, benchmark.id)
    inv = _investment_dto("Euro Fund", ac.id, currency="EUR")
    nav_rows = {inv.id: _daily_nav_series(inv.id, date(2024, 1, 1), 90)}
    observations = {benchmark.id: _daily_obs_series(benchmark.id, date(2024, 1, 1), 90)}

    fx_calls: list[list[str]] = []
    service = _build_service_with_mocks(
        investments=[inv],
        asset_classes=[ac],
        benchmarks=[benchmark],
        mappings=[mapping],
        nav_rows_by_inv=nav_rows,
        observations_by_benchmark=observations,
        fx_load_calls=fx_calls,
    )

    bundle = await service.get_investment_comparisons(as_of_date=date(2024, 3, 31))
    composites = await service.get_asset_class_composites(as_of_date=date(2024, 3, 31))

    assert fx_calls == []
    # ... and the surfaces still produce their rows.
    assert [r.investment_name for r in bundle.rows] == ["Euro Fund"]
    assert [r.asset_class_code for r in composites.rows] == ["equities"]


async def test_stage_a_converts_investment_series_before_comparison() -> None:
    """Stage a: a flat USD NAV yields a +10 % EUR return over the month.

    USD rates (price of 1 USD in EUR): 2024-01-31 → 1.00, 2024-02-29 → 1.10.
    Converted NAVs: 100 → 110 EUR. The monthly return is therefore
    ``110 / 100 - 1 = 0.10`` and the cumulative endpoint is exactly +0.10,
    against a benchmark that returned zero.

    Unconverted, this investment's NAV never moves and every metric here
    would be 0.0 — the assertion below fails on a service that skips the
    conversion.
    """
    ac, benchmark, mapping, inv, nav_rows, observations = _flat_usd_fixture()

    service = _build_service_with_mocks(
        investments=[inv],
        asset_classes=[ac],
        benchmarks=[benchmark],
        mappings=[mapping],
        nav_rows_by_inv=nav_rows,
        observations_by_benchmark=observations,
        fx_rows=[
            ("USD", date(2024, 1, 31), "1.00"),
            ("USD", date(2024, 2, 29), "1.10"),
        ],
    )

    detail = await service.get_investment_comparison_detail(inv.id, as_of_date=date(2024, 2, 29))

    assert detail is not None
    # Hand-computed: 100 USD × 1.00 = 100 EUR → 100 USD × 1.10 = 110 EUR.
    assert detail.investment_cumulative_returns.iloc[-1] == pytest.approx(0.10)
    assert detail.benchmark_cumulative_returns.iloc[-1] == pytest.approx(0.0)
    assert detail.excess_cumulative_returns.iloc[-1] == pytest.approx(0.10)


async def test_stage_c_actual_portfolio_returns_are_converted() -> None:
    """Stage c: the synthetic "_portfolio" class carries the FX effect.

    Same flat-USD investment, now read through the SAA-hypothetical path's
    actual-portfolio builder. A single-investment book means the composite
    *is* that investment, so the Actual series' cumulative endpoint must be
    the same hand-computed +10 %.
    """
    ac, benchmark, mapping, inv, nav_rows, observations = _flat_usd_fixture()
    config = _saa_config("Base")
    optimization = _OptimizationResultMock(
        asset_names=[ac.display_name],
        tangency=_PortfolioResultMock(weights=np.array([1.0])),
        min_var=_PortfolioResultMock(weights=np.array([1.0])),
    )

    service = _build_service_with_mocks(
        investments=[inv],
        asset_classes=[ac],
        benchmarks=[benchmark],
        mappings=[mapping],
        nav_rows_by_inv=nav_rows,
        observations_by_benchmark=observations,
        active_saa_config=config,
        saa_configurations=[config],
        optimization_result=optimization,
        fx_rows=[
            ("USD", date(2024, 1, 31), "1.00"),
            ("USD", date(2024, 2, 29), "1.10"),
        ],
    )

    bundle = await service.get_saa_hypothetical(as_of_date=date(2024, 2, 29))

    assert bundle.series is not None
    assert bundle.effects is not None
    assert bundle.effects.actual_cumulative_endpoint == pytest.approx(0.10)
    # The SAA × Benchmark leg is 100 % of a zero-return benchmark, so the
    # whole +10 pp allocation effect is the currency move the conversion
    # surfaced. Nominal (unconverted) NAVs would report 0.0 pp here.
    assert bundle.effects.allocation_effect_pp == pytest.approx(10.0)


async def test_uncovered_currency_raises_missing_fx_rate() -> None:
    """A USD position with no USD rate fails loudly — never a 1:1 fallback."""
    ac, benchmark, mapping, inv, nav_rows, observations = _flat_usd_fixture()

    service = _build_service_with_mocks(
        investments=[inv],
        asset_classes=[ac],
        benchmarks=[benchmark],
        mappings=[mapping],
        nav_rows_by_inv=nav_rows,
        observations_by_benchmark=observations,
        fx_rows=None,
    )

    with pytest.raises(MissingFxRateError) as excinfo:
        await service.get_investment_comparisons(as_of_date=date(2024, 2, 29))
    assert excinfo.value.currency == "USD"

    # Stage b assembles NAVs independently — it must fail the same way.
    with pytest.raises(MissingFxRateError):
        await service.get_asset_class_composites(as_of_date=date(2024, 2, 29))
