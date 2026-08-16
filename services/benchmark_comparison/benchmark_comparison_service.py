# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""BenchmarkComparisonService — orchestrator for the Benchmarks & Attribution section.

Sub-stream A12 Phase 1a orchestration layer for the Back Office
"Benchmarks & Attribution" section. Wires the pure analytics layer
(``services.analytics.benchmark_comparison``) to the persistence
layer (per-tenant repositories) and exposes three coarse-grained
methods aligned with the three blocks of the section:

  a) :meth:`get_investment_comparisons` — per-investment vs.
     benchmark metrics (Stage a, Block 1).
  b) :meth:`get_asset_class_composites` — NAV-weighted composites
     vs. benchmarks per asset class (Stage b, Block 2).
  c) :meth:`get_saa_hypothetical` — SAA-hypothetical comparison
     (Stage c, Block 3).

Per ADR-0045 §3 the analytics layer is pure and DB-free; the database
fan-out, Decimal→float conversion at the repository boundary, and
DTO composition for the template all live here. Cross-tenant safety
is enforced by the active tenant context (RLS hides foreign-tenant
rows).

Risk-free rate sourcing
-----------------------
The "interest rates" Excel sheet is not persisted to the DB in the
current schema (see ADR-0061; persistence is deferred to a follow-up
that lands a dedicated InterestRateRepository). For Phase 1 the
risk-free rate is sourced from the active SAA configuration's
``risk_free_rate`` column (annualised float). When no active SAA
configuration exists the rate defaults to ``0.0``. Monthly conversion
follows the standard identity
``r_m = (1 + r_ann) ** (days_in_month / 365) - 1``.

References:
    - ADR-0061 (Benchmarks & Attribution architecture)
    - ADR-0042 §2 (SAA optimization is recomputed, not persisted)
    - ADR-0045 §3 (Analytics service foundation)
"""

from __future__ import annotations

import calendar
import logging
import math
from dataclasses import dataclass
from datetime import date as _date
from typing import Literal
from uuid import UUID

import numpy as np
import pandas as pd

from core.repositories.asset_class_benchmark_mapping_repository import (
    AssetClassBenchmarkMappingRepository,
)
from core.repositories.asset_class_repository import (
    AssetClassDTO,
    AssetClassRepository,
)
from core.repositories.benchmark_observation_repository import (
    BenchmarkObservationDTO,
    BenchmarkObservationRepository,
)
from core.repositories.benchmark_repository import (
    BenchmarkDTO,
    BenchmarkRepository,
)
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_nav_repository import (
    InvestmentNavRepository,
)
from core.repositories.investment_repository import (
    InvestmentRepository,
)
from core.repositories.tenant_repository import TenantRepository
from services.analytics.benchmark_comparison import (
    AssetClassCompositeSeries,
    SAAHypotheticalSeries,
    compute_asset_class_composites,
    compute_benchmark_comparison,
    compute_saa_hypothetical_series,
)
from services.analytics.investment_returns import compute_total_return_series
from services.fx.functional_currency import (
    PortfolioFxConverter,
    build_portfolio_fx_converter,
)
from services.saa import SAAService, SAAValidationError

_LOG = logging.getLogger(__name__)

WeightSet = Literal["tangency", "min_var"]
# Note: a "target" weight set is deferred — the current SAA schema has
# no ``target_weight`` column on ``SAAAssetClassInput``. See ADR-0061
# §Rationale.

_MIN_OBSERVATIONS_FOR_RATIO_METRICS = 12


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvestmentBenchmarkRowDTO:
    """One row of the Investment-vs-Benchmark table (Stage a).

    Pre-formatted for direct consumption by the template. Numeric
    fields stay as floats (template applies number filters); strings
    are operator-facing labels.

    Attributes:
        investment_id: Stable identifier for the per-investment
            detail dropdown selector.
        investment_name: Display name.
        asset_class_code: Resolution for which benchmark is mapped.
        benchmark_id: Stable identifier of the mapped benchmark.
        benchmark_display_name: Operator-facing benchmark label.
        excess_return_annualised: Annualised arithmetic excess.
        alpha_annualised: Jensen alpha (annualised).
        beta: Regression beta.
        r_squared: R-squared of the excess-vs-excess regression.
        tracking_error_annualised: Annualised stddev of (r_i - r_b).
        information_ratio: excess_ann / tracking_error_ann.
        up_capture_ratio: Up-market capture.
        down_capture_ratio: Down-market capture.
        sharpe_investment: Sharpe ratio of the investment.
        sharpe_benchmark: Sharpe ratio of the benchmark.
        n_observations: Aligned monthly observation count.
        period_start_iso: ISO date string of the period start.
        period_end_iso: ISO date string of the period end.
    """

    investment_id: UUID
    investment_name: str
    asset_class_code: str
    benchmark_id: UUID
    benchmark_display_name: str
    excess_return_annualised: float
    alpha_annualised: float
    beta: float
    r_squared: float
    tracking_error_annualised: float
    information_ratio: float
    up_capture_ratio: float
    down_capture_ratio: float
    sharpe_investment: float
    sharpe_benchmark: float
    n_observations: int
    period_start_iso: str
    period_end_iso: str


@dataclass(frozen=True)
class InvestmentComparisonsBundle:
    """Bundle for Block 1 (Stage a) of the Benchmarks & Attribution section."""

    rows: list[InvestmentBenchmarkRowDTO]
    investments_without_benchmark: list[str]
    """Investment names whose asset class has no benchmark mapping."""
    as_of_date: _date


@dataclass(frozen=True)
class InvestmentComparisonDetailDTO:
    """Per-investment cumulative-return series for the Stage-a detail chart."""

    investment_name: str
    benchmark_display_name: str
    investment_cumulative_returns: pd.Series
    benchmark_cumulative_returns: pd.Series
    excess_cumulative_returns: pd.Series
    as_of_date: _date


@dataclass(frozen=True)
class AssetClassCompositeRowDTO:
    """One asset class's composite-vs-benchmark comparison (Stage b)."""

    asset_class_code: str
    asset_class_display_name: str
    benchmark_display_name: str
    composite_cumulative_returns: pd.Series
    benchmark_cumulative_returns: pd.Series
    n_investments: int
    excess_return_annualised: float
    information_ratio: float
    n_observations: int


