# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ArchetypeChartsService — per-archetype Front-Office charts assembly.

The data-assembly half of ADR-0082: it resolves an investment's
presentation archetype (ADR-0082 §1) and returns the **pure data** the
matching tile-set needs — pandas Series / DataFrames, plain ``dict``
mappings, and frozen KPI dataclasses. It builds no Plotly specs; the
route (ADR-0082 §6, a later step) builds those from this data.

Why a dedicated service rather than an ``InvestmentService`` method
(a small, documented deviation from the ADR-0082 §6 wording): the
assembly needs eight composition / FI-reference repositories plus the
benchmark machinery on top of the three Investment-domain repositories.
Folding that into ``InvestmentService`` would bloat its constructor and
mix its import/CRUD responsibility with a read-only assembly. This is
the same dedicated-service pattern as ``overview_service`` and
``portfolio_review_service``; it can be folded back later if desired.

Architecture (ADR-0082 §"Option A"):

- **Income-aware total return.** For both listed archetypes the return
  is the cash-flow-adjusted time-weighted series
  (:func:`~services.analytics.investment_returns.compute_cashflow_adjusted_return_series`,
  ADR-0066 / ADR-0079 §3) over the ex-income price NAV (ADR-0081
  variant A), with ``dividend``/``coupon`` flows added back as the
  signed income — **not** ``nav.pct_change()``. That one series feeds
  the hero investment line, the underwater profile, the trailing-TWR
  table, and the rolling volatility / Sharpe consistently.
- **No silent fallbacks.** Every edge case (no NAV, no benchmark
  mapping, no bond analytics, empty weights) surfaces an explicit
  sentinel — an empty Series, an empty mapping, ``None``, or ``nan``.
  An unknown / cross-tenant id returns ``None`` (RLS hides the row; the
  route renders the neutral empty state).

All repositories and the two injected services must be tenant-scoped
(the caller constructs them against a session obtained via
:func:`core.repositories.tenant_context`); the service neither sets nor
reads ``app.tenant_id`` itself.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date as _date
from typing import Any
from uuid import UUID

import pandas as pd

from core.repositories.investment_bond_analytics_repository import (
    InvestmentBondAnalyticsRepository,
)
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.investment_maturity_weights_repository import (
    InvestmentMaturityWeightsRepository,
)
from core.repositories.investment_nav_repository import (
    InvestmentNavRepository,
)
from core.repositories.investment_rating_weights_repository import (
    InvestmentRatingWeightsRepository,
)
from core.repositories.investment_region_weights_repository import (
    InvestmentRegionWeightsRepository,
)
from core.repositories.investment_repository import (
    InvestmentDTO,
    InvestmentRepository,
)
from core.repositories.investment_sector_weights_repository import (
    InvestmentSectorWeightsRepository,
)
from core.repositories.region_repository import RegionRepository
from core.repositories.sector_repository import SectorRepository
from services.analytics.benchmark_comparison import (
    BenchmarkComparisonMetrics,
    compute_benchmark_comparison,
    normalise_monthly_index,
)
from services.analytics.fixed_income import (
    compute_notch_weighted_average_rating,
)
from services.analytics.investment_returns import (
    compute_cashflow_adjusted_return_series,
    compute_trailing_returns,
)
from services.analytics.statistics import (
    compute_rolling_sharpe,
    compute_rolling_volatility,
    compute_underwater_series,
)
from services.benchmark_comparison import BenchmarkComparisonService
from services.front_office_charts._dtos import (
    ArchetypeChartsResult,
    CapitalAccountKPI,
    CapitalAccountTiles,
    EquityKPI,
    FixedIncomeKPI,
    FixedIncomeTiles,
    NavOnlyTiles,
    TotalReturnEquityTiles,
)
from services.investments.archetype import Archetype, resolve_archetype
from services.investments.investment_service import InvestmentService

# The two income flow types that the listed archetypes add back into
# the time-weighted return (ADR-0079 §3): equity dividends and bond
# coupons. Both are positive under the signed-amount convention.
_INCOME_FLOW_TYPES: frozenset[str] = frozenset({"dividend", "coupon"})

# Months in the monthly grid for the rolling-window KPIs (ADR-0082 §5).
_ROLLING_WINDOW_MONTHS: int = 12


