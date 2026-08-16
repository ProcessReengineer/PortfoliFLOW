# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Portfolio-level roll-ups — sub-stream 5e (ADR-0045 §3).

Pure-Python migration of the QT report-engine providers in
``services/reporting/data_providers/``. Functions take typed inputs
(per-investment NAV series, signed cashflow frames, country / sector
weight DTO lists) and return frozen dataclasses. None of the
functions reach into the database directly — that responsibility
lives on :class:`services.portfolio_review_service.PortfolioReviewService`.

Sign convention (mirrors the Phase-4 ``investment_cashflows`` table
and the QT chart code): ``amount`` is signed. Capital calls are
negative; distributions are positive.

Cumulative-calls magnitude (``invested_capital``) is the absolute
value of the cumulative *negative* portion of the cashflow stream up
to and including each year-end. NAV at year-end is the most recent
non-NaN NAV at or before that year-end, summed across investments
for the portfolio aggregate.

Yearly bars and multiples buckets each cashflow on the year of its
``flow_timestamp``; the ``[first activity year, report year]`` range
is dense (no skipped years) so the bar / multiples chart x-axis is
continuous.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from uuid import UUID

import numpy as np
import pandas as pd

from core.repositories.investment_region_weights_repository import (
    RegionWeightDTO,
)
from core.repositories.investment_repository import InvestmentDTO
from core.repositories.investment_sector_weights_repository import (
    SectorWeightDTO,
)
from core.repositories.region_repository import RegionDTO
from core.repositories.sector_repository import SectorDTO
from services.reporting.data_providers._calculations import compute_irr


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvestedCapitalNavSeries:
    """Year-end invested-capital and NAV totals.

    Attributes:
        years: Integer years covering the dense range
            ``[first activity, report year]``.
        invested_capital: Magnitude of cumulative capital calls
            (positive numbers, EUR) at each year-end. ``len`` matches
            ``years``.
        nav: Most-recent NAV at or before each year-end. ``len``
            matches ``years``.
    """

    years: list[int]
    invested_capital: list[float]
    nav: list[float]


@dataclass(frozen=True)
class PortfolioCashflowSeries:
    """Year-end portfolio (or single-investment) cashflow buckets.

    Attributes:
        years: Integer years.
        calls: Signed yearly capital calls (negative). ``len`` matches
            ``years``.
        distributions: Signed yearly distributions (positive). ``len``
            matches ``years``.
        nav: NAV line overlay (latest NAV at or before each year-end).
        ncg: Net Capital Gain line overlay
            (``nav + cum_distributions - cum_calls_magnitude``).
    """

    years: list[int]
    calls: list[float]
    distributions: list[float]
    nav: list[float]
    ncg: list[float]


@dataclass(frozen=True)
class PortfolioMultiplesSeries:
    """Year-end multiples and IRR-since-inception time series.

    Attributes:
        years: Integer years.
        dpi: Distributions / cumulative calls magnitude. NaN before
            any capital is called.
        rvpi: NAV / cumulative calls magnitude. NaN before any
            capital is called.
        tvpi: ``dpi + rvpi``. NaN before any capital is called.
        irr: IRR-since-inception evaluated at each year-end with the
            year-end NAV as the synthetic terminal cashflow. NaN when
            the root-finder cannot converge.
    """

    years: list[int]
    dpi: list[float]
    rvpi: list[float]
    tvpi: list[float]
    irr: list[float]


@dataclass(frozen=True)
class RegionBreakdownRow:
    """One row in a NAV-weighted region breakdown.

    Attributes:
        region_code: Tenant-scoped region code (e.g. ``"dach"``,
            ``"asia_emerging"``).
        region_display_name: Human-readable region label rendered in
            the treemap.
        nav_eur: NAV (EUR) attributable to this region. For the
            portfolio aggregate this is the sum of
            ``investment_nav * weight_pct/100`` across investments.
        weight_pct: Share of total NAV in this row (percent, ``0..100``).
    """

    region_code: str
    region_display_name: str
    nav_eur: float
    weight_pct: float


@dataclass(frozen=True)
class RegionBreakdown:
    """NAV-weighted region breakdown for a portfolio or one investment.

    Attributes:
        rows: Breakdown rows sorted by ``weight_pct`` descending.
            Empty when no investment has either a NAV or a region
            weight.
    """

    rows: list[RegionBreakdownRow]


@dataclass(frozen=True)
class SectorBreakdownRow:
    """One row in a NAV-weighted sector breakdown.

    Attributes:
        sector_code: Tenant-scoped sector code (e.g.
            ``"tech_software"``).
        sector_display_name: Human-readable sector label.
        nav_eur: NAV (EUR) attributable to this sector.
        weight_pct: Share of total NAV in this row (percent, ``0..100``).
    """

    sector_code: str
    sector_display_name: str
    nav_eur: float
    weight_pct: float


@dataclass(frozen=True)
class SectorBreakdown:
    """NAV-weighted sector breakdown.

    Attributes:
        rows: Breakdown rows sorted by ``weight_pct`` descending.
    """

    rows: list[SectorBreakdownRow]


@dataclass(frozen=True)
class VintageDistribution:
    """NAV-weighted distribution over investment vintage years.

    Attributes:
        vintages: Vintage years sorted ascending.
        weight_pct: NAV-weighted share of the portfolio for each
            vintage (percent, ``0..100``).
        count: Number of investments in each vintage. ``len`` matches
            ``vintages``.
    """

    vintages: list[int]
    weight_pct: list[float]
    count: list[int]


