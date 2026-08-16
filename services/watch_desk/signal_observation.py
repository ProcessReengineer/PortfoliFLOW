# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The one signal-family observation path the beat and the monitor share.

ADR-0116 §1 makes ``effective_watchpoints`` "*the* read — the one the beat
and the web surface share". P6 applies the same promise one layer further
down, to the **data access** behind it: what a ``price`` row on the monitor
says and what the beat measured for that subject must come from one fetch
and one producer call, not from two that happen to agree today.

So this module is the impure half of the four defined families, lifted out
of :mod:`services.irene.signal_delta` and imported by both consumers:

* the **beat** (:func:`services.irene.signal_delta.evaluate_signal_deltas`)
  runs the results through the watch-state / delta pipeline and may raise
  findings from them;
* the **monitor** (:mod:`web.routes.watch_desk`) renders the same results
  as rows, writing nothing at all.

Neither computes an observation of its own. A monitor row is therefore not
a second opinion about a subject — it is literally the number the next beat
will classify, derived at request time from the same resolution
(:func:`services.watch_desk.overlay.resolve_watch_desk`), the same fetch,
and the same pure producer. The structural guard in
``tests/regression/test_watch_desk_single_resolution.py`` pins all three
layers.

Four families, one batched fetch each
--------------------------------------
Every family reads in a fixed number of queries, never one per watchpoint
or one per subject: the cost must not scale with how much a tenant chose to
watch, nor with how large their book is — and since the monitor shares this
path, that bound is now a *page-load* budget as well as a beat budget.

* ``price`` reads ``instrument_prices`` for every watched instrument at
  once. A price move is measured inside one instrument's own currency and
  is therefore FX-free by construction — watching what the *currency* did
  is the other family's job.
* ``fx`` reads the rate frame once and derives each pair through
  :class:`~services.fx.conversion.FxConverter`. ``fx_rates`` stores
  **legs** — each currency priced against the dataset's reference currency
  — never pairs, so serving a watchpoint's ``BASE/QUOTE`` is always a
  derivation and, when QUOTE is the reference, an inversion. Fixing the
  orientation *here* rather than in the producer is required, not stylistic:
  a percentage move is not inversion-symmetric (see
  :mod:`services.analytics.fx_watch`).
* ``freshness`` reads the active book and every investment's latest actual
  NAV in one ``DISTINCT ON`` pass
  (:meth:`~core.repositories.investment_nav_repository.InvestmentNavRepository.latest_actual_by_investment`),
  then enumerates one subject per investment under the singleton
  watchpoint's rule — the quota families' enumeration pattern, applied to a
  rule that has no limit set behind it.
* ``liquidity`` resolves the book's liquid balance through the **one** AUM
  definition (:func:`services.investments.aum.compute_aum`, ADR-0103 §2),
  so the ratio's numerator is digit-for-digit the figure the Front-Office
  hero states, and reads the plan world twice: the projected calls inside
  the horizon, and whether a forward cash plan path exists at all. The
  plan path is **read, never re-materialised** (ADR-0116 §4) — this module
  imports nothing from
  :mod:`services.investments.cash_plan_materialisation` and writes no NAV.

Reads only, and that is load-bearing
------------------------------------
Nothing here writes: no watch-state upsert, no acknowledgement, no finding.
That is what lets the monitor call it on every render without advancing any
subject's state machine — rendering a row must never consume an edge. The
writes live in :func:`services.irene.signal_delta.evaluate_signal_deltas`,
which is called by the beat and by nothing else.

Conversion, and where it may fail
---------------------------------
``liquidity`` is the only family here that crosses currencies: a balance
in one currency and a call in another are not comparable until both are in
the tenant's functional currency, which happens at the ADR-0099 §4
boundary and nowhere else. A missing rate there raises
:class:`~core.exceptions.MissingFxRateError` — never a silent 1:1 fallback
— and this module turns it into a
:class:`~services.analytics.signal_watch.NoObservation` for the one
subject rather than letting it fail the tenant's whole beat (or blank the
monitor). Saying "this could not be evaluated, and here is the leg that was
missing" is both more honest and more useful than producing nothing.

