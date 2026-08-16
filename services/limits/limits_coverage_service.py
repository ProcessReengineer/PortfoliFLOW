# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""LimitsCoverageService — orchestrator for the Investment Limits section.

Sub-stream 1 of the Phase-7 Investment-Limits web surface (Kickoff
#3b). Loads the engine inputs from the repositories
(:class:`InvestmentRepository`, :class:`InvestmentNavRepository`,
:class:`LimitsRepository`, :class:`AssetClassRepository`, plus
:class:`TenantRepository` / :class:`FxRateRepository` for the conversion
seam), composes the engine's DTO compositions, resolves the date range,
builds the month-end evaluation grid, calls
:func:`services.analytics.limit_coverage.compute_coverage`, derives the
per-class limit step-line series for the renderer, and assembles the
:class:`LimitsCoverageBundle` returned to the web route.

**No AUM series is loaded (ADR-0103 §2).** ``portfolio_aum`` is gone;
the coverage denominator is ``Σ nav_functional(t)`` over the same
investments the numerators come from — cash rows included as ordinary
members (ADR-0103 §8). What that costs this layer is one load fewer and
one clamp rewritten: the evaluation range is now bounded by the book's
own NAV horizon rather than by an AUM forecast end.

Per ADR-0045 §3 the analytics layer is pure-functional and DB-free;
the database fan-out, the DTO composition and the ADR-0099 §4 currency
conversion belong here. Cross-tenant safety is enforced by the active
tenant context (RLS hides foreign-tenant rows).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from uuid import UUID

import pandas as pd
from dateutil.relativedelta import relativedelta

from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_nav_repository import (
    InvestmentNavDTO,
    InvestmentNavRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.limits_repository import LimitsRepository
from core.repositories.tenant_repository import TenantRepository
from services.analytics._dtos import (
    InvestmentWithClassCodeDTO,
    LimitSetWithLimitsDTO,
)
from services.analytics.limit_coverage import (
    FamilyCoverageResult,
    compute_coverage,
)
from services.fx.functional_currency import (
    PortfolioFxConverter,
    build_portfolio_fx_converter,
)

_LOG = logging.getLogger(__name__)

_SAA: str = "saa"
_ANLV: str = "anlv"


@dataclass(frozen=True)
class LimitsKpiStrip:
    """Headline KPI strip for the Investment Limits section.

    Aggregates status counts across both families at the most recent
    Stichtag in the evaluation range. NO_LIMIT and UNALLOCATED rows
    are excluded from the OK denominator because they do not represent
    a constrained class (there is no ceiling to compare against).

    Attributes:
        aum_eur: AUM denominator used at the most recent Stichtag, or
            ``None`` when the range carries no month-end Stichtag.
        ok_total_count: Number of class rows with ``OK`` status at
            the most recent Stichtag, summed across SAA and AnlV.
        ok_classes_denominator: Total constrained class rows
            (``OK + WARN + BREACH``) at the most recent Stichtag.
        warn_count: Number of class rows with ``WARN`` status at the
            most recent Stichtag.
        breach_count: Number of class rows with ``BREACH`` status at
            the most recent Stichtag.
    """

    aum_eur: Decimal | None
    ok_total_count: int
    ok_classes_denominator: int
    warn_count: int
    breach_count: int


@dataclass(frozen=True)
class LimitsCoverageBundle:
    """Pre-computed bundle for the Investment Limits section.

    Returned by :meth:`LimitsCoverageService.get_coverage`. ``None``
    instead of an empty bundle indicates an **empty universe** — the book
    carries no NAV observation at all (ADR-0103 §2: a book with NAVs
    always has a denominator). The route renders the empty-state partial
    in that case.

    Attributes:
        kpi_strip: Headline status counts at the most recent Stichtag.
        latest_as_of_date: The most recent month-end Stichtag in the
            evaluation range, or ``None`` when the range carries no
            month-end (e.g. same-month range without month-end).
        saa: SAA coverage rows + set history (forwarded from the
            engine).
        anlv: AnlV coverage rows + set history (forwarded from the
            engine).
        limit_step_lines: ``{family: {class_key: [(date, max_pct),
            ...]}}`` step-line series for the renderer. Per-class
            transitions across set boundaries; ``None`` values mark
            class-removed gaps.
        aum_used: Series indexed by Stichtag carrying the AUM
            denominator — ``Σ nav_functional(t)`` over every
            investment, cash included — at every evaluation date.
        evaluation_dates: The month-end Stichtage the engine was
            evaluated on.
        from_date: Lower bound of the (possibly clamped) evaluation
            range.
        to_date: Upper bound of the (possibly clamped) evaluation
            range.
    """

    kpi_strip: LimitsKpiStrip
    latest_as_of_date: date | None
    saa: FamilyCoverageResult
    anlv: FamilyCoverageResult
    limit_step_lines: dict[str, dict[str, list[tuple[date, Decimal | None]]]]
    aum_used: pd.Series
    evaluation_dates: list[date]
    from_date: date
    to_date: date


class LimitsCoverageService:
    """Aggregator for the Investment Limits section.

    Every repository must be tenant-scoped (the caller obtains them
    via :func:`core.repositories.tenant_context`). The service does
    not set or read ``app.tenant_id`` itself.
    """

    def __init__(
        self,
        investments: InvestmentRepository,
        navs: InvestmentNavRepository,
        limits: LimitsRepository,
        asset_classes: AssetClassRepository,
        tenants: TenantRepository,
        fx_rates: FxRateRepository,
    ) -> None:
        self._investments = investments
        self._navs = navs
        self._limits = limits
        self._asset_classes = asset_classes
        self._tenants = tenants
        self._fx_rates = fx_rates

    async def get_coverage(
        self,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        cut_over: date | None = None,
        warn_threshold_pct: Decimal = Decimal("90.0"),
    ) -> LimitsCoverageBundle | None:
        """Build a :class:`LimitsCoverageBundle` for the active tenant.

        The service lets engine-level exceptions
        (:class:`LimitSetNotEffective`, :class:`CoverageInputMissing`,
        :class:`CoverageInputOutOfRange`) propagate to the caller —
        the web route renders an error-state partial that names the
        condition and points at the corrective workflow.

        Args:
            from_date: Lower bound of the evaluation range. ``None``
                resolves to ``to_date - 12 months``.
            to_date: Upper bound of the evaluation range. ``None``
                resolves to the book's NAV horizon — the latest
                observation across the actual and plan streams. Values
                past it are silently clamped to it.
            cut_over: Global plan/actual cut-over date. ``None``
                resolves to ``date.today()``.
            warn_threshold_pct: WARN floor as a percentage of
                ``max_pct``. Default ``Decimal('90.0')`` matches the
                engine.

        Returns:
            A :class:`LimitsCoverageBundle` when the book carries at
            least one NAV observation. ``None`` for an empty universe —
            no investments, or none of them valued yet (ADR-0103 §2).
        """
        (
            investments_with_class,
            actual_navs,
            plan_navs,
            saa_sets,
            anlv_sets,
        ) = await self._load_engine_inputs()

        resolved = self._resolve_date_range(from_date, to_date, actual_navs, plan_navs)
        if resolved is None:
            _LOG.debug(
                "LimitsCoverageService: no NAV observation in the book — empty-universe path."
            )
            return None
        resolved_from, resolved_to = resolved

        evaluation_dates = self._build_evaluation_grid(resolved_from, resolved_to)

        if not evaluation_dates:
            _LOG.debug(
                "LimitsCoverageService: no month-end Stichtag in [%s, %s] — range-empty path.",
                resolved_from,
                resolved_to,
            )
            empty_coverage = pd.DataFrame(
                columns=[
                    "as_of_date",
                    "class_key",
                    "max_pct",
                    "nav_sum_eur",
                    "coverage_pct",
                    "headroom_eur",
                    "status",
                ]
            )
            empty_series = pd.Series([], dtype=object)
            return LimitsCoverageBundle(
                kpi_strip=LimitsKpiStrip(
                    aum_eur=None,
                    ok_total_count=0,
                    ok_classes_denominator=0,
                    warn_count=0,
                    breach_count=0,
                ),
                latest_as_of_date=None,
                saa=FamilyCoverageResult(family=_SAA, coverage=empty_coverage, set_history=[]),
                anlv=FamilyCoverageResult(family=_ANLV, coverage=empty_coverage, set_history=[]),
                limit_step_lines={_SAA: {}, _ANLV: {}},
                aum_used=empty_series,
                evaluation_dates=[],
                from_date=resolved_from,
                to_date=resolved_to,
            )

        effective_cut_over = cut_over if cut_over is not None else date.today()

        engine_result = compute_coverage(
            investments=investments_with_class,
            actual_navs=actual_navs,
            plan_navs=plan_navs,
            cut_over=effective_cut_over,
            saa_sets=saa_sets,
            anlv_sets=anlv_sets,
            evaluation_dates=evaluation_dates,
            warn_threshold_pct=warn_threshold_pct,
        )

        latest_as_of = evaluation_dates[-1]

        limit_step_lines = {
            _SAA: self._build_limit_step_lines(saa_sets),
            _ANLV: self._build_limit_step_lines(anlv_sets),
        }

        kpi_strip = self._build_kpi_strip(
            saa_coverage=engine_result.saa.coverage,
            anlv_coverage=engine_result.anlv.coverage,
            aum_used=engine_result.aum_used,
            latest_as_of_date=latest_as_of,
        )

        return LimitsCoverageBundle(
            kpi_strip=kpi_strip,
            latest_as_of_date=latest_as_of,
            saa=engine_result.saa,
            anlv=engine_result.anlv,
            limit_step_lines=limit_step_lines,
            aum_used=engine_result.aum_used,
            evaluation_dates=evaluation_dates,
            from_date=resolved_from,
            to_date=resolved_to,
        )

    # ------------------------------------------------------------------
    # Date-range resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_date_range(
        from_date: date | None,
        to_date: date | None,
        actual_navs: dict[UUID, list[InvestmentNavDTO]],
        plan_navs: dict[UUID, list[InvestmentNavDTO]],
    ) -> tuple[date, date] | None:
        """Resolve None-defaults and clamp to the book's own horizon.

        ADR-0103 §2 leaves no independent AUM series to bound the range, so
        the bound is the book: the latest NAV observation across both
        streams. Plan NAVs carry it into the future exactly as the AUM
        forecast used to, and the clamp keeps the grid from running past
        the last date the book has anything to say about (an unbounded
        carry-forward into empty space).

        Args:
            from_date: Lower bound, or ``None`` for "12 months back".
            to_date: Upper bound, or ``None`` for the book's horizon.
            actual_navs: Per-investment actual-NAV streams.
            plan_navs: Per-investment plan-NAV streams.

        Returns:
            Tuple ``(resolved_from, resolved_to)``, or ``None`` when the
            book carries no NAV observation at all — the empty-universe
            path (no investments, or none of them valued yet). A book with
            NAVs always has a denominator, so that is the only ``None``
            case left.
        """
        observed = [
            row.as_of_date
            for streams in (actual_navs, plan_navs)
            for rows in streams.values()
            for row in rows
        ]
        if not observed:
            return None
        horizon = max(observed)

        resolved_to = to_date if to_date is not None else horizon
        resolved_from = (
            from_date if from_date is not None else resolved_to - relativedelta(months=12)
        )

        if resolved_from > resolved_to:
            resolved_from, resolved_to = resolved_to, resolved_from

        if resolved_to > horizon:
            resolved_to = horizon
        if resolved_from > horizon:
            resolved_from = horizon

        return resolved_from, resolved_to

    # ------------------------------------------------------------------
    # Evaluation grid
    # ------------------------------------------------------------------

    @staticmethod
    def _build_evaluation_grid(
        from_date: date,
        to_date: date,
    ) -> list[date]:
        """Build the month-end Stichtag list for the engine.

        Args:
            from_date: Inclusive lower bound.
            to_date: Inclusive upper bound.

        Returns:
            Month-end calendar dates in ``[from_date, to_date]``, in
            chronological order. Empty list when no month-end falls
            into the range (e.g. same-month range without month-end).
        """
        ts_range = pd.date_range(start=from_date, end=to_date, freq="ME")
        return [ts.date() for ts in ts_range]

    # ------------------------------------------------------------------
    # Engine input loading
    # ------------------------------------------------------------------

    async def _load_engine_inputs(
        self,
    ) -> tuple[
        list[InvestmentWithClassCodeDTO],
        dict[UUID, list[InvestmentNavDTO]],
        dict[UUID, list[InvestmentNavDTO]],
        list[LimitSetWithLimitsDTO],
        list[LimitSetWithLimitsDTO],
    ]:
        """Load the five engine inputs from the four repositories.

        The returned tuple matches the keyword-argument order of
        :func:`services.analytics.limit_coverage.compute_coverage` so
        the caller can unpack directly. Nothing here is date-bounded: the
        NAV streams *are* the book's horizon (ADR-0103 §2), so they are
        loaded before the range is resolved, not after.
        """
        active_investments = await self._investments.list_active()
        asset_classes = await self._asset_classes.list_all()
        ac_code_by_id: dict[UUID, str] = {ac.id: ac.code for ac in asset_classes}

        investments_with_class: list[InvestmentWithClassCodeDTO] = [
            InvestmentWithClassCodeDTO(
                investment=inv,
                asset_class_code=ac_code_by_id.get(inv.asset_class_id),
            )
            for inv in active_investments
        ]

        investment_ids = [inv.investment.id for inv in investments_with_class]
        actual_navs = await self._navs.list_by_investments_and_kind(investment_ids, "actual")
        plan_navs = await self._navs.list_by_investments_and_kind(investment_ids, "plan")

        # ADR-0099 §4 conversion boundary: restate every per-investment NAV
        # series from its position currency into the functional currency
        # before the coverage engine sees it. Since ADR-0103 §2 the engine's
        # denominator is Σ of these same NAVs, so numerator and denominator
        # are converted by construction — one pass, no second seam to keep
        # in step. A single-currency tenant gets the identity pass-through
        # and reads zero FX rows.
        currency_by_inv = {
            inv.investment.id: inv.investment.currency for inv in investments_with_class
        }
        fx = await build_portfolio_fx_converter(
            tenants=self._tenants,
            fx_rates=self._fx_rates,
            position_currencies=list(currency_by_inv.values()),
        )
        actual_navs = self._convert_navs(fx, actual_navs, currency_by_inv)
        plan_navs = self._convert_navs(fx, plan_navs, currency_by_inv)

        saa_sets = await self._load_family_sets(_SAA)
        anlv_sets = await self._load_family_sets(_ANLV)

        return (
            investments_with_class,
            actual_navs,
            plan_navs,
            saa_sets,
            anlv_sets,
        )

    async def _load_family_sets(self, family: str) -> list[LimitSetWithLimitsDTO]:
        """Compose the ``LimitSetWithLimitsDTO`` list for one family.

        ``LimitsRepository.list_sets(family=...)`` returns sets sorted
        by ``effective_from`` ascending (ADR-0056 §Selection); the
        engine relies on that order.
        """
        sets = await self._limits.list_sets(family=family)
        composed: list[LimitSetWithLimitsDTO] = []
        for s in sets:
            limit_rows = await self._limits.list_limits(s.id)
            composed.append(
                LimitSetWithLimitsDTO(
                    set=s,
                    limits={row.class_key: row.max_pct for row in limit_rows},
                )
            )
        return composed

    @staticmethod
    def _convert_navs(
        fx: PortfolioFxConverter,
        navs_by_inv: dict[UUID, list[InvestmentNavDTO]],
        currency_by_inv: dict[UUID, str],
    ) -> dict[UUID, list[InvestmentNavDTO]]:
        """Restate every NAV amount into the functional currency (ADR-0099 §4).

        Each NAV converts point-in-time at its own ``as_of_date`` (exact
        Decimal arithmetic — the engine computes in Decimal). Plan NAVs
        dated beyond the last stored rate convert at that last, frozen rate;
        that is the defined ADR-0060-style carry-forward semantics, not a
        defect — a tenant wanting FX-differentiated plan years supplies
        plan-year rates in the ``FX rates`` sheet.

        On the identity fast-path the input mapping is returned unchanged, so
        a single-currency tenant is byte-identical and reads no FX rows.
        """
        if fx.is_identity:
            return navs_by_inv
        converted: dict[UUID, list[InvestmentNavDTO]] = {}
        for inv_id, rows in navs_by_inv.items():
            currency = currency_by_inv.get(inv_id, fx.functional_currency)
            converted[inv_id] = [
                replace(
                    row,
                    nav_value=fx.convert_amount(row.nav_value, currency, row.as_of_date),
                )
                for row in rows
            ]
        return converted

    # ------------------------------------------------------------------
    # Renderer-side derivatives
    # ------------------------------------------------------------------

    @staticmethod
    def _build_limit_step_lines(
        family_sets: list[LimitSetWithLimitsDTO],
    ) -> dict[str, list[tuple[date, Decimal | None]]]:
        """Derive per-class step-line series from a family's sets.

        For each class_key that appears in any set of this family,
        produces an ordered list of ``(effective_from, max_pct)``
        Sprungpunkte covering every transition. When a class is
        present in set N but absent in set N+1, a
        ``(set_{n+1}.effective_from, None)`` sentinel is appended —
        the renderer interprets ``None`` as a gap.

        Args:
            family_sets: Limit sets for one family, sorted ascending
                by ``effective_from`` (guaranteed by the repository
                per ADR-0056 §Selection).

        Returns:
            Dict keyed by ``class_key``. Each value is a list of
            ``(date, Decimal | None)`` tuples.
        """
        all_classes: set[str] = set()
        for s in family_sets:
            all_classes |= set(s.limits.keys())

        step_lines: dict[str, list[tuple[date, Decimal | None]]] = {cls: [] for cls in all_classes}

        for s in family_sets:
            eff = s.set.effective_from
            for cls in all_classes:
                if cls in s.limits:
                    step_lines[cls].append((eff, s.limits[cls]))
                else:
                    # Klasse fehlt in diesem Set: append a gap sentinel
                    # only when the previous entry carried a value
                    # (avoids a leading ``None`` for a class that was
                    # never present in earlier sets).
                    if step_lines[cls] and step_lines[cls][-1][1] is not None:
                        step_lines[cls].append((eff, None))

        return step_lines

    @staticmethod
    def _build_kpi_strip(
        *,
        saa_coverage: pd.DataFrame,
        anlv_coverage: pd.DataFrame,
        aum_used: pd.Series,
        latest_as_of_date: date | None,
    ) -> LimitsKpiStrip:
        """Aggregate KPI counts across families at the most recent Stichtag.

        ``ok_classes_denominator`` excludes ``NO_LIMIT`` and
        ``UNALLOCATED`` rows because they do not represent a
        constrained class (no ceiling to compare against).
        """
        if latest_as_of_date is None:
            return LimitsKpiStrip(
                aum_eur=None,
                ok_total_count=0,
                ok_classes_denominator=0,
                warn_count=0,
                breach_count=0,
            )

        latest_ts = pd.Timestamp(latest_as_of_date)
        try:
            aum_eur: Decimal | None = aum_used.loc[latest_ts]
        except KeyError:
            aum_eur = None

        slices = []
        for frame in (saa_coverage, anlv_coverage):
            if frame.empty:
                continue
            slice_df = frame[frame["as_of_date"] == latest_ts]
            if not slice_df.empty:
                slices.append(slice_df)

        if not slices:
            return LimitsKpiStrip(
                aum_eur=aum_eur,
                ok_total_count=0,
                ok_classes_denominator=0,
                warn_count=0,
                breach_count=0,
            )

        combined = pd.concat(slices, ignore_index=True)
        counts = combined["status"].value_counts()
        ok_total = int(counts.get("OK", 0))
        warn_total = int(counts.get("WARN", 0))
        breach_total = int(counts.get("BREACH", 0))

        return LimitsKpiStrip(
            aum_eur=aum_eur,
            ok_total_count=ok_total,
            ok_classes_denominator=ok_total + warn_total + breach_total,
            warn_count=warn_total,
            breach_count=breach_total,
        )


__all__ = [
    "LimitsCoverageBundle",
    "LimitsCoverageService",
    "LimitsKpiStrip",
]