@dataclass(frozen=True)
class FundCompositionRow:
    """One fund's NAV contribution in the portfolio composition.

    Attributes:
        investment_id: The investment's UUID, or ``None`` for the
            synthetic ``"Other"`` aggregate row.
        name: Display name of the fund (or ``"Other (k funds)"``).
        nav_eur: Latest NAV attributable to this fund (EUR, > 0).
        weight_pct: Share of total portfolio NAV (percent, 0..100).
        cumulative_pct: Running cumulative share over the
            descending-by-NAV ordering (percent); the last row is
            ~100.0.
        irr: IRR-since-inception for this fund as a decimal, or
            ``None`` when the root-finder cannot converge. For the
            ``"Other"`` row this is the NAV-weighted average of the
            constituent funds' IRRs (a deliberate approximation,
            ADR-0072 §1.1).
    """

    investment_id: UUID | None
    name: str
    nav_eur: float
    weight_pct: float
    cumulative_pct: float
    irr: float | None


@dataclass(frozen=True)
class FundCompositionBreakdown:
    """NAV-weighted portfolio composition by individual fund.

    Attributes:
        rows: Rows sorted by ``nav_eur`` descending, each carrying its
            running ``cumulative_pct``. Empty when no fund has a
            positive NAV.
    """

    rows: list[FundCompositionRow]


@dataclass(frozen=True)
class CurrencyExposureRow:
    """One position currency's share of the portfolio's converted NAV.

    Attributes:
        currency: The ISO 4217 **position** currency the underlying
            investments are denominated in (``investment.currency``) — not
            the functional currency the amount is expressed in.
        amount: Latest NAV attributable to this currency, **in the tenant's
            functional currency** (the inputs are already converted at the
            ADR-0099 §4 boundary). Deliberately not named ``*_eur``: unlike
            its pre-multi-currency siblings this field is new, so it starts
            out with an honest name rather than inheriting the legacy one.
        weight_pct: Share of total converted NAV (percent, ``0..100``).
    """

    currency: str
    amount: float
    weight_pct: float


@dataclass(frozen=True)
class CurrencyExposure:
    """Unhedged notional NAV exposure by position currency (ADR-0101 §1).

    The supervisory default view: *what fraction of the book is denominated
    in what*. It is exposure by **denomination**, not economic exposure —
    look-through to a fund's underlying currencies is out of scope
    (ADR-0101 §Consequences), which is why the chart tile carries the
    "by position currency (unhedged)" subtitle.

    Attributes:
        rows: One row per position currency, sorted by ``amount``
            descending. ``weight_pct`` sums to ``100.0``. Empty when no
            investment carries a positive NAV.
    """

    rows: list[CurrencyExposureRow]

    @property
    def currency_count(self) -> int:
        """Number of distinct position currencies with a positive NAV.

        The Overview tile's rendering condition (ADR-0101 §1): a
        single-currency tenant (``<= 1``) shows no exposure tile at all.
        """
        return len(self.rows)


@dataclass(frozen=True)
class ConcentrationStats:
    """Portfolio NAV-concentration summary over the full fund set.

    Computed on the **full, ungrouped** :class:`FundCompositionBreakdown`
    (never on a :func:`group_fund_composition` result, whose single tail
    bucket would distort the HHI). Surfaced as the NAV-by-fund tile's
    concentration strip (ADR-0072 §1.3).

    Attributes:
        top1_pct: Cumulative NAV share (percent, ``0..100``) of the
            largest fund. Lands at ~100 when fewer funds exist.
        top3_pct: Cumulative NAV share of the largest 3 funds.
        top5_pct: Cumulative NAV share of the largest 5 funds.
        top10_pct: Cumulative NAV share of the largest 10 funds.
        hhi: Herfindahl–Hirschman index — the sum of squared NAV
            fractions (each ``0..1``) over **all** funds. Range
            ``(0..1]``; ``0.0`` for an empty portfolio.
        fund_count: Number of funds in the breakdown.
    """

    top1_pct: float
    top3_pct: float
    top5_pct: float
    top10_pct: float
    hhi: float
    fund_count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_nav_series(nav_series: pd.Series) -> pd.Series:
    """Return a NAV series sorted ascending with NaNs dropped."""
    if nav_series is None or nav_series.empty:
        return pd.Series(dtype="float64")
    cleaned = nav_series.dropna().sort_index()
    return cleaned


def _signed_cashflow_series(cashflows: pd.DataFrame) -> pd.Series:
    """Aggregate a flat actuals cashflow frame to a signed timestamp series.

    Args:
        cashflows: DataFrame with columns ``flow_timestamp`` and
            ``amount``. Caller has filtered to actuals.

    Returns:
        Pandas Series indexed by ``pd.Timestamp`` (UTC-normalised),
        values are the sum of signed amounts per timestamp. Empty
        when input is empty.
    """
    if cashflows is None or cashflows.empty:
        return pd.Series(dtype="float64")
    df = cashflows.copy()
    df["flow_timestamp"] = pd.to_datetime(df["flow_timestamp"], utc=True).dt.normalize()
    df["amount"] = df["amount"].astype("float64")
    return df.groupby("flow_timestamp")["amount"].sum().sort_index()


