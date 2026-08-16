# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Cash Flow Planning timeline — result assembly (ADR-0104 §3/§5).

Turns *(plan frames, actual cash history, converter)* into the one object the
Cash Flow Planning lens renders: a period grid, a **balance** row per
currency in position currency, a functional-currency total row, and the
ADR-0060 seam between realised history and plan. The chart and the table of
S2.4b both read this DTO — there is no second computation path, so the two
cannot disagree about a number (ADR-0104 §5, deltas-first).

**Balances, not net flows.** Every cell is the balance *in force* at the
period end. The convention is not invented here: a plan path is a balance
series whose value holds from its date until the next observation — the
semantics :mod:`services.overlay.steps` is built on — so sampling a period
end means taking the **latest observation at or before it**. That is the same
carry-forward rule as ADR-0060's NAV rule, as
:func:`services.investments.aum.compute_aum`'s per-investment lookup, and as
the :class:`~services.fx.conversion.FxConverter`'s rate lookup. One
convention, four places, no drift.

**Empty is not zero.** A currency with no observation at or before a period
end contributes *nothing* — an empty cell, never a zero balance. The
distinction is the house semantics of
:func:`services.investments.aum.build_nav_series` ("an investment with no
observation at or before the date was not yet in the book and contributes
nothing — not zero, *nothing*"), and it matters on this surface more than on
most: a fabricated zero balance in a currency the mandate had not yet opened
reads as a funded account that was drawn to nil.

**The seam is a single rule** (ADR-0104 §6). A period is *actual* iff its end
falls at or before ``t₀`` and *plan* iff it falls after. Actual periods are
therefore strictly left of the seam and plan periods strictly right, with no
interleaving and no period that is somehow both. The DTO carries the seam's
index and date so the template renders the amber rule without re-deriving it.

**Two layers, one grid.** ``baseline`` and ``scenario`` are built from the
same grid parameters over the same actual history — only the frames differ
(the scenario's are :func:`~services.overlay.pipeline.apply_overlay`'s
output). With an empty overlay the two timelines are value-identical, which
is the Baseline/Scenario toggle's contract (ADR-0104 §4) and the foundation
the deltas rest on.

**No conversion of the currency rows.** They stay in position currency, as
the frames are (ADR-0104 §3, N2). Only the total row crosses the ADR-0099 §4
boundary, one balance at a time, at the period end's own carry-forward rate —
which past the last actual rate *is* the last actual rate, held flat: the
plan-world FX convention (N1) falls out of the converter's existing
semantics rather than being restated here.

**This is therefore where an ``fx_shock`` acts** (ADR-0104 §2/§3, N3). It is
the one transformation kind that is not a value transformation: it restates the
held-flat path above — the converter — rather than any path in the frames, and
§3 fixes the ordering as *value transformations, then the FX restatement, then
functional aggregation*. :func:`project_cash_flow_planning` composes exactly
that. The consequence is visible in the DTO: an ``fx_shock`` moves
:attr:`CashFlowTimeline.total` and leaves every
:class:`CurrencyRow` byte-identical, because a currency's *balance* does not
move when its *translation* does.
"""

from __future__ import annotations

import logging
from calendar import monthrange
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date as _date
from decimal import Decimal
from enum import StrEnum

import pandas as pd

from core.exceptions import (
    DuplicateCashPositionError,
    PlanHorizonInvalidError,
)
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.tenant_repository import TenantRepository
from services.fx.functional_currency import (
    PortfolioFxConverter,
    build_portfolio_fx_converter,
)
from services.fx.plan_shock import shock_plan_fx_path
from services.investments.aum import CASH_TYPE, NavSeries, load_nav_series
from services.investments.plan_world import assemble_plan_frames
from services.overlay import (
    Overlay,
    PlanFrames,
    apply_overlay,
    partition_fx_shocks,
)

_LOG = logging.getLogger("portfoliflow.services.investments.cash_flow_timeline")

#: The ``nav_kind`` the actual cash history is read from. Left of the seam the
#: statement stream is the only truth (ADR-0060).
_ACTUAL: str = "actual"

#: Horizons the Planning Desk offers, in quarters (ADR-0104 §6: "8-quarter
#: horizon (4Q/12Q options)"). A closed set, validated rather than clamped: a
#: horizon the operator did not choose is a wrong answer, not a near one.
HORIZON_QUARTERS: frozenset[int] = frozenset({4, 8, 12})

#: The default horizon (ADR-0104 §6).
DEFAULT_HORIZON_QUARTERS: int = 8

#: How many periods of realised history the timeline shows left of the seam —
#: the mockup's shape. Fewer where the book's actual history is shorter (an
#: empty column is not history), never more.
ACTUAL_PERIODS: int = 2

#: Months per period, by periodisation. Drives both the grid step and the
#: horizon translation: a quarterly horizon of 8 is 8 quarterly columns or 24
#: monthly ones — the horizon is measured in quarters either way (ADR-0104 §6).
_MONTHS_PER_PERIOD: dict[str, int] = {"quarterly": 3, "monthly": 1}


class Periodisation(StrEnum):
    """The timeline's period length (ADR-0104 §6).

    Quarterly is the default and monthly the toggle. The horizon is measured
    in **quarters** under both, so switching periodisation re-cuts the same
    span into finer columns rather than extending it.

    Members:
        QUARTERLY: Calendar quarters, ending 31 Mar / 30 Jun / 30 Sep / 31 Dec.
        MONTHLY: Calendar months, ending on each month's last day.
    """

    QUARTERLY = "quarterly"
    MONTHLY = "monthly"


@dataclass(frozen=True)
class TimelinePeriod:
    """One column of the timeline.

    Attributes:
        end_date: The period's last calendar day — the date every balance in
            this column is sampled at (latest observation at or before it).
        label: The column's display label (``'Q3 2026'``, ``'Jul 2026'``).
            Carried here because the label's *form* follows from the
            periodisation, which is this module's knowledge; the template
            formats values, not periods.
        is_actual: Whether the period lies at or before the seam. Actual
            columns are strictly left of the amber rule, plan columns strictly
            right (ADR-0104 §6) — the two never interleave.
    """

    end_date: _date
    label: str
    is_actual: bool


@dataclass(frozen=True)
class CurrencyRow:
    """One currency's balance row, in **position currency** (ADR-0104 §3, N2).

    Attributes:
        currency: The uppercased currency code — the key of the cash paths and
            of the actual history alike.
        balances: One cell per period, positionally aligned with
            :attr:`CashFlowTimeline.periods`. ``None`` is an **empty** cell —
            no balance was observed at or before that period end — and is not
            the same as a balance of zero.
    """

    currency: str
    balances: tuple[Decimal | None, ...]


@dataclass(frozen=True)
class CashFlowTimeline:
    """The rendered result: one grid, the currency rows, the total, the seam.

    The single source of numbers for the Cash Flow Planning lens (ADR-0104
    §5). The table and the chart of S2.4b both project this object; neither
    re-samples a path, re-derives the grid, or re-converts a balance.

    Attributes:
        periods: The period grid, ascending: up to :data:`ACTUAL_PERIODS`
            actual columns followed by the plan columns of the horizon.
        seam_index: The index of the **first plan period** — equivalently, the
            number of actual periods. The amber rule (ADR-0104 §6) is drawn
            immediately left of this column. ``0`` where the book's cash
            history reaches no period end at or before the seam.
        seam_date: ``t₀``, the book's last actual statement date (ADR-0060) —
            the date the rule *means*, as distinct from the column it sits
            beside.
        currency_rows: One row per currency, ordered by currency code, in
            position currency.
        total: The functional-currency total per period, positionally aligned
            with :attr:`periods`. Each currency's balance is converted at the
            period end's own carry-forward rate and the results are summed.
            ``None`` where **no** currency contributed a balance — the empty
            cell again, propagated rather than collapsed to zero.
        functional_currency: The currency :attr:`total` is stated in.
        periodisation: The grid's period length, so a consumer can label or
            axis-tick without inferring it back out of the dates.
    """

    periods: tuple[TimelinePeriod, ...]
    seam_index: int
    seam_date: _date
    currency_rows: tuple[CurrencyRow, ...]
    total: tuple[Decimal | None, ...]
    functional_currency: str
    periodisation: Periodisation


@dataclass(frozen=True)
class CashFlowPlanningResult:
    """The two worlds the lens shows side by side (ADR-0104 §4).

    Attributes:
        baseline: The plan world as the book states it — the timeline of the
            untransformed frames.
        scenario: The same timeline over the overlaid frames. For an empty
            overlay it is **value-identical** to :attr:`baseline`: baseline and
            scenario render through one code path, so the baseline cannot
            drift from the world the scenario is measured against.
    """

    baseline: CashFlowTimeline
    scenario: CashFlowTimeline


@dataclass(frozen=True, eq=False)
class CashFlowPlanningInputs:
    """Everything the pure assembly needs, read from the book in one place.

    Equality is by identity (``eq=False``): the container holds
    :class:`~services.overlay.pipeline.PlanFrames` and pandas objects, whose
    ``==`` is elementwise — a generated ``__eq__`` would return an array
    rather than a bool.

    Attributes:
        baseline: The plan frames assembled from the book (ADR-0104 §1).
        actual_cash: Per currency, the realised cash-balance path — the
            statement history left of the seam. **Display data**: it is never
            fed through an overlay. The identical-history invariant (ADR-0104
            §5) makes that a no-op by construction, and an assembly that tried
            anyway would be asserting the invariant rather than resting on it.
        converter: The functional-currency converter, built over exactly the
            **cash** currencies the timeline totals — so a book whose cash is
            functional-currency-only reads no FX row at all (the ADR-0099 §3
            zero-read guarantee).
    """

    baseline: PlanFrames
    actual_cash: Mapping[str, pd.Series]
    converter: PortfolioFxConverter


# ---------------------------------------------------------------------------
# The period grid
# ---------------------------------------------------------------------------


def _month_end(year: int, month: int) -> _date:
    """Return the last calendar day of ``(year, month)``."""
    return _date(year, month, monthrange(year, month)[1])


def _containing_period_end(day: _date, periodisation: Periodisation) -> _date:
    """Return the end of the period ``day`` falls in — at or after ``day``."""
    if periodisation is Periodisation.MONTHLY:
        return _month_end(day.year, day.month)
    quarter_month = ((day.month - 1) // 3 + 1) * 3
    return _month_end(day.year, quarter_month)


def _shift(end: _date, periods: int, periodisation: Periodisation) -> _date:
    """Return the period end ``periods`` periods away from ``end``.

    Args:
        end: A period end (the last day of its month, by construction).
        periods: How many periods to move — negative walks backwards.
        periodisation: The period length.

    Returns:
        The shifted period end, snapped to its own month's last day (so a
        31 Mar → +1 quarter lands on 30 Jun, not on an invalid 31 Jun).
    """
    step = _MONTHS_PER_PERIOD[periodisation.value]
    total = end.year * 12 + (end.month - 1) + periods * step
    year, month = divmod(total, 12)
    return _month_end(year, month + 1)


def _plan_period_count(periodisation: Periodisation, horizon_quarters: int) -> int:
    """Translate a horizon in quarters into a column count."""
    return horizon_quarters * 3 // _MONTHS_PER_PERIOD[periodisation.value]


def _label(end: _date, periodisation: Periodisation) -> str:
    """Label a period by its end date (``'Q3 2026'`` / ``'Jul 2026'``)."""
    if periodisation is Periodisation.MONTHLY:
        return f"{end:%b %Y}"
    return f"Q{(end.month - 1) // 3 + 1} {end.year}"


def _has_observation_at_or_before(paths: Mapping[str, pd.Series], end: _date) -> bool:
    """Whether *any* path carries an observation at or before ``end``."""
    return any(_sample(path, end) is not None for path in paths.values())


def _build_grid(
    *,
    t0: _date,
    actual_cash: Mapping[str, pd.Series],
    periodisation: Periodisation,
    horizon_quarters: int,
) -> tuple[TimelinePeriod, ...]:
    """Lay out the period grid around the seam (ADR-0104 §6).

    One rule fixes the two sides: a period is **actual** iff its end falls at
    or before ``t₀``, **plan** iff it falls after. So the last actual column is
    the greatest period end at or before the seam, the first plan column is
    the next one, and no period can be both — the strict left/right split the
    amber rule draws.

    The actual side is trimmed rather than padded. Its candidate columns are
    the last :data:`ACTUAL_PERIODS` ends at or before the seam, and a
    candidate is dropped when **no** currency has a balance at or before it:
    a column in which nothing was ever observed is not short history, it is a
    fabricated one. Because "observed at or before" only grows with the date,
    the drop is always a prefix — the columns that survive are contiguous and
    end at the seam.

    Args:
        t0: The plan/actual seam (ADR-0060).
        actual_cash: The realised cash paths, per currency.
        periodisation: The period length.
        horizon_quarters: The horizon, in quarters — validated by the caller.

    Returns:
        The grid, ascending: the surviving actual columns, then the horizon's
        plan columns.
    """
    containing = _containing_period_end(t0, periodisation)
    last_actual_end = containing if containing == t0 else _shift(containing, -1, periodisation)

    actual_ends = [
        _shift(last_actual_end, offset, periodisation) for offset in range(-(ACTUAL_PERIODS - 1), 1)
    ]
    observed = [end for end in actual_ends if _has_observation_at_or_before(actual_cash, end)]

    plan_ends = [
        _shift(last_actual_end, offset, periodisation)
        for offset in range(1, _plan_period_count(periodisation, horizon_quarters) + 1)
    ]

    return tuple(
        TimelinePeriod(
            end_date=end,
            label=_label(end, periodisation),
            is_actual=is_actual,
        )
        for ends, is_actual in ((observed, True), (plan_ends, False))
        for end in ends
    )


# ---------------------------------------------------------------------------
# Sampling — one convention (services/overlay/steps.py)
# ---------------------------------------------------------------------------


#: The path an unobserved currency is sampled from — empty in, ``None`` out.
#: Shared rather than rebuilt per cell: nothing here writes to a path, and
#: constructing an empty series per cell is pure waste over a 24-column grid.
_EMPTY_PATH: pd.Series = pd.Series(dtype="object", index=pd.DatetimeIndex([]))


def _sample(path: pd.Series, at: _date) -> Decimal | None:
    """Return the balance in force at ``at`` — the latest at or before it.

    The sampling half of the balance-path convention
    :mod:`services.overlay.steps` writes with: a level holds *from* its date
    until the next observation, so the value at a period end is the value of
    the latest earlier-or-equal point. A path with no such point contributes
    ``None`` — nothing, not zero (:func:`services.investments.aum.build_nav_series`).

    Both index conventions of the step primitive are honoured (a
    :class:`pandas.DatetimeIndex`, as every producer builds, and a plain
    ``object`` index of dates), so this module imposes no index convention
    the frames do not already meet. The index is assumed ascending and
    unique — the same assumption
    :func:`services.overlay.steps.add_step` makes, and every producer of a
    path upholds.

    Args:
        path: A balance path.
        at: The date to read the balance at.

    Returns:
        The balance in force, or ``None`` where the path opens later.
    """
    if path.empty:
        return None
    key = pd.Timestamp(at) if isinstance(path.index, pd.DatetimeIndex) else at
    position = int(path.index.searchsorted(key, side="right")) - 1
    if position < 0:
        return None
    return path.iloc[position]


# ---------------------------------------------------------------------------
# The pure core
# ---------------------------------------------------------------------------


def build_cash_flow_timeline(
    *,
    frames: PlanFrames,
    actual_cash: Mapping[str, pd.Series],
    converter: PortfolioFxConverter,
    periodisation: Periodisation = Periodisation.QUARTERLY,
    horizon_quarters: int = DEFAULT_HORIZON_QUARTERS,
) -> CashFlowTimeline:
    """Assemble the timeline of one world — baseline or scenario.

    Pure: no repository, no clock, no randomness. The grid is derived from
    ``frames.t0`` and the parameters alone, so the same *(book, parameters)*
    always produce the same timeline — the ADR-0104 §2 reproducibility
    contract, carried through to the surface that states the numbers.

    Which side of the seam a cell comes from follows from the grid's own rule:
    an actual column reads the statement history (``actual_cash``), a plan
    column reads the plan world (``frames.cash_paths``). The overlay is
    already folded into ``frames`` by the time this function sees them — it
    never touches the actual history, which is why the history is passed
    beside the frames rather than through them.

    Args:
        frames: The plan frames of the world to render — the baseline
            straight from :func:`~services.investments.plan_world.assemble_plan_frames`,
            or :func:`~services.overlay.pipeline.apply_overlay`'s output.
        actual_cash: Per currency, the realised cash-balance path. Display
            data, never overlaid.
        converter: The functional-currency converter (ADR-0099 §4). The
            currency rows are not converted; the total row is.
        periodisation: Quarterly (default) or monthly.
        horizon_quarters: The horizon in quarters — one of
            :data:`HORIZON_QUARTERS`.

    Returns:
        The :class:`CashFlowTimeline`.

    Raises:
        PlanHorizonInvalidError: If ``horizon_quarters`` is not one of the
            three offered horizons.
        MissingFxRateError: If a currency holding a balance has no rate at or
            before a period end. Propagated untouched — there is no silent 1:1
            fallback anywhere (ADR-0099 §3), least of all on a surface whose
            whole point is a funding figure.
    """
    if horizon_quarters not in HORIZON_QUARTERS:
        raise PlanHorizonInvalidError(
            f"cash-flow horizon of {horizon_quarters} quarter(s) is not one of "
            f"the offered horizons {sorted(HORIZON_QUARTERS)} (ADR-0104 §6)",
            field="horizon_quarters",
        )

    periods = _build_grid(
        t0=frames.t0,
        actual_cash=actual_cash,
        periodisation=periodisation,
        horizon_quarters=horizon_quarters,
    )
    currencies = sorted(set(actual_cash) | set(frames.cash_paths))

    rows = tuple(
        CurrencyRow(
            currency=currency,
            balances=tuple(
                _sample(
                    (actual_cash if period.is_actual else frames.cash_paths).get(
                        currency, _EMPTY_PATH
                    ),
                    period.end_date,
                )
                for period in periods
            ),
        )
        for currency in currencies
    )

    total = tuple(
        _total(rows, index, period.end_date, converter) for index, period in enumerate(periods)
    )

    seam_index = sum(1 for period in periods if period.is_actual)
    _LOG.info(
        "cash_flow_timeline: t0=%s periodisation=%s horizon=%dQ periods=%d "
        "seam_index=%d currencies=%s",
        frames.t0.isoformat(),
        periodisation.value,
        horizon_quarters,
        len(periods),
        seam_index,
        ",".join(currencies) or "-",
    )
    return CashFlowTimeline(
        periods=periods,
        seam_index=seam_index,
        seam_date=frames.t0,
        currency_rows=rows,
        total=total,
        functional_currency=converter.functional_currency,
        periodisation=periodisation,
    )


def _total(
    rows: tuple[CurrencyRow, ...],
    index: int,
    end: _date,
    converter: PortfolioFxConverter,
) -> Decimal | None:
    """Sum one period's balances in the functional currency (ADR-0099 §4).

    Each balance converts at **its period end's own** carry-forward rate. Past
    the last stored rate that resolves to the last stored rate, held flat —
    the plan-world FX convention (ADR-0104 §3, N1) is the converter's ordinary
    carry-forward, not a special case bolted on here.

    Args:
        rows: The currency rows.
        index: The period's column index.
        end: The period end — the conversion date.
        converter: The functional-currency converter.

    Returns:
        The total, or ``None`` where no currency contributed a balance: the
        empty cell propagates rather than collapsing to a zero the book never
        stated.

    Raises:
        MissingFxRateError: If a contributing currency has no rate at or
            before ``end``.
    """
    contributions = [
        converter.convert_amount(row.balances[index], row.currency, end)
        for row in rows
        if row.balances[index] is not None
    ]
    if not contributions:
        return None
    return sum(contributions, Decimal(0))


# ---------------------------------------------------------------------------
# The scenario composer
# ---------------------------------------------------------------------------


def project_cash_flow_planning(
    *,
    baseline: PlanFrames,
    overlay: Overlay,
    actual_cash: Mapping[str, pd.Series],
    converter: PortfolioFxConverter,
    periodisation: Periodisation = Periodisation.QUARTERLY,
    horizon_quarters: int = DEFAULT_HORIZON_QUARTERS,
) -> CashFlowPlanningResult:
    """Build both worlds of the Baseline/Scenario toggle (ADR-0104 §4/§5).

    The composer takes any valid overlay and constructs none: pacing (S2.5)
    and transaction entry (S2.6) build overlays, this function applies them.
    Both timelines are built from **identical** grid parameters over the same
    actual history, so a cell-to-cell delta is meaningful by construction —
    the deltas-first rule (ADR-0104 §5) rests on the two grids being the same
    grid, and they are the same grid because they are built by one call each
    into one function.

    With an empty overlay :func:`~services.overlay.pipeline.apply_overlay`
    returns the very frames it was given, so ``baseline`` and ``scenario`` are
    value-identical. That is the toggle's contract and not an accident of the
    data.

    **The overlay is applied at two seams, in the order ADR-0104 §3 fixes.** An
    ``fx_shock`` does not act on a value path — it "restates the held-flat path
    for its currency *before the seam conversion runs*" — and that path is not
    in the frames at all: it lives in the converter. So the parameter set splits
    (:func:`~services.overlay.pipeline.partition_fx_shocks`), the value
    transformations fold over the frames, the FX shocks restate the converter,
    and the functional aggregation inside :func:`_total` then runs through the
    restated rates. Values first, FX at the seam, aggregation last.

    The **baseline** leg converts through the converter it was handed, unshocked
    — which is why the restatement returns a new converter rather than mutating
    one (ADR-0104 §4: the two worlds are rendered from a single request, and
    §5's deltas rest on the baseline being the world the book states).

    Args:
        baseline: The plan frames assembled from the book (ADR-0104 §1).
        overlay: The ordered transformations. Empty means baseline.
        actual_cash: Per currency, the realised cash-balance path. Shared by
            both worlds untouched — an overlay never reaches realised history
            (ADR-0104 §5, the identical-history invariant).
        converter: The functional-currency converter (ADR-0099 §4). Not mutated.
        periodisation: Quarterly (default) or monthly.
        horizon_quarters: The horizon in quarters — one of
            :data:`HORIZON_QUARTERS`.

    Returns:
        The :class:`CashFlowPlanningResult` — both timelines, one grid.

    Raises:
        PlanHorizonInvalidError: If ``horizon_quarters`` is not offered.
        ExecutorNotRegisteredError: If the overlay carries a value kind with no
            executor. Not reachable for an ``fx_shock``, which is partitioned
            out before the fold sees it — the fold still refuses one, but this
            function no longer hands it one.
        OverlayExecutionError: If an executor cannot apply a transformation to
            these frames.
        MissingFxRateError: If a rate is missing at a period end — including a
            rate an ``fx_shock`` named: a shock restates a path, it never
            invents one.
    """
    value_overlay, fx_shocks = partition_fx_shocks(overlay)
    return CashFlowPlanningResult(
        baseline=build_cash_flow_timeline(
            frames=baseline,
            actual_cash=actual_cash,
            converter=converter,
            periodisation=periodisation,
            horizon_quarters=horizon_quarters,
        ),
        scenario=build_cash_flow_timeline(
            frames=apply_overlay(baseline, value_overlay),
            actual_cash=actual_cash,
            converter=shock_plan_fx_path(
                converter,
                [(shock.currency, shock.magnitude) for shock in fx_shocks],
                t0=baseline.t0,
            ),
            periodisation=periodisation,
            horizon_quarters=horizon_quarters,
        ),
    )


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


def build_actual_cash_paths(series: list[NavSeries]) -> dict[str, pd.Series]:
    """Shape the actual NAV streams of the cash positions into balance paths.

    Pure — the assembly half of the actual-history read, kept apart from the
    repository half exactly as :func:`services.investments.aum.build_nav_series`
    is, so a caller already holding the streams does not read them twice.

    The statement history of a cash position *is* its realised balance path
    (ADR-0103 §1: cash is unitised at unity price, so a statement level is a
    balance). Non-cash investments are skipped: the Cash Flow Planning lens
    states cash, and Σ over both frames would double-count a position that is
    in neither (ADR-0103 §2, the clean cash/non-cash split).

    Args:
        series: The active universe's ``nav_kind='actual'`` streams, dates
            ascending.

    Returns:
        One realised balance path per currency that has an anchored cash
        position, keyed by uppercased currency code. A cash position with no
        statement contributes no path — not an empty one and not a zero.

    Raises:
        DuplicateCashPositionError: If one currency has two active cash
            positions. The plan-world assembly refuses the same shape for the
            same reason (its path would be undefined); this builder refuses it
            rather than letting the second position silently overwrite the
            first, which would drop real money out of the total row.
    """
    paths: dict[str, pd.Series] = {}
    holders: dict[str, str] = {}
    for investment, dates, values in series:
        if investment.investment_type != CASH_TYPE:
            continue
        currency = investment.currency.upper()
        held = holders.get(currency)
        if held is not None:
            raise DuplicateCashPositionError(
                f"{currency} has two active cash positions — {held!r} and "
                f"{investment.name!r}. The cash-flow timeline carries one "
                f"balance row per currency (ADR-0104 §1) and cannot say which "
                f"of the two it states; deactivate one, or merge the balances."
            )
        holders[currency] = investment.name
        if dates:
            paths[currency] = pd.Series(values, index=pd.to_datetime(dates), dtype="object")
    return paths


async def load_cash_flow_planning_inputs(
    *,
    investments: InvestmentRepository,
    navs: InvestmentNavRepository,
    cashflows: InvestmentCashflowRepository,
    tenants: TenantRepository,
    fx_rates: FxRateRepository,
    periodisation: Periodisation,
) -> CashFlowPlanningInputs:
    """Read everything the Cash Flow Planning lens computes from.

    Repository reads and their shaping, no calculation — the
    :func:`services.investments.aum.load_nav_series` style. The three inputs
    of the pure core come from three seams that already exist, and this
    function is only their composition:

    * the **plan frames**, from
      :func:`~services.investments.plan_world.assemble_plan_frames`
      (ADR-0104 §1) — whose errors propagate untouched: a book with no seam
      has no plan world, and the honest answer is to say so rather than to
      show a timeline anchored to a clock;
    * the **actual cash history**, from the ordinary ``nav_kind='actual'``
      read of the active universe, filtered to the cash positions. It is the
      same read the plan-world assembly makes internally for its seam and its
      ADR-0060 fallback; re-reading it here (rather than reaching into the
      frames, which carry the *plan* paths only) costs one batch query and
      keeps the assembly seam's signature untouched;
    * the **converter**, built over exactly the **cash** currencies — the only
      ones the total row converts. A book whose cash is functional-currency
      only therefore reads no FX row at all (ADR-0099 §3, the zero-read
      guarantee), even where its investments hold foreign positions.

    Args:
        investments: Tenant-scoped investment repository.
        navs: Tenant-scoped NAV repository.
        cashflows: Tenant-scoped cashflow repository.
        tenants: Tenant-scoped repository supplying the functional currency.
        fx_rates: Tenant-scoped FX-rate repository.
        periodisation: The grid the caller will render on. It is **required
            rather than defaulted**: the assembly dates any generated pacing
            profile on it (ADR-0105 §2), so a caller that let it default would
            get quarterly flows under a monthly timeline and no error to say so.
            Pass the same value to :func:`project_cash_flow_planning`.

    Returns:
        The :class:`CashFlowPlanningInputs`.

    Raises:
        PlanSeamMissingError: If the book carries no active investment, or no
            actual NAV row to anchor the seam to (ADR-0104 §1).
        DuplicateCashPositionError: If one currency has two active cash
            positions.
    """
    baseline = await assemble_plan_frames(
        investments=investments,
        navs=navs,
        cashflows=cashflows,
        periodisation=periodisation,
    )
    actual_series = await load_nav_series(investments=investments, navs=navs, nav_kind=_ACTUAL)
    actual_cash = build_actual_cash_paths(actual_series)

    converter = await build_portfolio_fx_converter(
        tenants=tenants,
        fx_rates=fx_rates,
        position_currencies=set(actual_cash) | set(baseline.cash_paths),
    )
    return CashFlowPlanningInputs(baseline=baseline, actual_cash=actual_cash, converter=converter)


__all__ = [
    "ACTUAL_PERIODS",
    "DEFAULT_HORIZON_QUARTERS",
    "HORIZON_QUARTERS",
    "CashFlowPlanningInputs",
    "CashFlowPlanningResult",
    "CashFlowTimeline",
    "CurrencyRow",
    "Periodisation",
    "TimelinePeriod",
    "build_actual_cash_paths",
    "build_cash_flow_timeline",
    "load_cash_flow_planning_inputs",
    "project_cash_flow_planning",
]