Because it reads the database it lives here under ``services/watch_desk/``
beside the other impure halves of ADR-0116, never under
``services/analytics/``, and imports only from ``core`` and ``services``
(Qt-free).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import replace
from datetime import date as _date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import MissingFxRateError
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.instrument_price_repository import InstrumentPriceRepository
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentDTO, InvestmentRepository
from core.repositories.tenant_repository import TenantRepository
from services.analytics.cash_coverage_watch import (
    FLOW_TYPE_CAPITAL_CALL,
    PlannedFlow,
    coverage_horizon_end,
    evaluate_cash_coverage,
)
from services.analytics.fx_watch import evaluate_fx_watchpoint
from services.analytics.nav_freshness import evaluate_nav_freshness
from services.analytics.price_watch import evaluate_price_watchpoint
from services.analytics.signal_watch import (
    FAMILY_FRESHNESS,
    FAMILY_FX,
    FAMILY_LIQUIDITY,
    FAMILY_PRICE,
    DatedValue,
    NoObservation,
    SignalResult,
    freshness_subject_key,
)
from services.fx.conversion import FxConverter
from services.fx.functional_currency import (
    PortfolioFxConverter,
    build_portfolio_fx_converter,
)
from services.investments.aum import CASH_TYPE, compute_aum, load_nav_series
from services.watch_desk.overlay import SignalWatchpoint, WatchDeskResolution

_LOG = logging.getLogger(__name__)

#: One unit of BASE, converted into QUOTE, *is* the pair rate — which is
#: how the orientation gets fixed with the existing conversion seam rather
#: than a second rate arithmetic of its own.
_ONE: Decimal = Decimal("1")

#: The ``flow_kind`` the coverage denominator reads. Actual flows have
#: already settled and are therefore not something a balance must still
#: cover (ADR-0103 §5).
_PLAN_FLOW_KIND: str = "plan"

#: The ``nav_kind`` whose forward rows evidence a materialised plan path.
_PLAN_NAV_KIND: str = "plan"

#: The evaluation order — family order, then the registry's stable order
#: within a family, and (for ``freshness``) the book's own order. The beat
#: reports its eligible findings in it and the monitor lists its groups in
#: it, so the two surfaces never present the same subjects in two orders.
SIGNAL_FAMILY_ORDER: tuple[str, ...] = (FAMILY_PRICE, FAMILY_FX, FAMILY_FRESHNESS, FAMILY_LIQUIDITY)

__all__ = ["SIGNAL_FAMILY_ORDER", "observe_signal_families", "observe_signal_family"]


async def observe_signal_families(
    session: AsyncSession,
    *,
    as_of: _date,
    resolution: WatchDeskResolution,
) -> list[tuple[SignalWatchpoint, SignalResult]]:
    """Evaluate every effective signal watchpoint at ``as_of``, writing nothing.

    Must run on a **tenant-scoped** session: every read is RLS-policed for
    the active tenant.

    A ``freshness`` watchpoint is one row but many subjects — one per active
    investment — so this is also where the book is enumerated. The subjects
    it produces resolve their mute and their thresholds through the
    singleton's own settings (ADR-0116 §4).

    Args:
        session: A tenant-scoped session.
        as_of: The evaluation date. The beat passes its clock's date; the
            monitor passes request time's date, which is why a row and the
            next beat's classification of it agree.
        resolution: This tenant's effective calibration, resolved **once**
            by :func:`services.watch_desk.overlay.resolve_watch_desk` and
            threaded in as a plain argument. There is no default: a second
            resolution path is exactly what ADR-0116 §1 forbids. A retired
            watchpoint is absent from it, which is how retirement stops both
            evaluation and rendering.

    Returns:
        ``(watchpoint, result)`` pairs in ``(price, fx, freshness,
        liquidity)`` family order, then registry order. Empty when the
        tenant watches nothing. A pair whose result is a
        :class:`~services.analytics.signal_watch.NoObservation` is
        **returned, not dropped**: the caller decides what silence means —
        the beat writes no watch-state row for it, the monitor renders it as
        an explicit "no data" row.
    """
    evaluated: list[tuple[SignalWatchpoint, SignalResult]] = []
    for family in SIGNAL_FAMILY_ORDER:
        evaluated += await observe_signal_family(
            session, family, as_of=as_of, resolution=resolution
        )
    return evaluated


