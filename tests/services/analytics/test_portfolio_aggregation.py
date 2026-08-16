# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.analytics.portfolio_aggregation``.

Pure-function aggregations — no DB, no fixtures from the live
compose Postgres. The tests construct synthetic NAV / cashflow /
weight inputs and verify the per-year roll-ups, breakdown rows, and
edge cases (empty inputs, single-investment, NaN at zero capital).
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pandas as pd
import pytest

from core.repositories.investment_region_weights_repository import (
    RegionWeightDTO,
)
from core.repositories.investment_repository import InvestmentDTO
from core.repositories.investment_sector_weights_repository import (
    SectorWeightDTO,
)
from core.repositories.region_repository import RegionDTO
from core.repositories.sector_repository import SectorDTO
from services.analytics.portfolio_aggregation import (
    ConcentrationStats,
    FundCompositionBreakdown,
    FundCompositionRow,
    aggregate_currency_exposure,
    aggregate_fund_composition,
    aggregate_invested_capital_and_nav,
    aggregate_portfolio_cashflows,
    aggregate_portfolio_multiples,
    aggregate_region_breakdown,
    aggregate_sector_breakdown,
    aggregate_vintage_distribution,
    compute_concentration,
    compute_total_return_index_series,
    group_fund_composition,
)


# ---------------------------------------------------------------------------
# Fixtures (plain helpers — not pytest fixtures, faster + clearer).
# ---------------------------------------------------------------------------


def _make_investment(
    *,
    name: str = "Fund",
    vintage_year: int | None = 2020,
    currency: str = "EUR",
    investment_type: str = "private_equity",
) -> InvestmentDTO:
    """Build an :class:`InvestmentDTO` with sensible defaults."""
    now = pd.Timestamp("2024-01-01", tz="UTC").to_pydatetime()
    return InvestmentDTO(
        id=uuid4(),
        tenant_id=uuid4(),
        name=name,
        investment_type=investment_type,
        asset_class_id=uuid4(),
        manager_name=None,
        region=None,
        currency=currency,
        vintage_year=vintage_year,
        commitment_amount=None,
        is_active=True,
        type_specific_data=None,
        created_by=uuid4(),
        created_at=now,
        updated_at=now,
    )


def _make_region_weight(investment_id: UUID, region_id: UUID, weight_pct: float) -> RegionWeightDTO:
    now = pd.Timestamp("2024-01-01", tz="UTC").to_pydatetime()
    # ADR-0080: composition DTOs are snapshot-aware (as_of_date + basis).
    # A single canonical snapshot date keeps the breakdown numbers
    # bit-for-bit identical to the pre-historisation results — the
    # aggregations ignore both new fields (ADR-0080 Test 5).
    return RegionWeightDTO(
        id=uuid4(),
        tenant_id=uuid4(),
        investment_id=investment_id,
        as_of_date=date(2024, 1, 1),
        region_id=region_id,
        weight_pct=Decimal(str(weight_pct)),
        basis="reported",
        created_by=uuid4(),
        created_at=now,
    )


def _make_region_dto(code: str, display_name: str) -> RegionDTO:
    now = pd.Timestamp("2024-01-01", tz="UTC").to_pydatetime()
    return RegionDTO(
        id=uuid4(),
        tenant_id=uuid4(),
        code=code,
        display_name=display_name,
        description=None,
        sort_order=0,
        created_at=now,
        updated_at=now,
    )


def _make_sector_weight(investment_id: UUID, sector_id: UUID, weight_pct: float) -> SectorWeightDTO:
    now = pd.Timestamp("2024-01-01", tz="UTC").to_pydatetime()
    # ADR-0080: snapshot-aware DTO — see ``_make_region_weight``.
    return SectorWeightDTO(
        id=uuid4(),
        tenant_id=uuid4(),
        investment_id=investment_id,
        as_of_date=date(2024, 1, 1),
        sector_id=sector_id,
        weight_pct=Decimal(str(weight_pct)),
        basis="reported",
        created_by=uuid4(),
        created_at=now,
        updated_at=now,
    )


