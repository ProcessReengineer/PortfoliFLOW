# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Deltas-first scenario result assembly (ADR-0104 §5).

Runs a **baseline** overlay and a **scenario** overlay through the *existing*
coverage / composition / return engines and returns deltas-first result DTOs:
the Σ-NAV path pair (incl. cash), the ADR-0066 cumulative return index over the
performance universe, the four v1 KPI-delta tiles, the headroom-per-limit-family
deltas, and the composition pair for the lazy drill-down. It is the ADR-0104 §5
"result assembly" concern and nothing else.

**It forks no engine (ADR-0104 §4/§5, D17).** The engines predate overlays and
were never touched: this module produces the two worlds' *inputs* and hands
them to `compute_coverage`, `compute_aum`, `aggregate_fund_composition`, and the
ADR-0066 return functions unchanged. The frames→DTO projection and the E5
cash-exclusion predicate live **here, at the assembly seam** — never pushed down
into :mod:`services.analytics` (whose purity guard forbids it) and never into an
engine (which stays cash-blind and overlay-blind).

**The two worlds (ADR-0104 §2/§4).** ``value_overlay, fx_shocks =
partition_fx_shocks(overlay)``. The baseline is the untransformed
:class:`~services.overlay.pipeline.PlanFrames` converted through the untouched
:class:`~services.fx.functional_currency.PortfolioFxConverter`; the scenario is
``apply_overlay(baseline, value_overlay)`` converted through
:func:`~services.fx.plan_shock.shock_plan_fx_path`. This is the same split
:func:`services.investments.cash_flow_timeline.project_cash_flow_planning`
already runs — the empty overlay returns the very frames and the very converter
it was given (``is``), so **baseline and scenario collapse to one world for the
empty overlay**, and the deltas-first pairs are all zero by construction rather
than by luck (the assembly adds no drift of its own).

**Two universes, kept explicitly apart (ADR-0100 §4, the E5 decision).** The
Σ-NAV path (:attr:`ScenarioResult.nav_path`) is over the **full** universe, cash
included — it is the ADR-0103 §2 ``aum(t) = Σ nav_functional(t)`` produced by the
one canonical Σ, :func:`services.investments.aum.compute_aum`. The cumulative
return index (:attr:`ScenarioResult.return_index`) is over the **performance**
universe only — cash excluded via the single :func:`_is_cash` predicate, the
same ``!= 'cash'`` split
:meth:`services.portfolio_review.PortfolioReviewService` applies at its own
data-assembly seam. A reader must never conflate the two: the NAV line includes
the cash balance, the return index does not.

