# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for :class:`ArchetypeChartsService` (ADR-0082, Option A).

Pure-mock tests with in-memory fakes for the eight repositories, a
stubbed :class:`BenchmarkComparisonService` (its ``get_investment_benchmark_inputs``
returns a pre-built tuple or ``None``), and a stubbed
:class:`InvestmentService` for the Capital-Account delegation. No DB.

The pure analytics primitives are exercised in
``tests/services/analytics``; here the focus is the assembly: archetype
routing, the income-aware total-return reconstruction, the
no-benchmark-mapping neutral state, ID→name composition resolution, the
Fixed-Income KPI wiring, and the unknown-id ``None`` contract.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pandas as pd
import pytest

from core.repositories.investment_bond_analytics_repository import (
    BondAnalyticsDTO,
)
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowDTO,
)
from core.repositories.investment_maturity_weights_repository import (
    MaturityWeightDTO,
)
from core.repositories.investment_nav_repository import InvestmentNavDTO
from core.repositories.investment_rating_weights_repository import (
    RatingWeightDTO,
)
from core.repositories.investment_region_weights_repository import (
    RegionWeightDTO,
)
from core.repositories.investment_repository import InvestmentDTO
from core.repositories.investment_sector_weights_repository import (
    SectorWeightDTO,
)
from core.repositories.region_repository import RegionDTO
from core.repositories.sector_repository import SectorDTO
from services.analytics.benchmark_comparison import compute_benchmark_comparison
from services.analytics.investment_returns import compute_total_return_series
from services.front_office_charts import ArchetypeChartsService
from services.front_office_charts.archetype_charts_service import (
    _compound_daily_to_monthly,
)
from services.investments import InvestmentChartsBundle
from services.investments.archetype import Archetype


_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = UUID("00000000-0000-0000-0000-0000000000aa")


# ---------------------------------------------------------------------------
# Fabrication helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _investment_dto(
    name: str,
    investment_type: str,
    *,
    commitment_amount: Decimal | None = None,
) -> InvestmentDTO:
    return InvestmentDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        name=name,
        investment_type=investment_type,
        asset_class_id=uuid4(),
        manager_name=None,
        region=None,
        currency="EUR",
        vintage_year=2020,
        commitment_amount=commitment_amount,
        is_active=True,
        type_specific_data=None,
        created_by=_USER_ID,
        created_at=_now(),
        updated_at=_now(),
        anlv_code=None,
    )


def _nav_dto(inv_id: UUID, as_of: date, value: float, kind: str = "actual") -> InvestmentNavDTO:
    return InvestmentNavDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        investment_id=inv_id,
        as_of_date=as_of,
        nav_value=Decimal(str(value)),
        currency="EUR",
        nav_kind=kind,
        source=None,
        created_by=_USER_ID,
        created_at=_now(),
        updated_at=_now(),
    )


def _cashflow_dto(
    inv_id: UUID,
    ts: datetime,
    flow_type: str,
    amount: float,
    kind: str = "actual",
) -> InvestmentCashflowDTO:
    return InvestmentCashflowDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        investment_id=inv_id,
        flow_timestamp=ts,
        flow_type=flow_type,
        flow_kind=kind,
        amount=Decimal(str(amount)),
        currency="EUR",
        description=None,
        created_by=_USER_ID,
        created_at=_now(),
        updated_at=_now(),
    )


def _bond_analytics_dto(
    inv_id: UUID,
    as_of: date,
    ytm: float,
    eff_duration: float,
    oas: float | None,
) -> BondAnalyticsDTO:
    return BondAnalyticsDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        investment_id=inv_id,
        as_of_date=as_of,
        ytm=Decimal(str(ytm)),
        eff_duration=Decimal(str(eff_duration)),
        oas=Decimal(str(oas)) if oas is not None else None,
        convexity=None,
        basis="reported",
        created_by=_USER_ID,
        created_at=_now(),
        updated_at=_now(),
    )


def _rating_weight_dto(
    inv_id: UUID, as_of: date, bucket: str, weight_pct: float
) -> RatingWeightDTO:
    return RatingWeightDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        investment_id=inv_id,
        as_of_date=as_of,
        rating_bucket=bucket,
        weight_pct=Decimal(str(weight_pct)),
        basis="reported",
        created_by=_USER_ID,
        created_at=_now(),
        updated_at=_now(),
    )


def _maturity_weight_dto(
    inv_id: UUID, as_of: date, bucket: str, weight_pct: float
) -> MaturityWeightDTO:
    return MaturityWeightDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        investment_id=inv_id,
        as_of_date=as_of,
        maturity_bucket=bucket,
        weight_pct=Decimal(str(weight_pct)),
        basis="reported",
        created_by=_USER_ID,
        created_at=_now(),
        updated_at=_now(),
    )