def _make_sector_dto(name: str, code: str) -> SectorDTO:
    now = pd.Timestamp("2024-01-01", tz="UTC").to_pydatetime()
    return SectorDTO(
        id=uuid4(),
        tenant_id=uuid4(),
        code=code,
        display_name=name,
        is_active=True,
        created_by=uuid4(),
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# aggregate_invested_capital_and_nav
# ---------------------------------------------------------------------------


def test_invested_capital_grows_with_calls() -> None:
    inv = _make_investment()
    nav = pd.Series(
        [50.0, 100.0, 150.0],
        index=pd.to_datetime([date(2023, 12, 31), date(2024, 12, 31), date(2025, 12, 31)]),
    )
    cf = pd.DataFrame(
        {
            "flow_timestamp": pd.to_datetime([date(2023, 6, 1), date(2024, 3, 1)]),
            "amount": [-100.0, -50.0],
        }
    )
    series = aggregate_invested_capital_and_nav(
        [inv],
        {inv.id: nav},
        {inv.id: cf},
        report_date=date(2025, 12, 31),
    )
    assert series.years == [2023, 2024, 2025]
    assert series.invested_capital == [100.0, 150.0, 150.0]
    assert series.nav == [50.0, 100.0, 150.0]


def test_invested_capital_distributions_do_not_offset() -> None:
    """Cumulative-calls magnitude stays at the calls magnitude even when
    distributions appear — distributions don't ``un-invest`` capital."""
    inv = _make_investment()
    nav = pd.Series(
        [200.0],
        index=pd.to_datetime([date(2024, 12, 31)]),
    )
    cf = pd.DataFrame(
        {
            "flow_timestamp": pd.to_datetime([date(2024, 1, 1), date(2024, 6, 1)]),
            "amount": [-150.0, 30.0],
        }
    )
    series = aggregate_invested_capital_and_nav(
        [inv],
        {inv.id: nav},
        {inv.id: cf},
        report_date=date(2024, 12, 31),
    )
    assert series.invested_capital == [150.0]


def test_invested_capital_empty_inputs() -> None:
    inv = _make_investment()
    series = aggregate_invested_capital_and_nav(
        [inv], {inv.id: pd.Series(dtype="float64")}, {inv.id: pd.DataFrame()}
    )
    assert series.years == []
    assert series.invested_capital == []
    assert series.nav == []


# ---------------------------------------------------------------------------
# aggregate_portfolio_cashflows
# ---------------------------------------------------------------------------


def test_yearly_cashflow_buckets() -> None:
    inv = _make_investment()
    nav = pd.Series(
        [50.0, 100.0],
        index=pd.to_datetime([date(2023, 12, 31), date(2024, 12, 31)]),
    )
    cf = pd.DataFrame(
        {
            "flow_timestamp": pd.to_datetime(
                [
                    date(2023, 3, 1),
                    date(2023, 9, 1),
                    date(2024, 6, 1),
                ]
            ),
            "amount": [-50.0, -50.0, 30.0],
        }
    )
    series = aggregate_portfolio_cashflows(
        {inv.id: cf}, {inv.id: nav}, report_date=date(2024, 12, 31)
    )
    assert series.years == [2023, 2024]
    assert series.calls == [-100.0, 0.0]
    assert series.distributions == [0.0, 30.0]
    assert series.nav == [50.0, 100.0]
    # NCG_2023 = 50 + 0 - 100 = -50
    # NCG_2024 = 100 + 30 - 100 = 30
    assert series.ncg == [-50.0, 30.0]


def test_yearly_cashflow_empty() -> None:
    series = aggregate_portfolio_cashflows({}, {})
    assert series.years == []
    assert series.calls == []


# ---------------------------------------------------------------------------
# aggregate_portfolio_multiples
# ---------------------------------------------------------------------------


def test_portfolio_multiples_basic_shape() -> None:
    inv = _make_investment()
    nav = pd.Series(
        [120.0, 150.0],
        index=pd.to_datetime([date(2023, 12, 31), date(2024, 12, 31)]),
    )
    cf = pd.DataFrame(
        {
            "flow_timestamp": pd.to_datetime([date(2023, 1, 1), date(2024, 6, 1)]),
            "amount": [-100.0, 20.0],
        }
    )
    series = aggregate_portfolio_multiples(
        {inv.id: cf}, {inv.id: nav}, report_date=date(2024, 12, 31)
    )
    assert series.years == [2023, 2024]
    # Year 2023: calls_mag=100, dist=0, nav=120 → DPI=0, RVPI=1.20, TVPI=1.20.
    assert series.dpi[0] == pytest.approx(0.0)
    assert series.rvpi[0] == pytest.approx(1.20)
    assert series.tvpi[0] == pytest.approx(1.20)
    # Year 2024: calls_mag=100, dist=20, nav=150 → DPI=0.20, RVPI=1.50, TVPI=1.70.
    assert series.dpi[1] == pytest.approx(0.20)
    assert series.rvpi[1] == pytest.approx(1.50)
    assert series.tvpi[1] == pytest.approx(1.70)


def test_portfolio_multiples_no_calls_yields_nan() -> None:
    inv = _make_investment()
    nav = pd.Series(
        [100.0],
        index=pd.to_datetime([date(2024, 12, 31)]),
    )
    cf = pd.DataFrame(
        {
            "flow_timestamp": pd.to_datetime([date(2024, 6, 1)]),
            "amount": [50.0],  # only a distribution, no call
        }
    )
    series = aggregate_portfolio_multiples(
        {inv.id: cf}, {inv.id: nav}, report_date=date(2024, 12, 31)
    )
    assert series.years == [2024]
    assert math.isnan(series.dpi[0])
    assert math.isnan(series.rvpi[0])
    assert math.isnan(series.tvpi[0])
    assert math.isnan(series.irr[0])


# ---------------------------------------------------------------------------
# aggregate_region_breakdown
# ---------------------------------------------------------------------------


def test_region_breakdown_nav_weighted() -> None:
    inv1 = _make_investment(name="A")
    inv2 = _make_investment(name="B")
    region_dach = _make_region_dto("dach", "DACH")
    region_usa = _make_region_dto("north_america_usa", "North America — USA")
    regions = {region_dach.id: region_dach, region_usa.id: region_usa}
    weights = {
        inv1.id: [
            _make_region_weight(inv1.id, region_dach.id, 100.0),
        ],
        inv2.id: [
            _make_region_weight(inv2.id, region_usa.id, 100.0),
        ],
    }
    nav_by_inv = {inv1.id: 100.0, inv2.id: 300.0}
    breakdown = aggregate_region_breakdown([inv1, inv2], weights, nav_by_inv, regions)
    assert len(breakdown.rows) == 2
    by_code = {r.region_code: r for r in breakdown.rows}
    assert by_code["dach"].weight_pct == pytest.approx(25.0)
    assert by_code["north_america_usa"].weight_pct == pytest.approx(75.0)
    assert by_code["dach"].region_display_name == "DACH"
    assert by_code["north_america_usa"].region_display_name == "North America — USA"
    # Sorted descending by weight_pct.
    assert breakdown.rows[0].region_code == "north_america_usa"


def test_region_breakdown_split_within_one_investment() -> None:
    inv = _make_investment()
    region_dach = _make_region_dto("dach", "DACH")
    region_uk = _make_region_dto("uk_ireland", "UK & Ireland")
    regions = {region_dach.id: region_dach, region_uk.id: region_uk}
    weights = {
        inv.id: [
            _make_region_weight(inv.id, region_dach.id, 60.0),
            _make_region_weight(inv.id, region_uk.id, 40.0),
        ],
    }
    breakdown = aggregate_region_breakdown([inv], weights, {inv.id: 100.0}, regions)
    by_code = {r.region_code: r for r in breakdown.rows}
    assert by_code["dach"].weight_pct == pytest.approx(60.0)
    assert by_code["uk_ireland"].weight_pct == pytest.approx(40.0)


def test_region_breakdown_empty_when_no_nav() -> None:
    inv = _make_investment()
    region_dach = _make_region_dto("dach", "DACH")
    regions = {region_dach.id: region_dach}
    weights = {inv.id: [_make_region_weight(inv.id, region_dach.id, 100.0)]}
    breakdown = aggregate_region_breakdown([inv], weights, {inv.id: 0.0}, regions)
    assert breakdown.rows == []


# ---------------------------------------------------------------------------
# aggregate_sector_breakdown
# ---------------------------------------------------------------------------


def test_sector_breakdown_nav_weighted() -> None:
    inv1 = _make_investment(name="A")
    inv2 = _make_investment(name="B")
    tech = _make_sector_dto("Technology", "tech")
    health = _make_sector_dto("Healthcare", "health")
    sectors = {tech.id: tech, health.id: health}
    weights = {
        inv1.id: [_make_sector_weight(inv1.id, tech.id, 100.0)],
        inv2.id: [_make_sector_weight(inv2.id, health.id, 100.0)],
    }
    breakdown = aggregate_sector_breakdown(
        [inv1, inv2],
        weights,
        {inv1.id: 200.0, inv2.id: 100.0},
        sectors,
    )
    by_code = {r.sector_code: r for r in breakdown.rows}
    assert by_code["tech"].weight_pct == pytest.approx(200.0 / 300.0 * 100.0)
    assert by_code["health"].weight_pct == pytest.approx(100.0 / 300.0 * 100.0)


# ---------------------------------------------------------------------------
# aggregate_vintage_distribution
# ---------------------------------------------------------------------------


def test_vintage_distribution_nav_weighted() -> None:
    inv1 = _make_investment(name="A", vintage_year=2018)
    inv2 = _make_investment(name="B", vintage_year=2018)
    inv3 = _make_investment(name="C", vintage_year=2020)
    nav_by_inv = {inv1.id: 100.0, inv2.id: 100.0, inv3.id: 200.0}
    dist = aggregate_vintage_distribution([inv1, inv2, inv3], nav_by_inv)
    assert dist.vintages == [2018, 2020]
    # 2018 = 200/400 = 50%; 2020 = 200/400 = 50%.
    assert dist.weight_pct == pytest.approx([50.0, 50.0])
    assert dist.count == [2, 1]


def test_vintage_distribution_skips_no_vintage() -> None:
    inv1 = _make_investment(name="A", vintage_year=None)
    inv2 = _make_investment(name="B", vintage_year=2020)
    dist = aggregate_vintage_distribution([inv1, inv2], {inv1.id: 100.0, inv2.id: 100.0})
    assert dist.vintages == [2020]
    assert dist.weight_pct == [100.0]


# ---------------------------------------------------------------------------
# aggregate_fund_composition
# ---------------------------------------------------------------------------


def _empty_cf_series() -> pd.Series:
    """Return a UTC-indexed empty cashflow series (the IRR helper shape)."""
    return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))


