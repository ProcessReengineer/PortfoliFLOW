# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Data-transfer objects for the Front-Office archetype-charts assembly.

The :class:`~services.front_office_charts.archetype_charts_service.ArchetypeChartsService`
returns **pure data** — pandas Series / DataFrames, plain ``dict``
mappings, and the frozen KPI dataclasses below — and never a Plotly
spec. The route (ADR-0082 §6, a separate step) builds the specs from
this data and dispatches on :attr:`ArchetypeChartsResult.archetype`.

One :class:`ArchetypeChartsResult` carries the resolved archetype, the
investment's display name, and exactly **one** populated tile bundle;
the other three bundle fields are ``None``. Per the no-silent-fallback
discipline (ADR-0082) every missing input surfaces as an explicit
sentinel — an empty Series, an empty mapping, ``None``, or ``nan`` — so
the absence is visible rather than masked by a fabricated value.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.repositories.investment_nav_repository import InvestmentNavDTO
from core.repositories.investment_repository import InvestmentDTO
from services.analytics._dtos import NotchWeightedRating, TrailingReturns
from services.investments.archetype import Archetype
from services.investments.investment_service import InvestmentChartsBundle


# ---------------------------------------------------------------------------
# KPI dataclasses (the per-archetype caption payloads)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapitalAccountKPI:
    """KPI caption for the Capital-Account archetype (ADR-0082 §2).

    Attributes:
        tvpi: Total Value to Paid-In, from the last valid
            ``rolling_multiples`` observation. ``None`` when no
            observation has a defined multiple (no calls to date).
        dpi: Distributions to Paid-In, same source. ``None`` likewise.
        net_irr: Net IRR since inception, from the last valid
            ``rolling_irr`` observation. ``None`` when none converged.
        unfunded_commitment: ``commitment_amount − total_called``.
            ``None`` when the investment carries no commitment amount.
    """

    tvpi: float | None
    dpi: float | None
    net_irr: float | None
    unfunded_commitment: float | None


@dataclass(frozen=True)
class EquityKPI:
    """KPI caption for the Total-Return-Equity archetype (ADR-0082 §2).

    Attributes:
        trailing: Trailing-TWR bundle (1M/3M/YTD/1Y/3Y/SI) over the
            total-return index.
        vol_12m: Rolling 12-month annualised volatility at the latest
            observation; ``nan`` when the monthly series is empty or
            shorter than the window.
        sharpe_12m: Rolling 12-month annualised Sharpe at the latest
            observation; ``nan`` likewise.
        beta: Benchmark-relative beta; ``None`` when the asset class has
            no benchmark mapping.
        tracking_error: Annualised tracking error; ``None`` when
            unmapped.
        information_ratio: Information ratio; ``None`` when unmapped.
        dividend_yield_ttm: Trailing-twelve-month dividends over the
            latest actual NAV; ``None`` when there is no actual NAV to
            divide by.
    """

    trailing: TrailingReturns
    vol_12m: float | None
    sharpe_12m: float | None
    beta: float | None
    tracking_error: float | None
    information_ratio: float | None
    dividend_yield_ttm: float | None


@dataclass(frozen=True)
class FixedIncomeKPI:
    """KPI caption for the Fixed-Income archetype (ADR-0082 §2).

    Attributes:
        twr: Annualised since-inception time-weighted return over the
            total-return index; ``None`` when the series is empty.
        ytm: Yield to maturity at the latest bond-analytics row;
            ``None`` when there are no bond-analytics rows.
        eff_duration: Effective duration at the latest row; ``None``
            likewise.
        oas: Option-adjusted spread at the latest row; ``None`` when
            absent (government bonds carry no spread) or no rows.
        avg_rating: Notch-weighted average credit rating over the
            latest rating-weight snapshot.
    """

    twr: float | None
    ytm: float | None
    eff_duration: float | None
    oas: float | None
    avg_rating: NotchWeightedRating


# ---------------------------------------------------------------------------
# Tile bundles (one populated per result)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapitalAccountTiles:
    """Capital-Account tile inputs (ADR-0082 §2).

    The three existing private-markets specs are unchanged; the route
    feeds them from ``charts`` directly.

    Attributes:
        charts: The existing :class:`InvestmentChartsBundle` (Total
            Return · Cashflows & NAV · TVPI/DPI/RVPI).
        nav_plan: The investment's full ``'plan'`` NAV series,
            ``as_of_date``-indexed with float values, from which the
            Cashflows & NAV tile draws the ADR-0113 §2 plan tail. An
            **empty Series** is the sentinel for an investment with no
            plan rows — the tile then shows its NAV line ending before
            the unified axis end, the honest gap rather than a
            fabricated continuation. Carried here rather than on
            ``charts`` because the tail is a Front-Office display
            concern: no plan value reaches
            :class:`~services.investments.investment_service.InvestmentService`
            or any analytics input (ADR-0113 §3).
        kpi: The Capital-Account KPI caption payload.
    """

    charts: InvestmentChartsBundle
    nav_plan: pd.Series
    kpi: CapitalAccountKPI