def _sector_weight_dto(
    inv_id: UUID, as_of: date, sector_id: UUID, weight_pct: float
) -> SectorWeightDTO:
    return SectorWeightDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        investment_id=inv_id,
        as_of_date=as_of,
        sector_id=sector_id,
        weight_pct=Decimal(str(weight_pct)),
        basis="reported",
        created_by=_USER_ID,
        created_at=_now(),
        updated_at=_now(),
    )


def _region_weight_dto(
    inv_id: UUID, as_of: date, region_id: UUID, weight_pct: float
) -> RegionWeightDTO:
    return RegionWeightDTO(
        id=uuid4(),
        tenant_id=_TENANT_ID,
        investment_id=inv_id,
        as_of_date=as_of,
        region_id=region_id,
        weight_pct=Decimal(str(weight_pct)),
        basis="reported",
        created_by=_USER_ID,
        created_at=_now(),
    )


def _sector_dto(sector_id: UUID, code: str, display_name: str) -> SectorDTO:
    return SectorDTO(
        id=sector_id,
        tenant_id=_TENANT_ID,
        code=code,
        display_name=display_name,
        is_active=True,
        created_by=_USER_ID,
        created_at=_now(),
        updated_at=_now(),
    )


def _region_dto(region_id: UUID, code: str, display_name: str) -> RegionDTO:
    return RegionDTO(
        id=region_id,
        tenant_id=_TENANT_ID,
        code=code,
        display_name=display_name,
        description=None,
        sort_order=0,
        created_at=_now(),
        updated_at=_now(),
    )


# ---------------------------------------------------------------------------
# Service fabrication
# ---------------------------------------------------------------------------


def _build_service(
    *,
    investments: list[InvestmentDTO],
    navs_by_inv: dict[UUID, list[InvestmentNavDTO]] | None = None,
    cashflows_by_inv: dict[UUID, list[InvestmentCashflowDTO]] | None = None,
    bond_analytics_by_inv: dict[UUID, list[BondAnalyticsDTO]] | None = None,
    rating_weights_by_inv: dict[UUID, list[RatingWeightDTO]] | None = None,
    maturity_weights_by_inv: (dict[UUID, list[MaturityWeightDTO]] | None) = None,
    sector_weights_by_inv: dict[UUID, list[SectorWeightDTO]] | None = None,
    region_weights_by_inv: dict[UUID, list[RegionWeightDTO]] | None = None,
    sectors: list[SectorDTO] | None = None,
    regions: list[RegionDTO] | None = None,
    charts_bundle_by_inv: (dict[UUID, InvestmentChartsBundle | None] | None) = None,
    benchmark_inputs_by_inv: (dict[UUID, tuple[str, pd.Series, float]] | None) = None,
) -> ArchetypeChartsService:
    navs_by_inv = navs_by_inv or {}
    cashflows_by_inv = cashflows_by_inv or {}
    bond_analytics_by_inv = bond_analytics_by_inv or {}
    rating_weights_by_inv = rating_weights_by_inv or {}
    maturity_weights_by_inv = maturity_weights_by_inv or {}
    sector_weights_by_inv = sector_weights_by_inv or {}
    region_weights_by_inv = region_weights_by_inv or {}
    charts_bundle_by_inv = charts_bundle_by_inv or {}
    benchmark_inputs_by_inv = benchmark_inputs_by_inv or {}

    inv_lookup = {inv.id: inv for inv in investments}

    investments_repo = MagicMock()
    investments_repo.get_by_id = AsyncMock(side_effect=lambda inv_id: inv_lookup.get(inv_id))
    investments_repo.list_active = AsyncMock(
        return_value=[inv for inv in investments if inv.is_active]
    )

    navs_repo = MagicMock()

    async def _list_by_inv_kind(inv_id: UUID, kind: str):
        return [n for n in navs_by_inv.get(inv_id, []) if n.nav_kind == kind]

    async def _list_by_inv(inv_id: UUID):
        return list(navs_by_inv.get(inv_id, []))

    async def _latest_actual_as_of_date(inv_ids: list[UUID]):
        dates = [
            n.as_of_date
            for inv_id in inv_ids
            for n in navs_by_inv.get(inv_id, [])
            if n.nav_kind == "actual"
        ]
        return max(dates) if dates else None

    navs_repo.list_by_investment_and_kind = AsyncMock(side_effect=_list_by_inv_kind)
    navs_repo.list_by_investment = AsyncMock(side_effect=_list_by_inv)
    navs_repo.latest_actual_as_of_date = AsyncMock(side_effect=_latest_actual_as_of_date)

    cashflows_repo = MagicMock()
    cashflows_repo.list_by_investment = AsyncMock(
        side_effect=lambda inv_id: list(cashflows_by_inv.get(inv_id, []))
    )

    def _filtered_list(by_inv: dict[UUID, list]):
        async def _inner(inv_id: UUID, as_of_cutoff: date | None = None):
            rows = by_inv.get(inv_id, [])
            if as_of_cutoff is not None:
                rows = [r for r in rows if r.as_of_date <= as_of_cutoff]
            return list(rows)

        return _inner

    bond_repo = MagicMock()
    bond_repo.list_for_investment = AsyncMock(side_effect=_filtered_list(bond_analytics_by_inv))

    rating_repo = MagicMock()
    rating_repo.list_for_investment = AsyncMock(side_effect=_filtered_list(rating_weights_by_inv))

    maturity_repo = MagicMock()
    maturity_repo.list_for_investment = AsyncMock(
        side_effect=_filtered_list(maturity_weights_by_inv)
    )

    def _latest_snapshot(by_inv: dict[UUID, list]):
        async def _inner(inv_id: UUID, *, as_of_cutoff: date | None = None):
            rows = by_inv.get(inv_id, [])
            if as_of_cutoff is not None:
                rows = [r for r in rows if r.as_of_date <= as_of_cutoff]
            if not rows:
                return []
            latest = max(r.as_of_date for r in rows)
            return [r for r in rows if r.as_of_date == latest]

        return _inner

    sector_weights_repo = MagicMock()
    sector_weights_repo.list_latest_for_investment = AsyncMock(
        side_effect=_latest_snapshot(sector_weights_by_inv)
    )

    region_weights_repo = MagicMock()
    region_weights_repo.list_latest_for_investment = AsyncMock(
        side_effect=_latest_snapshot(region_weights_by_inv)
    )

    sectors_repo = MagicMock()
    sectors_repo.list_all = AsyncMock(return_value=list(sectors or []))

    regions_repo = MagicMock()
    regions_repo.list_all = AsyncMock(return_value=list(regions or []))

    investments_service = MagicMock()
    investments_service.get_charts_data = AsyncMock(
        side_effect=lambda inv_id, **_: charts_bundle_by_inv.get(inv_id)
    )

    benchmarks = MagicMock()
    benchmarks.get_investment_benchmark_inputs = AsyncMock(
        side_effect=lambda inv_id, as_of_date=None: benchmark_inputs_by_inv.get(inv_id)
    )

    return ArchetypeChartsService(
        investments=investments_repo,
        navs=navs_repo,
        cashflows=cashflows_repo,
        bond_analytics=bond_repo,
        rating_weights=rating_repo,
        maturity_weights=maturity_repo,
        sector_weights=sector_weights_repo,
        region_weights=region_weights_repo,
        sectors=sectors_repo,
        regions=regions_repo,
        investments_service=investments_service,
        benchmarks=benchmarks,
    )