class ArchetypeChartsService:
    """Assemble per-archetype tile inputs and KPI payloads for one investment."""

    def __init__(
        self,
        investments: InvestmentRepository,
        navs: InvestmentNavRepository,
        cashflows: InvestmentCashflowRepository,
        bond_analytics: InvestmentBondAnalyticsRepository,
        rating_weights: InvestmentRatingWeightsRepository,
        maturity_weights: InvestmentMaturityWeightsRepository,
        sector_weights: InvestmentSectorWeightsRepository,
        region_weights: InvestmentRegionWeightsRepository,
        sectors: SectorRepository,
        regions: RegionRepository,
        investments_service: InvestmentService,
        benchmarks: BenchmarkComparisonService,
    ) -> None:
        self._investments = investments
        self._navs = navs
        self._cashflows = cashflows
        self._bond_analytics = bond_analytics
        self._rating_weights = rating_weights
        self._maturity_weights = maturity_weights
        self._sector_weights = sector_weights
        self._region_weights = region_weights
        self._sectors = sectors
        self._regions = regions
        self._investments_service = investments_service
        self._benchmarks = benchmarks

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_universe_axis_end(self) -> _date | None:
        """Return the tenant's "universe as-of" — the shared tile axis end.

        ADR-0113 §1: every time-series tile in the Charts section shares
        one x-axis **end**, the latest ``'actual'`` NAV date observed
        across the active investment universe, so equal tile widths stop
        implying equal periods. The axis *start* is deliberately not
        unified — vintages differ by years and a common start would
        compress long-lived funds into a sliver.

        Returns:
            The latest actual NAV date across all active investments, or
            ``None`` when the universe is empty or carries no actual NAV
            row. ``None`` leaves every tile on its own auto-range (the
            pre-ADR-0113 behaviour) rather than inventing a date.
        """
        investments = await self._investments.list_active()
        return await self._navs.latest_actual_as_of_date([inv.id for inv in investments])

    async def get_archetype_charts_data(
        self,
        investment_id: UUID,
        as_of_date: _date | None = None,
    ) -> ArchetypeChartsResult | None:
        """Resolve the archetype and assemble its single tile bundle.

        Loads the investment, resolves its archetype from
        ``investment_type`` via
        :func:`~services.investments.archetype.resolve_archetype`, and
        populates exactly one tile bundle on the returned
        :class:`~services.front_office_charts._dtos.ArchetypeChartsResult`.

        Args:
            investment_id: The investment to assemble charts data for.
            as_of_date: Optional cut-off applied to every time-series
                read; defaults to today.

        Returns:
            An :class:`ArchetypeChartsResult`, or ``None`` when the
            investment is unknown to the active tenant (RLS hides
            cross-tenant rows, so a foreign id surfaces as ``None``).
        """
        resolved_as_of = as_of_date if as_of_date is not None else _date.today()

        investment = await self._investments.get_by_id(investment_id)
        if investment is None:
            return None

        archetype = resolve_archetype(investment.investment_type)

        if archetype is Archetype.CAPITAL_ACCOUNT:
            capital_account = await self._capital_account_tiles(investment)
            if capital_account is None:
                return None
            return ArchetypeChartsResult(
                archetype=archetype,
                investment_name=investment.name,
                capital_account=capital_account,
            )

        if archetype is Archetype.TOTAL_RETURN_EQUITY:
            total_return_equity = await self._total_return_equity_tiles(investment, resolved_as_of)
            return ArchetypeChartsResult(
                archetype=archetype,
                investment_name=investment.name,
                total_return_equity=total_return_equity,
            )

        if archetype is Archetype.FIXED_INCOME:
            fixed_income = await self._fixed_income_tiles(investment, resolved_as_of)
            return ArchetypeChartsResult(
                archetype=archetype,
                investment_name=investment.name,
                fixed_income=fixed_income,
            )

        # NAV-only fallback.
        nav_only = await self._nav_only_tiles(investment)
        return ArchetypeChartsResult(
            archetype=archetype,
            investment_name=investment.name,
            nav_only=nav_only,
        )

    # ------------------------------------------------------------------
    # Income-aware total return (shared by both listed archetypes)
    # ------------------------------------------------------------------

    async def _investment_total_return_monthly(
        self, investment_id: UUID, as_of_date: _date
    ) -> pd.Series:
        """Income-aware monthly time-weighted return series (ADR-0082 §"Option A").

        1. Load the actual NAVs at or before ``as_of_date``, sorted, as a
           date-indexed Series (the ex-income price NAV, ADR-0081 A).
        2. Load the actual income cashflows (``flow_type`` in
           ``{dividend, coupon}``) into the ``{flow_timestamp, amount}``
           frame that
           :func:`~services.analytics.investment_returns.compute_cashflow_adjusted_return_series`
           expects (signed amounts; income is positive).
        3. ``daily_tr = compute_cashflow_adjusted_return_series(...)`` —
           the income is added back interval-by-interval so the series
           is the total (income-inclusive) return, not the ex-income
           price return.
        4. Compound to month-end. The compounding mirrors the benchmark
           path's resampling (drop empty months) so the monthly grid
           lines up bit-for-bit with the benchmark series the hero tile
           aligns against.

        Args:
            investment_id: The investment whose return series to build.
            as_of_date: Upper bound for both the NAV and the income
                reads.

        Returns:
            Month-end-indexed monthly decimal returns. Empty Series when
            the actual NAV history has fewer than two datapoints.
        """
        nav_rows = await self._navs.list_by_investment_and_kind(investment_id, "actual")
        nav_rows = [n for n in nav_rows if n.as_of_date <= as_of_date]
        nav_series = pd.Series(
            data=[float(n.nav_value) for n in nav_rows],
            index=[n.as_of_date for n in nav_rows],
            dtype="float64",
        )

        cashflows = await self._cashflows.list_by_investment(investment_id)
        income = [
            c for c in cashflows if c.flow_kind == "actual" and c.flow_type in _INCOME_FLOW_TYPES
        ]
        income_frame = pd.DataFrame(
            {
                "flow_timestamp": [c.flow_timestamp for c in income],
                "amount": [float(c.amount) for c in income],
            }
        )

        daily_tr = compute_cashflow_adjusted_return_series(nav_series, income_frame)
        return _compound_daily_to_monthly(daily_tr)

    # ------------------------------------------------------------------
    # Hero + benchmark metrics (shared by both listed archetypes)
    # ------------------------------------------------------------------

    async def _benchmark_block(
        self,
        investment: InvestmentDTO,
        inv_tr_monthly: pd.Series,
        as_of_date: _date,
    ) -> tuple[
        str | None,
        pd.Series,
        pd.Series,
        pd.Series,
        BenchmarkComparisonMetrics | None,
    ]:
        """Hero cumulative series + benchmark metrics for the listed archetypes.

        Returns ``(benchmark_display_name, investment_cumulative,
        benchmark_cumulative, excess_cumulative, metrics)``.

        With a benchmark mapping: a constant monthly risk-free series
        (``rf / 12`` over the investment grid) is built and fed with the
        benchmark monthly returns into
        :func:`~services.analytics.benchmark_comparison.compute_benchmark_comparison`.
        The bundle's *aligned* (inner-joined) series feed the metrics —
        beta / tracking error / information ratio — and bound the excess
        window; they no longer bound the **drawn lines**. Per ADR-0113
        §5 the two display cumulatives are start-aligned and end-free:
        both begin at the later of the two first months (so the lines
        stay comparable from a common 0 % origin) and each runs to its
        own last available month. A market-data tick that extends the
        investment's monthly series therefore visibly extends the
        investment line even while benchmark observations lag at the
        import state. The excess is defined only where both lines
        exist, so it equals the visible vertical gap between them.

        Without a benchmark mapping (``get_investment_benchmark_inputs``
        returns ``None``): only the investment cumulative is built (over
        the full monthly series); the benchmark and excess series are
        empty, the display name is ``None``, and the metrics are
        ``None``.
        """
        inputs = await self._benchmarks.get_investment_benchmark_inputs(investment.id, as_of_date)
        if inputs is None:
            inv_cumulative = (1.0 + inv_tr_monthly).cumprod() - 1.0
            empty = pd.Series(dtype="float64")
            return None, inv_cumulative, empty, empty.copy(), None

        benchmark_display_name, benchmark_monthly, risk_free_rate = inputs
        risk_free_monthly = pd.Series(
            risk_free_rate / 12.0,
            index=inv_tr_monthly.index,
            dtype="float64",
        )
        bundle = compute_benchmark_comparison(
            investment_returns=inv_tr_monthly,
            benchmark_returns=benchmark_monthly,
            risk_free_returns=risk_free_monthly,
            investment_identifier=investment.name,
            benchmark_identifier=benchmark_display_name,
        )

        # Display series: the same alignment key the analytics inner-join
        # uses, but only the *start* is aligned. Sorting is what makes the
        # cumulative product chronological — the caller's ordering is not
        # a guarantee once the inner-join no longer imposes one.
        inv_full = normalise_monthly_index(inv_tr_monthly).sort_index()
        bench_full = normalise_monthly_index(benchmark_monthly).sort_index()
        empty = pd.Series(dtype="float64")
        if inv_full.empty or bench_full.empty:
            return (benchmark_display_name, empty, empty.copy(), empty.copy(), bundle.metrics)

        common_start = max(inv_full.index.min(), bench_full.index.min())
        inv_from_start = inv_full.loc[inv_full.index >= common_start]
        bench_from_start = bench_full.loc[bench_full.index >= common_start]
        if inv_from_start.empty or bench_from_start.empty:
            # Disjoint periods — no month where both series exist. Same
            # empty-state outcome as an empty inner-join.
            return (benchmark_display_name, empty, empty.copy(), empty.copy(), bundle.metrics)

        inv_cumulative = (1.0 + inv_from_start).cumprod() - 1.0
        bench_cumulative = (1.0 + bench_from_start).cumprod() - 1.0
        # Pandas aligns on the union and yields NaN outside the
        # intersection; dropping those leaves the months both lines cover.
        excess_cumulative = (inv_cumulative - bench_cumulative).dropna()
        return (
            benchmark_display_name,
            inv_cumulative,
            bench_cumulative,
            excess_cumulative,
            bundle.metrics,
        )

    # ------------------------------------------------------------------
    # Branch: CAPITAL_ACCOUNT (delegates to the existing path)
    # ------------------------------------------------------------------

    async def _capital_account_tiles(self, investment: InvestmentDTO) -> CapitalAccountTiles | None:
        """Capital-Account bundle — delegate charts, derive the KPI caption.

        The plan-NAV series is loaded here as a *whole*: the display
        window (last actual → universe as-of) and the anchor point are
        the spec builder's job (ADR-0113 §2), so the service stays free
        of presentation geometry. The series never joins the actual one —
        it travels as its own field into the Cashflows & NAV tile and
        reaches no KPI or return computation (ADR-0113 §3).
        """
        bundle = await self._investments_service.get_charts_data(investment.id)
        if bundle is None:
            return None

        plan_navs = await self._navs.list_by_investment_and_kind(investment.id, "plan")
        nav_plan = pd.Series(
            data=[float(n.nav_value) for n in plan_navs],
            index=[n.as_of_date for n in plan_navs],
            dtype="float64",
        ).sort_index()

        # TVPI / DPI from the last observation with a defined multiple
        # (rows are NaN before the first capital call).
        valid_multiples = bundle.rolling_multiples.dropna(subset=["tvpi"])
        if not valid_multiples.empty:
            tvpi: float | None = float(valid_multiples["tvpi"].iloc[-1])
            dpi: float | None = float(valid_multiples["dpi"].iloc[-1])
        else:
            tvpi = None
            dpi = None

        valid_irr = bundle.rolling_irr.dropna()
        net_irr = float(valid_irr.iloc[-1]) if not valid_irr.empty else None

        cashflows = bundle.cashflows_actual
        if not cashflows.empty:
            calls = cashflows.loc[cashflows["flow_type"] == "capital_call", "amount"]
            total_called = float(calls.abs().sum())
        else:
            total_called = 0.0
        commitment = investment.commitment_amount
        unfunded = float(commitment) - total_called if commitment is not None else None

        return CapitalAccountTiles(
            charts=bundle,
            nav_plan=nav_plan,
            kpi=CapitalAccountKPI(
                tvpi=tvpi,
                dpi=dpi,
                net_irr=net_irr,
                unfunded_commitment=unfunded,
            ),
        )

    # ------------------------------------------------------------------
    # Branch: TOTAL_RETURN_EQUITY
    # ------------------------------------------------------------------

    async def _total_return_equity_tiles(
        self, investment: InvestmentDTO, as_of_date: _date
    ) -> TotalReturnEquityTiles:
        """Total-Return-Equity bundle: hero, underwater, sector|region, KPI."""
        inv_tr_monthly = await self._investment_total_return_monthly(investment.id, as_of_date)
        (
            benchmark_display_name,
            inv_cumulative,
            bench_cumulative,
            excess_cumulative,
            metrics,
        ) = await self._benchmark_block(investment, inv_tr_monthly, as_of_date)

        underwater_series = compute_underwater_series(inv_tr_monthly)
        tr_index = (1.0 + inv_tr_monthly).cumprod()
        trailing = compute_trailing_returns(tr_index, as_of=as_of_date)
        vol_12m = _last_or_nan(
            compute_rolling_volatility(inv_tr_monthly, window=_ROLLING_WINDOW_MONTHS)
        )
        sharpe_12m = _last_or_nan(
            compute_rolling_sharpe(inv_tr_monthly, window=_ROLLING_WINDOW_MONTHS)
        )

        sector_weights = await self._latest_sector_weights(investment.id, as_of_date)
        region_weights = await self._latest_region_weights(investment.id, as_of_date)
        dividend_yield_ttm = await self._dividend_yield_ttm(investment.id, as_of_date)

        kpi = EquityKPI(
            trailing=trailing,
            vol_12m=vol_12m,
            sharpe_12m=sharpe_12m,
            beta=metrics.beta if metrics is not None else None,
            tracking_error=(metrics.tracking_error_annualised if metrics is not None else None),
            information_ratio=(metrics.information_ratio if metrics is not None else None),
            dividend_yield_ttm=dividend_yield_ttm,
        )
        return TotalReturnEquityTiles(
            benchmark_display_name=benchmark_display_name,
            investment_cumulative=inv_cumulative,
            benchmark_cumulative=bench_cumulative,
            excess_cumulative=excess_cumulative,
            underwater_series=underwater_series,
            sector_weights=sector_weights,
            region_weights=region_weights,
            kpi=kpi,
        )

    # ------------------------------------------------------------------
    # Branch: FIXED_INCOME
    # ------------------------------------------------------------------

    async def _fixed_income_tiles(
        self, investment: InvestmentDTO, as_of_date: _date
    ) -> FixedIncomeTiles:
        """Fixed-Income bundle: hero, YTM/OAS & duration, rating|maturity, KPI."""
        inv_tr_monthly = await self._investment_total_return_monthly(investment.id, as_of_date)
        (
            benchmark_display_name,
            inv_cumulative,
            bench_cumulative,
            excess_cumulative,
            _metrics,
        ) = await self._benchmark_block(investment, inv_tr_monthly, as_of_date)

        bond_analytics = await self._bond_analytics_frame(investment.id, as_of_date)
        rating_weights = await self._latest_rating_weights(investment.id, as_of_date)
        maturity_weights = await self._latest_maturity_weights(investment.id, as_of_date)

        tr_index = (1.0 + inv_tr_monthly).cumprod()
        trailing = compute_trailing_returns(tr_index, as_of=as_of_date)
        ytm, eff_duration, oas = _latest_bond_metrics(bond_analytics)

        kpi = FixedIncomeKPI(
            twr=trailing.since_inception_annualised,
            ytm=ytm,
            eff_duration=eff_duration,
            oas=oas,
            avg_rating=compute_notch_weighted_average_rating(rating_weights),
        )
        return FixedIncomeTiles(
            benchmark_display_name=benchmark_display_name,
            investment_cumulative=inv_cumulative,
            benchmark_cumulative=bench_cumulative,
            excess_cumulative=excess_cumulative,
            bond_analytics=bond_analytics,
            rating_weights=rating_weights,
            maturity_weights=maturity_weights,
            kpi=kpi,
        )

    # ------------------------------------------------------------------
    # Branch: NAV_ONLY
    # ------------------------------------------------------------------

    async def _nav_only_tiles(self, investment: InvestmentDTO) -> NavOnlyTiles:
        """NAV-only bundle: the investment DTO plus its full NAV history."""
        navs = await self._navs.list_by_investment(investment.id)
        return NavOnlyTiles(investment=investment, navs=navs)

    # ------------------------------------------------------------------
    # Composition / reference-data helpers
    # ------------------------------------------------------------------

    async def _latest_sector_weights(
        self, investment_id: UUID, as_of_date: _date
    ) -> dict[str, float]:
        """Latest sector snapshot as ``{sector_display_name: weight_pct}``."""
        rows = await self._sector_weights.list_latest_for_investment(
            investment_id, as_of_cutoff=as_of_date
        )
        if not rows:
            return {}
        name_by_id = {s.id: s.display_name for s in await self._sectors.list_all()}
        resolved: dict[str, float] = {}
        for row in rows:
            name = name_by_id.get(row.sector_id)
            if name is None:
                # FK integrity guarantees the sector exists; skip
                # defensively rather than leak a UUID label.
                continue
            resolved[name] = float(row.weight_pct)
        return resolved

    async def _latest_region_weights(
        self, investment_id: UUID, as_of_date: _date
    ) -> dict[str, float]:
        """Latest region snapshot as ``{region_display_name: weight_pct}``."""
        rows = await self._region_weights.list_latest_for_investment(
            investment_id, as_of_cutoff=as_of_date
        )
        if not rows:
            return {}
        name_by_id = {r.id: r.display_name for r in await self._regions.list_all()}
        resolved: dict[str, float] = {}
        for row in rows:
            name = name_by_id.get(row.region_id)
            if name is None:
                continue
            resolved[name] = float(row.weight_pct)
        return resolved

    async def _latest_rating_weights(
        self, investment_id: UUID, as_of_date: _date
    ) -> dict[str, float]:
        """Latest rating snapshot as ``{rating_bucket: weight_pct}``."""
        rows = await self._rating_weights.list_for_investment(
            investment_id, as_of_cutoff=as_of_date
        )
        return _latest_bucket_weights(rows, lambda row: row.rating_bucket)

    async def _latest_maturity_weights(
        self, investment_id: UUID, as_of_date: _date
    ) -> dict[str, float]:
        """Latest maturity snapshot as ``{maturity_bucket: weight_pct}``."""
        rows = await self._maturity_weights.list_for_investment(
            investment_id, as_of_cutoff=as_of_date
        )
        return _latest_bucket_weights(rows, lambda row: row.maturity_bucket)

    async def _bond_analytics_frame(self, investment_id: UUID, as_of_date: _date) -> pd.DataFrame:
        """Bond-analytics rows as a date-indexed float DataFrame.

        Columns ``ytm``, ``oas`` (NaN where the row carries no spread),
        ``eff_duration``. Empty (column-only) DataFrame when there are
        no rows — the no-silent-fallback empty state.
        """
        rows = await self._bond_analytics.list_for_investment(
            investment_id, as_of_cutoff=as_of_date
        )
        if not rows:
            return pd.DataFrame(columns=["ytm", "oas", "eff_duration"])
        frame = pd.DataFrame(
            {
                "ytm": [float(r.ytm) for r in rows],
                "oas": [float(r.oas) if r.oas is not None else float("nan") for r in rows],
                "eff_duration": [float(r.eff_duration) for r in rows],
            },
            index=pd.to_datetime([r.as_of_date for r in rows]),
        )
        return frame.sort_index()

    async def _dividend_yield_ttm(self, investment_id: UUID, as_of_date: _date) -> float | None:
        """Trailing-twelve-month dividends over the latest actual NAV.

        ``None`` when there is no actual NAV at or before ``as_of_date``
        to divide by (or it is zero) — the missing denominator is
        explicit, never a fabricated zero yield.
        """
        nav_rows = await self._navs.list_by_investment_and_kind(investment_id, "actual")
        nav_rows = [n for n in nav_rows if n.as_of_date <= as_of_date]
        if not nav_rows:
            return None
        # The repository returns rows sorted by ``as_of_date`` ascending.
        latest_nav = float(nav_rows[-1].nav_value)
        if latest_nav == 0.0:
            return None

        as_of_ts = pd.Timestamp(as_of_date, tz="UTC")
        window_start = as_of_ts - pd.DateOffset(years=1)
        cashflows = await self._cashflows.list_by_investment(investment_id)
        ttm_dividends = 0.0
        for cashflow in cashflows:
            if cashflow.flow_kind != "actual" or cashflow.flow_type != "dividend":
                continue
            timestamp = pd.Timestamp(cashflow.flow_timestamp)
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize("UTC")
            else:
                timestamp = timestamp.tz_convert("UTC")
            if window_start < timestamp <= as_of_ts:
                ttm_dividends += float(cashflow.amount)
        return ttm_dividends / latest_nav


