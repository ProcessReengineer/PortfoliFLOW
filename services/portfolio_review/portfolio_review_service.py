# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""PortfolioReviewService — orchestrator for the Portfolio Review page.

Sub-stream 5e of the Phase-5 web migration. Loads the active
investment universe, fetches NAV histories, cashflow histories, and
country / sector weights, then runs the six aggregation functions in
:mod:`services.analytics.portfolio_aggregation` to produce a
:class:`PortfolioOverviewBundle` (3×2 portfolio tile grid) or a
:class:`SingleInvestmentReviewBundle` (3×2 single-investment tile
grid).

Per ADR-0045 §3 the analytics layer is pure; the database fan-out
and cross-investment composition lives here. Cross-tenant safety is
enforced by the active tenant context (RLS hides foreign-tenant
rows).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as _date
from uuid import UUID

import pandas as pd

from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowDTO,
    InvestmentCashflowRepository,
)
from core.repositories.investment_nav_repository import (
    InvestmentNavDTO,
    InvestmentNavRepository,
)
from core.repositories.investment_region_weights_repository import (
    InvestmentRegionWeightsRepository,
    RegionWeightDTO,
)
from core.repositories.investment_repository import (
    InvestmentDTO,
    InvestmentRepository,
)
from core.repositories.investment_sector_weights_repository import (
    InvestmentSectorWeightsRepository,
    SectorWeightDTO,
)
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.region_repository import RegionDTO, RegionRepository
from core.repositories.sector_repository import SectorDTO, SectorRepository
from core.repositories.tenant_repository import TenantRepository
from services.analytics.investment_returns import compute_total_return_series
from services.analytics.portfolio_aggregation import (
    CurrencyExposure,
    FundCompositionBreakdown,
    InvestedCapitalNavSeries,
    PortfolioCashflowSeries,
    PortfolioMultiplesSeries,
    RegionBreakdown,
    SectorBreakdown,
    VintageDistribution,
    aggregate_currency_exposure,
    aggregate_fund_composition,
    aggregate_invested_capital_and_nav,
    aggregate_portfolio_cashflows,
    aggregate_portfolio_multiples,
    aggregate_region_breakdown,
    aggregate_sector_breakdown,
    aggregate_vintage_distribution,
    compute_total_return_index_series,
)
from services.fx.functional_currency import (
    PortfolioFxConverter,
    build_portfolio_fx_converter,
)
from services.reporting.data_providers._calculations import compute_irr

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Header / bundle dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioHeaderMetrics:
    """Four scalar headline metrics for the Portfolio Overview header.

    Every monetary figure here is expressed in the tenant's functional
    currency (ADR-0099 §4): the per-investment NAV and cashflow series are
    converted from their position currencies before aggregation, so
    ``nav_eur`` / ``tvpi`` / ``dpi`` include the FX effect. The legacy
    ``nav_eur`` field name is retained for this block — the ``*_eur`` →
    ``*_functional`` rename is deferred (ADR-0099 §Follow-ups, Block 5).

    Attributes:
        nav_eur: Total portfolio NAV at the resolved as-of date, in the
            functional currency, over the **full** universe — explicit
            cash positions included (ADR-0100 §3), so this figure feeds
            the front-office residual ``aum − Σ NAV``. ``None`` when no
            investment has a NAV at-or-before that date.
        irr: Portfolio IRR-since-inception as a decimal, over the
            **performance** universe (cash excluded, ADR-0100 §4).
            ``None`` when the root finder cannot converge.
        tvpi: Aggregate TVPI multiple, performance universe (cash
            excluded). ``None`` when no capital has been called.
        dpi: Aggregate DPI multiple, performance universe (cash
            excluded). ``None`` when no capital has been called.
        functional_currency: The tenant's functional currency — the
            currency every monetary figure above is denominated in. Data
            only in this block; no template or label consumes it yet
            (Block 5).
    """

    nav_eur: float | None
    irr: float | None
    tvpi: float | None
    dpi: float | None
    functional_currency: str


@dataclass(frozen=True)
class InvestmentHeaderMetrics:
    """Four scalar headline metrics for the Single-Investment Review header.

    Same shape as :class:`PortfolioHeaderMetrics` — the dataclass is
    duplicated to make the API self-documenting at the call site.

    Attributes:
        nav_eur: Latest NAV for the investment.
        irr: IRR-since-inception for the investment.
        tvpi: TVPI multiple for the investment.
        dpi: DPI multiple for the investment.
    """

    nav_eur: float | None
    irr: float | None
    tvpi: float | None
    dpi: float | None