def _flat_nav_series(inv_id: UUID, value: float = 100.0) -> list[InvestmentNavDTO]:
    """Three month-end actual NAVs, flat ex-income price."""
    return [
        _nav_dto(inv_id, date(2024, 1, 31), value),
        _nav_dto(inv_id, date(2024, 2, 29), value),
        _nav_dto(inv_id, date(2024, 3, 31), value),
    ]


def _capital_account_bundle(name: str) -> InvestmentChartsBundle:
    """A minimal Capital-Account bundle with defined last multiples / IRR."""
    d1 = date(2023, 12, 31)
    d2 = date(2024, 3, 31)
    multiples = pd.DataFrame(
        {
            "as_of_date": [d1, d2],
            "tvpi": [float("nan"), 1.5],
            "dpi": [float("nan"), 0.5],
            "rvpi": [float("nan"), 1.0],
        }
    )
    rolling_irr = pd.Series([float("nan"), 0.12], index=pd.to_datetime([d1, d2]))
    cashflows_actual = pd.DataFrame(
        {
            "flow_timestamp": [
                datetime(2023, 12, 31, tzinfo=timezone.utc),
                datetime(2024, 3, 31, tzinfo=timezone.utc),
            ],
            "flow_type": ["capital_call", "distribution"],
            "amount": [-100.0, 50.0],
        }
    )
    return InvestmentChartsBundle(
        total_return_series=pd.Series(dtype="float64"),
        nav_series=pd.Series(dtype="float64"),
        cashflows_actual=cashflows_actual,
        net_capital_gain=pd.Series(dtype="float64"),
        rolling_multiples=multiples,
        rolling_irr=rolling_irr,
        investment_name=name,
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


async def test_routes_listed_bonds_to_fixed_income() -> None:
    inv = _investment_dto("Bond Fund", "listed_bonds")
    service = _build_service(
        investments=[inv],
        navs_by_inv={inv.id: _flat_nav_series(inv.id)},
    )

    result = await service.get_archetype_charts_data(inv.id, as_of_date=date(2024, 3, 31))

    assert result is not None
    assert result.archetype is Archetype.FIXED_INCOME
    assert result.fixed_income is not None
    # No Capital-Account / equity / NAV-only fields leak in.
    assert result.capital_account is None
    assert result.total_return_equity is None
    assert result.nav_only is None


async def test_routes_private_equity_to_capital_account() -> None:
    inv = _investment_dto("PE Fund", "private_equity", commitment_amount=Decimal("200"))
    service = _build_service(
        investments=[inv],
        charts_bundle_by_inv={inv.id: _capital_account_bundle("PE Fund")},
    )

    result = await service.get_archetype_charts_data(inv.id)

    assert result is not None
    assert result.archetype is Archetype.CAPITAL_ACCOUNT
    assert result.capital_account is not None
    assert result.fixed_income is None
    # The KPI caption is derived from the delegated bundle.
    kpi = result.capital_account.kpi
    assert kpi.tvpi == pytest.approx(1.5)
    assert kpi.dpi == pytest.approx(0.5)
    assert kpi.net_irr == pytest.approx(0.12)
    # unfunded = commitment(200) - total_called(|−100|) = 100.
    assert kpi.unfunded_commitment == pytest.approx(100.0)


async def test_routes_listed_equity_to_total_return_equity() -> None:
    inv = _investment_dto("Equity Fund", "listed_equity")
    service = _build_service(
        investments=[inv],
        navs_by_inv={inv.id: _flat_nav_series(inv.id)},
    )

    result = await service.get_archetype_charts_data(inv.id, as_of_date=date(2024, 3, 31))

    assert result is not None
    assert result.archetype is Archetype.TOTAL_RETURN_EQUITY
    assert result.total_return_equity is not None
    assert result.capital_account is None
    assert result.fixed_income is None


async def test_routes_other_to_nav_only() -> None:
    inv = _investment_dto("Hedge Fund", "other")
    service = _build_service(
        investments=[inv],
        navs_by_inv={inv.id: _flat_nav_series(inv.id)},
    )

    result = await service.get_archetype_charts_data(inv.id)

    assert result is not None
    assert result.archetype is Archetype.NAV_ONLY
    assert result.nav_only is not None
    assert result.total_return_equity is None
    # All NAV rows are passed through for the single wide tile.
    assert len(result.nav_only.navs) == 3


# ---------------------------------------------------------------------------
# Income-aware total return
# ---------------------------------------------------------------------------


async def test_income_aware_total_return_differs_from_pct_change() -> None:
    """A dividend is added back, so the TR series beats naive pct_change."""
    inv = _investment_dto("Equity Fund", "listed_equity")
    navs = _flat_nav_series(inv.id, value=100.0)
    # +5 dividend inside the February interval (Jan-31, Feb-29].
    dividend = _cashflow_dto(inv.id, datetime(2024, 2, 15, tzinfo=timezone.utc), "dividend", 5.0)
    service = _build_service(
        investments=[inv],
        navs_by_inv={inv.id: navs},
        cashflows_by_inv={inv.id: [dividend]},
    )

    income_aware = await service._investment_total_return_monthly(inv.id, date(2024, 3, 31))

    # Naive ex-income price return: flat NAV → zero every month.
    nav_series = pd.Series(
        [100.0, 100.0, 100.0],
        index=[date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31)],
        dtype="float64",
    )
    naive_monthly = _compound_daily_to_monthly(compute_total_return_series(nav_series))

    feb = pd.Timestamp("2024-02-29")
    # Income-aware February return reflects the +5 dividend on a 100 NAV.
    assert income_aware.loc[feb] == pytest.approx(0.05)
    # The naive series shows a flat 0.0 — the demonstrable difference.
    assert naive_monthly.loc[feb] == pytest.approx(0.0)
    assert income_aware.loc[feb] != pytest.approx(naive_monthly.loc[feb])