def test_fund_composition_sorted_and_weighted() -> None:
    inv_a = _make_investment(name="A")
    inv_b = _make_investment(name="B")
    inv_c = _make_investment(name="C")
    nav_by_inv = {inv_a.id: 100.0, inv_b.id: 300.0, inv_c.id: 100.0}
    breakdown = aggregate_fund_composition(
        [inv_a, inv_b, inv_c], nav_by_inv, {}, {}, date(2025, 12, 31)
    )
    # Sorted by NAV descending: B (300), then A / C (100 each).
    assert breakdown.rows[0].name == "B"
    assert [r.nav_eur for r in breakdown.rows] == [300.0, 100.0, 100.0]
    # weight_pct sums to ~100.
    assert sum(r.weight_pct for r in breakdown.rows) == pytest.approx(100.0)
    # Largest fund carries 300/500 = 60%.
    assert breakdown.rows[0].weight_pct == pytest.approx(60.0)
    # With no cashflows supplied every row's IRR is None.
    assert all(r.irr is None for r in breakdown.rows)


def test_fund_composition_cumulative_monotonic_to_100() -> None:
    inv_a = _make_investment(name="A")
    inv_b = _make_investment(name="B")
    inv_c = _make_investment(name="C")
    breakdown = aggregate_fund_composition(
        [inv_a, inv_b, inv_c],
        {inv_a.id: 50.0, inv_b.id: 200.0, inv_c.id: 250.0},
        {},
        {},
        date(2025, 12, 31),
    )
    cumulative = [r.cumulative_pct for r in breakdown.rows]
    # Monotonically non-decreasing.
    assert all(cumulative[i] <= cumulative[i + 1] + 1e-9 for i in range(len(cumulative) - 1))
    # Last row reaches ~100.
    assert cumulative[-1] == pytest.approx(100.0)