def _split_cashflows_for_irr(
    cashflows: pd.DataFrame,
) -> tuple[pd.Series, pd.Series]:
    """Split signed actuals cashflows into ``(cf_in, cf_out)`` series.

    Mirrors :func:`services.analytics.investment_returns._split_cashflows_for_irr`
    but works on the unioned frame for portfolio-level aggregation.
    """
    if cashflows is None or cashflows.empty:
        empty_idx = pd.DatetimeIndex([], tz="UTC")
        empty = pd.Series(dtype="float64", index=empty_idx)
        return empty, empty.copy()

    df = cashflows.copy()
    df["flow_timestamp"] = pd.to_datetime(df["flow_timestamp"], utc=True).dt.normalize()
    df["amount"] = df["amount"].astype("float64")
    in_mask = df["amount"] > 0.0
    out_mask = df["amount"] < 0.0
    cf_in = (
        df.loc[in_mask, ["flow_timestamp", "amount"]]
        .groupby("flow_timestamp")["amount"]
        .sum()
        .sort_index()
    )
    cf_out = (
        df.loc[out_mask, ["flow_timestamp", "amount"]]
        .groupby("flow_timestamp")["amount"]
        .sum()
        .sort_index()
    )
    return cf_in, cf_out


def _aggregate_cashflows(
    cashflows_by_investment: dict[UUID, pd.DataFrame],
) -> pd.DataFrame:
    """Concatenate per-investment cashflow frames into one signed frame.

    Args:
        cashflows_by_investment: Mapping ``investment_id -> DataFrame``.
            Each frame has columns ``flow_timestamp`` and ``amount``.

    Returns:
        One concatenated frame. Empty when the input is empty.
    """
    frames = [df for df in cashflows_by_investment.values() if df is not None and not df.empty]
    if not frames:
        return pd.DataFrame(columns=["flow_timestamp", "amount"])
    return pd.concat(frames, ignore_index=True)


def _portfolio_nav_series(
    nav_history_by_investment: dict[UUID, pd.Series],
) -> pd.Series:
    """Build the portfolio NAV series by summing per-investment NAVs.

    The QT provider semantics: at each date present in *any*
    investment's NAV history, the portfolio NAV is the sum of each
    investment's most-recent NAV at-or-before that date (forward-fill
    semantics). This avoids a missing NAV in one investment from
    zeroing out the portfolio total on cross-investment dates.
    """
    cleaned = {inv_id: _normalise_nav_series(s) for inv_id, s in nav_history_by_investment.items()}
    cleaned = {k: v for k, v in cleaned.items() if not v.empty}
    if not cleaned:
        return pd.Series(dtype="float64")

    all_dates = pd.DatetimeIndex([])
    for s in cleaned.values():
        all_dates = all_dates.union(pd.DatetimeIndex(s.index))
    all_dates = all_dates.sort_values()

    total = pd.Series(0.0, index=all_dates, dtype="float64")
    for s in cleaned.values():
        # Forward-fill each investment onto the union, but only after
        # its own first datapoint — investments that haven't started
        # contribute zero.
        s_idx = pd.Series(s.values, index=pd.DatetimeIndex(s.index))
        reindexed = s_idx.reindex(all_dates).ffill()
        # Zero out dates before the investment's first datapoint.
        first = s_idx.index.min()
        reindexed = reindexed.where(all_dates >= first, 0.0)
        total = total + reindexed.fillna(0.0)
    return total


def _latest_at_or_before(series: pd.Series, ts: pd.Timestamp) -> float:
    """Return ``series`` value at the latest index <= ``ts`` (or 0.0)."""
    if series.empty:
        return 0.0
    window = series.loc[series.index <= ts]
    if window.empty:
        return 0.0
    return float(window.iloc[-1])


def _activity_year_range(
    cashflows: pd.DataFrame,
    fallback_nav_series: pd.Series | None,
    report_year: int,
) -> list[int]:
    """Build the dense year range from first activity to the report year.

    First activity year is the earliest ``flow_timestamp.year`` across
    the cashflows. If the cashflow frame is empty, the fallback is
    the earliest NAV year (used in single-investment mode where an
    early-stage investment may carry a NAV before its first call).
    """
    first_year: int | None = None
    if cashflows is not None and not cashflows.empty:
        ts = pd.to_datetime(cashflows["flow_timestamp"], utc=True)
        first_year = int(ts.dt.year.min())
    if first_year is None and fallback_nav_series is not None:
        nav_clean = _normalise_nav_series(fallback_nav_series)
        if not nav_clean.empty:
            first_year = int(pd.Timestamp(nav_clean.index.min()).year)
    if first_year is None or report_year < first_year:
        return []
    return list(range(first_year, report_year + 1))


def _yearly_calls_distributions(
    cashflows: pd.DataFrame, years: list[int]
) -> tuple[list[float], list[float]]:
    """Bucket signed cashflows by ``flow_timestamp.year`` for each year."""
    calls = [0.0] * len(years)
    distributions = [0.0] * len(years)
    if cashflows is None or cashflows.empty or not years:
        return calls, distributions
    df = cashflows.copy()
    df["flow_timestamp"] = pd.to_datetime(df["flow_timestamp"], utc=True)
    df["amount"] = df["amount"].astype("float64")
    df["year"] = df["flow_timestamp"].dt.year
    by_year = df.groupby("year")["amount"]
    for i, year in enumerate(years):
        if year in by_year.groups:
            grp = by_year.get_group(year)
            calls[i] = float(grp[grp < 0.0].sum())
            distributions[i] = float(grp[grp > 0.0].sum())
    return calls, distributions