async def test_coupon_income_added_back_for_fixed_income() -> None:
    """Coupons (not just dividends) feed the Fixed-Income TR reconstruction."""
    inv = _investment_dto("Bond Fund", "listed_bonds")
    navs = _flat_nav_series(inv.id, value=100.0)
    coupon = _cashflow_dto(inv.id, datetime(2024, 2, 10, tzinfo=timezone.utc), "coupon", 2.0)
    service = _build_service(
        investments=[inv],
        navs_by_inv={inv.id: navs},
        cashflows_by_inv={inv.id: [coupon]},
    )

    income_aware = await service._investment_total_return_monthly(inv.id, date(2024, 3, 31))
    assert income_aware.loc[pd.Timestamp("2024-02-29")] == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# No benchmark mapping → neutral hero, None metrics
# ---------------------------------------------------------------------------


async def test_no_benchmark_mapping_leaves_neutral_state() -> None:
    inv = _investment_dto("Equity Fund", "listed_equity")
    service = _build_service(
        investments=[inv],
        navs_by_inv={inv.id: _flat_nav_series(inv.id, value=100.0)},
        cashflows_by_inv={
            inv.id: [
                _cashflow_dto(
                    inv.id,
                    datetime(2024, 2, 15, tzinfo=timezone.utc),
                    "dividend",
                    5.0,
                )
            ]
        },
        # No entry → get_investment_benchmark_inputs returns None.
        benchmark_inputs_by_inv={},
    )

    result = await service.get_archetype_charts_data(inv.id, as_of_date=date(2024, 3, 31))
    assert result is not None
    tiles = result.total_return_equity
    assert tiles is not None
    # Benchmark side is empty / None.
    assert tiles.benchmark_display_name is None
    assert tiles.benchmark_cumulative.empty
    assert tiles.excess_cumulative.empty
    assert tiles.kpi.beta is None
    assert tiles.kpi.tracking_error is None
    assert tiles.kpi.information_ratio is None
    # The investment hero is still populated (income-aware TR is present).
    assert not tiles.investment_cumulative.empty