@dataclass(frozen=True)
class CashPositionRow:
    """One explicit foreign-currency cash balance (ADR-0100 / ADR-0101 §2).

    The only place in the system where a **pre-conversion** monetary value
    survives the ADR-0099 §4 boundary — and it does so deliberately. The
    seam's purpose is that position-currency values stop circulating; a
    cash balance is the one thing a treasurer reads in its native
    denomination ("we hold 500k USD"), so the native figure is captured
    *at* the seam as a side-map rather than re-derived downstream through a
    second conversion path.

    Rows exist only for ``investment_type == 'cash'`` investments whose
    position currency differs from the functional currency. Same-currency
    cash needs no FX card: the native and functional figures coincide.

    Attributes:
        name: The investment's display name (e.g. ``"Cash USD"``).
        currency: The position currency the balance is held in.
        native_balance: The balance **in its own currency** — the
            pre-conversion latest NAV.
        functional_value: The same balance converted into the tenant's
            functional currency (the post-conversion latest NAV). The
            implied rate is ``functional_value / native_balance``,
            derivable in presentation; no converter API is exposed.
        as_of_date: The NAV as-of date the balance was observed on. Not
            the report's as-of date — a stale cash NAV must say so.
    """

    name: str
    currency: str
    native_balance: float
    functional_value: float
    as_of_date: _date


@dataclass(frozen=True)
class PortfolioOverviewBundle:
    """Pre-computed bundle for the Portfolio Overview surface.

    Attributes:
        header_metrics: NAV / IRR / TVPI / DPI strip values.
        invested_capital_nav: Tile 1 — yearly invested capital and NAV.
        cashflows: Tile 2 — yearly calls / distributions / NAV / NCG.
        multiples: Tile 3 — yearly DPI / RVPI / TVPI / IRR.
        region_breakdown: Tile 4 — NAV-weighted region split.
        vintage_distribution: Tile 5 — NAV-weighted vintage shares.
        sector_breakdown: Tile 6 — NAV-weighted sector split.
        as_of_date: Resolved as-of date for the report.
        investment_count: Number of investments included.
        fund_composition: NAV-weighted composition by individual fund
            (the Front-Office Overview Pareto, ADR-0072). Defaults to
            an empty breakdown.
        currency_exposure: NAV share by position currency over the full
            universe (the Front-Office Overview donut, ADR-0101 §1).
            Defaults to an empty exposure.
        cash_positions: Explicit foreign-currency cash balances with their
            native amounts (ADR-0101 §2). Empty for a tenant whose cash —
            or whose whole universe — is in the functional currency.
    """

    header_metrics: PortfolioHeaderMetrics
    invested_capital_nav: InvestedCapitalNavSeries
    cashflows: PortfolioCashflowSeries
    multiples: PortfolioMultiplesSeries
    region_breakdown: RegionBreakdown
    vintage_distribution: VintageDistribution
    sector_breakdown: SectorBreakdown
    as_of_date: _date
    investment_count: int
    fund_composition: FundCompositionBreakdown = field(
        default_factory=lambda: FundCompositionBreakdown(rows=[])
    )
    currency_exposure: CurrencyExposure = field(default_factory=lambda: CurrencyExposure(rows=[]))
    cash_positions: list[CashPositionRow] = field(default_factory=list)