# ---------------------------------------------------------------------------
# Module-level helpers (pure)
# ---------------------------------------------------------------------------


def _compound_daily_to_monthly(daily_returns: pd.Series) -> pd.Series:
    """Compound a return series to month-end-stamped monthly returns.

    Mirrors the benchmark path's resampling
    (``BenchmarkComparisonService._resample_daily_to_monthly``):
    ``r_m = (1 + r).prod() - 1`` over each calendar month, with months
    that carry no observation **dropped** rather than emitted as a
    spurious ``0.0``. Reproducing the benchmark convention exactly is
    what lets the investment grid line up bit-for-bit with the benchmark
    series the hero tile aligns against (ADR-0082 §"Option A").

    Args:
        daily_returns: Date-indexed decimal return series (the output of
            :func:`~services.analytics.investment_returns.compute_cashflow_adjusted_return_series`).

    Returns:
        Month-end-indexed monthly decimal returns. Empty when the input
        is empty.
    """
    if daily_returns.empty:
        return pd.Series(dtype="float64")
    cleaned = daily_returns.dropna().sort_index()
    if cleaned.empty:
        return pd.Series(dtype="float64")
    index = pd.to_datetime(cleaned.index)
    if getattr(index, "tz", None) is not None:
        index = index.tz_convert("UTC").tz_localize(None)
    cleaned = pd.Series(cleaned.to_numpy(dtype="float64"), index=index)
    monthly = cleaned.resample("ME").apply(
        lambda window: (1.0 + window).prod() - 1.0 if len(window) else float("nan")
    )
    return monthly.dropna()