async def test_mapped_benchmark_populates_hero_and_metrics() -> None:
    """With a mapping, the hero carries aligned fund + benchmark cumulatives."""
    inv = _investment_dto("Equity Fund", "listed_equity")
    # Benchmark monthly returns on the same month-end grid as the fund TR.
    bench_monthly = pd.Series(
        [0.01, 0.02],
        index=pd.to_datetime(["2024-02-29", "2024-03-31"]),
        dtype="float64",
    )
    service = _build_service(
        investments=[inv],
        navs_by_inv={inv.id: _flat_nav_series(inv.id, value=100.0)},
        cashflows_by_inv={
            inv.id: [
                _cashflow_dto(
                    inv.id,
                    datetime(2024, 2, 15, tzinfo=timezone.utc),
                    "dividend",
                    5.0,
                )
            ]
        },
        benchmark_inputs_by_inv={
            inv.id: ("MSCI World", bench_monthly, 0.02),
        },
    )

    result = await service.get_archetype_charts_data(inv.id, as_of_date=date(2024, 3, 31))
    assert result is not None
    tiles = result.total_return_equity
    assert tiles is not None
    assert tiles.benchmark_display_name == "MSCI World"
    assert not tiles.investment_cumulative.empty
    assert not tiles.benchmark_cumulative.empty
    # excess = fund − benchmark over the months both lines cover.
    assert not tiles.excess_cumulative.empty


# ---------------------------------------------------------------------------
# Hero de-clipping — start aligned, ends free (ADR-0113 §5)
#
# These exercise ``_benchmark_block`` directly: it is the seam that turns
# monthly return series into the drawn cumulatives, and driving it with
# hand-built series pins the start/end geometry far more precisely than
# reconstructing the fund series from NAV fixtures would.
# ---------------------------------------------------------------------------


def _monthly_returns(months: list[str], value: float = 0.01) -> pd.Series:
    """Constant monthly returns stamped on the given month-ends."""
    return pd.Series(
        [value] * len(months),
        index=pd.to_datetime(months),
        dtype="float64",
    )


_Q1_2024 = ["2024-01-31", "2024-02-29", "2024-03-31"]
_H1_2024 = [*_Q1_2024, "2024-04-30", "2024-05-31", "2024-06-30"]


async def test_stale_benchmark_lets_the_investment_line_run_on() -> None:
    """A tick extends the fund line; the benchmark stops at its import state."""
    inv = _investment_dto("Equity Fund", "listed_equity")
    inv_monthly = _monthly_returns(_H1_2024, 0.01)
    bench_monthly = _monthly_returns(_Q1_2024, 0.005)
    service = _build_service(
        investments=[inv],
        benchmark_inputs_by_inv={inv.id: ("MSCI World", bench_monthly, 0.02)},
    )

    _name, inv_cum, bench_cum, excess, _metrics = await service._benchmark_block(
        inv, inv_monthly, date(2024, 6, 30)
    )

    assert inv_cum.index.max() == pd.Timestamp("2024-06-30")
    assert bench_cum.index.max() == pd.Timestamp("2024-03-31")
    # The excess stops where the benchmark does — it is undefined beyond.
    assert excess.index.max() == pd.Timestamp("2024-03-31")
    # Same start for all three.
    assert inv_cum.index.min() == bench_cum.index.min() == pd.Timestamp("2024-01-31")
    # The fund line keeps compounding past the benchmark's last month.
    assert inv_cum.loc[pd.Timestamp("2024-06-30")] == pytest.approx(1.01**6 - 1.0)


async def test_stale_investment_lets_the_benchmark_line_run_on() -> None:
    """The mirrored case: a fresher benchmark extends past the fund."""
    inv = _investment_dto("Equity Fund", "listed_equity")
    inv_monthly = _monthly_returns(_Q1_2024, 0.01)
    bench_monthly = _monthly_returns(_H1_2024, 0.005)
    service = _build_service(
        investments=[inv],
        benchmark_inputs_by_inv={inv.id: ("MSCI World", bench_monthly, 0.02)},
    )

    _name, inv_cum, bench_cum, excess, _metrics = await service._benchmark_block(
        inv, inv_monthly, date(2024, 6, 30)
    )

    assert inv_cum.index.max() == pd.Timestamp("2024-03-31")
    assert bench_cum.index.max() == pd.Timestamp("2024-06-30")
    assert excess.index.max() == pd.Timestamp("2024-03-31")
    assert bench_cum.loc[pd.Timestamp("2024-06-30")] == pytest.approx(1.005**6 - 1.0)