def test_fund_composition_irr_finite_for_converging_stream() -> None:
    """A single fund with a real call/NAV stream gets a finite IRR.

    Call of 100 at 2024-01-01 and a terminal NAV of 130 one year later
    → IRR ≈ 30 %.
    """
    inv = _make_investment(name="A")
    call_idx = pd.DatetimeIndex([pd.Timestamp("2024-01-01", tz="UTC")])
    cf_out = pd.Series([-100.0], index=call_idx)
    cf_in = _empty_cf_series()
    breakdown = aggregate_fund_composition(
        [inv],
        {inv.id: 130.0},
        {inv.id: cf_in},
        {inv.id: cf_out},
        date(2025, 1, 1),
    )
    irr = breakdown.rows[0].irr
    assert irr is not None
    assert math.isfinite(irr)
    assert irr == pytest.approx(0.30, abs=1e-3)


def test_fund_composition_skips_non_positive_and_missing_nav() -> None:
    inv_a = _make_investment(name="A")
    inv_b = _make_investment(name="B")  # zero NAV → skipped
    inv_c = _make_investment(name="C")  # missing from dict → skipped
    breakdown = aggregate_fund_composition(
        [inv_a, inv_b, inv_c],
        {inv_a.id: 100.0, inv_b.id: 0.0},
        {},
        {},
        date(2025, 12, 31),
    )
    assert [r.name for r in breakdown.rows] == ["A"]
    assert breakdown.rows[0].weight_pct == pytest.approx(100.0)