def _last_or_nan(series: pd.Series) -> float:
    """Return the last value of ``series`` (possibly NaN), or NaN if empty."""
    if series.empty:
        return float("nan")
    return float(series.iloc[-1])


def _latest_bucket_weights(
    rows: Sequence[Any],
    bucket_getter: Callable[[Any], str],
) -> dict[str, float]:
    """Build ``{bucket: weight_pct}`` for the latest ``as_of_date`` snapshot.

    Args:
        rows: Weight DTOs each carrying ``as_of_date`` and ``weight_pct``
            plus a bucket attribute selected by ``bucket_getter``.
            ``list_for_investment`` returns the full time-series; the
            most-recent ``as_of_date`` is selected here.
        bucket_getter: Extracts the bucket label from a row.

    Returns:
        The latest snapshot as a mapping; empty dict when ``rows`` is
        empty.
    """
    if not rows:
        return {}
    latest = max(row.as_of_date for row in rows)
    return {bucket_getter(row): float(row.weight_pct) for row in rows if row.as_of_date == latest}


def _latest_bond_metrics(
    bond_analytics: pd.DataFrame,
) -> tuple[float | None, float | None, float | None]:
    """Latest ``(ytm, eff_duration, oas)`` from a bond-analytics frame.

    Each figure is ``None`` when the frame is empty or the latest row's
    value is NaN (e.g. a government bond carries no OAS).
    """
    if bond_analytics.empty:
        return None, None, None
    latest = bond_analytics.sort_index().iloc[-1]
    ytm = float(latest["ytm"]) if pd.notna(latest["ytm"]) else None
    eff_duration = float(latest["eff_duration"]) if pd.notna(latest["eff_duration"]) else None
    oas = float(latest["oas"]) if pd.notna(latest["oas"]) else None
    return ytm, eff_duration, oas


__all__ = ["ArchetypeChartsService"]