async def test_hero_lines_start_at_the_common_first_month() -> None:
    """Years of fund history before the benchmark's first month are trimmed."""
    inv = _investment_dto("Equity Fund", "listed_equity")
    inv_monthly = _monthly_returns(
        ["2021-01-31", "2021-02-28", "2021-03-31", *_Q1_2024],
        0.01,
    )
    bench_monthly = _monthly_returns(_Q1_2024, 0.005)
    service = _build_service(
        investments=[inv],
        benchmark_inputs_by_inv={inv.id: ("MSCI World", bench_monthly, 0.02)},
    )

    _name, inv_cum, bench_cum, excess, _metrics = await service._benchmark_block(
        inv, inv_monthly, date(2024, 3, 31)
    )

    common_start = pd.Timestamp("2024-01-31")
    assert inv_cum.index.min() == bench_cum.index.min() == common_start
    assert not (inv_cum.index < common_start).any()
    assert not (bench_cum.index < common_start).any()
    # Compounding restarts at the common month — the 2021 months contribute
    # nothing, so both lines still leave a common 0 % origin.
    assert inv_cum.loc[common_start] == pytest.approx(0.01)
    assert excess.loc[common_start] == pytest.approx(0.01 - 0.005)


async def test_excess_equals_the_visible_gap_between_the_lines() -> None:
    """The excess is the two drawn cumulatives' difference, month by month."""
    inv = _investment_dto("Equity Fund", "listed_equity")
    inv_monthly = _monthly_returns(_H1_2024, 0.01)
    bench_monthly = _monthly_returns(_Q1_2024, 0.005)
    service = _build_service(
        investments=[inv],
        benchmark_inputs_by_inv={inv.id: ("MSCI World", bench_monthly, 0.02)},
    )

    _name, inv_cum, bench_cum, excess, _metrics = await service._benchmark_block(
        inv, inv_monthly, date(2024, 6, 30)
    )

    for month in excess.index:
        assert excess.loc[month] == pytest.approx(inv_cum.loc[month] - bench_cum.loc[month])
    assert not excess.isna().any()


async def test_metrics_stay_on_the_inner_join() -> None:
    """De-clipping the lines leaves the metrics bundle byte-identical."""
    inv = _investment_dto("Equity Fund", "listed_equity")
    inv_monthly = pd.Series(
        [0.01, -0.02, 0.03, 0.015, -0.005, 0.02],
        index=pd.to_datetime(_H1_2024),
        dtype="float64",
    )
    bench_monthly = pd.Series(
        [0.005, -0.01, 0.02],
        index=pd.to_datetime(_Q1_2024),
        dtype="float64",
    )
    service = _build_service(
        investments=[inv],
        benchmark_inputs_by_inv={inv.id: ("MSCI World", bench_monthly, 0.02)},
    )

    _name, _inv_cum, _bench_cum, _excess, metrics = await service._benchmark_block(
        inv, inv_monthly, date(2024, 6, 30)
    )

    expected = compute_benchmark_comparison(
        investment_returns=inv_monthly,
        benchmark_returns=bench_monthly,
        risk_free_returns=pd.Series(0.02 / 12.0, index=inv_monthly.index, dtype="float64"),
        investment_identifier=inv.name,
        benchmark_identifier="MSCI World",
    ).metrics
    assert metrics is not None
    # Field-wise: several metrics are NaN sentinels at this sample size,
    # and NaN != NaN would make a plain dataclass comparison meaningless.
    actual_fields = asdict(metrics)
    for field, expected_value in asdict(expected).items():
        actual_value = actual_fields[field]
        if isinstance(expected_value, float):
            assert actual_value == pytest.approx(expected_value, nan_ok=True), field
        else:
            assert actual_value == expected_value, field
    # The inner-join count, not the drawn-months count.
    assert metrics.n_observations == 3