# ---------------------------------------------------------------------------
# Public API — six aggregations + one helper
# ---------------------------------------------------------------------------


def aggregate_invested_capital_and_nav(
    investments: list[InvestmentDTO],
    nav_history_by_investment: dict[UUID, pd.Series],
    cashflows_by_investment: dict[UUID, pd.DataFrame],
    *,
    report_date: date | None = None,
) -> InvestedCapitalNavSeries:
    """Year-end invested capital (cumulative calls magnitude) and NAV.

    Per-year invested capital is the magnitude of cumulative capital
    calls (``-amount`` summed for ``amount < 0``) up to and including
    each year-end. NAV at year-end is the cross-investment sum of
    each investment's latest NAV at-or-before that year-end (the QT
    convention from :class:`InvestedNavProvider`).

    Args:
        investments: Investments in scope. Used only to determine
            which UUIDs to include from the dicts.
        nav_history_by_investment: Mapping ``investment_id -> NAV
            series`` (date-indexed, plan / actual filtered).
        cashflows_by_investment: Mapping ``investment_id -> DataFrame``
            of signed actuals cashflows.
        report_date: As-of date. ``None`` resolves to the latest date
            present in either NAV or cashflow data. The result year
            range ends at ``report_date.year``.

    Returns:
        :class:`InvestedCapitalNavSeries` with per-year invested
        capital and NAV totals. ``years`` is empty when there is no
        activity.
    """
    inv_ids = {inv.id for inv in investments}
    nav_dict = {i: nav_history_by_investment.get(i, pd.Series(dtype="float64")) for i in inv_ids}
    cf_dict = {
        i: cashflows_by_investment.get(i, pd.DataFrame(columns=["flow_timestamp", "amount"]))
        for i in inv_ids
    }

    portfolio_cf = _aggregate_cashflows(cf_dict)
    portfolio_nav = _portfolio_nav_series(nav_dict)

    if report_date is None:
        candidates: list[pd.Timestamp] = []
        if not portfolio_nav.empty:
            candidates.append(pd.Timestamp(portfolio_nav.index.max()))
        if not portfolio_cf.empty:
            candidates.append(
                pd.Timestamp(pd.to_datetime(portfolio_cf["flow_timestamp"], utc=True).max())
            )
        if not candidates:
            return InvestedCapitalNavSeries([], [], [])
        resolved = max(candidates)
    else:
        resolved = pd.Timestamp(report_date, tz="UTC")
    report_year = int(resolved.year)

    years = _activity_year_range(portfolio_cf, portfolio_nav, report_year)
    if not years:
        return InvestedCapitalNavSeries([], [], [])

    cf_signed = _signed_cashflow_series(portfolio_cf)
    if not cf_signed.empty:
        # Magnitude of negative portion only — distributions don't
        # offset invested capital.
        calls_only = cf_signed.where(cf_signed < 0.0, 0.0).abs()
        cum_calls_mag = calls_only.cumsum()
    else:
        cum_calls_mag = pd.Series(dtype="float64")

    nav_clean = (
        pd.Series(
            portfolio_nav.values,
            index=pd.to_datetime(portfolio_nav.index, utc=True),
            dtype="float64",
        )
        if not portfolio_nav.empty
        else portfolio_nav
    )

    invested_capital: list[float] = []
    nav_values: list[float] = []
    for year in years:
        year_end = pd.Timestamp(year=year, month=12, day=31, tz="UTC")
        invested_capital.append(_latest_at_or_before(cum_calls_mag, year_end))
        nav_values.append(_latest_at_or_before(nav_clean, year_end))

    return InvestedCapitalNavSeries(
        years=years,
        invested_capital=invested_capital,
        nav=nav_values,
    )


def aggregate_portfolio_cashflows(
    cashflows_by_investment: dict[UUID, pd.DataFrame],
    nav_history_by_investment: dict[UUID, pd.Series],
    *,
    report_date: date | None = None,
) -> PortfolioCashflowSeries:
    """Per-year calls / distributions plus NAV and NCG line overlays.

    Yearly calls are the sum of negative cashflow amounts in each
    year (negative numbers); yearly distributions are the sum of
    positive amounts. NAV at year-end follows the
    :class:`CashflowWithNavProvider` convention (latest NAV at-or-
    before year-end). NCG = NAV + cumulative_distributions
    - cumulative_calls_magnitude.

    Args:
        cashflows_by_investment: Mapping ``investment_id -> signed
            actuals DataFrame``.
        nav_history_by_investment: Mapping ``investment_id -> NAV
            series``.
        report_date: As-of date. ``None`` resolves to the latest
            activity date.

    Returns:
        :class:`PortfolioCashflowSeries` with year-aligned lists.
    """
    portfolio_cf = _aggregate_cashflows(cashflows_by_investment)
    portfolio_nav = _portfolio_nav_series(nav_history_by_investment)

    if report_date is None:
        candidates: list[pd.Timestamp] = []
        if not portfolio_nav.empty:
            candidates.append(pd.Timestamp(portfolio_nav.index.max()))
        if not portfolio_cf.empty:
            candidates.append(
                pd.Timestamp(pd.to_datetime(portfolio_cf["flow_timestamp"], utc=True).max())
            )
        if not candidates:
            return PortfolioCashflowSeries([], [], [], [], [])
        resolved = max(candidates)
    else:
        resolved = pd.Timestamp(report_date, tz="UTC")
    report_year = int(resolved.year)

    years = _activity_year_range(portfolio_cf, portfolio_nav, report_year)
    if not years:
        return PortfolioCashflowSeries([], [], [], [], [])

    calls, distributions = _yearly_calls_distributions(portfolio_cf, years)

    nav_clean = (
        pd.Series(
            portfolio_nav.values,
            index=pd.to_datetime(portfolio_nav.index, utc=True),
            dtype="float64",
        )
        if not portfolio_nav.empty
        else portfolio_nav
    )
    navs = [
        _latest_at_or_before(nav_clean, pd.Timestamp(year=y, month=12, day=31, tz="UTC"))
        for y in years
    ]

    cum_calls_mag = np.cumsum([abs(c) for c in calls])
    cum_distributions = np.cumsum(distributions)
    ncg = [navs[i] + cum_distributions[i] - cum_calls_mag[i] for i in range(len(years))]

    return PortfolioCashflowSeries(
        years=years,
        calls=calls,
        distributions=distributions,
        nav=navs,
        ncg=ncg,
    )