@dataclass(frozen=True)
class AssetClassCompositesBundle:
    """Bundle for Block 2 (Stage b) of the Benchmarks & Attribution section."""

    rows: list[AssetClassCompositeRowDTO]
    asset_classes_without_benchmark: list[str]
    """Asset class display names that exist in the catalogue but have
    no benchmark mapping. Rendered as greyed-out placeholders."""
    as_of_date: _date


@dataclass(frozen=True)
class SAAConfigurationOptionDTO:
    """One entry in the SAA-configuration dropdown."""

    saa_configuration_id: UUID
    name: str
    is_active: bool


@dataclass(frozen=True)
class WeightSetOptionDTO:
    """One entry in the weight-set dropdown.

    ``available`` is False when the SAA optimization for the selected
    configuration cannot be run (e.g. fewer than two asset classes
    configured, missing inputs). The option is shown in the dropdown
    but greyed out with a hint explaining why. SAA optimizations are
    never persisted — every call to the service recomputes (per
    ADR-0042 §2) — so "availability" is determined by whether a fresh
    ``run_optimization`` call would succeed.
    """

    code: WeightSet
    display_name: str
    available: bool
    unavailable_hint: str | None


@dataclass(frozen=True, slots=True)
class PortfolioSummaryKPIs:
    """Aggregate KPIs derived from the Stage-a per-investment rows.

    Used by the Phase-1b Quick-Wins KPI strip rendered above Stage a.
    The median fields filter to investments with at least
    ``_MIN_OBSERVATIONS_FOR_RATIO_METRICS`` aligned monthly observations
    so very short histories do not dominate the centre tendency.

    Attributes:
        active_investments_count: Total active investments for the
            tenant.
        investments_with_benchmark_count: Subset whose asset class is
            mapped to a benchmark.
        investments_without_benchmark_count: Complement.
        median_excess_return_annualised: Median annualised arithmetic
            excess across investments with ``n_observations >= 12``;
            ``None`` when the filtered set is empty.
        hit_rate: Fraction of investments with positive excess across
            the same filtered set; ``None`` when the filtered set is
            empty.
        median_information_ratio: Median IR across the same filtered
            set; ``None`` when the filtered set is empty.
    """

    active_investments_count: int
    investments_with_benchmark_count: int
    investments_without_benchmark_count: int
    median_excess_return_annualised: float | None
    hit_rate: float | None
    median_information_ratio: float | None


@dataclass(frozen=True, slots=True)
class SAAHypotheticalEffects:
    """Cumulative endpoints and allocation-effect summary for Stage c.

    Computed at the service layer from the three monthly return
    series in :class:`SAAHypotheticalSeries`. The endpoints are the
    cumulative decimal returns at ``period_end`` (i.e. the final
    value of ``(1 + r).cumprod() - 1`` per series).

    The allocation effect is defined here as the *arithmetic*
    difference between the Actual cumulative return and the
    SAA × Benchmark cumulative return, expressed in percentage
    points (i.e. ``0.137`` → ``13.7`` pp). This is the practitioner-
    natural definition for a single-sentence headline; the full
    Brinson decomposition (Selection / Allocation / Interaction) is
    a Phase-2 deliverable per ADR-0061.

    Attributes:
        actual_cumulative_endpoint: Cumulative decimal return of the
            Actual series at ``period_end``. ``None`` if the series
            is empty.
        saa_x_benchmark_cumulative_endpoint: Same for SAA × Benchmark.
            ``None`` if the series is empty.
        saa_x_composite_cumulative_endpoint: Same for SAA × Composite.
            ``None`` if the series is empty.
        allocation_effect_pp: ``actual - saa_x_benchmark`` in
            percentage points. ``None`` if either operand is ``None``.
        selection_effect_pp: ``actual - saa_x_composite`` in
            percentage points. ``None`` if either operand is ``None``.
            Named "selection" because it isolates the effect of own-
            fund manager selection versus the SAA's intended composite.
    """

    actual_cumulative_endpoint: float | None
    saa_x_benchmark_cumulative_endpoint: float | None
    saa_x_composite_cumulative_endpoint: float | None
    allocation_effect_pp: float | None
    selection_effect_pp: float | None