def test_fund_composition_empty_investments() -> None:
    breakdown = aggregate_fund_composition([], {}, {}, {}, date(2025, 12, 31))
    assert isinstance(breakdown, FundCompositionBreakdown)
    assert breakdown.rows == []


def test_fund_composition_all_zero_nav() -> None:
    inv_a = _make_investment(name="A")
    inv_b = _make_investment(name="B")
    breakdown = aggregate_fund_composition(
        [inv_a, inv_b],
        {inv_a.id: 0.0, inv_b.id: -5.0},
        {},
        {},
        date(2025, 12, 31),
    )
    assert breakdown.rows == []


# ---------------------------------------------------------------------------
# group_fund_composition
# ---------------------------------------------------------------------------


def _comp_row(name: str, nav: float, weight: float, irr: float | None) -> FundCompositionRow:
    """Build a composition row with a placeholder cumulative share."""
    return FundCompositionRow(
        investment_id=uuid4(),
        name=name,
        nav_eur=nav,
        weight_pct=weight,
        cumulative_pct=0.0,
        irr=irr,
    )


def test_group_fund_composition_identity_when_within_top_n() -> None:
    breakdown = FundCompositionBreakdown(
        rows=[
            _comp_row("A", 60.0, 60.0, 0.10),
            _comp_row("B", 40.0, 40.0, 0.05),
        ]
    )
    grouped = group_fund_composition(breakdown, top_n=10)
    assert grouped is breakdown


def test_group_fund_composition_folds_tail_into_other() -> None:
    rows = [
        _comp_row("A", 50.0, 50.0, 0.20),
        _comp_row("B", 20.0, 20.0, 0.10),
        _comp_row("C", 15.0, 15.0, 0.05),
        _comp_row("D", 15.0, 15.0, 0.00),
    ]
    grouped = group_fund_composition(FundCompositionBreakdown(rows=rows), top_n=2)
    # 2 head rows + 1 "Other".
    assert len(grouped.rows) == 3
    other = grouped.rows[-1]
    assert other.name == "Other (2 funds)"
    assert other.investment_id is None
    # Tail sums: nav 30, weight 30.
    assert other.nav_eur == pytest.approx(30.0)
    assert other.weight_pct == pytest.approx(30.0)
    # Cumulative recomputed to ~100 on the last row.
    assert grouped.rows[-1].cumulative_pct == pytest.approx(100.0)