async def observe_signal_family(
    session: AsyncSession,
    family: str,
    *,
    as_of: _date,
    resolution: WatchDeskResolution,
) -> list[tuple[SignalWatchpoint, SignalResult]]:
    """Evaluate one family's effective watchpoints, writing nothing.

    The per-family seam behind :func:`observe_signal_families`, so the
    monitor can render one group at a time without fetching the other three
    — and so that adding a fifth family means adding one evaluator here and
    one name to :data:`SIGNAL_FAMILY_ORDER`, not teaching a second module the
    list.

    Args:
        session: A tenant-scoped session.
        family: One of the four defined families. A family this module has
            no evaluator for yields an empty list with a warning rather
            than raising: the resolution already filters to the families
            with a producer, so reaching that branch means the two lists
            drifted, which is worth saying out loud.
        as_of: The evaluation date.
        resolution: The tenant's effective calibration.

    Returns:
        The family's ``(watchpoint, result)`` pairs, in registry order.
    """
    watchpoints = resolution.signals_for(family)
    if not watchpoints:
        return []
    evaluator = _EVALUATOR_BY_FAMILY.get(family)
    if evaluator is None:  # pragma: no cover - the resolution filters first
        _LOG.warning(
            "watch-desk observation: no evaluator for family %r; %d watchpoint(s) skipped.",
            family,
            len(watchpoints),
        )
        return []
    return await evaluator(session, watchpoints, as_of=as_of, resolution=resolution)


# ---------------------------------------------------------------------------
# One fetch per family, then the pure producer per subject.
# ---------------------------------------------------------------------------


async def _observe_price(
    session: AsyncSession,
    watchpoints: tuple[SignalWatchpoint, ...],
    *,
    as_of: _date,
    resolution: WatchDeskResolution,
) -> list[tuple[SignalWatchpoint, SignalResult]]:
    """Fetch every watched instrument's prices at once, then evaluate each.

    The window is opened at the **widest** watchpoint's start, so one query
    serves them all; a narrower watchpoint simply resolves its reference
    later inside the same rows. The repository's soft lower bound supplies
    the carry-forward anchor, so a sparse series still has a reference.
    """
    # Both families state a window in days (the b033 CHECKs force it
    # non-NULL for them); the filter narrows the type rather than
    # inventing a fallback window a subject was never measured on.
    from_date = as_of - timedelta(
        days=max(w.window_days for w in watchpoints if w.window_days is not None)
    )
    investment_ids = [w.instrument_id for w in watchpoints if w.instrument_id is not None]
    rows = await InstrumentPriceRepository(session).list_by_investments(
        investment_ids, from_date=from_date, to_date=as_of
    )

    series_by_investment: dict[UUID, list[DatedValue]] = defaultdict(list)
    for row in rows:
        series_by_investment[row.investment_id].append(
            DatedValue(as_of_date=row.as_of_date, value=row.price)
        )

    evaluated: list[tuple[SignalWatchpoint, SignalResult]] = []
    for watchpoint in watchpoints:
        prices = (
            series_by_investment.get(watchpoint.instrument_id, [])
            if watchpoint.instrument_id is not None
            else []
        )
        evaluated.append(
            (
                watchpoint,
                evaluate_price_watchpoint(
                    subject_key=watchpoint.subject_key,
                    prices=prices,
                    drop_pct=watchpoint.threshold_pct,
                    window_days=watchpoint.window_days,
                    as_of=as_of,
                    warn_threshold_pct=resolution.warn_threshold_for(watchpoint.subject_key),
                ),
            )
        )
    return evaluated