def aggregate_portfolio_multiples(
    cashflows_by_investment: dict[UUID, pd.DataFrame],
    nav_history_by_investment: dict[UUID, pd.Series],
    *,
    report_date: date | None = None,
) -> PortfolioMultiplesSeries:
    """Per-year DPI / RVPI / TVPI plus IRR-since-inception.

    For each year-end ``t``::

        cum_calls_mag_t = magnitude of cumulative negative cashflows
        cum_distributions_t = cumulative positive cashflows
        nav_t = latest NAV at or before t

        dpi_t  = cum_distributions_t / cum_calls_mag_t   (NaN if zero)
        rvpi_t = nav_t / cum_calls_mag_t                 (NaN if zero)
        tvpi_t = dpi_t + rvpi_t

        irr_t  = IRR of all flows up to t with +nav_t at t.

    IRR uses the same Brent-method engine as the QT report.

    Args:
        cashflows_by_investment: Mapping ``investment_id -> signed
            actuals DataFrame``.
        nav_history_by_investment: Mapping ``investment_id -> NAV
            series``.
        report_date: As-of date.

    Returns:
        :class:`PortfolioMultiplesSeries`.
    """
    portfolio_cf = _aggregate_cashflows(cashflows_by_investment)
    portfolio_nav = _portfolio_nav_series(nav_history_by_investment)

    if report_date is None:
        candidates: list[pd.Timestamp] = []
        if not portfolio_nav.empty:
            candidates.append(pd.Timestamp(portfolio_nav.index.max()))
        if not portfolio_cf.empty:
            candidates.append(
                pd.Timestamp(pd.to_datetime(portfolio_cf["flow_timestamp"], utc=True).max())
            )
        if not candidates:
            return PortfolioMultiplesSeries([], [], [], [], [])
        resolved = max(candidates)
    else:
        resolved = pd.Timestamp(report_date, tz="UTC")
    report_year = int(resolved.year)

    years = _activity_year_range(portfolio_cf, portfolio_nav, report_year)
    if not years:
        return PortfolioMultiplesSeries([], [], [], [], [])

    cf_signed = _signed_cashflow_series(portfolio_cf)
    if not cf_signed.empty:
        calls_only = cf_signed.where(cf_signed < 0.0, 0.0).abs()
        dist_only = cf_signed.where(cf_signed > 0.0, 0.0)
        cum_calls_mag = calls_only.cumsum()
        cum_distributions = dist_only.cumsum()
    else:
        cum_calls_mag = pd.Series(dtype="float64")
        cum_distributions = pd.Series(dtype="float64")

    nav_clean = (
        pd.Series(
            portfolio_nav.values,
            index=pd.to_datetime(portfolio_nav.index, utc=True),
            dtype="float64",
        )
        if not portfolio_nav.empty
        else portfolio_nav
    )

    cf_in, cf_out = _split_cashflows_for_irr(portfolio_cf)

    dpi: list[float] = []
    rvpi: list[float] = []
    tvpi: list[float] = []
    irr: list[float] = []
    for year in years:
        year_end = pd.Timestamp(year=year, month=12, day=31, tz="UTC")
        calls_mag = _latest_at_or_before(cum_calls_mag, year_end)
        dist = _latest_at_or_before(cum_distributions, year_end)
        nav_v = _latest_at_or_before(nav_clean, year_end)
        if calls_mag <= 0.0:
            dpi.append(float("nan"))
            rvpi.append(float("nan"))
            tvpi.append(float("nan"))
            irr.append(float("nan"))
            continue
        d = dist / calls_mag
        r = nav_v / calls_mag
        dpi.append(d)
        rvpi.append(r)
        tvpi.append(d + r)
        irr.append(compute_irr(cf_in, cf_out, nav_v, year_end))

    return PortfolioMultiplesSeries(
        years=years,
        dpi=dpi,
        rvpi=rvpi,
        tvpi=tvpi,
        irr=irr,
    )