async def test_empty_and_disjoint_inputs_keep_the_empty_state() -> None:
    """Unmapped, empty benchmark, and disjoint periods all stay empty."""
    inv = _investment_dto("Equity Fund", "listed_equity")
    inv_monthly = _monthly_returns(_Q1_2024, 0.01)
    disjoint_bench = _monthly_returns(["2021-01-31", "2021-02-28"], 0.005)
    service = _build_service(
        investments=[inv],
        benchmark_inputs_by_inv={inv.id: ("MSCI World", pd.Series(dtype="float64"), 0.02)},
    )

    # Empty benchmark series.
    name, inv_cum, bench_cum, excess, metrics = await service._benchmark_block(
        inv, inv_monthly, date(2024, 3, 31)
    )
    assert name == "MSCI World"
    assert inv_cum.empty and bench_cum.empty and excess.empty
    assert metrics is not None and metrics.n_observations == 0

    # Disjoint periods — no month where both series exist.
    disjoint_service = _build_service(
        investments=[inv],
        benchmark_inputs_by_inv={inv.id: ("MSCI World", disjoint_bench, 0.02)},
    )
    _name, inv_cum, bench_cum, excess, metrics = await disjoint_service._benchmark_block(
        inv, inv_monthly, date(2024, 3, 31)
    )
    assert inv_cum.empty and bench_cum.empty and excess.empty
    assert metrics is not None and metrics.n_observations == 0

    # Unmapped — the fund line alone, benchmark side empty, metrics None.
    unmapped_service = _build_service(investments=[inv], benchmark_inputs_by_inv={})
    name, inv_cum, bench_cum, excess, metrics = await unmapped_service._benchmark_block(
        inv, inv_monthly, date(2024, 3, 31)
    )
    assert name is None and metrics is None
    assert not inv_cum.empty
    assert bench_cum.empty and excess.empty


# ---------------------------------------------------------------------------
# Composition resolution (IDs → names)
# ---------------------------------------------------------------------------


async def test_equity_composition_resolves_ids_to_names() -> None:
    inv = _investment_dto("Equity Fund", "listed_equity")
    tech_id = uuid4()
    health_id = uuid4()
    dach_id = uuid4()
    as_of = date(2024, 3, 31)
    service = _build_service(
        investments=[inv],
        navs_by_inv={inv.id: _flat_nav_series(inv.id)},
        sector_weights_by_inv={
            inv.id: [
                _sector_weight_dto(inv.id, as_of, tech_id, 60.0),
                _sector_weight_dto(inv.id, as_of, health_id, 40.0),
            ]
        },
        region_weights_by_inv={inv.id: [_region_weight_dto(inv.id, as_of, dach_id, 100.0)]},
        sectors=[
            _sector_dto(tech_id, "tech", "Technology"),
            _sector_dto(health_id, "health", "Healthcare"),
        ],
        regions=[_region_dto(dach_id, "dach", "DACH")],
    )

    result = await service.get_archetype_charts_data(inv.id, as_of_date=as_of)
    assert result is not None
    tiles = result.total_return_equity
    assert tiles is not None
    assert tiles.sector_weights == {"Technology": 60.0, "Healthcare": 40.0}
    assert tiles.region_weights == {"DACH": 100.0}
    # Labels, never UUIDs.
    for key in {**tiles.sector_weights, **tiles.region_weights}:
        assert isinstance(key, str)
        with pytest.raises(ValueError):
            UUID(key)


# ---------------------------------------------------------------------------
# Fixed-Income KPI
# ---------------------------------------------------------------------------


async def test_fixed_income_kpi_uses_latest_and_notch_weighted_rating() -> None:
    inv = _investment_dto("Bond Fund", "listed_bonds")
    older = date(2024, 1, 31)
    latest = date(2024, 3, 31)
    service = _build_service(
        investments=[inv],
        navs_by_inv={inv.id: _flat_nav_series(inv.id)},
        bond_analytics_by_inv={
            inv.id: [
                _bond_analytics_dto(inv.id, older, 0.03, 5.0, 0.012),
                _bond_analytics_dto(inv.id, latest, 0.045, 6.2, 0.015),
            ]
        },
        rating_weights_by_inv={
            inv.id: [
                _rating_weight_dto(inv.id, latest, "AAA", 50.0),
                _rating_weight_dto(inv.id, latest, "A", 50.0),
            ]
        },
        maturity_weights_by_inv={inv.id: [_maturity_weight_dto(inv.id, latest, "5-7y", 100.0)]},
    )

    result = await service.get_archetype_charts_data(inv.id, as_of_date=latest)
    assert result is not None
    tiles = result.fixed_income
    assert tiles is not None
    kpi = tiles.kpi
    # Latest (newest) bond-analytics values.
    assert kpi.ytm == pytest.approx(0.045)
    assert kpi.eff_duration == pytest.approx(6.2)
    assert kpi.oas == pytest.approx(0.015)
    # AAA=1, A=3, equal weight → notch 2.0 → nearest bucket "AA".
    assert kpi.avg_rating.average_notch == pytest.approx(2.0)
    assert kpi.avg_rating.average_bucket == "AA"
    # The composition tile carries both rating and maturity breakdowns.
    assert tiles.rating_weights == {"AAA": 50.0, "A": 50.0}
    assert tiles.maturity_weights == {"5-7y": 100.0}