@dataclass(frozen=True)
class SingleInvestmentReviewBundle:
    """Pre-computed bundle for a Single Investment Review.

    Attributes:
        header_metrics: NAV / IRR / TVPI / DPI strip values for the
            single investment.
        invested_capital_nav: Tile 1 — yearly invested capital and NAV.
        cashflows: Tile 2 — yearly cashflow buckets.
        multiples: Tile 3 — yearly DPI / RVPI / TVPI / IRR.
        total_return_index: Tile 4 — daily cumulative-return index
            ``cumprod(1+r) * 100`` rebased at inception.
        region_breakdown: Tile 5 — region split (the investment's
            own weight rows).
        sector_breakdown: Tile 6 — sector split.
        investment: The investment DTO (used by the template for the
            page header).
        as_of_date: Resolved as-of date.
    """

    header_metrics: InvestmentHeaderMetrics
    invested_capital_nav: InvestedCapitalNavSeries
    cashflows: PortfolioCashflowSeries
    multiples: PortfolioMultiplesSeries
    total_return_index: pd.Series
    region_breakdown: RegionBreakdown
    sector_breakdown: SectorBreakdown
    investment: InvestmentDTO
    as_of_date: _date


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PortfolioReviewService:
    """Aggregator for both Portfolio Review surfaces.

    Every repository must be tenant-scoped (the caller obtains them
    via :func:`core.repositories.tenant_context`). The service does
    not set or read ``app.tenant_id`` itself.
    """

    def __init__(
        self,
        investments: InvestmentRepository,
        navs: InvestmentNavRepository,
        cashflows: InvestmentCashflowRepository,
        region_weights: InvestmentRegionWeightsRepository,
        sector_weights: InvestmentSectorWeightsRepository,
        regions: RegionRepository,
        sectors: SectorRepository,
        tenants: TenantRepository,
        fx_rates: FxRateRepository,
    ) -> None:
        self._investments = investments
        self._navs = navs
        self._cashflows = cashflows
        self._region_weights = region_weights
        self._sector_weights = sector_weights
        self._regions = regions
        self._sectors = sectors
        self._tenants = tenants
        self._fx_rates = fx_rates

    # ------------------------------------------------------------------
    # Portfolio Overview
    # ------------------------------------------------------------------

    async def get_portfolio_overview(
        self,
        as_of_date: _date | None = None,
        *,
        investment_ids: list[UUID] | None = None,
    ) -> PortfolioOverviewBundle | None:
        """Build a :class:`PortfolioOverviewBundle` for the active tenant.

        Args:
            as_of_date: As-of date for the report. ``None`` resolves to
                the latest activity date observed across the universe
                — practical default for a freshly imported tenant.
            investment_ids: Optional UUID filter. ``None`` includes
                every active investment.

        Returns:
            :class:`PortfolioOverviewBundle` when at least one
            investment with NAV or cashflow data exists. ``None``
            when the universe is empty (the route renders an empty-
            state page).
        """
        investments = await self._resolve_universe(investment_ids)
        if not investments:
            _LOG.debug("PortfolioReviewService: empty universe for portfolio overview.")
            return None

        investment_ids = [inv.id for inv in investments]
        nav_rows_by_inv = await self._navs.list_by_investments_and_kind(investment_ids, "actual")
        cf_rows_by_inv = await self._cashflows.list_by_investments_and_kind(
            investment_ids, "actual"
        )

        # ADR-0099 §4 conversion boundary. Build one converter per request
        # from the position currencies actually present; a single-currency
        # tenant gets the identity pass-through and reads zero FX rows.
        fx = await build_portfolio_fx_converter(
            tenants=self._tenants,
            fx_rates=self._fx_rates,
            position_currencies=[inv.currency for inv in investments],
        )

        nav_history_by_inv: dict[UUID, pd.Series] = {}
        cashflows_by_inv: dict[UUID, pd.DataFrame] = {}
        nav_by_inv: dict[UUID, float] = {}
        cf_in_by_inv: dict[UUID, pd.Series] = {}
        cf_out_by_inv: dict[UUID, pd.Series] = {}
        # ADR-0101 §2 side-map: the latest NAV in its *position* currency,
        # plus the date it was observed on, captured before conversion runs.
        # Only the FX-cash card reads it. This is a read of a value the seam
        # already holds — not a second conversion path.
        native_latest_nav: dict[UUID, tuple[float, _date]] = {}
        for inv in investments:
            (
                nav_series,
                cf_frame,
                latest_nav_v,
                cf_in_series,
                cf_out_series,
            ) = self._build_investment_series(
                nav_rows_by_inv.get(inv.id, []),
                cf_rows_by_inv.get(inv.id, []),
                as_of_date,
            )
            if not nav_series.empty:
                native_latest_nav[inv.id] = (
                    latest_nav_v,
                    pd.Timestamp(nav_series.index[-1]).date(),
                )
            # Convert every monetary series for this investment from its
            # position currency into the functional currency before it
            # enters any aggregation input. Point-in-time: each NAV at its
            # own date's rate, each flow at its flow date's rate — so the
            # portfolio IRR / TVPI / DPI include the FX effect (ADR-0099 §4).
            nav_series = fx.convert_series(nav_series, inv.currency)
            cf_frame = self._convert_cashflow_frame(fx, cf_frame, inv.currency)
            cf_in_series = fx.convert_series(cf_in_series, inv.currency)
            cf_out_series = fx.convert_series(cf_out_series, inv.currency)
            # Re-derive the latest-NAV scalar from the *converted* series so
            # it converts at the date it was observed, not at as_of_date;
            # carry-forward makes these coincide when a rate exists at the
            # NAV date.
            latest_nav_v = float(nav_series.iloc[-1]) if not nav_series.empty else 0.0
            nav_history_by_inv[inv.id] = nav_series
            cashflows_by_inv[inv.id] = cf_frame
            nav_by_inv[inv.id] = latest_nav_v
            cf_in_by_inv[inv.id] = cf_in_series
            cf_out_by_inv[inv.id] = cf_out_series

        resolved_as_of = self._resolve_as_of(as_of_date, nav_history_by_inv, cashflows_by_inv)

        # ADR-0100 §4 performance/full universe split. Cash positions are
        # part of Σ NAV (headline, residual, composition) but must NOT enter
        # the private-markets performance metrics: the residual never fed
        # IRR/TVPI/DPI under ADR-0055, and an explicit cash NAV with zero
        # flows would distort them now. The filter lives *here*, at the
        # ADR-0099 §4 data-assembly seam — the pure analytics functions stay
        # untouched; we simply hand them the cash-free frames.
        performance_investments = [inv for inv in investments if inv.investment_type != "cash"]
        performance_ids = {inv.id for inv in performance_investments}
        nav_history_perf = {
            iid: s for iid, s in nav_history_by_inv.items() if iid in performance_ids
        }
        cashflows_perf = {iid: f for iid, f in cashflows_by_inv.items() if iid in performance_ids}
        cf_in_perf = {iid: s for iid, s in cf_in_by_inv.items() if iid in performance_ids}
        cf_out_perf = {iid: s for iid, s in cf_out_by_inv.items() if iid in performance_ids}
        nav_by_inv_perf = {iid: v for iid, v in nav_by_inv.items() if iid in performance_ids}

        invested_capital_nav = aggregate_invested_capital_and_nav(
            performance_investments,
            nav_history_perf,
            cashflows_perf,
            report_date=resolved_as_of,
        )
        cashflows = aggregate_portfolio_cashflows(
            cashflows_perf,
            nav_history_perf,
            report_date=resolved_as_of,
        )
        multiples = aggregate_portfolio_multiples(
            cashflows_perf,
            nav_history_perf,
            report_date=resolved_as_of,
        )

        region_weights_by_inv = await self._load_region_weights_for([inv.id for inv in investments])
        sector_weights_by_inv = await self._load_sector_weights_for([inv.id for inv in investments])
        regions_by_id = await self._load_region_lookups(region_weights_by_inv)
        sectors_by_id = await self._load_sector_lookups(sector_weights_by_inv)

        region_breakdown = aggregate_region_breakdown(
            investments,
            region_weights_by_inv,
            nav_by_inv,
            regions_by_id,
        )
        sector_breakdown = aggregate_sector_breakdown(
            investments,
            sector_weights_by_inv,
            nav_by_inv,
            sectors_by_id,
        )
        vintage_distribution = aggregate_vintage_distribution(investments, nav_by_inv)
        fund_composition = aggregate_fund_composition(
            investments,
            nav_by_inv,
            cf_in_by_inv,
            cf_out_by_inv,
            resolved_as_of,
        )
        # ADR-0101 §1: exposure by denomination over the *full* universe —
        # cash included, since foreign-currency cash is FX exposure in its
        # purest form. The NAVs are already converted, the grouping key is
        # the position currency.
        currency_exposure = aggregate_currency_exposure(investments, nav_by_inv)
        cash_positions = self._build_cash_positions(
            investments,
            native_latest_nav,
            nav_by_inv,
            fx.functional_currency,
        )

        # Header sharp edge (ADR-0100 §4): the headline NAV sums the *full*
        # universe (cash included — it feeds the front-office residual,
        # §3), while IRR / TVPI / DPI take the *performance* universe's flows
        # and NAV total so they stay continuous with their pre-cash meaning.
        header_metrics = self._build_portfolio_header(
            cf_in_perf,
            cf_out_perf,
            nav_by_inv_perf,
            resolved_as_of,
            fx.functional_currency,
            full_nav_by_inv=nav_by_inv,
        )

        _LOG.debug(
            "PortfolioReviewService.get_portfolio_overview: n=%d as_of=%s nav=%s irr=%s",
            len(investments),
            resolved_as_of,
            header_metrics.nav_eur,
            header_metrics.irr,
        )

        return PortfolioOverviewBundle(
            header_metrics=header_metrics,
            invested_capital_nav=invested_capital_nav,
            cashflows=cashflows,
            multiples=multiples,
            region_breakdown=region_breakdown,
            vintage_distribution=vintage_distribution,
            sector_breakdown=sector_breakdown,
            as_of_date=resolved_as_of,
            investment_count=len(investments),
            fund_composition=fund_composition,
            currency_exposure=currency_exposure,
            cash_positions=cash_positions,
        )

    # ------------------------------------------------------------------
    # Single Investment Review
    # ------------------------------------------------------------------

    async def get_single_investment_review(
        self,
        investment_id: UUID,
        as_of_date: _date | None = None,
    ) -> SingleInvestmentReviewBundle | None:
        """Build a :class:`SingleInvestmentReviewBundle` for one investment.

        Args:
            investment_id: The investment to review.
            as_of_date: As-of date.

        Returns:
            :class:`SingleInvestmentReviewBundle` when the investment
            exists in the active tenant. ``None`` when RLS hides the
            row (cross-tenant access) or the id is unknown.
        """
        investment = await self._investments.get_by_id(investment_id)
        if investment is None:
            _LOG.debug(
                "PortfolioReviewService.get_single_investment_review: "
                "investment %s absent in active tenant.",
                investment_id,
            )
            return None

        (
            nav_series,
            cf_frame,
            latest_nav_v,
            cf_in_series,
            cf_out_series,
        ) = await self._load_investment_series(investment.id, as_of_date)

        nav_history_by_inv = {investment.id: nav_series}
        cashflows_by_inv = {investment.id: cf_frame}
        nav_by_inv = {investment.id: latest_nav_v}

        resolved_as_of = self._resolve_as_of(as_of_date, nav_history_by_inv, cashflows_by_inv)

        invested_capital_nav = aggregate_invested_capital_and_nav(
            [investment],
            nav_history_by_inv,
            cashflows_by_inv,
            report_date=resolved_as_of,
        )
        cashflows = aggregate_portfolio_cashflows(
            cashflows_by_inv,
            nav_history_by_inv,
            report_date=resolved_as_of,
        )
        multiples = aggregate_portfolio_multiples(
            cashflows_by_inv,
            nav_history_by_inv,
            report_date=resolved_as_of,
        )

        # Total Return index — uses NAV pct_change directly.
        return_series = compute_total_return_series(nav_series)
        total_return_index = compute_total_return_index_series(return_series)

        region_weights_by_inv = await self._load_region_weights_for([investment.id])
        sector_weights_by_inv = await self._load_sector_weights_for([investment.id])
        regions_by_id = await self._load_region_lookups(region_weights_by_inv)
        sectors_by_id = await self._load_sector_lookups(sector_weights_by_inv)
        region_breakdown = aggregate_region_breakdown(
            [investment],
            region_weights_by_inv,
            nav_by_inv,
            regions_by_id,
        )
        sector_breakdown = aggregate_sector_breakdown(
            [investment],
            sector_weights_by_inv,
            nav_by_inv,
            sectors_by_id,
        )

        header_metrics = self._build_investment_header(
            cf_in_series, cf_out_series, latest_nav_v, resolved_as_of
        )

        return SingleInvestmentReviewBundle(
            header_metrics=header_metrics,
            invested_capital_nav=invested_capital_nav,
            cashflows=cashflows,
            multiples=multiples,
            total_return_index=total_return_index,
            region_breakdown=region_breakdown,
            sector_breakdown=sector_breakdown,
            investment=investment,
            as_of_date=resolved_as_of,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_universe(self, investment_ids: list[UUID] | None) -> list[InvestmentDTO]:
        """Return the investments included in the report."""
        if investment_ids is None:
            return await self._investments.list_active()
        wanted = set(investment_ids)
        all_active = await self._investments.list_active()
        return [inv for inv in all_active if inv.id in wanted]

    async def _load_investment_series(
        self,
        investment_id: UUID,
        as_of_date: _date | None,
    ) -> tuple[pd.Series, pd.DataFrame, float, pd.Series, pd.Series]:
        """Load NAV and cashflow series for one investment.

        Singular-investment counterpart kept for
        :meth:`get_single_investment_review`. The
        :meth:`get_portfolio_overview` path uses the batched
        :meth:`_build_investment_series` instead — see P6-H.
        """
        nav_rows = await self._navs.list_by_investment_and_kind(investment_id, "actual")
        cf_rows = await self._cashflows.list_by_investment_and_kind(investment_id, "actual")
        return self._build_investment_series(nav_rows, cf_rows, as_of_date)

    @staticmethod
    def _build_investment_series(
        nav_rows: list[InvestmentNavDTO],
        cf_rows: list[InvestmentCashflowDTO],
        as_of_date: _date | None,
    ) -> tuple[pd.Series, pd.DataFrame, float, pd.Series, pd.Series]:
        """Pure transform from raw repo rows to aggregation-ready series.

        Separated from :meth:`_load_investment_series` so the
        batched-fetch path in :meth:`get_portfolio_overview` can
        reuse the same transform without duplicating it.

        Returns the per-investment data shapes the aggregation
        functions need:

        - ``nav_series`` — date-indexed actual NAV history (truncated
          at ``as_of_date``).
        - ``cf_frame`` — flat ``flow_timestamp`` / ``amount`` actuals
          frame.
        - ``latest_nav`` — single scalar (latest NAV at-or-before the
          as-of date), used by the country / sector / vintage
          aggregations.
        - ``cf_in_series`` / ``cf_out_series`` — split actuals for the
          IRR helper.
        """
        if as_of_date is not None:
            nav_rows = [n for n in nav_rows if n.as_of_date <= as_of_date]

        nav_series = pd.Series(
            data=[float(n.nav_value) for n in nav_rows],
            index=pd.to_datetime([n.as_of_date for n in nav_rows]),
            dtype="float64",
        ).sort_index()

        if as_of_date is not None:
            cutoff_ts = pd.Timestamp(as_of_date, tz="UTC")
            filtered: list = []
            for c in cf_rows:
                ts = pd.Timestamp(c.flow_timestamp)
                ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
                if ts <= cutoff_ts:
                    filtered.append(c)
            cf_rows = filtered

        cf_frame = pd.DataFrame(
            {
                "flow_timestamp": [c.flow_timestamp for c in cf_rows],
                "amount": [float(c.amount) for c in cf_rows],
            }
        )

        latest_nav_v = float(nav_series.iloc[-1]) if not nav_series.empty else 0.0

        # Build ``cf_in`` / ``cf_out`` series shaped like the QT
        # provider input: positive amounts → cf_in, negative amounts
        # → cf_out (the IRR helper internally negates the magnitude
        # of cf_out).
        if not cf_frame.empty:
            df = cf_frame.copy()
            df["flow_timestamp"] = pd.to_datetime(df["flow_timestamp"], utc=True).dt.normalize()
            df["amount"] = df["amount"].astype("float64")
            cf_in_series = (
                df.loc[df["amount"] > 0.0, ["flow_timestamp", "amount"]]
                .groupby("flow_timestamp")["amount"]
                .sum()
                .sort_index()
            )
            cf_out_series = (
                df.loc[df["amount"] < 0.0, ["flow_timestamp", "amount"]]
                .groupby("flow_timestamp")["amount"]
                .sum()
                .sort_index()
            )
        else:
            empty_idx = pd.DatetimeIndex([], tz="UTC")
            cf_in_series = pd.Series(dtype="float64", index=empty_idx)
            cf_out_series = pd.Series(dtype="float64", index=empty_idx)

        return nav_series, cf_frame, latest_nav_v, cf_in_series, cf_out_series

    @staticmethod
    def _convert_cashflow_frame(
        fx: PortfolioFxConverter,
        cf_frame: pd.DataFrame,
        from_currency: str,
    ) -> pd.DataFrame:
        """Convert a cashflow frame's ``amount`` column point-in-time.

        Each flow converts at the carry-forward rate of its own
        ``flow_timestamp`` (ADR-0099 §4) — the property that lets the
        portfolio IRR / TVPI / DPI include the FX effect. Row order and the
        ``flow_timestamp`` column are untouched; only ``amount`` is
        restated. An empty frame is returned as an unchanged copy.
        """
        if cf_frame.empty:
            return cf_frame.copy()
        amounts = pd.Series(
            cf_frame["amount"].to_numpy(dtype="float64"),
            index=pd.to_datetime(cf_frame["flow_timestamp"]),
        )
        converted = fx.convert_series(amounts, from_currency)
        out = cf_frame.copy()
        out["amount"] = converted.to_numpy(dtype="float64")
        return out

    async def _load_region_weights_for(
        self, investment_ids: list[UUID]
    ) -> dict[UUID, list[RegionWeightDTO]]:
        """Load the latest region-weight snapshot per investment (batched).

        Per ADR-0080 §4 the composition surface reads "the" weights as
        the single most-recent historised snapshot; full-history
        readers are reserved for the forthcoming drift / attribution
        surfaces.
        """
        return await self._region_weights.list_latest_by_investments(investment_ids)

    async def _load_sector_weights_for(
        self, investment_ids: list[UUID]
    ) -> dict[UUID, list[SectorWeightDTO]]:
        """Load the latest sector-weight snapshot per investment (batched).

        Per ADR-0080 §4 — see :meth:`_load_region_weights_for`.
        """
        return await self._sector_weights.list_latest_by_investments(investment_ids)

    async def _load_region_lookups(
        self,
        region_weights_by_inv: dict[UUID, list[RegionWeightDTO]],
    ) -> dict[UUID, RegionDTO]:
        """Resolve region DTOs for every region_id present in the weights."""
        unique_ids: set[UUID] = set()
        for rows in region_weights_by_inv.values():
            for w in rows:
                unique_ids.add(w.region_id)
        result: dict[UUID, RegionDTO] = {}
        for rid in unique_ids:
            region = await self._regions.get_by_id(rid)
            if region is not None:
                result[rid] = region
        return result

    async def _load_sector_lookups(
        self,
        sector_weights_by_inv: dict[UUID, list[SectorWeightDTO]],
    ) -> dict[UUID, SectorDTO]:
        """Resolve sector DTOs for every sector_id present in the weights."""
        unique_ids: set[UUID] = set()
        for rows in sector_weights_by_inv.values():
            for w in rows:
                unique_ids.add(w.sector_id)
        result: dict[UUID, SectorDTO] = {}
        for sid in unique_ids:
            sector = await self._sectors.get_by_id(sid)
            if sector is not None:
                result[sid] = sector
        return result

    @staticmethod
    def _build_cash_positions(
        investments: list[InvestmentDTO],
        native_latest_nav: dict[UUID, tuple[float, _date]],
        nav_by_inv: dict[UUID, float],
        functional_currency: str,
    ) -> list[CashPositionRow]:
        """Pair each foreign-currency cash balance's native and converted NAV.

        The ADR-0101 §2 card's whole content. Both figures are already in
        hand — the native one from the pre-conversion side-map, the
        functional one from the converted NAV map the aggregations use — so
        this is a join, not a computation, and in particular not a second
        conversion.

        Included: ``investment_type == 'cash'`` (ADR-0100) **and** a
        position currency differing from the functional one **and** a
        positive balance. Functional-currency cash is excluded because
        native and functional coincide — there is nothing for the card to
        tell the reader, and rendering it would break the §4 invisibility
        guarantee for a EUR tenant holding EUR cash.

        Args:
            investments: The full universe.
            native_latest_nav: Pre-conversion ``(latest NAV, as-of date)``
                per investment, captured at the seam.
            nav_by_inv: Post-conversion latest NAV per investment.
            functional_currency: The tenant's functional currency.

        Returns:
            Rows sorted by functional value descending — the biggest FX cash
            balance reads first. Empty when no such position exists.
        """
        rows: list[CashPositionRow] = []
        for inv in investments:
            if inv.investment_type != "cash":
                continue
            if inv.currency == functional_currency:
                continue
            native = native_latest_nav.get(inv.id)
            if native is None:
                continue
            native_balance, nav_as_of = native
            if native_balance <= 0.0:
                continue
            rows.append(
                CashPositionRow(
                    name=inv.name,
                    currency=inv.currency,
                    native_balance=native_balance,
                    functional_value=nav_by_inv.get(inv.id, 0.0),
                    as_of_date=nav_as_of,
                )
            )
        rows.sort(key=lambda r: r.functional_value, reverse=True)
        return rows

    @staticmethod
    def _resolve_as_of(
        as_of_date: _date | None,
        nav_history_by_inv: dict[UUID, pd.Series],
        cashflows_by_inv: dict[UUID, pd.DataFrame],
    ) -> _date:
        """Resolve the as-of date for the report.

        - If ``as_of_date`` is supplied, that's the answer.
        - Otherwise, take the latest of (a) any NAV index entry, (b)
          any cashflow timestamp.
        - If both are empty, fall back to today (the bundle will be
          empty, but consumers still expect a date).
        """
        if as_of_date is not None:
            return as_of_date

        candidates: list[pd.Timestamp] = []
        for s in nav_history_by_inv.values():
            if s is None or s.empty:
                continue
            ts = pd.Timestamp(s.index.max())
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
            candidates.append(ts)
        for df in cashflows_by_inv.values():
            if df is None or df.empty:
                continue
            ts = pd.to_datetime(df["flow_timestamp"], utc=True)
            candidates.append(pd.Timestamp(ts.max()))
        if not candidates:
            return _date.today()
        latest = max(candidates)
        return latest.tz_convert("UTC").date()

    @staticmethod
    def _build_portfolio_header(
        cf_in_by_inv: dict[UUID, pd.Series],
        cf_out_by_inv: dict[UUID, pd.Series],
        nav_by_inv: dict[UUID, float],
        as_of_date: _date,
        functional_currency: str = "EUR",
        *,
        full_nav_by_inv: dict[UUID, float] | None = None,
    ) -> PortfolioHeaderMetrics:
        """Compute the four headline scalars for the portfolio header.

        Every input series is already in ``functional_currency`` (converted
        at the ADR-0099 §4 boundary), so the scalars carry the currency
        through unchanged as metadata. ``functional_currency`` defaults to
        the reference ``"EUR"`` for direct static-method callers (the
        QT-consistency test); :meth:`get_portfolio_overview` always passes
        the tenant's resolved functional currency explicitly.

        Args (ADR-0100 §4 sharp edge):
            cf_in_by_inv, cf_out_by_inv, nav_by_inv: the **performance**
                universe (cash excluded). ``nav_by_inv`` here is the IRR /
                TVPI terminal value and the DPI denominator's counterpart —
                it must not carry cash NAV, which has no flows and would
                distort the multiples.
            full_nav_by_inv: the **full** universe NAV map (cash included)
                for the headline ``nav_eur``, which feeds the front-office
                residual (§3: the residual shrinks because cash sits inside
                Σ NAV). Defaults to ``nav_by_inv`` — so a cash-free direct
                caller (the QT-consistency test) keeps the pre-ADR-0100
                behaviour where headline and performance NAV coincide.
        """
        headline_nav_by_inv = nav_by_inv if full_nav_by_inv is None else full_nav_by_inv
        if not headline_nav_by_inv and not nav_by_inv:
            return PortfolioHeaderMetrics(None, None, None, None, functional_currency)

        # Aggregate cf_in / cf_out across investments by union-then-sum.
        cf_in_total = _sum_series(cf_in_by_inv)
        cf_out_total = _sum_series(cf_out_by_inv)

        # Headline NAV: full universe (explicit cash rows included, §3).
        headline_nav_total = sum(v for v in headline_nav_by_inv.values() if v > 0.0)
        nav_eur = headline_nav_total if headline_nav_total > 0.0 else None

        # Performance NAV total: cash excluded (§4). Feeds the IRR terminal
        # value and the TVPI numerator so both stay continuous with their
        # pre-cash meaning.
        nav_total = sum(v for v in nav_by_inv.values() if v > 0.0)

        report_ts = pd.Timestamp(as_of_date, tz="UTC")
        calls_mag = float(cf_out_total.fillna(0.0).abs().sum()) if not cf_out_total.empty else 0.0
        dist = float(cf_in_total.fillna(0.0).sum()) if not cf_in_total.empty else 0.0

        if calls_mag <= 0.0:
            tvpi: float | None = None
            dpi: float | None = None
        else:
            tvpi = (dist + nav_total) / calls_mag
            dpi = dist / calls_mag

        if cf_in_total.empty and cf_out_total.empty:
            irr: float | None = None
        else:
            raw_irr = compute_irr(cf_in_total, cf_out_total, nav_total, report_ts)
            irr = raw_irr if raw_irr == raw_irr else None  # NaN-safe

        return PortfolioHeaderMetrics(
            nav_eur=nav_eur,
            irr=irr,
            tvpi=tvpi,
            dpi=dpi,
            functional_currency=functional_currency,
        )

    @staticmethod
    def _build_investment_header(
        cf_in_series: pd.Series,
        cf_out_series: pd.Series,
        latest_nav_v: float,
        as_of_date: _date,
    ) -> InvestmentHeaderMetrics:
        """Compute the four headline scalars for one investment."""
        nav_eur = latest_nav_v if latest_nav_v > 0.0 else None

        calls_mag = float(cf_out_series.fillna(0.0).abs().sum()) if not cf_out_series.empty else 0.0
        dist = float(cf_in_series.fillna(0.0).sum()) if not cf_in_series.empty else 0.0
        if calls_mag <= 0.0:
            tvpi: float | None = None
            dpi: float | None = None
        else:
            tvpi = (dist + latest_nav_v) / calls_mag
            dpi = dist / calls_mag

        report_ts = pd.Timestamp(as_of_date, tz="UTC")
        if cf_in_series.empty and cf_out_series.empty:
            irr: float | None = None
        else:
            raw_irr = compute_irr(cf_in_series, cf_out_series, latest_nav_v, report_ts)
            irr = raw_irr if raw_irr == raw_irr else None

        return InvestmentHeaderMetrics(nav_eur=nav_eur, irr=irr, tvpi=tvpi, dpi=dpi)


def _sum_series(series_by_inv: dict[UUID, pd.Series]) -> pd.Series:
    """Sum a mapping of date-indexed series via union-and-add.

    Handles empty inputs (returns an empty DatetimeIndex-backed
    series) and series whose indices don't overlap (zero-fill on
    the union).
    """
    non_empty = [s for s in series_by_inv.values() if s is not None and not s.empty]
    if not non_empty:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    union = non_empty[0].index
    for s in non_empty[1:]:
        union = union.union(s.index)
    union = union.sort_values()
    total = pd.Series(0.0, index=union, dtype="float64")
    for s in non_empty:
        total = total.add(s.reindex(union, fill_value=0.0), fill_value=0.0)
    return total


__all__ = [
    "CashPositionRow",
    "InvestmentHeaderMetrics",
    "PortfolioHeaderMetrics",
    "PortfolioOverviewBundle",
    "PortfolioReviewService",
    "SingleInvestmentReviewBundle",
]
