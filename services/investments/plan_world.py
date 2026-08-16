# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Plan-world baseline assembly — Layer 1 → Layer 2 (ADR-0104 §1/§3).

Reads the persisted plan world (the *book*) and returns the
:class:`~services.overlay.pipeline.PlanFrames` the overlay pipeline
transforms. This module is the **whole** of the seam between the two layers
ADR-0104 §1 separates:

* **Layer 1 — the book (persisted).** Plan NAV series, the materialised
  cash plan path (ADR-0103 §6), plan flows. Read here, written nowhere.
* **Layer 2 — the overlay (ephemeral).** Pure ``frames → frames``
  transformations, held nowhere. It cannot reach the book at all — the
  purity guard in ``tests/regression/test_overlay_layer_pure.py`` proves it —
  so *something* has to hand it the frames. This is that something, and it
  sits on the DB-coupled side of the line on purpose.

**The scenario baseline (ADR-0104 §1, D19)** is the plan world as it stands:
``nav_kind='plan'`` series and plan flows per investment, the ADR-0103 cash
plan path, with ADR-0060 carry-forward as the fallback where a plan stream is
missing. That sentence is the specification; the rest of this module is its
transcription.

Four rules carry the assembly, and none of them is invented here:

1. **The seam, ``t₀`` (ADR-0060, ADR-0103 §6).** The latest
   ``nav_kind='actual'`` NAV date over the active universe — *the book's last
   statement*. ADR-0103 §6 fixes the rule per cash position ("the anchor **is**
   the last actual statement"); the plan world needs one seam for the whole
   book (ADR-0104 §6: "the ADR-0060 seam as a single amber rule"), so the same
   rule is read over the universe rather than over one position. It is derived
   from the book and never from a clock: :func:`datetime.date.today` appears
   nowhere in this module, because a seam that moved with the wall clock would
   break the ADR-0104 §2 reproducibility contract — a scenario must be
   reproducible from *(book, parameters)* alone.

2. **Carry-forward (ADR-0060 §Decision 2/3).** A path is a **balance** series:
   the value at a date is the level in force *from* that date until the next
   observation (the semantics :mod:`services.overlay.steps` is built on). So a
   NAV stream, laid down as a path, *is* its own carry-forward — no
   interpolation, no zero-extrapolation, the last level held flat past the end.
   Where an active non-cash investment carries **no plan stream at all**, the
   ADR-0060 cross-stream fallback applies and its **actual** stream becomes the
   path: the last actual NAV, carried flat across the plan horizon. That is the
   conservative approximation ADR-0060 chose over synthesising a forecast, and
   the realistic data profile needs it — liquid mandates are not forecasted.

3. **The cash/non-cash split (ADR-0103 §2, ADR-0104 §1).** A cash position
   contributes to :attr:`~services.overlay.pipeline.PlanFrames.cash_paths` and
   to **nothing else** — it is absent from ``value_paths`` and from
   ``investments``. Σ over the two frames would otherwise count it twice, and
   the AUM-invariance anchor (ADR-0104 §5) rests on the split being clean.

4. **No conversion (ADR-0104 §3, N2).** Frames are assembled in **position
   currency**. This module takes no ``FxConverter``, reads no rate and knows
   nothing of the functional currency; conversion happens downstream at the
   ordinary ADR-0099 §4 seam, where the plan-world convention (N1: the latest
   available rate, held flat over the plan horizon) is already the
   :class:`~services.fx.conversion.FxConverter`'s own carry-forward.

A fifth rule joined them with ADR-0105, and it is the only one that puts
something in the frames the book does not hold:

5. **Generated profiles for plan-less funds (ADR-0105 §4).** A capital-account
   fund the book states no remaining plan for gets a standard
   Takahashi–Alexander profile generated in its place — ephemerally, here, at
   this seam. Its flows join ``plan_flows`` and settle against cash like any
   other plan flow, and the fund is marked in ``profile_source`` so every
   surface can badge it. See :func:`_with_ta_profiles`. The rule holds only
   *forward*: nothing is written, and the funds it touches are exactly the ones
   the pacing surface reports un-paceable, by the imported predicate rather
   than a second copy of it.

**Reads, no writes.** Three tenant-scoped repositories, five reads, nothing
persisted — not the plan world it finds, and not the profiles it generates
(ADR-0105 §4: "nothing is written", binding). The function is a *reader*: it
never repairs the book it finds. A cash position whose plan path is stale is
the materialisation service's business (ADR-0103 §6), not this module's; a
fund with no plan is nobody's, which is why the generated profile stays in
memory rather than becoming a row that would claim to be one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import date as _date
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import pandas as pd

from core.exceptions import (
    DuplicateCashPositionError,
    PlanSeamMissingError,
)
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.investment_nav_repository import (
    InvestmentNavDTO,
    InvestmentNavRepository,
)
from core.repositories.investment_repository import (
    InvestmentDTO,
    InvestmentRepository,
)
from services.investments.aum import CASH_TYPE, NavSeries, load_nav_series
from services.investments.cash_plan_materialisation import CASH_PLAN_SOURCE

# The un-paceable predicate, imported rather than restated (ADR-0105 §4,
# binding). The surface that reports a fund un-paceable and the seam that
# generates a profile for it must select the same set, or the classic drift bug
# follows: the row says "no plan" while the assembly quietly generates, or the
# reverse. There is exactly one formulation of each half, and both live in
# `pacing_rows` — `capital_account_ids` for the scope, `repaceable_flows` for
# the emptiness. `PLAN_SOURCE_TA` comes from there too, so the mark this module
# sets and the badge that module reads are one string.
from services.investments.pacing_rows import (
    PLAN_SOURCE_TA,
    capital_account_ids,
    load_called_amounts,
    repaceable_flows,
)
from services.investments.ta_profile import generate_remaining_profile
from services.overlay import PlanFlow, PlanFrames, PlanInvestment, add_step

if TYPE_CHECKING:  # pragma: no cover - type-only import kept out of runtime
    # `Periodisation` lives in `cash_flow_timeline`, which imports *this*
    # module: a runtime import here would close the cycle. It is a `StrEnum`,
    # and this module only ever passes the value through to the generator
    # (which reads it as a string key), so it never needs the class at runtime
    # and the type hint resolves statically only. `ta_profile` keeps the same
    # import under the same annotation for its own (purity) reason.
    from services.investments.cash_flow_timeline import Periodisation

_LOG = logging.getLogger("portfoliflow.services.investments.plan_world")

#: The ``nav_kind`` / ``flow_kind`` of the plan world. The actual streams are
#: read too, but only to locate the seam and to serve the ADR-0060 fallback —
#: never as plan data.
_PLAN: str = "plan"

#: The ``nav_kind`` the seam is read from (ADR-0060: left of ``t₀`` the actual
#: stream is the preferred one, and its last observation *is* the seam).
_ACTUAL: str = "actual"


def _path(dates: list[_date], values: list[Decimal]) -> pd.Series:
    """Lay a NAV stream down as a Decimal-valued balance path.

    The index is a :class:`pandas.DatetimeIndex` and the dtype is ``object``,
    which is what keeps the values :class:`~decimal.Decimal` end to end:
    :func:`services.overlay.steps.add_step` reads the dtype to decide whether
    a step is Decimal-safe, and money that round-trips through a float to gain
    a step no longer reconciles against the book.

    Args:
        dates: Observation dates, ascending (every repository read returns
            them so).
        values: The NAV values, positionally aligned with ``dates``.

    Returns:
        The balance path. Empty in, empty out — an empty path is a level of
        zero throughout, which is the same thing an absent path means
        (:func:`services.overlay.steps.zero_path`).
    """
    return pd.Series(values, index=pd.to_datetime(dates), dtype="object")


def _seam(actual_series: list[NavSeries]) -> _date:
    """Return ``t₀`` — the book's last actual statement date (ADR-0060).

    The single seam of the plan world: the latest ``nav_kind='actual'``
    observation over the whole active universe, cash positions included (a
    cash statement is a statement). ADR-0103 §6 states the rule per position;
    ADR-0104 §6 wants one amber rule for the book, so it is read over the
    universe.

    Args:
        actual_series: The actual NAV streams of the active universe, as
            :data:`~services.investments.aum.NavSeries` triples with dates
            ascending.

    Returns:
        The seam date.

    Raises:
        PlanSeamMissingError: If no active investment carries an actual NAV
            row. The book has never been stated, so there is no boundary
            between realised history and plan — and the assembly declines to
            substitute a clock for one (ADR-0104 §2).
    """
    last_observations = [dates[-1] for _, dates, _ in actual_series if dates]
    if not last_observations:
        raise PlanSeamMissingError(
            "the book carries no actual NAV row on any active investment, so "
            "it has no plan/actual seam to project from: import a statement "
            "before opening the plan world (ADR-0060, ADR-0104 §1)"
        )
    return max(last_observations)


def _cash_positions(
    plan_series: list[NavSeries],
) -> dict[str, InvestmentDTO]:
    """Index the active cash positions by their currency code.

    Args:
        plan_series: The active universe (the triples' first members).

    Returns:
        One cash position per uppercased currency code.

    Raises:
        DuplicateCashPositionError: If a currency has two active cash
            positions. See the error's docstring for why the reader refuses a
            shape the writer resolves.
    """
    positions: dict[str, InvestmentDTO] = {}
    for investment, _dates, _values in plan_series:
        if investment.investment_type != CASH_TYPE:
            continue
        currency = investment.currency.upper()
        held = positions.get(currency)
        if held is not None:
            raise DuplicateCashPositionError(
                f"{currency} has two active cash positions — {held.name!r} and "
                f"{investment.name!r}. The plan world carries one cash path "
                f"per currency (ADR-0104 §1) and cannot say which of the two "
                f"a hypothetical transaction settles against; deactivate one, "
                f"or merge the balances. Multi-custodian sub-balances per "
                f"currency are out of scope (ADR-0103 §10)."
            )
        positions[currency] = investment
    return positions


def _cash_path(
    position: InvestmentDTO,
    plan_rows: list[InvestmentNavDTO],
    anchor: tuple[_date, Decimal] | None,
) -> pd.Series | None:
    """Assemble one cash position's plan balance path (ADR-0103 §6).

    The path is the §6 projection, read back off the book::

        cash_plan(d) = balance(t₀) + Σ_{t₀ < t ≤ d} signed plan flows(t)

    — its **anchor** (the position's last actual statement: the ``balance(t₀)``
    term, and the level the path opens at) followed by the materialised
    forward rows the
    :class:`~services.investments.cash_plan_materialisation.CashPlanMaterialisationService`
    wrote for the ``Σ`` term. Reader and writer select those rows through one
    formulation: :data:`~services.investments.cash_plan_materialisation.CASH_PLAN_SOURCE`.

    Carrying the anchor point matters and is not cosmetic. Without it a
    currency whose plan flows all lie further out would open at *nothing*, and
    :func:`services.overlay.steps.add_step` would settle a hypothetical trade
    dated before the first plan flow against a zero balance — inventing a
    funding gap out of a fully funded account. The anchor is the level the
    account actually holds, and ADR-0060 already prefers the actual stream at
    and before the seam, so reading it there is the existing rule, not a new
    one.

    Args:
        position: The cash position.
        plan_rows: Its ``nav_kind='plan'`` NAV rows, any source, ascending.
        anchor: Its last actual observation as ``(date, balance)``, or
            ``None`` if it has none.

    Returns:
        The balance path, or ``None`` for a position with no anchor — which
        ADR-0103 §6 refuses to project onto ("the anchor *is* the last actual
        statement"), so the plan world carries no path for it either. A
        transformation settling in that currency then fails with
        :class:`~services.overlay.errors.MissingCashPathError`, which is the
        honest answer: a scenario cannot invent a balance nobody stated.
    """
    if anchor is None:
        _LOG.info(
            "plan_world: cash position %r (%s) has no actual NAV row — no "
            "anchor, so the plan world carries no %s cash path (ADR-0103 §6).",
            position.name,
            position.currency,
            position.currency,
        )
        return None

    anchor_date, anchor_balance = anchor
    own = [row for row in plan_rows if row.source == CASH_PLAN_SOURCE]

    foreign = len(plan_rows) - len(own)
    if foreign:
        # A plan row of another origin on a cash position (an 'excel' plan
        # column, a 'manual' edit): the materialisation service leaves it
        # standing (ADR-0098 §1 precedence) and this reader does not consume
        # it. Loud, because it means the plan path on screen omits a figure
        # the book carries.
        _LOG.warning(
            "plan_world: cash position %r carries %d plan NAV row(s) of a "
            "foreign source — not part of the materialised cash plan path "
            "(ADR-0103 §6) and not shown in the plan world.",
            position.name,
            foreign,
        )

    forward = [row for row in own if row.as_of_date > anchor_date]
    stale = len(own) - len(forward)
    if stale:
        # Own rows at or before the anchor are stale history: a statement moved
        # t₀ past them and the next materialisation run will delete them
        # (ADR-0103 §6, stranded-row cleanup). Dropping them here keeps the
        # path single-valued at the anchor date; repairing them is the writer's
        # job, not the reader's.
        _LOG.warning(
            "plan_world: cash position %r carries %d projected plan row(s) at "
            "or before its anchor %s — stale, ignored. Re-run the cash-plan "
            "materialisation to clean them up (ADR-0103 §6).",
            position.name,
            stale,
            anchor_date.isoformat(),
        )

    return _path(
        [anchor_date] + [row.as_of_date for row in forward],
        [anchor_balance] + [row.nav_value for row in forward],
    )


async def assemble_plan_frames(
    *,
    investments: InvestmentRepository,
    navs: InvestmentNavRepository,
    cashflows: InvestmentCashflowRepository,
    periodisation: Periodisation,
) -> PlanFrames:
    """Assemble the baseline :class:`PlanFrames` from the book (ADR-0104 §1).

    The Layer-1 → Layer-2 seam. Everything the overlay pipeline transforms
    enters here and nowhere else: the plan value paths, the ADR-0103 §6 cash
    plan paths, the plan flows, and the ADR-0060 seam they are all measured
    about.

    What the frames carry, and why:

    * **``t0``** — the book's last actual statement date, over the active
      universe (see :func:`_seam`).
    * **``value_paths``** — per active **non-cash** investment, its plan NAV
      path in position currency; or, where it has no plan stream at all, its
      actual stream carried forward (the ADR-0060 cross-stream fallback). An
      investment with no NAV in *either* stream gets **no path** — it was not
      yet in the book and contributes nothing, not zero (the
      :func:`services.investments.aum.build_nav_series` semantics). It stays in
      ``investments``, and the executors treat its absent path as the zero
      level it is (:func:`services.overlay.steps.zero_path`).
    * **``cash_paths``** — per currency, the ADR-0103 §6 projection: the
      position's anchor balance and its materialised forward rows. Cash
      positions appear *only* here.
    * **``plan_flows``** — every ``flow_kind='plan'`` row of the active
      universe, **unfiltered**. The exempt types (``investor_flow``,
      ADR-0103 §5) are carried like any other: the exemption is an invariant of
      the *executors*, which enforce it through
      :func:`services.investments.flow_type_invariants.is_overlay_exempt`.
      Pre-filtering them out of the frames would enforce it in a second place
      — and would hide the mandate's own flows from a Planning Desk that has to
      show them.
    * **``investments``** — the static metadata the executors dispatch on, for
      the same non-cash active universe. The raw ``investment_type`` travels;
      archetype resolution stays inside the executor (ADR-0104 §2).
    * **``profile_source``** — which funds carry a *generated* remaining
      profile rather than the book's own (see :func:`_with_ta_profiles`). Empty
      for a book the generator does not touch, which is most books.

    Currency codes are uppercased throughout the frames — the convention the
    cash-plan materialisation groups by — so a cash path, an investment's
    currency and a flow's settlement currency can be compared as keys without
    a per-call-site normalisation.

    Args:
        investments: Tenant-scoped investment repository.
        navs: Tenant-scoped NAV repository.
        cashflows: Tenant-scoped cashflow repository.
        periodisation: The period grid a generated profile's flows are dated on
            (ADR-0105 §2). It is the one *view* parameter this seam takes, and
            it is here because the generator needs it: a TA profile must sit on
            the same period-end grid a manager plan would, so switching the
            Planning Desk to monthly re-cuts the generated flow dates with the
            columns. It reaches nothing else — the book's own plan flows keep
            the dates the book states, whatever the grid.

    Returns:
        The baseline :class:`~services.overlay.pipeline.PlanFrames`. Passing
        them through :func:`services.overlay.pipeline.apply_overlay` with an
        empty overlay returns them unchanged — that is the Baseline side of the
        Planning Desk's toggle (ADR-0104 §4).

    Raises:
        PlanSeamMissingError: If the tenant has no active investment, or none
            of them carries an actual NAV row. There is no plan world to
            assemble and no seam to anchor one to — the "nothing to state" case
            (:func:`services.investments.aum.load_nav_series` returns its empty
            list for the same book), stated as a typed error rather than as an
            empty container because ``t₀`` cannot be fabricated without a clock.
        DuplicateCashPositionError: If one currency has two active cash
            positions.
    """
    plan_series = await load_nav_series(investments=investments, navs=navs, nav_kind=_PLAN)
    if not plan_series:
        raise PlanSeamMissingError(
            "the tenant has no active investment, so there is no plan world "
            "to assemble (ADR-0104 §1)"
        )

    actual_series = await load_nav_series(investments=investments, navs=navs, nav_kind=_ACTUAL)
    t0 = _seam(actual_series)
    actuals: dict[UUID, NavSeries] = {
        investment.id: (investment, dates, values) for investment, dates, values in actual_series
    }

    cash_positions = _cash_positions(plan_series)

    value_paths: dict[UUID, pd.Series] = {}
    plan_investments: dict[UUID, PlanInvestment] = {}
    for investment, dates, values in plan_series:
        if investment.investment_type == CASH_TYPE:
            continue
        plan_investments[investment.id] = PlanInvestment(
            investment_id=investment.id,
            currency=investment.currency.upper(),
            investment_type=investment.investment_type,
        )
        if not dates:
            # No plan stream: ADR-0060 falls back across the streams, and the
            # actual path *is* that fallback — its last level held flat over
            # the plan horizon.
            _, dates, values = actuals.get(investment.id, (investment, [], []))
        if dates:
            value_paths[investment.id] = _path(dates, values)

    cash_paths = await _load_cash_paths(navs=navs, positions=cash_positions, actuals=actuals)
    plan_flows = await _load_plan_flows(
        cashflows=cashflows,
        investment_ids=[investment.id for investment, _, _ in plan_series],
    )

    _LOG.info(
        "plan_world: t0=%s investments=%d value_paths=%d cash_paths=%s plan_flows=%d",
        t0.isoformat(),
        len(plan_investments),
        len(value_paths),
        ",".join(sorted(cash_paths)) or "-",
        len(plan_flows),
    )
    return await _with_ta_profiles(
        PlanFrames(
            t0=t0,
            value_paths=value_paths,
            cash_paths=cash_paths,
            plan_flows=plan_flows,
            investments=plan_investments,
        ),
        cashflows=cashflows,
        investments_by_id={investment.id: investment for investment, _, _ in plan_series},
        actuals=actuals,
        periodisation=periodisation,
    )


# ---------------------------------------------------------------------------
# The TA hook — ephemeral profiles for plan-less funds (ADR-0105 §4/§5)
# ---------------------------------------------------------------------------


async def _with_ta_profiles(
    frames: PlanFrames,
    *,
    cashflows: InvestmentCashflowRepository,
    investments_by_id: dict[UUID, InvestmentDTO],
    actuals: dict[UUID, NavSeries],
    periodisation: Periodisation,
) -> PlanFrames:
    """Fold generated remaining profiles into the frames (ADR-0105 §4).

    The whole of the TA integration. A capital-account fund the book states no
    remaining plan for gets a standard Takahashi–Alexander profile
    (:func:`~services.investments.ta_profile.generate_remaining_profile`) in its
    place: the flows join :attr:`~services.overlay.pipeline.PlanFrames.plan_flows`,
    settle against their currency's cash path exactly as a manager-plan flow
    does, and the fund is marked
    :data:`~services.investments.pacing_rows.PLAN_SOURCE_TA`. It runs **here**,
    inside the seam, so the frames leave complete and every downstream consumer
    — the pacing rows, the cash lens, the scenario result — sees one plan world.
    No consumer re-derives TA, and none can forget to.

    **Nothing is written** (ADR-0105 §4, binding). The profile exists in these
    frames and nowhere else: no ``investment_cashflows`` row, no
    ``investment_navs`` row, no ``source`` marker, no entry in the Strand-1
    §2.6 disjointness registry — there is no second writer to be disjoint from.
    The one read this adds is
    :func:`~services.investments.pacing_rows.load_called_amounts` over the
    un-paceable funds, a strict subset of the read the pacing surface already
    makes over every capital-account fund.

    **Never for a fund with a plan** (ADR-0104 D18's never-calibrate rule).
    That holds by construction rather than by check: the generator runs only
    where :func:`~services.investments.pacing_rows.repaceable_flows` is empty,
    so there is nothing to calibrate to even in principle. A book with no such
    fund gets its frames back **unchanged and uncopied** — the ADR-0105 §6
    non-interference invariant, stated as an ``is`` rather than approximated by
    a rebuild.

    **No NAV path is generated** (ADR-0105 §5, E4). A reader who has just seen
    ``current_nav`` go into the generator will expect a NAV path to come out and
    must not: the §2 recursion is machinery for sizing distributions, and v1
    surfaces TA *flows* only. The fund's ``value_paths`` entry stays the ADR-0060
    carry-forward it already was, which is why this function touches
    ``value_paths`` nowhere. Generation asserts no NAV consequence, exactly as
    ``repace_flows`` moves flows without asserting one.

    Args:
        frames: The baseline frames, as the book states them.
        cashflows: Tenant-scoped cashflow repository — for the called-to-date
            read, the one input the frames do not already carry.
        investments_by_id: The active universe, keyed by id — the commitment,
            type and currency the generator takes.
        actuals: The actual NAV streams, for each fund's ``current_nav``.
        periodisation: The grid the generated flows are dated on.

    Returns:
        The frames, with every generated profile folded in and marked; or the
        ``frames`` argument itself where nothing was generated.
    """
    un_paceable = [
        investment_id
        for investment_id in capital_account_ids(frames=frames, investments_by_id=investments_by_id)
        if not repaceable_flows(frames, investment_id)
    ]
    if not un_paceable:
        return frames

    called = await load_called_amounts(cashflows=cashflows, investment_ids=un_paceable)

    generated: list[PlanFlow] = []
    profile_source: dict[UUID, str] = {}
    for investment_id in un_paceable:
        investment = investments_by_id[investment_id]
        flows = generate_remaining_profile(
            investment_id=investment_id,
            commitment=investment.commitment_amount,
            called_to_date=called.get(investment_id, Decimal(0)),
            current_nav=_last_actual(actuals, investment_id),
            t0=frames.t0,
            investment_type=investment.investment_type,
            currency=investment.currency.upper(),
            periodisation=periodisation,
        )
        if not flows:
            # Un-modellable: the book states no commitment, or the commitment
            # is drawn and realised. The fund stays exactly as disabled as it
            # was — unmarked, no flows — and the row says why (NO_PLAN_NOTE).
            continue
        generated.extend(flows)
        profile_source[investment_id] = PLAN_SOURCE_TA

    if not generated:
        return frames

    _LOG.info(
        "plan_world: generated TA profiles for %d of %d un-paceable "
        "capital-account fund(s) on the %s grid — %d ephemeral flow(s), "
        "written nowhere (ADR-0105 §4).",
        len(profile_source),
        len(un_paceable),
        periodisation,
        len(generated),
    )
    return replace(
        frames,
        plan_flows=(*frames.plan_flows, *generated),
        cash_paths=_settled(frames.cash_paths, generated),
        profile_source=profile_source,
    )


def _last_actual(actuals: dict[UUID, NavSeries], investment_id: UUID) -> Decimal:
    """Return a fund's last actual NAV — the generator's ``current_nav`` seed.

    Zero where the fund carries no statement at all. That is not the
    "contributes nothing, not zero" case the value paths are careful about: the
    generator needs a *number* to seed its recursion, and a fund with no
    statement genuinely holds nothing yet — it builds NAV from the calls the
    profile is about to draw.
    """
    series = actuals.get(investment_id)
    if series is None:
        return Decimal(0)
    _investment, _dates, values = series
    return values[-1] if values else Decimal(0)


def _settled(
    cash_paths: Mapping[str, pd.Series], flows: Iterable[PlanFlow]
) -> dict[str, pd.Series]:
    """Settle generated flows against their currencies' cash paths.

    ADR-0105 §2 is explicit that a generated flow settles "against the cash path
    of that currency **exactly like manager-plan flows** … no new settlement
    rule", and §5 lists the cash lens among the three surfaces v1 shows TA on.
    So this is the ADR-0103 §6 projection — ``cash_plan(d) = balance(t₀) +
    Σ_{t₀ < t ≤ d} signed plan flows(t)`` — continued over the generated flows,
    through :func:`~services.overlay.steps.add_step`, the same
    settle-against-cash primitive the executors use. A manager-plan flow reaches
    the cash path via the materialisation service (which reads the book); a
    generated flow never touches the book, so it is applied here instead. The
    *rule* is one; only the place it is applied differs.

    It is not optional. ``execute_repace_flows`` moves a flow by lifting it off
    its old date and setting it down on the new one — two steps on the cash
    path. Re-pacing a fund whose flows were never settled would lift an amount
    that is not there, and the operator would watch a deferred capital call
    *raise* the cash balance.

    **A currency with no cash position is skipped, not refused.** The plan world
    holds a path only per funded currency, and the book behaves identically: the
    materialisation service writes no cash plan row for a manager-plan flow in a
    currency the mandate holds no position in. Refusing here would make a book
    that assembles today stop assembling because a fund it holds no cash for
    gained a generated profile.

    Args:
        cash_paths: The book's cash plan paths, per currency code. Not mutated.
        flows: The generated flows, in any order — ``add_step`` places each at
            its own date, so the result does not depend on the order.

    Returns:
        The cash paths, with the generated flows settled into them.
    """
    settled = dict(cash_paths)
    unsettled: set[str] = set()
    for flow in flows:
        path = settled.get(flow.currency)
        if path is None:
            unsettled.add(flow.currency)
            continue
        settled[flow.currency] = add_step(path, flow.as_of_date, flow.amount)
    if unsettled:
        _LOG.warning(
            "plan_world: generated TA flows settle in %s, which the plan world "
            "holds no cash position in — the profiles are shown and paceable, "
            "but their cash effect is not (ADR-0103 §6). The book's own plan "
            "flows in an unfunded currency are treated the same way.",
            ", ".join(sorted(unsettled)),
        )
    return settled


async def _load_cash_paths(
    *,
    navs: InvestmentNavRepository,
    positions: dict[str, InvestmentDTO],
    actuals: dict[UUID, NavSeries],
) -> dict[str, pd.Series]:
    """Read and assemble the per-currency cash plan paths (ADR-0103 §6).

    The one read this seam makes outside
    :func:`~services.investments.aum.load_nav_series`: the cash paths are
    selected by ``source``, and a
    :data:`~services.investments.aum.NavSeries` triple — dates and values only
    — cannot carry one. The rows therefore come from the repository directly,
    through the same method ``load_nav_series`` itself calls.

    Args:
        navs: Tenant-scoped NAV repository.
        positions: The active cash position per currency code.
        actuals: The actual NAV streams of the active universe, for the
            anchors.

    Returns:
        One balance path per currency that has an anchored cash position.
    """
    if not positions:
        return {}

    plan_rows = await navs.list_by_investments_and_kind(
        [position.id for position in positions.values()], _PLAN
    )

    paths: dict[str, pd.Series] = {}
    for currency, position in positions.items():
        _investment, dates, values = actuals.get(position.id, (position, [], []))
        anchor = (dates[-1], values[-1]) if dates else None
        path = _cash_path(position, plan_rows.get(position.id, []), anchor)
        if path is not None:
            paths[currency] = path
    return paths


async def _load_plan_flows(
    *,
    cashflows: InvestmentCashflowRepository,
    investment_ids: list[UUID],
) -> tuple[PlanFlow, ...]:
    """Read the active universe's plan flows, unfiltered (ADR-0104 §1).

    Every ``flow_kind='plan'`` row of every active investment — the cash
    positions' own flows and the exempt ``investor_flow`` rows included. The
    read is the one
    :meth:`~services.investments.cash_plan_materialisation.CashPlanMaterialisationService._plan_flows_by_currency`
    makes, on the same kind discriminator: the frames and the projection they
    contain must be drawn from the same rows, or the cash path would settle
    flows the plan world does not show.

    A flow's ``currency`` is its **own**, not its investment's: settlement goes
    to the cash path of the settlement currency (ADR-0103 §6), which is why
    :class:`~services.overlay.pipeline.PlanFlow` carries it separately.

    Args:
        cashflows: Tenant-scoped cashflow repository.
        investment_ids: The active universe.

    Returns:
        The plan flows, ordered by investment and then by event date — the
        repository's own order. The contract guarantees no order; this one is
        merely deterministic, which the mid-position bit-identity anchor
        (ADR-0104 §5) relies on being true of *something*.
    """
    by_investment = await cashflows.list_by_investments_and_kind(investment_ids, _PLAN)
    return tuple(
        PlanFlow(
            investment_id=investment_id,
            as_of_date=flow.flow_timestamp.date(),
            amount=flow.amount,
            currency=flow.currency.upper(),
            flow_type=flow.flow_type,
        )
        for investment_id, flows in by_investment.items()
        for flow in flows
    )


__all__ = ["assemble_plan_frames"]