async def _observe_fx(
    session: AsyncSession,
    watchpoints: tuple[SignalWatchpoint, ...],
    *,
    as_of: _date,
    resolution: WatchDeskResolution,
) -> list[tuple[SignalWatchpoint, SignalResult]]:
    """Fetch the rate frame once, then derive and evaluate each pair.

    One query for every currency named by any watched pair. The reference
    currency is read off the frame rather than assumed — it is a property
    of the *dataset*, not of the tenant (ADR-0099 §2) — and a tenant with
    no rates at all yields a no-observation per pair rather than an
    exception.
    """
    currencies = sorted({code for w in watchpoints for code in _pair_legs(w.currency_pair) if code})
    # Both families state a window in days (the b033 CHECKs force it
    # non-NULL for them); the filter narrows the type rather than
    # inventing a fallback window a subject was never measured on.
    from_date = as_of - timedelta(
        days=max(w.window_days for w in watchpoints if w.window_days is not None)
    )
    frame = await FxRateRepository(session).load_rates_frame(
        currencies, from_date=from_date, to_date=as_of
    )
    if frame.empty:
        return [
            (
                watchpoint,
                NoObservation(
                    subject_key=watchpoint.subject_key,
                    reason="the tenant holds no FX rates for either leg of the pair",
                ),
            )
            for watchpoint in watchpoints
        ]

    converter = FxConverter(frame, str(frame["reference_currency"].iloc[0]))
    observation_dates = sorted({ts.date() for ts in frame["as_of_date"]})

    evaluated: list[tuple[SignalWatchpoint, SignalResult]] = []
    for watchpoint in watchpoints:
        base, quote = _pair_legs(watchpoint.currency_pair)
        evaluated.append(
            (
                watchpoint,
                evaluate_fx_watchpoint(
                    subject_key=watchpoint.subject_key,
                    rates=_pair_series(converter, base, quote, observation_dates),
                    move_pct=watchpoint.threshold_pct,
                    window_days=watchpoint.window_days,
                    as_of=as_of,
                    warn_threshold_pct=resolution.warn_threshold_for(watchpoint.subject_key),
                ),
            )
        )
    return evaluated


async def _observe_freshness(
    session: AsyncSession,
    watchpoints: tuple[SignalWatchpoint, ...],
    *,
    as_of: _date,
    resolution: WatchDeskResolution,
) -> list[tuple[SignalWatchpoint, SignalResult]]:
    """Enumerate the active book under the singleton rule, then age each row.

    Two queries whatever the size of the book: the active universe, and
    every one of its latest actual NAVs in one ``DISTINCT ON`` pass. The
    per-investment watchpoint handed back is the singleton's own row with
    the subject key and the investment's name substituted, so the
    parameters, the mute and the identity all remain the singleton's — the
    book supplies subjects, never settings.

    "The book" is ``list_active()``, the same universe AUM, coverage and
    the charts mean by it and the same one the P2 seeding derived its
    defaults from. A deactivated position is not something to complain
    about the staleness of.
    """
    watchpoint = _singleton(watchpoints, family=FAMILY_FRESHNESS)
    if watchpoint.max_age_days is None:  # pragma: no cover - CHECK-guarded
        return []

    book = await InvestmentRepository(session).list_active()
    if not book:
        return []
    latest_navs = await InvestmentNavRepository(session).latest_actual_by_investment(
        [investment.id for investment in book]
    )

    evaluated: list[tuple[SignalWatchpoint, SignalResult]] = []
    for investment in book:
        subject_key = freshness_subject_key(investment.id)
        nav = latest_navs.get(investment.id)
        evaluated.append(
            (
                replace(watchpoint, subject_key=subject_key, display_name=investment.name),
                evaluate_nav_freshness(
                    subject_key=subject_key,
                    latest_nav=(
                        None
                        if nav is None
                        else DatedValue(as_of_date=nav.as_of_date, value=nav.nav_value)
                    ),
                    max_age_days=watchpoint.max_age_days,
                    as_of=as_of,
                    warn_threshold_pct=resolution.warn_threshold_for(subject_key),
                ),
            )
        )
    return evaluated