@dataclass(frozen=True)
class SAAHypotheticalBundle:
    """Bundle for Block 3 (Stage c) of the section."""

    saa_configuration_options: list[SAAConfigurationOptionDTO]
    weight_set_options: list[WeightSetOptionDTO]
    selected_configuration_id: UUID | None
    selected_weight_set: WeightSet
    series: SAAHypotheticalSeries | None
    """``None`` when no configuration is selected, the configuration
    cannot be optimized, or the period has no aligned data."""
    effects: SAAHypotheticalEffects | None = None
    """Cumulative-endpoint and allocation-effect summary; ``None``
    whenever ``series`` is ``None`` (the two move together)."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class BenchmarkComparisonService:
    """Aggregator for the Benchmarks & Attribution section.

    Every repository must be tenant-scoped (the caller obtains them
    via :func:`core.repositories.tenant_context`). The service does
    not set or read ``app.tenant_id`` itself.

    Per ADR-0102 every NAV history this service turns into a return
    series is first converted into the tenant's functional currency at
    the ADR-0099 §4 boundary. This matters most here: a benchmark's
    observations are already stated in one currency, so comparing an
    *unconverted* foreign investment against it would score the
    currency's drift as manager alpha. Converting first makes excess
    return, beta and tracking error answer the question they claim to.
    """

    def __init__(
        self,
        investments: InvestmentRepository,
        navs: InvestmentNavRepository,
        asset_classes: AssetClassRepository,
        benchmarks: BenchmarkRepository,
        benchmark_observations: BenchmarkObservationRepository,
        mappings: AssetClassBenchmarkMappingRepository,
        saa_service: SAAService,
        tenants: TenantRepository,
        fx_rates: FxRateRepository,
    ) -> None:
        self._investments = investments
        self._navs = navs
        self._asset_classes = asset_classes
        self._benchmarks = benchmarks
        self._benchmark_observations = benchmark_observations
        self._mappings = mappings
        self._saa_service = saa_service
        self._tenants = tenants
        self._fx_rates = fx_rates

    # ------------------------------------------------------------------
    # Stage a — per-investment benchmark comparison
    # ------------------------------------------------------------------

    async def get_investment_comparisons(
        self,
        as_of_date: _date | None = None,
    ) -> InvestmentComparisonsBundle:
        """Compute Stage a metrics for every active investment.

        For each active investment:
          1. Resolve its asset class → benchmark mapping. Investments
             whose asset class has no benchmark mapping are skipped
             and listed in ``investments_without_benchmark``.
          2. Load the investment's NAV history (actual) and the
             benchmark's daily observations.
          3. Derive monthly returns for the investment (compounded
             from daily NAV pct_change) and for the benchmark
             (compounded from daily period returns).
          4. Build a monthly risk-free return series from the active
             SAA configuration's annualised rate.
          5. Call ``compute_benchmark_comparison`` and project the
             metrics into ``InvestmentBenchmarkRowDTO``.

        Args:
            as_of_date: Optional cut-off; defaults to today (UTC).

        Returns:
            Bundle. Rows are sorted alphabetically by
            ``investment_name`` for stable rendering.
        """
        resolved_as_of = as_of_date if as_of_date is not None else _date.today()

        investments = await self._investments.list_active()
        if not investments:
            return InvestmentComparisonsBundle(
                rows=[],
                investments_without_benchmark=[],
                as_of_date=resolved_as_of,
            )

        ac_lookup, benchmark_lookup, ac_to_benchmark = await self._load_benchmark_graph()
        risk_free_rate = await self._resolve_risk_free_rate()

        # ADR-0099 §4 conversion boundary (extended here by ADR-0102). One
        # converter for this method's investment set; a single-currency
        # universe gets the identity pass-through and reads zero FX rows.
        fx = await build_portfolio_fx_converter(
            tenants=self._tenants,
            fx_rates=self._fx_rates,
            position_currencies=[inv.currency for inv in investments],
        )

        rows: list[InvestmentBenchmarkRowDTO] = []
        without_benchmark: list[str] = []

        for inv in investments:
            ac = ac_lookup.get(inv.asset_class_id)
            mapped_benchmark_id = (
                ac_to_benchmark.get(inv.asset_class_id) if ac is not None else None
            )
            if ac is None or mapped_benchmark_id is None:
                without_benchmark.append(inv.name)
                continue
            benchmark = benchmark_lookup.get(mapped_benchmark_id)
            if benchmark is None:
                # Mapping references a benchmark that is no longer
                # in the catalogue — treat as unmapped from the
                # operator's perspective.
                without_benchmark.append(inv.name)
                continue

            investment_monthly = await self._load_investment_monthly_returns(
                inv.id, resolved_as_of, fx, inv.currency
            )
            benchmark_monthly = await self._load_benchmark_monthly_returns(
                mapped_benchmark_id, resolved_as_of
            )
            risk_free_monthly = _build_monthly_risk_free(investment_monthly.index, risk_free_rate)

            bundle = compute_benchmark_comparison(
                investment_returns=investment_monthly,
                benchmark_returns=benchmark_monthly,
                risk_free_returns=risk_free_monthly,
                investment_identifier=inv.name,
                benchmark_identifier=benchmark.display_name,
            )
            metrics = bundle.metrics

            rows.append(
                InvestmentBenchmarkRowDTO(
                    investment_id=inv.id,
                    investment_name=inv.name,
                    asset_class_code=ac.code,
                    benchmark_id=benchmark.id,
                    benchmark_display_name=benchmark.display_name,
                    excess_return_annualised=metrics.excess_return_annualised,
                    alpha_annualised=metrics.alpha_annualised,
                    beta=metrics.beta,
                    r_squared=metrics.r_squared,
                    tracking_error_annualised=metrics.tracking_error_annualised,
                    information_ratio=metrics.information_ratio,
                    up_capture_ratio=metrics.up_capture_ratio,
                    down_capture_ratio=metrics.down_capture_ratio,
                    sharpe_investment=metrics.sharpe_investment,
                    sharpe_benchmark=metrics.sharpe_benchmark,
                    n_observations=metrics.n_observations,
                    period_start_iso=metrics.period_start.isoformat(),
                    period_end_iso=metrics.period_end.isoformat(),
                )
            )

        rows.sort(key=lambda r: r.investment_name)
        without_benchmark.sort()
        return InvestmentComparisonsBundle(
            rows=rows,
            investments_without_benchmark=without_benchmark,
            as_of_date=resolved_as_of,
        )

    async def get_investment_comparison_detail(
        self,
        investment_id: UUID,
        as_of_date: _date | None = None,
    ) -> InvestmentComparisonDetailDTO | None:
        """Detail series for one investment in Block 1.

        Returns ``None`` when the investment is unknown to the active
        tenant, has no benchmark mapping, or no aligned observations.
        """
        resolved_as_of = as_of_date if as_of_date is not None else _date.today()

        investment = await self._investments.get_by_id(investment_id)
        if investment is None:
            return None

        _ac_lookup, benchmark_lookup, ac_to_benchmark = await self._load_benchmark_graph()
        mapped_benchmark_id = ac_to_benchmark.get(investment.asset_class_id)
        if mapped_benchmark_id is None:
            return None
        benchmark = benchmark_lookup.get(mapped_benchmark_id)
        if benchmark is None:
            return None

        risk_free_rate = await self._resolve_risk_free_rate()
        # This method's investment set is the single investment in scope.
        fx = await build_portfolio_fx_converter(
            tenants=self._tenants,
            fx_rates=self._fx_rates,
            position_currencies=[investment.currency],
        )
        investment_monthly = await self._load_investment_monthly_returns(
            investment.id, resolved_as_of, fx, investment.currency
        )
        benchmark_monthly = await self._load_benchmark_monthly_returns(
            mapped_benchmark_id, resolved_as_of
        )
        risk_free_monthly = _build_monthly_risk_free(investment_monthly.index, risk_free_rate)

        bundle = compute_benchmark_comparison(
            investment_returns=investment_monthly,
            benchmark_returns=benchmark_monthly,
            risk_free_returns=risk_free_monthly,
            investment_identifier=investment.name,
            benchmark_identifier=benchmark.display_name,
        )

        if bundle.aligned_investment_returns.empty:
            return None

        inv_cum = (1.0 + bundle.aligned_investment_returns).cumprod() - 1.0
        bench_cum = (1.0 + bundle.aligned_benchmark_returns).cumprod() - 1.0
        excess_cum = inv_cum - bench_cum

        return InvestmentComparisonDetailDTO(
            investment_name=investment.name,
            benchmark_display_name=benchmark.display_name,
            investment_cumulative_returns=inv_cum,
            benchmark_cumulative_returns=bench_cum,
            excess_cumulative_returns=excess_cum,
            as_of_date=resolved_as_of,
        )

    async def get_investment_benchmark_inputs(
        self,
        investment_id: UUID,
        as_of_date: _date | None = None,
    ) -> tuple[str, pd.Series, float] | None:
        """Resolve (benchmark_display_name, benchmark_monthly_returns, risk_free_rate).

        A reusable factorisation of the benchmark-loading half of
        :meth:`get_investment_comparison_detail`, so consumers that need
        the mapped benchmark series for an investment (e.g. the
        Front-Office archetype-charts assembly, ADR-0082) do not
        duplicate the mapping resolution and observation load. The
        mapping resolution and the
        :meth:`_load_benchmark_monthly_returns` /
        :meth:`_resolve_risk_free_rate` reads mirror
        :meth:`get_investment_comparison_detail` exactly; that method is
        left unchanged.

        Args:
            investment_id: The investment whose benchmark inputs to
                resolve.
            as_of_date: Optional cut-off applied to the benchmark
                observations; defaults to today (UTC).

        Returns:
            ``(benchmark_display_name, benchmark_monthly_returns,
            risk_free_rate)`` — the mapped benchmark's operator-facing
            label, its month-end-indexed monthly return series, and the
            active annualised risk-free rate.

            ``None`` when the investment is unknown to the active tenant
            or its asset class has no benchmark mapping — the same
            neutral cases :meth:`get_investment_comparison_detail`
            already handles (RLS hides cross-tenant rows, so an unknown
            investment surfaces as ``None``).
        """
        resolved_as_of = as_of_date if as_of_date is not None else _date.today()

        investment = await self._investments.get_by_id(investment_id)
        if investment is None:
            return None

        _, benchmark_lookup, ac_to_benchmark = await self._load_benchmark_graph()
        mapped_benchmark_id = ac_to_benchmark.get(investment.asset_class_id)
        if mapped_benchmark_id is None:
            return None
        benchmark = benchmark_lookup.get(mapped_benchmark_id)
        if benchmark is None:
            return None

        risk_free_rate = await self._resolve_risk_free_rate()
        benchmark_monthly = await self._load_benchmark_monthly_returns(
            mapped_benchmark_id, resolved_as_of
        )
        return benchmark.display_name, benchmark_monthly, risk_free_rate

    async def get_portfolio_summary_kpis(
        self,
        as_of_date: _date | None = None,
    ) -> PortfolioSummaryKPIs:
        """Compute aggregate KPIs across all investment-vs-benchmark rows.

        Builds on top of :meth:`get_investment_comparisons`. The median
        excess, hit rate, and median Information Ratio filter to rows
        with ``n_observations >= _MIN_OBSERVATIONS_FOR_RATIO_METRICS``
        (currently 12) so a few stub-length investments cannot dominate
        the centre tendency.

        Args:
            as_of_date: Optional cut-off; defaults to today (UTC).

        Returns:
            ``PortfolioSummaryKPIs``. The three count fields are always
            defined. The three central-tendency fields are ``None``
            when the filtered set is empty.
        """
        comparisons = await self.get_investment_comparisons(as_of_date=as_of_date)
        rows = comparisons.rows
        without_benchmark_count = len(comparisons.investments_without_benchmark)
        with_benchmark_count = len(rows)
        active_count = with_benchmark_count + without_benchmark_count

        eligible = [r for r in rows if r.n_observations >= _MIN_OBSERVATIONS_FOR_RATIO_METRICS]
        if not eligible:
            return PortfolioSummaryKPIs(
                active_investments_count=active_count,
                investments_with_benchmark_count=with_benchmark_count,
                investments_without_benchmark_count=without_benchmark_count,
                median_excess_return_annualised=None,
                hit_rate=None,
                median_information_ratio=None,
            )

        excesses = [r.excess_return_annualised for r in eligible]
        irs = [r.information_ratio for r in eligible]
        positive = sum(1 for x in excesses if x > 0)
        return PortfolioSummaryKPIs(
            active_investments_count=active_count,
            investments_with_benchmark_count=with_benchmark_count,
            investments_without_benchmark_count=without_benchmark_count,
            median_excess_return_annualised=float(np.median(excesses)),
            hit_rate=positive / len(eligible),
            median_information_ratio=float(np.median(irs)),
        )

    # ------------------------------------------------------------------
    # Stage b — asset-class composites
    # ------------------------------------------------------------------

    async def get_asset_class_composites(
        self,
        as_of_date: _date | None = None,
    ) -> AssetClassCompositesBundle:
        """Compute Stage b composites for every asset class with a benchmark.

        Asset classes that exist in the catalogue but lack a benchmark
        mapping (e.g. Cash) appear in ``asset_classes_without_benchmark``
        — the template renders placeholder tiles for them. Asset
        classes with a mapping but no own investments yet appear in
        ``rows`` with empty series and ``n_investments=0``.
        """
        resolved_as_of = as_of_date if as_of_date is not None else _date.today()

        ac_lookup, benchmark_lookup, ac_to_benchmark = await self._load_benchmark_graph()
        composites_by_code = await self._build_composite_series(resolved_as_of, ac_lookup)

        rows: list[AssetClassCompositeRowDTO] = []
        without_benchmark: list[str] = []

        ac_by_code = {ac.code: ac for ac in ac_lookup.values()}
        code_to_benchmark_id: dict[str, UUID] = {}
        for ac_id, benchmark_id in ac_to_benchmark.items():
            ac = ac_lookup.get(ac_id)
            if ac is not None:
                code_to_benchmark_id[ac.code] = benchmark_id

        for ac in sorted(ac_lookup.values(), key=lambda a: a.code):
            benchmark_id = code_to_benchmark_id.get(ac.code)
            if benchmark_id is None:
                without_benchmark.append(ac.display_name)
                continue
            benchmark = benchmark_lookup.get(benchmark_id)
            if benchmark is None:
                without_benchmark.append(ac.display_name)
                continue

            composite_series = composites_by_code.get(ac.code)
            benchmark_monthly = await self._load_benchmark_monthly_returns(
                benchmark_id, resolved_as_of
            )

            risk_free_rate = await self._resolve_risk_free_rate()

            if composite_series is None or composite_series.monthly_returns.empty:
                rows.append(
                    AssetClassCompositeRowDTO(
                        asset_class_code=ac.code,
                        asset_class_display_name=ac.display_name,
                        benchmark_display_name=benchmark.display_name,
                        composite_cumulative_returns=pd.Series(dtype="float64"),
                        benchmark_cumulative_returns=(
                            (1.0 + benchmark_monthly).cumprod() - 1.0
                            if not benchmark_monthly.empty
                            else pd.Series(dtype="float64")
                        ),
                        n_investments=0,
                        excess_return_annualised=math.nan,
                        information_ratio=math.nan,
                        n_observations=0,
                    )
                )
                continue

            composite_monthly = composite_series.monthly_returns
            risk_free_monthly = _build_monthly_risk_free(composite_monthly.index, risk_free_rate)

            bundle = compute_benchmark_comparison(
                investment_returns=composite_monthly,
                benchmark_returns=benchmark_monthly,
                risk_free_returns=risk_free_monthly,
                investment_identifier=ac.code,
                benchmark_identifier=benchmark.display_name,
            )

            comp_cum = (
                (1.0 + bundle.aligned_investment_returns).cumprod() - 1.0
                if not bundle.aligned_investment_returns.empty
                else pd.Series(dtype="float64")
            )
            bench_cum = (
                (1.0 + bundle.aligned_benchmark_returns).cumprod() - 1.0
                if not bundle.aligned_benchmark_returns.empty
                else pd.Series(dtype="float64")
            )
            metrics = bundle.metrics

            rows.append(
                AssetClassCompositeRowDTO(
                    asset_class_code=ac.code,
                    asset_class_display_name=ac.display_name,
                    benchmark_display_name=benchmark.display_name,
                    composite_cumulative_returns=comp_cum,
                    benchmark_cumulative_returns=bench_cum,
                    n_investments=composite_series.n_investments,
                    excess_return_annualised=metrics.excess_return_annualised,
                    information_ratio=metrics.information_ratio,
                    n_observations=metrics.n_observations,
                )
            )

        # Stable display order independent of the lookup dict iteration.
        without_benchmark = sorted(set(without_benchmark))
        # `ac_by_code` referenced only to keep mypy happy about unused
        # locals; silence the variable.
        del ac_by_code

        return AssetClassCompositesBundle(
            rows=rows,
            asset_classes_without_benchmark=without_benchmark,
            as_of_date=resolved_as_of,
        )

    # ------------------------------------------------------------------
    # Stage c — SAA-hypothetical comparison
    # ------------------------------------------------------------------

    async def get_saa_hypothetical(
        self,
        saa_configuration_id: UUID | None = None,
        weight_set: WeightSet = "tangency",
        as_of_date: _date | None = None,
    ) -> SAAHypotheticalBundle:
        """Stage c) — SAA-hypothetical comparison.

        Process:
          1. Load all SAA configurations of the tenant (dropdown).
          2. Resolve the selected configuration: argument > active >
             first configuration. ``selected_configuration_id`` is
             ``None`` if the tenant has no configurations.
          3. Always call ``SAAService.run_optimization(config_id)`` to
             obtain Tangency and MinVar portfolios. Per ADR-0042 §2
             optimization results are not persisted; this is a live
             compute per Stage-c render.
          4. ``run_optimization`` raises ``SAAValidationError`` when
             the configuration is not optimizable; the exception is
             caught here and surfaces in the bundle as
             ``weight_set_options[*].available=False`` with the hint.
             Other exception types (e.g. ``ValueError`` from the
             numeric optimizer) are NOT caught — those are real bugs
             and propagate to the route's standard error handler.
          5. Map the chosen ``weight_set`` to the corresponding
             ``PortfolioResult.weights`` array.
          6. Compose the SAA weights dict and feed the analytics
             function.

        Args:
            saa_configuration_id: Optional pin to a specific config.
            weight_set: ``"tangency"`` or ``"min_var"``.
            as_of_date: Optional cut-off; defaults to today.

        Returns:
            Always returns a bundle. ``series`` is ``None`` when no
            configuration is selected or the optimization failed; the
            ``weight_set_options`` carry the reason in
            ``unavailable_hint`` in the failure case.
        """
        resolved_as_of = as_of_date if as_of_date is not None else _date.today()
        configurations = await self._saa_service.list_configurations()
        configuration_options = [
            SAAConfigurationOptionDTO(
                saa_configuration_id=cfg.id,
                name=cfg.name,
                is_active=cfg.is_active,
            )
            for cfg in configurations
        ]

        if not configurations:
            return SAAHypotheticalBundle(
                saa_configuration_options=[],
                weight_set_options=[],
                selected_configuration_id=None,
                selected_weight_set=weight_set,
                series=None,
            )

        selected_config = self._resolve_selected_configuration(configurations, saa_configuration_id)
        selected_id = selected_config.id

        # Always-attempt optimization (per process spec step 3).
        try:
            optimization = await self._saa_service.run_optimization(selected_id)
        except SAAValidationError as exc:
            hint = exc.message or str(exc)
            return SAAHypotheticalBundle(
                saa_configuration_options=configuration_options,
                weight_set_options=[
                    WeightSetOptionDTO(
                        code="tangency",
                        display_name="Tangency (max Sharpe)",
                        available=False,
                        unavailable_hint=hint,
                    ),
                    WeightSetOptionDTO(
                        code="min_var",
                        display_name="Minimum Variance",
                        available=False,
                        unavailable_hint=hint,
                    ),
                ],
                selected_configuration_id=selected_id,
                selected_weight_set=weight_set,
                series=None,
            )

        weight_set_options = [
            WeightSetOptionDTO(
                code="tangency",
                display_name="Tangency (max Sharpe)",
                available=True,
                unavailable_hint=None,
            ),
            WeightSetOptionDTO(
                code="min_var",
                display_name="Minimum Variance",
                available=True,
                unavailable_hint=None,
            ),
        ]

        portfolio_result = (
            optimization.tangency if weight_set == "tangency" else optimization.min_var
        )

        all_asset_classes = await self._asset_classes.list_all()
        name_to_code = {ac.display_name: ac.code for ac in all_asset_classes}

        saa_weights: dict[str, float] = {}
        for idx, display_name in enumerate(optimization.asset_names):
            code = name_to_code.get(display_name)
            if code is None:
                _LOG.debug(
                    "saa-hypothetical: optimisation asset name %r has no "
                    "matching asset_class.code; skipping.",
                    display_name,
                )
                continue
            saa_weights[code] = float(portfolio_result.weights[idx])

        if not saa_weights:
            return SAAHypotheticalBundle(
                saa_configuration_options=configuration_options,
                weight_set_options=weight_set_options,
                selected_configuration_id=selected_id,
                selected_weight_set=weight_set,
                series=None,
            )

        # Build composite returns and benchmark returns per asset class.
        ac_lookup, _benchmark_lookup, ac_to_benchmark = await self._load_benchmark_graph()
        composites_by_code = await self._build_composite_series(resolved_as_of, ac_lookup)
        code_to_benchmark_id: dict[str, UUID] = {}
        for ac_id, benchmark_id in ac_to_benchmark.items():
            ac = ac_lookup.get(ac_id)
            if ac is not None:
                code_to_benchmark_id[ac.code] = benchmark_id

        benchmark_returns_by_ac: dict[str, pd.Series] = {}
        composite_returns_by_ac: dict[str, pd.Series] = {}
        for code in saa_weights:
            benchmark_id = code_to_benchmark_id.get(code)
            if benchmark_id is not None:
                benchmark_returns_by_ac[code] = await self._load_benchmark_monthly_returns(
                    benchmark_id, resolved_as_of
                )
            comp = composites_by_code.get(code)
            if comp is not None:
                composite_returns_by_ac[code] = comp.monthly_returns

        actual_portfolio_returns = await self._build_actual_portfolio_returns(resolved_as_of)

        saa_label = self._build_saa_label(selected_config.name, weight_set)

        try:
            series = compute_saa_hypothetical_series(
                saa_weights=saa_weights,
                benchmark_returns_by_asset_class=benchmark_returns_by_ac,
                composite_returns_by_asset_class=composite_returns_by_ac,
                actual_portfolio_returns=actual_portfolio_returns,
                saa_label=saa_label,
            )
        except ValueError as exc:
            # The pure layer raises on weights not summing to ~1.0;
            # mean-variance optimised tangency/min_var weights always
            # sum to 1.0 by construction, but defensive: surface the
            # failure as an unavailable weight set instead of bubbling
            # the ValueError out.
            hint = str(exc)
            failing_options = [
                WeightSetOptionDTO(
                    code=option.code,
                    display_name=option.display_name,
                    available=False,
                    unavailable_hint=hint,
                )
                for option in weight_set_options
            ]
            return SAAHypotheticalBundle(
                saa_configuration_options=configuration_options,
                weight_set_options=failing_options,
                selected_configuration_id=selected_id,
                selected_weight_set=weight_set,
                series=None,
            )

        effects = _compute_saa_effects(series)

        return SAAHypotheticalBundle(
            saa_configuration_options=configuration_options,
            weight_set_options=weight_set_options,
            selected_configuration_id=selected_id,
            selected_weight_set=weight_set,
            series=series,
            effects=effects,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_benchmark_graph(
        self,
    ) -> tuple[
        dict[UUID, AssetClassDTO],
        dict[UUID, BenchmarkDTO],
        dict[UUID, UUID],
    ]:
        """Load the asset-class, benchmark, and mapping lookups in one fan-out.

        Phase 1 has one mapping per asset class with ``weight = 1.0``;
        when multiple mappings exist for a single asset class (Phase 2
        composites) the first by ``benchmark_id`` wins, deferring full
        composite-blend support to the Phase-2 follow-up.
        """
        asset_classes = await self._asset_classes.list_all()
        benchmarks = await self._benchmarks.list_all()
        mappings = await self._mappings.list_all()

        ac_lookup = {ac.id: ac for ac in asset_classes}
        benchmark_lookup = {b.id: b for b in benchmarks}

        ac_to_benchmark: dict[UUID, UUID] = {}
        for mapping in mappings:
            if mapping.asset_class_id in ac_to_benchmark:
                continue
            ac_to_benchmark[mapping.asset_class_id] = mapping.benchmark_id

        return ac_lookup, benchmark_lookup, ac_to_benchmark

    async def _resolve_risk_free_rate(self) -> float:
        """Return the active SAA configuration's risk-free rate, or 0.0.

        See module docstring "Risk-free rate sourcing" for the
        rationale: the persistent risk-free rate lives on
        ``SAAConfiguration``; the "interest rates" Excel sheet has no
        DB persistence in the current schema and would require a new
        Repository to surface time-varying rates.
        """
        active = await self._saa_service.get_active_configuration()
        if active is None:
            return 0.0
        return float(active.risk_free_rate)

    async def _load_investment_monthly_returns(
        self,
        investment_id: UUID,
        as_of_date: _date,
        fx: PortfolioFxConverter,
        currency: str,
    ) -> pd.Series:
        """Build a month-end-indexed monthly return series for one investment.

        Source: actual NAV history → functional-currency conversion
        (ADR-0102) → daily return series (pct_change) → monthly
        compounded. The analytics layer compounds the daily series
        internally inside ``compute_benchmark_comparison`` via the
        alignment re-resampling, but we pre-resample here so the
        alignment boundary is a simple inner join.

        Args:
            investment_id: The investment whose NAV history to load.
            as_of_date: Cut-off applied to the NAV rows.
            fx: The caller's converter into the functional currency.
            currency: The investment's position currency. Threaded in
                from the caller, which already holds the DTO — the row
                is never refetched to read one field.

        Raises:
            MissingFxRateError: If a NAV date has no resolvable rate.
        """
        nav_rows = await self._navs.list_by_investment_and_kind(investment_id, "actual")
        if not nav_rows:
            return pd.Series(dtype="float64")
        nav_rows_filtered = [row for row in nav_rows if row.as_of_date <= as_of_date]
        if len(nav_rows_filtered) < 2:
            return pd.Series(dtype="float64")
        nav_series = pd.Series(
            data=[float(row.nav_value) for row in nav_rows_filtered],
            index=pd.to_datetime([row.as_of_date for row in nav_rows_filtered]),
            dtype="float64",
        ).sort_index()
        nav_series = fx.convert_series(nav_series, currency)
        daily_returns = compute_total_return_series(nav_series)
        return _resample_daily_to_monthly(daily_returns)

    async def _load_benchmark_monthly_returns(
        self,
        benchmark_id: UUID,
        as_of_date: _date,
    ) -> pd.Series:
        """Build a month-end-indexed monthly return series for one benchmark."""
        observations = await self._benchmark_observations.list_for_benchmark(
            benchmark_id, to_date=as_of_date
        )
        return _benchmark_observations_to_monthly(observations)

    async def _build_composite_series(
        self,
        as_of_date: _date,
        ac_lookup: dict[UUID, AssetClassDTO],
    ) -> dict[str, AssetClassCompositeSeries]:
        """Compute composite monthly returns per asset class.

        Returns a dict keyed by ``asset_class.code``. Asset classes
        without any investments are present in the dict with empty
        ``monthly_returns`` so the caller can render placeholder tiles.
        """
        investments = await self._investments.list_active()
        if not investments:
            return {}
        investment_ids = [inv.id for inv in investments]
        nav_rows_by_inv = await self._navs.list_by_investments_and_kind(investment_ids, "actual")

        # ADR-0099 §4 conversion boundary (extended here by ADR-0102). The
        # composite is a NAV-*weighted* blend of its members' returns, so the
        # NAVs must share a currency before they can weight anything.
        fx = await build_portfolio_fx_converter(
            tenants=self._tenants,
            fx_rates=self._fx_rates,
            position_currencies=[inv.currency for inv in investments],
        )

        investment_to_ac: dict[str, str] = {}
        navs_daily_by_inv: dict[str, pd.Series] = {}
        returns_daily_by_inv: dict[str, pd.Series] = {}

        for inv in investments:
            ac = ac_lookup.get(inv.asset_class_id)
            if ac is None:
                continue
            inv_key = str(inv.id)
            investment_to_ac[inv_key] = ac.code

            nav_rows = nav_rows_by_inv.get(inv.id, [])
            nav_rows_filtered = [row for row in nav_rows if row.as_of_date <= as_of_date]
            if not nav_rows_filtered:
                navs_daily_by_inv[inv_key] = pd.Series(dtype="float64")
                returns_daily_by_inv[inv_key] = pd.Series(dtype="float64")
                continue
            nav_series = pd.Series(
                data=[float(row.nav_value) for row in nav_rows_filtered],
                index=pd.to_datetime([row.as_of_date for row in nav_rows_filtered]),
                dtype="float64",
            ).sort_index()
            nav_series = fx.convert_series(nav_series, inv.currency)
            navs_daily_by_inv[inv_key] = nav_series
            returns_daily_by_inv[inv_key] = compute_total_return_series(nav_series)

        composites = compute_asset_class_composites(
            investment_returns_daily=returns_daily_by_inv,
            investment_navs_daily=navs_daily_by_inv,
            investment_to_asset_class=investment_to_ac,
        )
        return {c.asset_class_code: c for c in composites}

    async def _build_actual_portfolio_returns(
        self,
        as_of_date: _date,
    ) -> pd.Series:
        """Portfolio-level NAV-weighted monthly actuals across all investments.

        Implemented inline here rather than calling out to
        ``PortfolioReviewService`` because the shapes differ: the
        review surface returns yearly aggregates, whereas the SAA-
        hypothetical comparison needs the monthly grid produced by
        the same Beginning-of-Period TWR machinery used for the
        per-asset-class composites. The analytics layer's
        ``compute_asset_class_composites`` already does exactly this
        when called with all investments under a single synthetic
        asset class.
        """
        investments = await self._investments.list_active()
        if not investments:
            return pd.Series(dtype="float64")
        investment_ids = [inv.id for inv in investments]
        nav_rows_by_inv = await self._navs.list_by_investments_and_kind(investment_ids, "actual")

        # ADR-0099 §4 conversion boundary (extended here by ADR-0102). The
        # synthetic "_portfolio" class is the whole book NAV-weighted into
        # one return series — the one place mixed currencies would be summed
        # most directly.
        fx = await build_portfolio_fx_converter(
            tenants=self._tenants,
            fx_rates=self._fx_rates,
            position_currencies=[inv.currency for inv in investments],
        )

        navs_daily_by_inv: dict[str, pd.Series] = {}
        returns_daily_by_inv: dict[str, pd.Series] = {}
        inv_to_synthetic: dict[str, str] = {}
        for inv in investments:
            inv_key = str(inv.id)
            inv_to_synthetic[inv_key] = "_portfolio"
            nav_rows = nav_rows_by_inv.get(inv.id, [])
            nav_rows_filtered = [row for row in nav_rows if row.as_of_date <= as_of_date]
            if not nav_rows_filtered:
                navs_daily_by_inv[inv_key] = pd.Series(dtype="float64")
                returns_daily_by_inv[inv_key] = pd.Series(dtype="float64")
                continue
            nav_series = pd.Series(
                data=[float(row.nav_value) for row in nav_rows_filtered],
                index=pd.to_datetime([row.as_of_date for row in nav_rows_filtered]),
                dtype="float64",
            ).sort_index()
            nav_series = fx.convert_series(nav_series, inv.currency)
            navs_daily_by_inv[inv_key] = nav_series
            returns_daily_by_inv[inv_key] = compute_total_return_series(nav_series)

        composites = compute_asset_class_composites(
            investment_returns_daily=returns_daily_by_inv,
            investment_navs_daily=navs_daily_by_inv,
            investment_to_asset_class=inv_to_synthetic,
        )
        for c in composites:
            if c.asset_class_code == "_portfolio":
                return c.monthly_returns
        return pd.Series(dtype="float64")

    @staticmethod
    def _resolve_selected_configuration(configurations, requested_id):
        """Apply the argument > active > first preference order."""
        if requested_id is not None:
            for cfg in configurations:
                if cfg.id == requested_id:
                    return cfg
        for cfg in configurations:
            if cfg.is_active:
                return cfg
        return configurations[0]

    @staticmethod
    def _build_saa_label(config_name: str, weight_set: WeightSet) -> str:
        if weight_set == "tangency":
            return f"Tangency — {config_name}"
        return f"Minimum Variance — {config_name}"


# ---------------------------------------------------------------------------
# Module-level utilities
# ---------------------------------------------------------------------------


def _compute_saa_effects(
    series: SAAHypotheticalSeries,
) -> SAAHypotheticalEffects:
    """Compound the three monthly series to cumulative endpoints.

    Derives the allocation and selection effects in percentage points
    from the cumulative-decimal endpoints of the Actual, SAA × Benchmark,
    and SAA × Composite series. Each endpoint is ``(1 + r).cumprod()
    .iloc[-1] - 1`` over the cleaned, sorted series; missing operands
    propagate as ``None``.
    """

    def _endpoint(monthly: pd.Series) -> float | None:
        if monthly is None or monthly.empty:
            return None
        cleaned = monthly.dropna().sort_index()
        if cleaned.empty:
            return None
        return float((1.0 + cleaned).cumprod().iloc[-1] - 1.0)

    actual_end = _endpoint(series.actual_portfolio_returns)
    bench_end = _endpoint(series.saa_x_benchmark)
    comp_end = _endpoint(series.saa_x_composite)

    def _diff_pp(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        return (a - b) * 100.0

    return SAAHypotheticalEffects(
        actual_cumulative_endpoint=actual_end,
        saa_x_benchmark_cumulative_endpoint=bench_end,
        saa_x_composite_cumulative_endpoint=comp_end,
        allocation_effect_pp=_diff_pp(actual_end, bench_end),
        selection_effect_pp=_diff_pp(actual_end, comp_end),
    )


def _benchmark_observations_to_monthly(
    observations: list[BenchmarkObservationDTO],
) -> pd.Series:
    """Compound a list of daily benchmark observations to monthly returns."""
    if not observations:
        return pd.Series(dtype="float64")
    daily = pd.Series(
        data=[float(obs.period_return) for obs in observations],
        index=pd.to_datetime([obs.as_of_date for obs in observations]),
        dtype="float64",
    ).sort_index()
    return _resample_daily_to_monthly(daily)


def _resample_daily_to_monthly(daily_returns: pd.Series) -> pd.Series:
    """Compound daily decimal returns to month-end-stamped monthly returns.

    Mirrors the convention in
    ``services.analytics.benchmark_comparison._resample_daily_to_monthly_return``
    (which is module-private). Reproduced here so the service does
    not depend on a private helper.
    """
    if daily_returns.empty:
        return pd.Series(dtype="float64")
    cleaned = daily_returns.dropna().sort_index()
    if cleaned.empty:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime(cleaned.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    cleaned = pd.Series(cleaned.to_numpy(dtype="float64"), index=idx)
    monthly = cleaned.resample("ME").apply(
        lambda x: (1.0 + x).prod() - 1.0 if len(x) else float("nan")
    )
    return monthly.dropna()


def _build_monthly_risk_free(
    target_index: pd.Index,
    risk_free_annual: float,
) -> pd.Series:
    """Build a monthly risk-free return series aligned to ``target_index``.

    Per ADR-0061 §Decision the monthly conversion is
    ``r_m = (1 + r_ann) ** (days_in_month / 365) - 1``. When the
    target index is empty (no investment returns at all), returns an
    empty series — the analytics layer's inner-join handles it.
    """
    if target_index.empty:
        return pd.Series(dtype="float64")
    if isinstance(target_index, pd.DatetimeIndex):
        timestamps = target_index
    else:
        timestamps = pd.DatetimeIndex(pd.to_datetime(list(target_index)))

    values: list[float] = []
    for ts in timestamps:
        year = ts.year
        month = ts.month
        days_in_month = calendar.monthrange(year, month)[1]
        monthly = (1.0 + risk_free_annual) ** (days_in_month / 365.0) - 1.0
        values.append(monthly)
    return pd.Series(values, index=timestamps, dtype="float64")


__all__ = [
    "AssetClassCompositeRowDTO",
    "AssetClassCompositesBundle",
    "BenchmarkComparisonService",
    "InvestmentBenchmarkRowDTO",
    "InvestmentComparisonDetailDTO",
    "InvestmentComparisonsBundle",
    "PortfolioSummaryKPIs",
    "SAAConfigurationOptionDTO",
    "SAAHypotheticalBundle",
    "SAAHypotheticalEffects",
    "WeightSet",
    "WeightSetOptionDTO",
]