def aggregate_region_breakdown(
    investments: list[InvestmentDTO],
    region_weights_by_investment: dict[UUID, list[RegionWeightDTO]],
    nav_by_investment: dict[UUID, float],
    regions_by_id: dict[UUID, RegionDTO],
) -> RegionBreakdown:
    """Aggregate per-investment region weights into a portfolio breakdown.

    For each ``(investment, region)`` pair the contribution is::

        contribution = nav[investment] * weight_pct[investment, region] / 100

    The region share is the sum of contributions divided by the
    total NAV across investments that carry both a region weight
    and a positive NAV.

    Args:
        investments: Investments in scope. Empty list yields an
            empty breakdown.
        region_weights_by_investment: Per-investment list of region
            weights (matched by ``investment_id``). Investments
            without an entry are silently skipped.
        nav_by_investment: Latest NAV per investment (EUR).
            Investments not in the dict, or with non-positive NAV,
            are skipped.
        regions_by_id: Lookup ``region_id -> RegionDTO`` for
            display-name resolution. Missing entries render with the
            bare UUID string — that points operators at a stale
            cache rather than swallowing the data.

    Returns:
        :class:`RegionBreakdown` with rows sorted by ``weight_pct``
        descending.
    """
    if not investments:
        return RegionBreakdown(rows=[])

    contributions_by_region: dict[UUID, float] = {}
    total_nav = 0.0
    for inv in investments:
        weights = region_weights_by_investment.get(inv.id, [])
        if not weights:
            continue
        nav_v = nav_by_investment.get(inv.id, 0.0)
        if nav_v <= 0.0:
            continue
        total_nav += nav_v
        for w in weights:
            share = nav_v * float(w.weight_pct) / 100.0
            contributions_by_region[w.region_id] = (
                contributions_by_region.get(w.region_id, 0.0) + share
            )

    if total_nav <= 0.0 or not contributions_by_region:
        return RegionBreakdown(rows=[])

    rows: list[RegionBreakdownRow] = []
    for region_id, nav_eur in contributions_by_region.items():
        if nav_eur <= 0.0:
            continue
        region = regions_by_id.get(region_id)
        rows.append(
            RegionBreakdownRow(
                region_code=region.code if region is not None else "",
                region_display_name=(region.display_name if region is not None else str(region_id)),
                nav_eur=nav_eur,
                weight_pct=100.0 * nav_eur / total_nav,
            )
        )
    rows.sort(key=lambda r: r.weight_pct, reverse=True)
    return RegionBreakdown(rows=rows)


def aggregate_sector_breakdown(
    investments: list[InvestmentDTO],
    sector_weights_by_investment: dict[UUID, list[SectorWeightDTO]],
    nav_by_investment: dict[UUID, float],
    sectors_by_id: dict[UUID, SectorDTO],
) -> SectorBreakdown:
    """Aggregate per-investment sector weights into a sector breakdown.

    Args:
        investments: Investments in scope.
        sector_weights_by_investment: Per-investment sector-weight
            lists.
        nav_by_investment: Latest NAV per investment.
        sectors_by_id: Lookup for display-name resolution.

    Returns:
        :class:`SectorBreakdown` sorted by ``weight_pct`` descending.
    """
    if not investments:
        return SectorBreakdown(rows=[])

    contributions_by_sector: dict[UUID, float] = {}
    total_nav = 0.0
    for inv in investments:
        weights = sector_weights_by_investment.get(inv.id, [])
        if not weights:
            continue
        nav_v = nav_by_investment.get(inv.id, 0.0)
        if nav_v <= 0.0:
            continue
        total_nav += nav_v
        for w in weights:
            share = nav_v * float(w.weight_pct) / 100.0
            contributions_by_sector[w.sector_id] = (
                contributions_by_sector.get(w.sector_id, 0.0) + share
            )

    if total_nav <= 0.0 or not contributions_by_sector:
        return SectorBreakdown(rows=[])

    rows: list[SectorBreakdownRow] = []
    for sector_id, nav_eur in contributions_by_sector.items():
        if nav_eur <= 0.0:
            continue
        sector = sectors_by_id.get(sector_id)
        rows.append(
            SectorBreakdownRow(
                sector_code=sector.code if sector is not None else "",
                sector_display_name=(sector.display_name if sector is not None else str(sector_id)),
                nav_eur=nav_eur,
                weight_pct=100.0 * nav_eur / total_nav,
            )
        )
    rows.sort(key=lambda r: r.weight_pct, reverse=True)
    return SectorBreakdown(rows=rows)


def aggregate_vintage_distribution(
    investments: list[InvestmentDTO],
    nav_by_investment: dict[UUID, float],
) -> VintageDistribution:
    """NAV-weighted distribution over distinct vintage years.

    Investments without a ``vintage_year`` or a positive NAV are
    skipped. The distribution sums to ``100.0`` percent across all
    rows.

    Args:
        investments: Investments in scope.
        nav_by_investment: Latest NAV per investment (EUR).

    Returns:
        :class:`VintageDistribution` with ``vintages`` sorted
        ascending.
    """
    by_vintage: dict[int, list[float]] = {}
    for inv in investments:
        if inv.vintage_year is None:
            continue
        nav_v = nav_by_investment.get(inv.id, 0.0)
        if nav_v <= 0.0:
            continue
        by_vintage.setdefault(int(inv.vintage_year), []).append(nav_v)

    if not by_vintage:
        return VintageDistribution([], [], [])

    total = sum(sum(navs) for navs in by_vintage.values())
    if total <= 0.0:
        return VintageDistribution([], [], [])

    years_sorted = sorted(by_vintage.keys())
    weight_pct = [100.0 * sum(by_vintage[y]) / total for y in years_sorted]
    count = [len(by_vintage[y]) for y in years_sorted]
    return VintageDistribution(
        vintages=years_sorted,
        weight_pct=weight_pct,
        count=count,
    )