@dataclass(frozen=True)
class TotalReturnEquityTiles:
    """Total-Return-Equity tile inputs (ADR-0082 §2).

    Attributes:
        benchmark_display_name: Mapped benchmark label, or ``None`` when
            the asset class has no benchmark mapping (the hero then
            draws the investment line alone).
        investment_cumulative: Cumulative-return series of the
            investment (income-aware TWR), month-end indexed. Starts at
            the first month common to fund and benchmark and runs to the
            investment's own last month (ADR-0113 §5).
        benchmark_cumulative: Cumulative-return series of the mapped
            benchmark, from that same common first month to the
            benchmark's own last month; empty when unmapped.
        excess_cumulative: ``investment_cumulative −
            benchmark_cumulative`` over the months both series cover —
            the two lines' ends are free, the excess is not; empty when
            unmapped.
        underwater_series: Drawdown-level profile of the monthly TWR.
        sector_weights: ``{sector_display_name: weight_pct}`` from the
            latest sector snapshot; empty when none.
        region_weights: ``{region_display_name: weight_pct}`` from the
            latest region snapshot; empty when none.
        kpi: The Total-Return-Equity KPI caption payload.
    """

    benchmark_display_name: str | None
    investment_cumulative: pd.Series
    benchmark_cumulative: pd.Series
    excess_cumulative: pd.Series
    underwater_series: pd.Series
    sector_weights: dict[str, float]
    region_weights: dict[str, float]
    kpi: EquityKPI


@dataclass(frozen=True)
class FixedIncomeTiles:
    """Fixed-Income tile inputs (ADR-0082 §2).

    Attributes:
        benchmark_display_name: Mapped benchmark label, or ``None`` when
            unmapped.
        investment_cumulative: Cumulative-return series of the
            investment (income-aware TWR), month-end indexed. Starts at
            the first month common to fund and benchmark and runs to the
            investment's own last month (ADR-0113 §5).
        benchmark_cumulative: Cumulative-return series of the mapped
            benchmark, from that same common first month to the
            benchmark's own last month; empty when unmapped.
        excess_cumulative: ``investment_cumulative −
            benchmark_cumulative`` over the months both series cover —
            the two lines' ends are free, the excess is not; empty when
            unmapped.
        bond_analytics: Date-indexed DataFrame with float columns
            ``ytm``, ``oas`` (nullable), ``eff_duration``; empty when no
            rows.
        rating_weights: ``{rating_bucket: weight_pct}`` from the latest
            rating snapshot; empty when none.
        maturity_weights: ``{maturity_bucket: weight_pct}`` from the
            latest maturity snapshot; empty when none.
        kpi: The Fixed-Income KPI caption payload.
    """

    benchmark_display_name: str | None
    investment_cumulative: pd.Series
    benchmark_cumulative: pd.Series
    excess_cumulative: pd.Series
    bond_analytics: pd.DataFrame
    rating_weights: dict[str, float]
    maturity_weights: dict[str, float]
    kpi: FixedIncomeKPI


@dataclass(frozen=True)
class NavOnlyTiles:
    """NAV-only tile inputs (ADR-0082 §2) — the minimal fallback.

    Attributes:
        investment: The investment DTO (title + currency for the spec).
        navs: All NAV rows (actual + plan) for
            :func:`services.chart_specs.investment_nav_timeseries.build_nav_timeseries_spec`.
    """

    investment: InvestmentDTO
    navs: list[InvestmentNavDTO]


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchetypeChartsResult:
    """One investment's resolved archetype and its single tile bundle.

    Exactly one of the four bundle fields is populated; the route
    dispatches on :attr:`archetype` and reads the matching bundle.

    Attributes:
        archetype: The resolved presentation archetype.
        investment_name: Display name of the investment.
        capital_account: Populated iff ``archetype`` is
            :attr:`~services.investments.archetype.Archetype.CAPITAL_ACCOUNT`.
        total_return_equity: Populated iff ``archetype`` is
            :attr:`~services.investments.archetype.Archetype.TOTAL_RETURN_EQUITY`.
        fixed_income: Populated iff ``archetype`` is
            :attr:`~services.investments.archetype.Archetype.FIXED_INCOME`.
        nav_only: Populated iff ``archetype`` is
            :attr:`~services.investments.archetype.Archetype.NAV_ONLY`.
    """

    archetype: Archetype
    investment_name: str
    capital_account: CapitalAccountTiles | None = None
    total_return_equity: TotalReturnEquityTiles | None = None
    fixed_income: FixedIncomeTiles | None = None
    nav_only: NavOnlyTiles | None = None


__all__ = [
    "ArchetypeChartsResult",
    "CapitalAccountKPI",
    "CapitalAccountTiles",
    "EquityKPI",
    "FixedIncomeKPI",
    "FixedIncomeTiles",
    "NavOnlyTiles",
    "TotalReturnEquityTiles",
]