def test_group_fund_composition_other_irr_is_nav_weighted() -> None:
    rows = [
        _comp_row("A", 50.0, 50.0, 0.20),
        _comp_row("B", 30.0, 30.0, 0.10),  # tail
        _comp_row("C", 10.0, 10.0, 0.40),  # tail
    ]
    grouped = group_fund_composition(FundCompositionBreakdown(rows=rows), top_n=1)
    other = grouped.rows[-1]
    # (30*0.10 + 10*0.40) / (30 + 10) = (3 + 4) / 40 = 0.175.
    assert other.irr == pytest.approx(0.175)


def test_group_fund_composition_other_irr_none_when_no_tail_irrs() -> None:
    rows = [
        _comp_row("A", 50.0, 50.0, 0.20),
        _comp_row("B", 30.0, 30.0, None),  # tail, no IRR
        _comp_row("C", 10.0, 10.0, None),  # tail, no IRR
    ]
    grouped = group_fund_composition(FundCompositionBreakdown(rows=rows), top_n=1)
    other = grouped.rows[-1]
    assert other.irr is None


# ---------------------------------------------------------------------------
# compute_concentration
# ---------------------------------------------------------------------------


def test_concentration_known_distribution() -> None:
    """Hand-checkable 4-fund book: weights 40/30/20/10, NAV 400/300/200/100."""
    breakdown = FundCompositionBreakdown(
        rows=[
            _comp_row("A", 400.0, 40.0, 0.10),
            _comp_row("B", 300.0, 30.0, 0.05),
            _comp_row("C", 200.0, 20.0, 0.00),
            _comp_row("D", 100.0, 10.0, None),
        ]
    )
    stats = compute_concentration(breakdown)
    assert stats.fund_count == 4
    assert stats.top1_pct == pytest.approx(40.0)
    assert stats.top3_pct == pytest.approx(90.0)  # 40 + 30 + 20
    # K above the fund count → the full ~100 %.
    assert stats.top5_pct == pytest.approx(100.0)
    assert stats.top10_pct == pytest.approx(100.0)
    # HHI = 0.4^2 + 0.3^2 + 0.2^2 + 0.1^2 = 0.30.
    assert stats.hhi == pytest.approx(0.30)


def test_concentration_empty_breakdown() -> None:
    stats = compute_concentration(FundCompositionBreakdown(rows=[]))
    assert stats.fund_count == 0
    assert stats.top1_pct == 0.0
    assert stats.top3_pct == 0.0
    assert stats.top5_pct == 0.0
    assert stats.top10_pct == 0.0
    assert stats.hhi == 0.0


def test_concentration_equal_weight_hhi_is_reciprocal_n() -> None:
    """N equal-weight funds → HHI = 1/N."""
    n = 5
    breakdown = FundCompositionBreakdown(
        rows=[_comp_row(f"F{i}", 100.0, 100.0 / n, 0.10) for i in range(n)]
    )
    stats = compute_concentration(breakdown)
    assert stats.fund_count == n
    assert stats.hhi == pytest.approx(1.0 / n)


def test_concentration_returns_frozen_dataclass() -> None:
    stats = compute_concentration(
        FundCompositionBreakdown(rows=[_comp_row("A", 100.0, 100.0, 0.10)])
    )
    assert isinstance(stats, ConcentrationStats)
    assert stats.top1_pct == pytest.approx(100.0)
    assert stats.hhi == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# compute_total_return_index_series
# ---------------------------------------------------------------------------


def test_total_return_index_compounds_correctly() -> None:
    returns = pd.Series(
        [0.10, -0.05, 0.20],
        index=pd.to_datetime([date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1)]),
    )
    idx = compute_total_return_index_series(returns)
    # 100 * 1.10 = 110; 110 * 0.95 = 104.5; 104.5 * 1.20 = 125.4.
    assert idx.iloc[0] == pytest.approx(110.0)
    assert idx.iloc[1] == pytest.approx(104.5)
    assert idx.iloc[2] == pytest.approx(125.4)


def test_total_return_index_empty_input() -> None:
    idx = compute_total_return_index_series(pd.Series(dtype="float64"))
    assert idx.empty