def aggregate_fund_composition(
    investments: list[InvestmentDTO],
    nav_by_investment: dict[UUID, float],
    cf_in_by_investment: dict[UUID, pd.Series],
    cf_out_by_investment: dict[UUID, pd.Series],
    report_date: date,
) -> FundCompositionBreakdown:
    """NAV-weighted composition of the portfolio by individual fund.

    Each fund's share is its latest NAV divided by the total NAV across
    all funds carrying a positive NAV. Funds without a positive NAV are
    skipped. Rows are sorted by NAV descending and carry a running
    cumulative share (the Pareto curve) plus the fund's
    IRR-since-inception evaluated at ``report_date``.

    This returns the **full** breakdown (every qualifying fund). The
    top-N grouping is applied separately by
    :func:`group_fund_composition` so the presentation policy stays out
    of the aggregation.

    Args:
        investments: Investments in scope.
        nav_by_investment: Latest NAV per investment (EUR).
        cf_in_by_investment: Per-investment positive-cashflow series
            (distributions), shaped as the IRR helper expects.
        cf_out_by_investment: Per-investment negative-cashflow series
            (calls).
        report_date: As-of date; the IRR terminal date.

    Returns:
        :class:`FundCompositionBreakdown` sorted by ``nav_eur``
        descending.
    """
    report_ts = pd.Timestamp(report_date, tz="UTC")
    empty = pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))

    entries: list[tuple[UUID, str, float, float | None]] = []
    total_nav = 0.0
    for inv in investments:
        nav_v = nav_by_investment.get(inv.id, 0.0)
        if nav_v <= 0.0:
            continue
        total_nav += nav_v

        cf_in = cf_in_by_investment.get(inv.id, empty)
        cf_out = cf_out_by_investment.get(inv.id, empty)
        if cf_in.empty and cf_out.empty:
            irr: float | None = None
        else:
            raw = compute_irr(cf_in, cf_out, nav_v, report_ts)
            irr = raw if raw == raw else None  # NaN-safe
        entries.append((inv.id, inv.name, nav_v, irr))

    if total_nav <= 0.0 or not entries:
        return FundCompositionBreakdown(rows=[])

    entries.sort(key=lambda e: e[2], reverse=True)

    rows: list[FundCompositionRow] = []
    cumulative = 0.0
    for investment_id, name, nav_eur, irr in entries:
        weight_pct = 100.0 * nav_eur / total_nav
        cumulative += weight_pct
        rows.append(
            FundCompositionRow(
                investment_id=investment_id,
                name=name,
                nav_eur=nav_eur,
                weight_pct=weight_pct,
                cumulative_pct=cumulative,
                irr=irr,
            )
        )
    return FundCompositionBreakdown(rows=rows)


def aggregate_currency_exposure(
    investments: list[InvestmentDTO],
    nav_by_investment: dict[UUID, float],
) -> CurrencyExposure:
    """Group converted NAVs by position currency (ADR-0101 §1).

    Each investment's latest NAV contributes to the bucket of its
    ``investment.currency`` — the currency the position is *denominated*
    in. The NAVs themselves arrive already converted into the tenant's
    functional currency (the caller sits behind the ADR-0099 §4 boundary),
    so the amounts are comparable and summable across buckets while the
    grouping key stays the position currency. That combination is the whole
    point: it answers "how much of my book is USD-denominated, expressed in
    the currency I report in".

    Runs over the **full** universe — explicit ADR-0100 cash positions
    included, since a foreign-currency cash balance is FX exposure in its
    purest form.

    Investments without a positive NAV are skipped, mirroring
    :func:`aggregate_fund_composition`: a zero or negative NAV carries no
    exposure and would only add an empty slice to the donut.

    Args:
        investments: Investments in scope (full universe).
        nav_by_investment: Latest NAV per investment, **already converted**
            into the functional currency. Investments absent from the dict,
            or with a non-positive NAV, are skipped.

    Returns:
        A :class:`CurrencyExposure` whose rows are sorted by ``amount``
        descending and whose ``weight_pct`` values sum to ``100.0``. Empty
        when no investment carries a positive NAV.
    """
    amount_by_currency: dict[str, float] = {}
    total_nav = 0.0
    for inv in investments:
        nav_v = nav_by_investment.get(inv.id, 0.0)
        if nav_v <= 0.0:
            continue
        currency = inv.currency
        amount_by_currency[currency] = amount_by_currency.get(currency, 0.0) + nav_v
        total_nav += nav_v

    if total_nav <= 0.0 or not amount_by_currency:
        return CurrencyExposure(rows=[])

    rows = [
        CurrencyExposureRow(
            currency=currency,
            amount=amount,
            weight_pct=100.0 * amount / total_nav,
        )
        for currency, amount in amount_by_currency.items()
    ]
    rows.sort(key=lambda r: r.amount, reverse=True)
    return CurrencyExposure(rows=rows)