async def _observe_liquidity(
    session: AsyncSession,
    watchpoints: tuple[SignalWatchpoint, ...],
    *,
    as_of: _date,
    resolution: WatchDeskResolution,
) -> list[tuple[SignalWatchpoint, SignalResult]]:
    """Assemble the coverage ratio's two sides, then measure them.

    The numerator is resolved through the **one** AUM definition
    (:func:`services.investments.aum.compute_aum`), so the balance the
    watchpoint divides is digit-for-digit the ``Cash`` figure the
    Front-Office hero states — a discrepancy between the two would be a
    second definition of the same quantity, which ADR-0103 §2 exists to
    prevent. The denominator is the plan world's projected calls inside the
    horizon, converted at the same seam.

    The plan path is **read**, never re-materialised (ADR-0116 §4): its
    forward rows are consulted only to answer "has this book been projected
    at all", and the answer is passed to the producer as a plain bool.
    """
    watchpoint = _singleton(watchpoints, family=FAMILY_LIQUIDITY)
    if watchpoint.horizon_months is None or watchpoint.min_coverage_ratio is None:
        return []  # pragma: no cover - CHECK-guarded

    subject_key = watchpoint.subject_key
    investments = InvestmentRepository(session)
    navs = InvestmentNavRepository(session)
    series = await load_nav_series(investments=investments, navs=navs)
    if not series:
        return [
            (
                watchpoint,
                NoObservation(
                    subject_key=subject_key,
                    reason="the tenant holds no active investment to value",
                ),
            )
        ]

    book = [investment for investment, _dates, _values in series]
    horizon_end = coverage_horizon_end(as_of, watchpoint.horizon_months)

    try:
        fx = await build_portfolio_fx_converter(
            tenants=TenantRepository(session),
            fx_rates=FxRateRepository(session),
            position_currencies=[investment.currency for investment in book],
        )
        liquid_balance = compute_aum(series, as_of, fx).cash
        planned_flows = await _projected_flows(
            session, book, fx=fx, opens_after=as_of, closes_on=horizon_end
        )
    except MissingFxRateError as exc:
        # Never a silent 1:1 fallback (ADR-0099), and never a failed beat
        # either: the one subject says what was missing and the rest of the
        # tenant's watchpoints are unaffected.
        return [
            (
                watchpoint,
                NoObservation(
                    subject_key=subject_key,
                    reason=f"a position or projected flow could not be converted — {exc}",
                ),
            )
        ]

    has_forward_plan_path = await _has_forward_plan_path(navs, book, after=as_of)

    return [
        (
            watchpoint,
            evaluate_cash_coverage(
                subject_key=subject_key,
                liquid_balance=liquid_balance,
                planned_flows=planned_flows,
                horizon_months=watchpoint.horizon_months,
                min_coverage_ratio=watchpoint.min_coverage_ratio,
                as_of=as_of,
                warn_threshold_pct=resolution.warn_threshold_for(subject_key),
                has_forward_plan_path=has_forward_plan_path,
            ),
        )
    ]


#: The four evaluators, keyed by family. The only place this module tells
#: the families apart, mirroring ``_SHAPE_BY_FAMILY`` on the resolution side.
_EVALUATOR_BY_FAMILY = {
    FAMILY_PRICE: _observe_price,
    FAMILY_FX: _observe_fx,
    FAMILY_FRESHNESS: _observe_freshness,
    FAMILY_LIQUIDITY: _observe_liquidity,
}


async def _projected_flows(
    session: AsyncSession,
    book: list[InvestmentDTO],
    *,
    fx: PortfolioFxConverter,
    opens_after: _date,
    closes_on: _date,
) -> list[PlannedFlow]:
    """Read the plan flows inside the horizon, converted, in one query.

    Only capital calls are converted, because only capital calls reach the
    denominator: converting the whole plan world would spend rate lookups
    on flows the ratio ignores, and — worse — could raise
    :class:`~core.exceptions.MissingFxRateError` over a currency the
    measurement never needed. The type filter is applied *again* inside the
    producer, where it is the pure, tested statement of what "what we have
    promised to pay" means; here it is a fetch narrowing, and the two
    agreeing is not a coincidence but the same constant.

    Each flow converts at **its own** projected date. Beyond the last
    stored rate the converter carries the last one forward (ADR-0060
    semantics, stated in :meth:`PortfolioFxConverter.convert_amount`),
    which is the plan-world FX convention and not a defect: the platform
    does not forecast rates, and pretending otherwise inside a liquidity
    figure would be the worst place to start.
    """
    rows = await InvestmentCashflowRepository(session).list_by_investments_and_kind(
        [investment.id for investment in book], _PLAN_FLOW_KIND
    )
    flows: list[PlannedFlow] = []
    for cashflows in rows.values():
        for cashflow in cashflows:
            flow_date = cashflow.flow_timestamp.date()
            if cashflow.flow_type != FLOW_TYPE_CAPITAL_CALL:
                continue
            if not opens_after < flow_date <= closes_on:
                continue
            flows.append(
                PlannedFlow(
                    as_of_date=flow_date,
                    flow_type=cashflow.flow_type,
                    amount=fx.convert_amount(cashflow.amount, cashflow.currency, flow_date),
                )
            )
    return flows