# ---------------------------------------------------------------------------
# Currency exposure — ADR-0101 §1
# ---------------------------------------------------------------------------


def test_currency_exposure_groups_by_position_currency() -> None:
    """NAVs bucket by ``investment.currency``; shares sum to 100.

    Two USD funds (300 + 100, already converted into the functional
    currency) collapse into one USD bucket of 400 against a 600 EUR fund:
    600 / 1000 = 60 % EUR, 400 / 1000 = 40 % USD.
    """
    eur_fund = _make_investment(name="Euro Fund", currency="EUR")
    usd_big = _make_investment(name="Dollar Fund", currency="USD")
    usd_small = _make_investment(name="Dollar Co-Invest", currency="USD")

    exposure = aggregate_currency_exposure(
        [eur_fund, usd_big, usd_small],
        {eur_fund.id: 600.0, usd_big.id: 300.0, usd_small.id: 100.0},
    )

    assert exposure.currency_count == 2
    # Sorted by amount descending.
    assert [r.currency for r in exposure.rows] == ["EUR", "USD"]
    assert exposure.rows[0].amount == pytest.approx(600.0)
    assert exposure.rows[0].weight_pct == pytest.approx(60.0)
    # The two USD positions are one bucket.
    assert exposure.rows[1].amount == pytest.approx(400.0)
    assert exposure.rows[1].weight_pct == pytest.approx(40.0)
    assert sum(r.weight_pct for r in exposure.rows) == pytest.approx(100.0)


def test_currency_exposure_skips_non_positive_navs() -> None:
    """Zero / negative NAVs carry no exposure (the composition sibling's rule).

    A currency present *only* on a zero-NAV investment must not appear as an
    empty slice — and must not dilute the shares of the currencies that do.
    """
    eur_fund = _make_investment(name="Euro Fund", currency="EUR")
    empty_gbp = _make_investment(name="Sterling Shell", currency="GBP")
    negative_chf = _make_investment(name="Franc Writeoff", currency="CHF")

    exposure = aggregate_currency_exposure(
        [eur_fund, empty_gbp, negative_chf],
        {eur_fund.id: 500.0, empty_gbp.id: 0.0, negative_chf.id: -20.0},
    )

    assert [r.currency for r in exposure.rows] == ["EUR"]
    assert exposure.rows[0].weight_pct == pytest.approx(100.0)


def test_currency_exposure_single_currency_short_circuit() -> None:
    """One currency → one 100 % bucket (the route then hides the tile).

    ``currency_count == 1`` is exactly the condition the Overview route
    tests to suppress the donut (ADR-0101 §4) — a single-currency tenant
    must never reach a rendered tile.
    """
    a = _make_investment(name="Fund A", currency="EUR")
    b = _make_investment(name="Fund B", currency="EUR")

    exposure = aggregate_currency_exposure([a, b], {a.id: 700.0, b.id: 300.0})

    assert exposure.currency_count == 1
    assert exposure.rows[0].currency == "EUR"
    assert exposure.rows[0].amount == pytest.approx(1000.0)
    assert exposure.rows[0].weight_pct == pytest.approx(100.0)


def test_currency_exposure_empty_universe() -> None:
    assert aggregate_currency_exposure([], {}).rows == []
    lonely = _make_investment(currency="EUR")
    assert aggregate_currency_exposure([lonely], {}).rows == []


def test_currency_exposure_includes_cash_positions() -> None:
    """Cash is in the full universe — foreign-currency cash *is* FX exposure.

    An ADR-0100 cash row carries no cashflows, so it is absent from the
    performance universe; the exposure donut runs over the full one, and a
    USD cash balance must show up as USD exposure.
    """
    fund = _make_investment(name="Euro Fund", currency="EUR")
    cash = _make_investment(
        name="Cash USD",
        currency="USD",
        vintage_year=None,
        investment_type="cash",
    )

    exposure = aggregate_currency_exposure([fund, cash], {fund.id: 900.0, cash.id: 100.0})

    assert [r.currency for r in exposure.rows] == ["EUR", "USD"]
    assert exposure.rows[1].amount == pytest.approx(100.0)
    assert exposure.rows[1].weight_pct == pytest.approx(10.0)