**Why ``compute_aum`` rather than the cash-flow timeline's ``_total``.** The
Σ-NAV line needs Σ over the *full* universe, but
:func:`services.investments.cash_flow_timeline.build_cash_flow_timeline`'s
``_total`` sums only the **cash** currency rows (it is the funding figure, not
AUM). :func:`~services.investments.aum.compute_aum` is the ADR-0103 §2 canonical
Σ NAV incl. cash — the correct existing engine to reuse — and its
:class:`~services.investments.aum.AumBreakdown` hands back ``total`` (the NAV
line), ``non_cash`` (the return-index universe's Σ), and ``cash`` (the cash KPI)
from one pass. Σ is therefore reused, not reimplemented.

**The identical-history invariant (ADR-0104 §5) holds structurally.** Every
value overlay touches only the plan segment (``> t0``); every ``fx_shock``
restates rates only ``after=t0``. So on the actual segment (``<= t0``) both
worlds read the same book actuals converted at the same rates — the baseline and
scenario Σ-NAV series are equal left of the seam, asserted in the assembly's
tests, not only at display.

Scope of this module (ADR-0104 §5): **pure assembly and DTOs only.** No chart
spec, no Plotly, no shared-axis / joint-extrema computation (the chart's job,
S34.5), no template, no route wiring. It returns values, not figures. It is
DB-free at its core: it receives already-loaded inputs (a
:class:`ScenarioResultInputs`) exactly as ``project_cash_flow_planning`` does,
and imports no repository, no session, no chart spec.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date as _date
from decimal import Decimal
from uuid import UUID

import pandas as pd
from dateutil.relativedelta import relativedelta

from core.repositories.investment_nav_repository import InvestmentNavDTO
from core.repositories.investment_repository import InvestmentDTO
from services.analytics._dtos import (
    InvestmentWithClassCodeDTO,
    LimitSetWithLimitsDTO,
)
from services.analytics.investment_returns import (
    compute_cashflow_adjusted_return_series,
)
from services.analytics.limit_coverage import (
    CoverageEngineResult,
    compute_coverage,
)
from services.analytics.portfolio_aggregation import (
    FundCompositionBreakdown,
    aggregate_fund_composition,
    compute_total_return_index_series,
)
from services.fx.functional_currency import PortfolioFxConverter
from services.fx.plan_shock import shock_plan_fx_path
from services.investments.aum import CASH_TYPE, NavSeries, compute_aum
from services.overlay import (
    Overlay,
    PlanFrames,
    apply_overlay,
    partition_fx_shocks,
)

_SAA: str = "saa"
_ANLV: str = "anlv"

#: Breach status the plan-horizon breach-count KPI counts (ADR-0104 §5). The
#: coverage engine's own label (:mod:`services.analytics.limit_coverage`).
_STATUS_BREACH: str = "BREACH"

#: The four quarters of "functional cash at t₀+4Q" (ADR-0104 §5, the mockup's
#: third KPI tile) — a full year past the seam.
_KPI_CASH_MONTHS: int = 12

#: A sentinel NAV-row id for the synthetic plan NAVs the frames→DTO projection
#: fabricates (:func:`_synthetic_plan_nav`). The coverage engine keys its NAV
#: lookup by ``as_of_date`` and reads only ``as_of_date`` / ``nav_value`` /
#: ``nav_kind`` (:func:`services.analytics.limit_coverage._build_nav_lookup`), so
#: the row id is never read — a fixed sentinel keeps the projection reproducible
#: from *(book, parameters)* alone (ADR-0104 §2) rather than minting a UUID.
_SYNTHETIC_NAV_ID: UUID = UUID("00000000-0000-0000-0000-0000000000ff")


# ---------------------------------------------------------------------------
# The one cash predicate (ADR-0100 §4 / ADR-0103 §8, the E5 decision)
# ---------------------------------------------------------------------------


def _is_cash(investment: InvestmentDTO) -> bool:
    """Whether ``investment`` is an explicit cash position (ADR-0100 §2).

    **The single home of the cash split in this service.** Every cash-aware
    decision here routes through it: the frames→DTO projection choosing
    ``cash_paths`` over ``value_paths``, the stitched-series routing, the
    performance-universe exclusion for the return index, and the performance
    cashflow filter. Built on :data:`services.investments.aum.CASH_TYPE`, the
    canonical ``'cash'`` value — never a restated literal (ADR-0104 §5's
    "one formulation").

    A non-cash investment is a **performance** investment (``not _is_cash``):
    the ADR-0100 §4 / ADR-0103 §8 rule that cash is Σ-NAV but never a
    performance-metric input, the same ``!= 'cash'`` split
    :meth:`services.portfolio_review.PortfolioReviewService` applies.

    Args:
        investment: The investment row.

    Returns:
        ``True`` iff ``investment.investment_type`` is the cash type.
    """
    return investment.investment_type == CASH_TYPE


# ---------------------------------------------------------------------------
# Result DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioSeriesPair:
    """Baseline and scenario values of one series over a shared period grid.

    The deltas-first shape (ADR-0104 §5): both legs are stated on the **same**
    grid, so a cell-to-cell delta is meaningful by construction. Left of the
    seam (:attr:`seam_index`) the two legs are equal for a series that respects
    the identical-history invariant — the Σ-NAV pair does (ADR-0104 §5).

    Attributes:
        period_ends: The period-end dates, ascending. Positionally aligned
            with :attr:`baseline` and :attr:`scenario`.
        seam_index: The index of the first **plan** period — equivalently the
            number of actual periods (period ends ``<= t0``). The amber rule
            (ADR-0104 §6) is drawn immediately left of this column.
        baseline: One value per period for the baseline world. ``None`` where
            the world states nothing at that period end. The Σ-NAV pair carries
            :class:`~decimal.Decimal` money; the return-index pair carries
            rebased-index ``float`` — the two never mix within one instance.
        scenario: The scenario world's values, positionally aligned.
    """

    period_ends: tuple[_date, ...]
    seam_index: int
    baseline: tuple[Decimal | float | None, ...]
    scenario: tuple[Decimal | float | None, ...]

    @property
    def baseline_end(self) -> Decimal | float | None:
        """The last non-``None`` baseline value — the end-of-horizon figure."""
        return _last_value(self.baseline)

    @property
    def scenario_end(self) -> Decimal | float | None:
        """The last non-``None`` scenario value — the end-of-horizon figure."""
        return _last_value(self.scenario)

    @property
    def delta_end(self) -> Decimal | float | None:
        """``scenario_end - baseline_end`` — the panel-footer delta badge.

        ``None`` where either end value is missing. The subtraction stays in
        the pair's own numeric type (Decimal for NAV, float for the index).
        """
        return _subtract(self.scenario_end, self.baseline_end)


@dataclass(frozen=True)
class KpiDelta:
    """One KPI tile as a ``(baseline, scenario, delta)`` triple (ADR-0104 §5).

    The strip renders the ADR-0067 idiom "always as a pair" — baseline struck
    through, scenario bold, delta badge — but that presentation is S34.5's
    concern. This DTO carries the *values* and a format hint; it computes no
    display string.

    Attributes:
        key: A stable machine key (``'aum'``, ``'tightest_anlv_headroom'``,
            ``'functional_cash_t0_plus_4q'``, ``'limit_breaches'``).
        label: The human tile label, as the mockup states it.
        unit: A format hint for the renderer: ``'functional_currency'`` for the
            three money tiles (AUM, tightest AnlV headroom in EUR, cash), or
            ``'count'`` for the breach tile.
        baseline: The baseline world's figure, or ``None`` when undefined
            (e.g. no AnlV limit rows).
        scenario: The scenario world's figure, positionally the same kind.
        delta: ``scenario - baseline`` — ``None`` where either side is.
    """

    key: str
    label: str
    unit: str
    baseline: Decimal | int | None
    scenario: Decimal | int | None
    delta: Decimal | int | None


@dataclass(frozen=True)
class HeadroomClassDelta:
    """One ``(family, class)`` row of the headroom table (ADR-0104 §5, §7).

    Baseline vs. scenario utilisation and headroom at the plan horizon, with
    their deltas — the grain the mockup's "coverage headroom per limit family"
    table draws (one row per limit class, columns baseline util. / scenario
    util. / headroom / Δ). Values are the coverage engine's own Decimals
    (percentage points for utilisation, functional currency for headroom).

    Attributes:
        family: ``'saa'`` or ``'anlv'``.
        class_key: The limit class (e.g. ``'listed_equity'``,
            ``'cash_band'``).
        baseline_coverage_pct: Utilisation (coverage %) in the baseline world,
            or ``None`` when the class is absent there.
        scenario_coverage_pct: Utilisation in the scenario world.
        delta_coverage_pct: ``scenario - baseline`` utilisation.
        baseline_headroom_eur: Headroom (functional currency) in the baseline
            world, or ``None`` for an unconstrained class (``NO_LIMIT`` /
            ``UNALLOCATED`` rows carry no ceiling).
        scenario_headroom_eur: Headroom in the scenario world.
        delta_headroom_eur: ``scenario - baseline`` headroom.
        baseline_status: The baseline coverage status (``OK`` / ``WARN`` /
            ``BREACH`` / ``NO_LIMIT`` / ``UNALLOCATED``), or ``None`` when
            absent.
        scenario_status: The scenario coverage status.
    """

    family: str
    class_key: str
    baseline_coverage_pct: Decimal | None
    scenario_coverage_pct: Decimal | None
    delta_coverage_pct: Decimal | None
    baseline_headroom_eur: Decimal | None
    scenario_headroom_eur: Decimal | None
    delta_headroom_eur: Decimal | None
    baseline_status: str | None
    scenario_status: str | None


@dataclass(frozen=True)
class FamilyHeadroomDelta:
    """The headroom rows of one limit family (ADR-0104 §5).

    Attributes:
        family: ``'saa'`` or ``'anlv'``.
        rows: One :class:`HeadroomClassDelta` per limit class present at the
            plan horizon in either world, ordered by ``class_key``.
    """

    family: str
    rows: tuple[HeadroomClassDelta, ...]


@dataclass(frozen=True)
class CompositionPair:
    """The baseline and scenario fund composition (ADR-0104 §5, §7).

    Feeds S34.5's **lazy** composition drill-down, which diffs the two
    NAV-weighted breakdowns. This module assembles the DTOs; it does not build
    the partial.

    Attributes:
        baseline: The baseline world's composition — every fund's NAV share of
            the (converted) full universe at the plan horizon.
        scenario: The scenario world's composition, on the same grain.
    """

    baseline: FundCompositionBreakdown
    scenario: FundCompositionBreakdown


@dataclass(frozen=True)
class ScenarioResult:
    """The deltas-first result of one scenario overlay (ADR-0104 §5).

    Everything the Scenario Analysis lens's §7 region draws, as values rather
    than figures: the chart-pair series, the KPI strip, the headroom table, and
    the composition drill-down.

    Attributes:
        nav_path: The Σ-NAV path pair (functional currency, **full** universe
            incl. cash), left-axis chart line + panel-footer end-of-horizon NAV.
        return_index: The cumulative ADR-0066 return-index pair (rebased to
            100, **performance** universe, cash excluded), right-axis chart line
            + panel-footer total return.
        kpis: The four v1 KPI-delta tiles, in tile order (AUM, tightest AnlV
            headroom, functional cash at t₀+4Q, limit breaches on the horizon).
        headroom: The per-limit-family headroom deltas, in ``(saa, anlv)``
            order.
        composition: The baseline/scenario composition pair for the drill-down.
    """

    nav_path: ScenarioSeriesPair
    return_index: ScenarioSeriesPair
    kpis: tuple[KpiDelta, ...]
    headroom: tuple[FamilyHeadroomDelta, ...]
    composition: CompositionPair


@dataclass(frozen=True, eq=False)
class ScenarioResultInputs:
    """Everything the pure assembly needs, read from the book in one place.

    The route already loads these for the Cash Flow Planning section (S2.x);
    this service is called **alongside** it, not through a new loader (ADR-0104
    §5). Equality is by identity (``eq=False``): the container holds
    :class:`~services.overlay.pipeline.PlanFrames` and pandas objects, whose
    ``==`` is elementwise.

    All NAV and cashflow inputs arrive in **position currency**; conversion to
    the functional currency happens inside the assembly, per world, at the
    ADR-0099 §4 boundary — the baseline through :attr:`converter`, the scenario
    through the ``fx_shock``-restated converter. The frames carry the *plan*
    world only (:attr:`baseline`); the realised history the identical-history
    segment needs is passed beside them (:attr:`actual_navs`,
    :attr:`actual_cashflows`), exactly as
    :func:`services.investments.cash_flow_timeline.build_cash_flow_timeline`
    takes ``actual_cash`` beside its frames.

    Attributes:
        baseline: The baseline plan frames, assembled from the book (ADR-0104
            §1). Reused **unshocked** for the baseline leg; the scenario leg is
            ``apply_overlay(baseline, value_overlay)``.
        converter: The functional-currency converter (ADR-0099 §4). Not mutated
            — the baseline leg converts through it, the scenario leg through the
            ``fx_shock``-restated copy.
        investments: The **full** classified active universe, cash positions
            included — the coverage denominator's universe (ADR-0103 §8) and the
            composition universe.
        actual_navs: Per-investment realised NAV streams (``nav_kind='actual'``,
            position currency), keyed by ``investment.id``. Display data: never
            overlaid. Anchors the ``<= t0`` segment of the Σ-NAV path and serves
            the coverage engine's ADR-0060 fallback.
        actual_cashflows: Per-investment realised cashflow frames (columns
            ``flow_timestamp``, ``amount`` — signed, ADR-0043 §1 — position
            currency), keyed by ``investment.id``. Feeds the return index's
            history segment and the composition IRR.
        saa_sets: SAA limit sets, ascending by ``effective_from`` (ADR-0056).
        anlv_sets: AnlV limit sets, ascending by ``effective_from``.
        evaluation_dates: The period-end grid, ascending, spanning the actual
            segment (``<= cut_over``) and the plan horizon (``> cut_over``) — so
            the Σ-NAV pair carries the history the identical-history invariant is
            asserted on. The route builds it (the cash-flow lens grid).
        cut_over: The plan/actual seam t₀ (ADR-0060). Period ends at or before
            it are actual, after it are plan.
        warn_threshold_pct: The coverage WARN floor, forwarded to the engine.
    """

    baseline: PlanFrames
    converter: PortfolioFxConverter
    investments: list[InvestmentWithClassCodeDTO]
    actual_navs: Mapping[UUID, list[InvestmentNavDTO]]
    actual_cashflows: Mapping[UUID, pd.DataFrame]
    saa_sets: list[LimitSetWithLimitsDTO]
    anlv_sets: list[LimitSetWithLimitsDTO]
    evaluation_dates: list[_date]
    cut_over: _date
    warn_threshold_pct: Decimal = Decimal("90.0")


# ---------------------------------------------------------------------------
# The composer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class _World:
    """One leg of the Baseline/Scenario pair: its frames and its converter."""

    frames: PlanFrames
    converter: PortfolioFxConverter


@dataclass(frozen=True, eq=False)
class _WorldMetrics:
    """The per-world figures the deltas are formed from (ADR-0104 §5)."""

    nav_total: tuple[Decimal, ...]
    return_index: tuple[float | None, ...]
    coverage: CoverageEngineResult
    cash_t0_plus_4q: Decimal
    composition: FundCompositionBreakdown


def assemble_scenario_result(
    inputs: ScenarioResultInputs,
    overlay: Overlay,
) -> ScenarioResult:
    """Assemble the deltas-first result of one scenario overlay (ADR-0104 §5).

    Builds the two worlds — baseline and scenario — and runs each through the
    existing engines, then forms the deltas-first DTOs. The overlay is split at
    two seams in the ADR-0104 §3 order: the value transformations fold over the
    frames (:func:`~services.overlay.pipeline.apply_overlay`), the ``fx_shock``
    restates the converter
    (:func:`~services.fx.plan_shock.shock_plan_fx_path`), and functional
    aggregation then runs through the restated rates.

    With the empty overlay the value fold returns the baseline frames and the
    shock returns the baseline converter (both by ``is``), so the two worlds are
    one world and every delta is zero — the Baseline/Scenario toggle's contract
    (ADR-0104 §4) and the deltas-first foundation (§5).

    Args:
        inputs: The already-loaded book inputs (see :class:`ScenarioResultInputs`).
        overlay: The scenario overlay. Empty means baseline.

    Returns:
        The :class:`ScenarioResult`.

    Raises:
        ExecutorNotRegisteredError: If the overlay carries a value kind with no
            executor (never an ``fx_shock`` — it is partitioned out first).
        OverlayExecutionError: If an executor cannot apply a transformation to
            these frames.
        MissingFxRateError: If a required rate is missing — including a rate an
            ``fx_shock`` named: a shock restates a path, it never invents one.
        LimitSetNotEffective, CoverageInputMissing, CoverageInputOutOfRange:
            Propagated from the coverage engine; the route renders them as
            actionable error partials (ADR-0104 §4), exactly as the Investment
            Limits section does.
    """
    value_overlay, fx_shocks = partition_fx_shocks(overlay)

    baseline_world = _World(frames=inputs.baseline, converter=inputs.converter)
    scenario_world = _World(
        frames=apply_overlay(inputs.baseline, value_overlay),
        converter=shock_plan_fx_path(
            inputs.converter,
            [(shock.currency, shock.magnitude) for shock in fx_shocks],
            t0=inputs.baseline.t0,
        ),
    )

    baseline = _world_metrics(inputs, baseline_world)
    scenario = _world_metrics(inputs, scenario_world)
    return _combine(inputs, baseline, scenario)


def _world_metrics(inputs: ScenarioResultInputs, world: _World) -> _WorldMetrics:
    """Run one world through the engines and collect its figures."""
    series = _stitched_series(inputs, world)

    # One AumBreakdown per period end: total (the Σ-NAV line, incl. cash),
    # non_cash (the return-index universe's Σ), and cash split from a single
    # canonical pass (ADR-0103 §2). Σ is reused, not reimplemented.
    breakdowns = [
        compute_aum(series, period_end, world.converter) for period_end in inputs.evaluation_dates
    ]
    nav_total = tuple(breakdown.total for breakdown in breakdowns)
    nav_non_cash = [breakdown.non_cash for breakdown in breakdowns]

    return_index = _return_index(inputs, world, nav_non_cash)

    cash_t0_plus_4q = compute_aum(
        series,
        inputs.cut_over + relativedelta(months=_KPI_CASH_MONTHS),
        world.converter,
    ).cash

    coverage = _coverage(inputs, world)
    composition = _composition(inputs, world, series)

    return _WorldMetrics(
        nav_total=nav_total,
        return_index=return_index,
        coverage=coverage,
        cash_t0_plus_4q=cash_t0_plus_4q,
        composition=composition,
    )


# ---------------------------------------------------------------------------
# The frames → NavSeries stitch (Σ-NAV path, cash KPI, composition NAVs)
# ---------------------------------------------------------------------------


def _stitched_series(inputs: ScenarioResultInputs, world: _World) -> list[NavSeries]:
    """Stitch each investment's realised history to its plan path (ADR-0060).

    The plan-world NAV series per investment: the realised observations at or
    before the seam, then the world's plan-path observations strictly after it.
    Reading the two segments off distinct sources is the ADR-0060 seam per
    investment — actual left of t₀, plan right — and it is what lets
    :func:`~services.investments.aum.compute_aum` produce a Σ-NAV path that is
    the realised book on the actual side and the (possibly overlaid) plan world
    on the plan side, with the identical-history invariant (ADR-0104 §5) holding
    by construction: the actual segment is the same book actuals in both worlds.

    A cash position's plan path is its ``cash_paths`` entry (keyed by currency);
    a non-cash investment's is its ``value_paths`` entry (keyed by id) — the one
    place the projection routes on cash (:func:`_is_cash`). An investment with no
    path on its side contributes only its actuals, and one absent from the book
    entirely contributes an empty series (compute_aum then carries it as the zero
    it is).

    Args:
        inputs: The book inputs.
        world: The world whose frames supply the plan paths.

    Returns:
        One :class:`~services.investments.aum.NavSeries` triple per investment
        in :attr:`ScenarioResultInputs.investments`, dates ascending, values in
        **position currency** (compute_aum converts).
    """
    series: list[NavSeries] = []
    for classified in inputs.investments:
        investment = classified.investment

        observations: list[tuple[_date, Decimal]] = [
            (row.as_of_date, row.nav_value)
            for row in inputs.actual_navs.get(investment.id, [])
            if row.as_of_date <= inputs.cut_over
        ]

        path = _plan_path(world.frames, investment)
        if path is not None:
            for stamp, value in path.items():
                observed = _as_date(stamp)
                if observed > inputs.cut_over:
                    observations.append((observed, value))

        observations.sort(key=lambda point: point[0])
        series.append(
            (
                investment,
                [observed for observed, _ in observations],
                [value for _, value in observations],
            )
        )
    return series


def _plan_path(frames: PlanFrames, investment: InvestmentDTO) -> pd.Series | None:
    """Return the world's plan path for ``investment`` — cash- or value-side.

    The routing half of the cash split: a cash position reads its per-currency
    ``cash_paths`` entry, a non-cash investment its per-id ``value_paths`` entry
    (ADR-0104 §1). ``None`` where the world carries no path on that side.
    """
    if _is_cash(investment):
        return frames.cash_paths.get(investment.currency.upper())
    return frames.value_paths.get(investment.id)


# ---------------------------------------------------------------------------
# The return index (performance universe, ADR-0066)
# ---------------------------------------------------------------------------


def _return_index(
    inputs: ScenarioResultInputs,
    world: _World,
    nav_non_cash: Sequence[Decimal],
) -> tuple[float | None, ...]:
    """Build one world's cumulative return index over the performance universe.

    The portfolio-level ADR-0066 return series over the **performance** (non-
    cash) universe, then :func:`~services.analytics.portfolio_aggregation.compute_total_return_index_series`
    to the rebased-to-100 index. The NAV feeding it is the per-period ``non_cash``
    Σ (cash excluded — the E5 universe), and the cashflows are the performance
    universe's realised flows (whole history) plus its plan flows strictly after
    the seam, converted through the world's converter. So an ``fx_shock`` and a
    ``market_shock`` both reach the index — through the converted plan flows and
    the shocked plan NAVs alike — while the actual segment stays identical across
    worlds (realised flows and rates are never restated).

    Args:
        inputs: The book inputs.
        world: The world whose converter and plan flows apply.
        nav_non_cash: The per-period ``non_cash`` Σ from the AUM breakdowns,
            positionally aligned with :attr:`ScenarioResultInputs.evaluation_dates`.

    Returns:
        One rebased-index value per period end (``float``), ``None`` for the
        first period (no predecessor to return against) and any period the
        return series does not reach.
    """
    performance_nav = pd.Series(
        [float(value) for value in nav_non_cash],
        index=[pd.Timestamp(period_end) for period_end in inputs.evaluation_dates],
        dtype="float64",
    )
    flows = _performance_cashflows(inputs, world)
    returns = compute_cashflow_adjusted_return_series(performance_nav, flows)
    index = compute_total_return_index_series(returns, base=100.0)

    index_by_stamp = {stamp: float(value) for stamp, value in index.items()}
    return tuple(
        index_by_stamp.get(pd.Timestamp(period_end)) for period_end in inputs.evaluation_dates
    )


def _performance_cashflows(inputs: ScenarioResultInputs, world: _World) -> pd.DataFrame:
    """Collect the performance universe's converted flows (ADR-0066).

    Realised flows (whole history) plus plan flows strictly after the seam, over
    the **non-cash** universe only (:func:`_is_cash`), each converted to the
    functional currency through the world's converter — realised flows at the
    investment's currency (as the review seam converts), plan flows at the
    flow's own settlement currency (the ADR-0103 §6 rule the contract carries).
    Investor flows never appear: they book against cash positions
    (ADR-0103 §5), which the non-cash filter excludes.

    Returns:
        A flat ``(flow_timestamp, amount)`` frame — the shape
        :func:`~services.analytics.investment_returns.compute_cashflow_adjusted_return_series`
        consumes. Empty when no performance flow exists.
    """
    investment_by_id = {
        classified.investment.id: classified.investment for classified in inputs.investments
    }
    rows: list[tuple[pd.Timestamp, float]] = []

    for classified in inputs.investments:
        investment = classified.investment
        if _is_cash(investment):
            continue
        frame = inputs.actual_cashflows.get(investment.id)
        if frame is None or frame.empty:
            continue
        stamps = pd.to_datetime(frame["flow_timestamp"], utc=True)
        for raw_amount, stamp in zip(frame["amount"], stamps):
            converted = world.converter.convert_amount(
                Decimal(str(raw_amount)), investment.currency, stamp.date()
            )
            rows.append((stamp, float(converted)))

    for flow in world.frames.plan_flows:
        if flow.as_of_date <= inputs.cut_over:
            continue
        investment = investment_by_id.get(flow.investment_id)
        if investment is None or _is_cash(investment):
            continue
        converted = world.converter.convert_amount(flow.amount, flow.currency, flow.as_of_date)
        rows.append((pd.Timestamp(flow.as_of_date, tz="UTC"), float(converted)))

    if not rows:
        return pd.DataFrame(columns=["flow_timestamp", "amount"])
    return pd.DataFrame(rows, columns=["flow_timestamp", "amount"])


# ---------------------------------------------------------------------------
# Coverage (the headroom table + AnlV / breach KPIs)
# ---------------------------------------------------------------------------


def _coverage(inputs: ScenarioResultInputs, world: _World) -> CoverageEngineResult:
    """Run the coverage engine on the world's plan horizon (ADR-0104 §5).

    Fed the world's plan NAVs — projected from its (possibly overlaid) frames
    and converted — plus the realised NAVs converted for the ADR-0060 fallback.
    Evaluated on the plan-horizon dates (strictly after the seam), where the
    engine prefers the plan stream (ADR-0060), so the coverage the headroom table
    and the AnlV / breach KPIs read reflects the scenario's transformed plan
    world.

    Returns:
        The :class:`~services.analytics.limit_coverage.CoverageEngineResult`;
        its families carry empty frames when the horizon is empty.
    """
    coverage_dates = [
        period_end for period_end in inputs.evaluation_dates if period_end > inputs.cut_over
    ]
    plan_navs = _project_plan_navs(inputs, world)
    actual_navs = _convert_actual_navs(inputs, world)

    return compute_coverage(
        investments=inputs.investments,
        actual_navs=actual_navs,
        plan_navs=plan_navs,
        cut_over=inputs.cut_over,
        saa_sets=inputs.saa_sets,
        anlv_sets=inputs.anlv_sets,
        evaluation_dates=coverage_dates,
        warn_threshold_pct=inputs.warn_threshold_pct,
    )


def _project_plan_navs(
    inputs: ScenarioResultInputs, world: _World
) -> dict[UUID, list[InvestmentNavDTO]]:
    """Project the world's plan frames into per-investment NAV DTOs (ADR-0104 §5).

    The load-bearing glue: the coverage engine predates overlays and takes
    ``plan_navs: dict[UUID, list[InvestmentNavDTO]]`` already converted to the
    functional currency (:meth:`services.limits.LimitsCoverageService._convert_navs`),
    while the overlay produces :class:`~services.overlay.pipeline.PlanFrames`
    (pandas paths in position currency). This turns one back into the other —
    the same projection for both worlds, so the empty overlay round-trips to the
    baseline DTOs with no drift.

    **The projection is the only seam that knows cash** (routing ``cash_paths``
    vs ``value_paths`` through :func:`_is_cash`): each path point becomes one
    plan NAV DTO, converted at its own date through the world's converter, so an
    ``fx_shock``'s restated rates and a value transformation's moved levels both
    reach the coverage numerators and its Σ-NAV denominator.

    Returns:
        Per investment id, its projected plan NAV stream (empty list where the
        world carries no path for it).
    """
    functional_currency = world.converter.functional_currency
    projected: dict[UUID, list[InvestmentNavDTO]] = {}
    for classified in inputs.investments:
        investment = classified.investment
        path = _plan_path(world.frames, investment)
        if path is None or path.empty:
            projected[investment.id] = []
            continue
        projected[investment.id] = [
            _synthetic_plan_nav(
                investment,
                _as_date(stamp),
                world.converter.convert_amount(value, investment.currency, _as_date(stamp)),
                functional_currency,
            )
            for stamp, value in path.items()
        ]
    return projected


def _convert_actual_navs(
    inputs: ScenarioResultInputs, world: _World
) -> dict[UUID, list[InvestmentNavDTO]]:
    """Restate the realised NAV streams into the functional currency (ADR-0099 §4).

    The realised counterpart of :func:`_project_plan_navs` — the same
    point-in-time conversion the Investment Limits section applies
    (:meth:`services.limits.LimitsCoverageService._convert_navs`), returned
    unchanged on the identity fast-path. Realised dates are ``<= t0`` and an
    ``fx_shock`` restates only ``after=t0``, so the scenario and baseline
    conversions of realised NAVs coincide — the identical-history invariant
    reaching the coverage inputs.

    Returns:
        Per investment id, its realised NAV stream converted to the functional
        currency.
    """
    if world.converter.is_identity:
        return dict(inputs.actual_navs)
    converted: dict[UUID, list[InvestmentNavDTO]] = {}
    for classified in inputs.investments:
        investment = classified.investment
        converted[investment.id] = [
            replace(
                row,
                nav_value=world.converter.convert_amount(
                    row.nav_value, investment.currency, row.as_of_date
                ),
            )
            for row in inputs.actual_navs.get(investment.id, [])
        ]
    return converted


def _synthetic_plan_nav(
    investment: InvestmentDTO,
    as_of_date: _date,
    nav_value: Decimal,
    functional_currency: str,
) -> InvestmentNavDTO:
    """Fabricate one projected plan NAV row for the coverage engine.

    The engine keys its lookup by ``as_of_date`` and reads only ``as_of_date`` /
    ``nav_value`` / ``nav_kind``
    (:func:`services.analytics.limit_coverage._build_nav_lookup`), so the
    identity fields carry the owning investment's own tenant / authorship (never
    fabricated) and a fixed sentinel row id (:data:`_SYNTHETIC_NAV_ID`, unread) —
    keeping the projection reproducible from *(book, parameters)* alone
    (ADR-0104 §2).
    """
    return InvestmentNavDTO(
        id=_SYNTHETIC_NAV_ID,
        tenant_id=investment.tenant_id,
        investment_id=investment.id,
        as_of_date=as_of_date,
        nav_value=nav_value,
        currency=functional_currency,
        nav_kind="plan",
        source=None,
        created_by=investment.created_by,
        created_at=investment.created_at,
        updated_at=investment.updated_at,
    )


# ---------------------------------------------------------------------------
# Composition (the lazy drill-down)
# ---------------------------------------------------------------------------


def _composition(
    inputs: ScenarioResultInputs,
    world: _World,
    series: list[NavSeries],
) -> FundCompositionBreakdown:
    """Assemble one world's fund composition (ADR-0104 §5, §7).

    The NAV-weighted composition over the **full** universe at the plan horizon,
    the grain the review's fund-composition tile uses
    (:meth:`services.portfolio_review.PortfolioReviewService`). Each fund's
    weight is its horizon NAV — read off the world's stitched series and
    converted, so it moves with the overlay — while its IRR is evaluated on the
    realised flows (identical across worlds). The composition drill-down's
    signal is therefore the NAV-weight delta, which is what a scenario changes.

    Returns:
        The :class:`~services.analytics.portfolio_aggregation.FundCompositionBreakdown`.
    """
    report_date = inputs.evaluation_dates[-1]
    nav_by_investment: dict[UUID, float] = {}
    for investment, dates, values in series:
        nav = _latest_at_or_before(dates, values, report_date)
        if nav is None:
            continue
        nav_by_investment[investment.id] = float(
            world.converter.convert_amount(nav, investment.currency, report_date)
        )

    cf_in_by_investment: dict[UUID, pd.Series] = {}
    cf_out_by_investment: dict[UUID, pd.Series] = {}
    for classified in inputs.investments:
        investment = classified.investment
        cf_in, cf_out = _split_converted_flows(
            world.converter,
            inputs.actual_cashflows.get(investment.id),
            investment.currency,
        )
        cf_in_by_investment[investment.id] = cf_in
        cf_out_by_investment[investment.id] = cf_out

    return aggregate_fund_composition(
        [classified.investment for classified in inputs.investments],
        nav_by_investment,
        cf_in_by_investment,
        cf_out_by_investment,
        report_date,
    )


def _split_converted_flows(
    converter: PortfolioFxConverter,
    frame: pd.DataFrame | None,
    currency: str,
) -> tuple[pd.Series, pd.Series]:
    """Split converted realised flows into ``(cf_in, cf_out)`` for the IRR helper.

    Mirrors :func:`services.analytics.investment_returns._split_cashflows_for_irr`,
    but converts each flow to the functional currency first (point-in-time at its
    own date) so composition IRR is stated in the same currency as the NAV
    weights. Positive amounts (distributions) aggregate into ``cf_in``, negative
    (calls) into ``cf_out`` — both date-indexed, UTC-normalised.
    """
    empty_index = pd.DatetimeIndex([], tz="UTC")
    empty = pd.Series(dtype="float64", index=empty_index)
    if frame is None or frame.empty:
        return empty, empty.copy()

    stamps = pd.to_datetime(frame["flow_timestamp"], utc=True).dt.normalize()
    amounts = [
        float(converter.convert_amount(Decimal(str(raw)), currency, stamp.date()))
        for raw, stamp in zip(frame["amount"], stamps)
    ]
    converted = pd.DataFrame({"flow_timestamp": stamps, "amount": amounts})
    cf_in = (
        converted.loc[converted["amount"] > 0.0]
        .groupby("flow_timestamp")["amount"]
        .sum()
        .sort_index()
    )
    cf_out = (
        converted.loc[converted["amount"] < 0.0]
        .groupby("flow_timestamp")["amount"]
        .sum()
        .sort_index()
    )
    return cf_in, cf_out


# ---------------------------------------------------------------------------
# Delta assembly
# ---------------------------------------------------------------------------


def _combine(
    inputs: ScenarioResultInputs,
    baseline: _WorldMetrics,
    scenario: _WorldMetrics,
) -> ScenarioResult:
    """Fold the two worlds' figures into the deltas-first result (ADR-0104 §5)."""
    period_ends = tuple(inputs.evaluation_dates)
    seam_index = sum(1 for period_end in inputs.evaluation_dates if period_end <= inputs.cut_over)

    nav_path = ScenarioSeriesPair(
        period_ends=period_ends,
        seam_index=seam_index,
        baseline=baseline.nav_total,
        scenario=scenario.nav_total,
    )
    return_index = ScenarioSeriesPair(
        period_ends=period_ends,
        seam_index=seam_index,
        baseline=baseline.return_index,
        scenario=scenario.return_index,
    )

    plan_dates = [
        period_end for period_end in inputs.evaluation_dates if period_end > inputs.cut_over
    ]
    horizon_stamp = pd.Timestamp(plan_dates[-1]) if plan_dates else None
    plan_stamps = {pd.Timestamp(period_end) for period_end in plan_dates}

    headroom = _headroom_deltas(baseline, scenario, horizon_stamp)
    kpis = _kpi_deltas(baseline, scenario, headroom, plan_stamps)
    composition = CompositionPair(baseline=baseline.composition, scenario=scenario.composition)
    return ScenarioResult(
        nav_path=nav_path,
        return_index=return_index,
        kpis=kpis,
        headroom=headroom,
        composition=composition,
    )


def _headroom_deltas(
    baseline: _WorldMetrics,
    scenario: _WorldMetrics,
    horizon_stamp: pd.Timestamp | None,
) -> tuple[FamilyHeadroomDelta, ...]:
    """Reduce the two coverage runs to per-family headroom deltas (ADR-0104 §5).

    Per family, the per-class utilisation and headroom at the plan horizon in
    each world, matched by ``class_key``, with their deltas — the §7 table's
    grain. The horizon is the last plan evaluation date, the representative plan
    figure the mockup table and the tightest-headroom KPI both read.
    """
    families: list[FamilyHeadroomDelta] = []
    for family in (_SAA, _ANLV):
        baseline_family = getattr(baseline.coverage, family)
        scenario_family = getattr(scenario.coverage, family)
        baseline_rows = _rows_at(baseline_family.coverage, horizon_stamp)
        scenario_rows = _rows_at(scenario_family.coverage, horizon_stamp)

        rows: list[HeadroomClassDelta] = []
        for class_key in sorted(set(baseline_rows) | set(scenario_rows)):
            base = baseline_rows.get(class_key)
            scen = scenario_rows.get(class_key)
            base_cov, base_head, base_status = base or (None, None, None)
            scen_cov, scen_head, scen_status = scen or (None, None, None)
            rows.append(
                HeadroomClassDelta(
                    family=family,
                    class_key=class_key,
                    baseline_coverage_pct=base_cov,
                    scenario_coverage_pct=scen_cov,
                    delta_coverage_pct=_subtract(scen_cov, base_cov),
                    baseline_headroom_eur=base_head,
                    scenario_headroom_eur=scen_head,
                    delta_headroom_eur=_subtract(scen_head, base_head),
                    baseline_status=base_status,
                    scenario_status=scen_status,
                )
            )
        families.append(FamilyHeadroomDelta(family=family, rows=tuple(rows)))
    return tuple(families)


def _kpi_deltas(
    baseline: _WorldMetrics,
    scenario: _WorldMetrics,
    headroom: tuple[FamilyHeadroomDelta, ...],
    plan_stamps: set[pd.Timestamp],
) -> tuple[KpiDelta, ...]:
    """Form the four v1 KPI-delta tiles, in tile order (ADR-0104 §5, §7).

    * **AUM** — Σ NAV incl. cash at the horizon (the last ``nav_total``). This
      value **falls** between baseline and scenario for TA funds and deferred
      (re-paced) calls, and a reader must not hunt for the offsetting position
      that rose: E4 (ADR-0105 §15) forbids the offsetting NAV path, so a
      generated or re-paced call moves cash down with no plan NAV moving up. The
      fall is the chosen v1 posture — ``execute_repace_flows`` moves flows and
      ``_with_ta_profiles`` generates them, each asserting no NAV consequence,
      with ``execute_market_shock`` the deliberate counter-case; the tile reports
      the fall, it does not net it out.
    * **Tightest AnlV headroom** — the minimum headroom over the AnlV family's
      horizon rows, reached from the very :class:`FamilyHeadroomDelta` the §7
      table draws, so the tile and the table agree by construction.
    * **Functional cash at t₀+4Q** — the cash balance a year past the seam.
    * **Limit breaches on the plan horizon** — the count of ``BREACH`` rows
      across both families and every plan date.
    """
    anlv = _family(headroom, _ANLV)
    baseline_tightest = _tightest(row.baseline_headroom_eur for row in anlv.rows)
    scenario_tightest = _tightest(row.scenario_headroom_eur for row in anlv.rows)

    baseline_breaches = _breach_count(baseline.coverage, plan_stamps)
    scenario_breaches = _breach_count(scenario.coverage, plan_stamps)

    return (
        KpiDelta(
            key="aum",
            label="AUM (Σ NAV incl. cash)",
            unit="functional_currency",
            baseline=_last_value(baseline.nav_total),
            scenario=_last_value(scenario.nav_total),
            delta=_subtract(
                _last_value(scenario.nav_total),
                _last_value(baseline.nav_total),
            ),
        ),
        KpiDelta(
            key="tightest_anlv_headroom",
            label="Tightest AnlV headroom",
            unit="functional_currency",
            baseline=baseline_tightest,
            scenario=scenario_tightest,
            delta=_subtract(scenario_tightest, baseline_tightest),
        ),
        KpiDelta(
            key="functional_cash_t0_plus_4q",
            label="Cash (functional, t₀+4Q)",
            unit="functional_currency",
            baseline=baseline.cash_t0_plus_4q,
            scenario=scenario.cash_t0_plus_4q,
            delta=_subtract(scenario.cash_t0_plus_4q, baseline.cash_t0_plus_4q),
        ),
        KpiDelta(
            key="limit_breaches",
            label="Limit breaches (plan horizon)",
            unit="count",
            baseline=baseline_breaches,
            scenario=scenario_breaches,
            delta=scenario_breaches - baseline_breaches,
        ),
    )


def _breach_count(coverage: CoverageEngineResult, plan_stamps: set[pd.Timestamp]) -> int:
    """Count ``BREACH`` coverage rows across both families and the plan dates."""
    total = 0
    for family in (coverage.saa, coverage.anlv):
        frame = family.coverage
        if frame.empty:
            continue
        matched = frame[frame["as_of_date"].isin(plan_stamps) & (frame["status"] == _STATUS_BREACH)]
        total += len(matched)
    return total


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _rows_at(
    coverage: pd.DataFrame, stamp: pd.Timestamp | None
) -> dict[str, tuple[Decimal | None, Decimal | None, str]]:
    """Index one coverage frame's rows at ``stamp`` by ``class_key``."""
    if stamp is None or coverage.empty:
        return {}
    at_stamp = coverage[coverage["as_of_date"] == stamp]
    return {
        row["class_key"]: (
            row["coverage_pct"],
            row["headroom_eur"],
            row["status"],
        )
        for _, row in at_stamp.iterrows()
    }


def _family(headroom: tuple[FamilyHeadroomDelta, ...], family: str) -> FamilyHeadroomDelta:
    """Return the :class:`FamilyHeadroomDelta` for ``family`` (empty fallback)."""
    for candidate in headroom:
        if candidate.family == family:
            return candidate
    return FamilyHeadroomDelta(family=family, rows=())


def _tightest(headrooms: Iterable[Decimal | None]) -> Decimal | None:
    """Return the minimum non-``None`` headroom, or ``None`` when there is none.

    ``UNALLOCATED`` / ``NO_LIMIT`` rows carry no ceiling (``None`` headroom) and
    are skipped: an unconstrained class has no headroom to be the tightest.
    """
    values = [value for value in headrooms if value is not None]
    return min(values) if values else None


def _latest_at_or_before(dates: list[_date], values: list[Decimal], at: _date) -> Decimal | None:
    """Return the value at the latest date ``<= at``, or ``None`` (carry-forward)."""
    latest: Decimal | None = None
    for observed, value in zip(dates, values):
        if observed <= at:
            latest = value
        else:
            break
    return latest


def _last_value(
    values: Sequence[Decimal | float | None],
) -> Decimal | float | None:
    """Return the last non-``None`` value in ``values``, or ``None``."""
    for value in reversed(values):
        if value is not None:
            return value
    return None


def _subtract(
    minuend: Decimal | float | int | None,
    subtrahend: Decimal | float | int | None,
) -> Decimal | float | int | None:
    """Return ``minuend - subtrahend``, or ``None`` when either is ``None``."""
    if minuend is None or subtrahend is None:
        return None
    return minuend - subtrahend


def _as_date(stamp: object) -> _date:
    """Coerce a path index key (Timestamp or date) to a plain ``date``."""
    if isinstance(stamp, pd.Timestamp):
        return stamp.date()
    if isinstance(stamp, _date):
        return stamp
    return pd.Timestamp(stamp).date()


__all__ = [
    "CompositionPair",
    "FamilyHeadroomDelta",
    "HeadroomClassDelta",
    "KpiDelta",
    "ScenarioResult",
    "ScenarioResultInputs",
    "ScenarioSeriesPair",
    "assemble_scenario_result",
]