async def _has_forward_plan_path(
    navs: InvestmentNavRepository,
    book: list[InvestmentDTO],
    *,
    after: _date,
) -> bool:
    """Whether the book carries any forward cash projection at all.

    The gate on ADR-0116 §4's third edge rule. It asks whether a projection
    *exists*, not who wrote it: the ordinary source is the materialised
    path (``ingest_origin='system'``, ``source='computed:cash-plan'``,
    ADR-0103 §6), but an Excel-imported or manually entered plan level on a
    cash position is a forward projection too, and it is precedence-
    protected precisely because it is the operator's own. Requiring the
    materialisation marker would hand a tenant who plans in the workbook a
    permanent "no data" for a question their data answers.

    A tenant with **no** explicit cash position has no path by
    construction, and correctly reports none: without one the platform does
    not know the balance, and ADR-0103 retired the residual that used to
    guess it.

    Dates at or before ``after`` are not forward: a plan row that has been
    overtaken by the calendar says nothing about the horizon.
    """
    cash_ids = [investment.id for investment in book if investment.investment_type == CASH_TYPE]
    if not cash_ids:
        return False
    plan_navs = await navs.list_by_investments_and_kind(cash_ids, _PLAN_NAV_KIND)
    return any(row.as_of_date > after for rows in plan_navs.values() for row in rows)


def _singleton(watchpoints: tuple[SignalWatchpoint, ...], *, family: str) -> SignalWatchpoint:
    """Return the one watchpoint of a singleton family, in registry order.

    The repository enforces at most one live identity per singleton family
    (``SINGLETON_FAMILIES``), so the second branch is unreachable through
    the write path. It is stated rather than assumed because the cost of
    being wrong is silent double evaluation of the whole book, and because
    a warning naming the extra identity is what an operator would need to
    fix it.
    """
    if len(watchpoints) > 1:  # pragma: no cover - repository-guarded
        _LOG.warning(
            "watch-desk observation: %s is a singleton family but %d identities are "
            "effective; evaluating %r and ignoring the rest.",
            family,
            len(watchpoints),
            watchpoints[0].subject_key,
        )
    return watchpoints[0]


def _pair_legs(currency_pair: str | None) -> tuple[str, str]:
    """Split a ``BASE/QUOTE`` pair into its two legs.

    A malformed value yields two empty codes, which resolve to an empty
    series and therefore a no-observation. The repository's format
    validation and the b033 CHECKs make that unreachable; degrading rather
    than raising keeps one bad row from failing a tenant's whole beat.
    """
    if not currency_pair or "/" not in currency_pair:  # pragma: no cover - validated on write
        return "", ""
    base, quote = currency_pair.split("/", 1)
    return base, quote


def _pair_series(
    converter: FxConverter,
    base: str,
    quote: str,
    observation_dates: list[_date],
) -> list[DatedValue]:
    """Build the pair's rate series in the watchpoint's own orientation.

    **This is where orientation is fixed** (see the module docstring).
    Converting one unit of BASE into QUOTE is the pair rate by definition,
    and it is the existing conversion seam that does it — triangulating
    through the dataset's reference currency, inverting when QUOTE *is*
    that reference, and carrying rates forward over weekend gaps.

    Dates before a leg's first stored rate are skipped rather than
    guessed: :class:`~core.exceptions.MissingFxRateError` there means the
    frame has no anchor for that leg yet, and a fabricated rate would be
    the silent 1:1 fallback ADR-0099 exists to eliminate. The window's
    reference date is never among the skipped ones — the frame's soft
    lower bound guarantees each covered leg an anchor at or before the
    widest window's start.

    Args:
        converter: The rate-backed converter built from this beat's frame.
        base: The pair's BASE leg.
        quote: The pair's QUOTE leg.
        observation_dates: Every date any leg was quoted on, ascending.

    Returns:
        The pair series, ascending by date. Empty when neither leg is
        covered.
    """
    series: list[DatedValue] = []
    for as_of_date in observation_dates:
        try:
            series.append(
                DatedValue(
                    as_of_date=as_of_date,
                    value=converter.convert(_ONE, base, quote, as_of_date),
                )
            )
        except MissingFxRateError:
            continue
    return series