def group_fund_composition(
    breakdown: FundCompositionBreakdown,
    top_n: int,
) -> FundCompositionBreakdown:
    """Fold all but the top ``top_n`` funds into one ``"Other"`` row.

    The first ``top_n`` rows (already sorted by NAV descending) are kept
    individually; the remainder are merged into a single
    ``"Other (k funds)"`` row whose ``nav_eur`` and ``weight_pct`` are
    the tail sums and whose ``irr`` is the **NAV-weighted average** of
    the tail funds' IRRs (skipping funds with no IRR). ``cumulative_pct``
    is recomputed over the merged ordering so the last row lands at
    ~100 %.

    When ``top_n`` is below 1 or the breakdown already has ``<= top_n``
    rows, the breakdown is returned unchanged.

    Args:
        breakdown: Full breakdown from :func:`aggregate_fund_composition`.
        top_n: Number of funds to show individually before grouping.

    Returns:
        A grouped :class:`FundCompositionBreakdown`.
    """
    rows = breakdown.rows
    if top_n < 1 or len(rows) <= top_n:
        return breakdown

    head = rows[:top_n]
    tail = rows[top_n:]

    tail_nav = sum(r.nav_eur for r in tail)
    tail_weight = sum(r.weight_pct for r in tail)

    irr_num = 0.0
    irr_den = 0.0
    for r in tail:
        if r.irr is not None:
            irr_num += r.nav_eur * r.irr
            irr_den += r.nav_eur
    other_irr = (irr_num / irr_den) if irr_den > 0.0 else None

    other = FundCompositionRow(
        investment_id=None,
        name=f"Other ({len(tail)} funds)",
        nav_eur=tail_nav,
        weight_pct=tail_weight,
        cumulative_pct=0.0,  # set by the recompute below
        irr=other_irr,
    )

    merged = [*list(head), other]
    out: list[FundCompositionRow] = []
    cumulative = 0.0
    for r in merged:
        cumulative += r.weight_pct
        out.append(replace(r, cumulative_pct=cumulative))
    return FundCompositionBreakdown(rows=out)


def compute_concentration(
    breakdown: FundCompositionBreakdown,
) -> ConcentrationStats:
    """Summarise NAV concentration over the full fund breakdown.

    Operates on the **full, ungrouped** breakdown from
    :func:`aggregate_fund_composition` — rows sorted by NAV descending.
    Never call this on a :func:`group_fund_composition` result: the
    single ``"Other"`` tail bucket would distort the HHI.

    Each ``topK_pct`` is the sum of ``weight_pct`` over the first
    ``min(K, n)`` rows (equivalently the ``cumulative_pct`` of row
    ``min(K, n) - 1``); when ``n < K`` it lands at the full ~100 %,
    clamped so floating-point drift cannot push it above 100. ``hhi``
    is the sum of squared NAV fractions over all rows.

    Args:
        breakdown: Full breakdown from
            :func:`aggregate_fund_composition`. Not mutated.

    Returns:
        A frozen :class:`ConcentrationStats`. An empty breakdown yields
        all-zero shares, ``hhi`` ``0.0`` and ``fund_count`` ``0``.
    """
    rows = breakdown.rows
    n = len(rows)
    if n == 0:
        return ConcentrationStats(
            top1_pct=0.0,
            top3_pct=0.0,
            top5_pct=0.0,
            top10_pct=0.0,
            hhi=0.0,
            fund_count=0,
        )

    def _top(k: int) -> float:
        return min(sum(r.weight_pct for r in rows[: min(k, n)]), 100.0)

    hhi = sum((r.weight_pct / 100.0) ** 2 for r in rows)
    return ConcentrationStats(
        top1_pct=_top(1),
        top3_pct=_top(3),
        top5_pct=_top(5),
        top10_pct=_top(10),
        hhi=hhi,
        fund_count=n,
    )


def compute_total_return_index_series(
    return_series: pd.Series,
    base: float = 100.0,
) -> pd.Series:
    """Rebase a periodic-return series to a cumulative index.

    ``index_t = base * cumprod(1 + return_t)``. The first observation
    is at ``base * (1 + r_0)`` so the series begins with a non-trivial
    value rather than the bare ``base``.

    Args:
        return_series: Pandas Series of decimal returns indexed by
            date. May contain NaN; rows with NaN are dropped before
            compounding.
        base: Starting index value. Defaults to ``100.0``.

    Returns:
        Pandas Series indexed by the same dates (minus dropped NaNs),
        values are the rebased index. Empty when ``return_series``
        has no usable observations.
    """
    if return_series is None or return_series.empty:
        return pd.Series(dtype="float64")
    cleaned = return_series.dropna().sort_index()
    if cleaned.empty:
        return pd.Series(dtype="float64")
    return (1.0 + cleaned).cumprod() * base


__all__ = [
    "ConcentrationStats",
    "CurrencyExposure",
    "CurrencyExposureRow",
    "FundCompositionBreakdown",
    "FundCompositionRow",
    "InvestedCapitalNavSeries",
    "PortfolioCashflowSeries",
    "PortfolioMultiplesSeries",
    "RegionBreakdown",
    "RegionBreakdownRow",
    "SectorBreakdown",
    "SectorBreakdownRow",
    "VintageDistribution",
    "aggregate_currency_exposure",
    "aggregate_fund_composition",
    "aggregate_invested_capital_and_nav",
    "aggregate_portfolio_cashflows",
    "aggregate_portfolio_multiples",
    "aggregate_region_breakdown",
    "aggregate_sector_breakdown",
    "aggregate_vintage_distribution",
    "compute_concentration",
    "compute_total_return_index_series",
    "group_fund_composition",
]