async def test_fixed_income_empty_bond_analytics_yields_none() -> None:
    """No bond-analytics rows → YTM/duration/OAS are None, not fabricated."""
    inv = _investment_dto("Bond Fund", "listed_bonds")
    service = _build_service(
        investments=[inv],
        navs_by_inv={inv.id: _flat_nav_series(inv.id)},
        # No bond analytics, no rating weights.
    )

    result = await service.get_archetype_charts_data(inv.id, as_of_date=date(2024, 3, 31))
    assert result is not None
    tiles = result.fixed_income
    assert tiles is not None
    assert tiles.bond_analytics.empty
    assert tiles.kpi.ytm is None
    assert tiles.kpi.eff_duration is None
    assert tiles.kpi.oas is None
    # An empty rating distribution is the explicit NR sentinel.
    assert tiles.kpi.avg_rating.average_bucket == "NR"
    assert tiles.rating_weights == {}


# ---------------------------------------------------------------------------
# Unknown / cross-tenant id
# ---------------------------------------------------------------------------


async def test_unknown_investment_returns_none() -> None:
    service = _build_service(investments=[])
    result = await service.get_archetype_charts_data(uuid4())
    assert result is None


# ---------------------------------------------------------------------------
# Universe axis end (ADR-0113 §1)
# ---------------------------------------------------------------------------


async def test_universe_axis_end_is_the_max_actual_across_active_investments() -> None:
    """A stale investment does not hold the shared axis end back."""
    stale = _investment_dto("Private Fund", "private_equity")
    fresh = _investment_dto("Listed Fund", "listed_equity")
    service = _build_service(
        investments=[stale, fresh],
        navs_by_inv={
            stale.id: [
                _nav_dto(stale.id, date(2025, 3, 31), 100.0),
                _nav_dto(stale.id, date(2025, 6, 30), 110.0),
            ],
            fresh.id: [
                _nav_dto(fresh.id, date(2025, 6, 30), 200.0),
                _nav_dto(fresh.id, date(2025, 9, 30), 210.0),
            ],
        },
    )

    assert await service.get_universe_axis_end() == date(2025, 9, 30)


async def test_universe_axis_end_ignores_plan_navs() -> None:
    """Plan rows project beyond the frontier; the as-of is what is observed."""
    inv = _investment_dto("Private Fund", "private_equity")
    service = _build_service(
        investments=[inv],
        navs_by_inv={
            inv.id: [
                _nav_dto(inv.id, date(2025, 6, 30), 100.0),
                _nav_dto(inv.id, date(2026, 12, 31), 150.0, kind="plan"),
            ]
        },
    )

    assert await service.get_universe_axis_end() == date(2025, 6, 30)


async def test_universe_axis_end_is_none_for_an_empty_universe() -> None:
    service = _build_service(investments=[])
    assert await service.get_universe_axis_end() is None


async def test_universe_axis_end_is_none_without_any_actual_nav() -> None:
    """No fabricated date — the tiles fall back to their own auto-range."""
    inv = _investment_dto("Fresh Fund", "private_equity")
    service = _build_service(investments=[inv], navs_by_inv={inv.id: []})
    assert await service.get_universe_axis_end() is None


# ---------------------------------------------------------------------------
# Capital-Account plan series (ADR-0113 §2)
# ---------------------------------------------------------------------------


async def test_capital_account_carries_the_full_plan_nav_series() -> None:
    """The whole plan series travels; the display window is the spec's job."""
    inv = _investment_dto("PE Fund", "private_equity", commitment_amount=Decimal("200"))
    service = _build_service(
        investments=[inv],
        charts_bundle_by_inv={inv.id: _capital_account_bundle("PE Fund")},
        navs_by_inv={
            inv.id: [
                _nav_dto(inv.id, date(2025, 6, 30), 100.0),
                _nav_dto(inv.id, date(2025, 12, 31), 130.0, kind="plan"),
                _nav_dto(inv.id, date(2025, 9, 30), 120.0, kind="plan"),
                _nav_dto(inv.id, date(2026, 12, 31), 180.0, kind="plan"),
            ]
        },
    )

    result = await service.get_archetype_charts_data(inv.id)

    assert result is not None
    assert result.capital_account is not None
    nav_plan = result.capital_account.nav_plan
    # Sorted ascending, no windowing and no anchor applied here.
    assert list(nav_plan.index) == [date(2025, 9, 30), date(2025, 12, 31), date(2026, 12, 31)]
    assert list(nav_plan.to_numpy()) == [120.0, 130.0, 180.0]


async def test_capital_account_plan_series_is_empty_without_plan_rows() -> None:
    """The empty-Series sentinel — the tile then shows the honest gap."""
    inv = _investment_dto("PE Fund", "private_equity")
    service = _build_service(
        investments=[inv],
        charts_bundle_by_inv={inv.id: _capital_account_bundle("PE Fund")},
        navs_by_inv={inv.id: [_nav_dto(inv.id, date(2025, 6, 30), 100.0)]},
    )

    result = await service.get_archetype_charts_data(inv.id)

    assert result is not None
    assert result.capital_account is not None
    assert result.capital_account.nav_plan.empty
