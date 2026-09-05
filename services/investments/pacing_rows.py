# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Drawdown-pacing rows — the control surface of ``repace_flows`` (ADR-0104 §4).

One row per **active capital-account** investment: the slider's name, meta,
enablement, and readout. The transformation itself already exists
(:func:`services.overlay.executors.execute_repace_flows`, S2.1b); this module
gives it the data a control needs to be drawn honestly, and nothing more.

**Pure.** No repository, no clock, no randomness — the same *(frames, book
metadata)* always produce the same rows, which is the ADR-0104 §2
reproducibility contract carried through to the surface that offers the
control. The one DB-coupled function, :func:`load_called_amounts`, is the
usual reads-and-shaping loader kept apart at the foot of the module (the
:mod:`services.investments.cash_flow_timeline` idiom).

**A row is offered only where the executor would accept it.** The executor
refuses a non-capital-account investment
(:class:`~services.overlay.errors.NotRepaceableError`), so the UI must not
offer a slider for one: an affordance that leads to a typed error is a
worse answer than no affordance. The archetype is resolved through
:func:`services.investments.archetype.resolve_archetype` — the single routing
formulation (ADR-0082 §1), never an ``investment_type`` literal.

**Where a rule is restated, it says so.** ``services/overlay/`` exposes no
reusable "remaining repaceable flows" selection — the predicate is inlined in
:func:`services.overlay.executors.execute_repace_flows`'s loop — so
:func:`repaceable_flows` restates it here, against the same three conditions
and the same imported exemption predicate. The **date rule** is *not*
restated: :func:`services.overlay.repaced_date` is imported from the overlay
package, because the readout has to state exactly the shift the executor will
perform. A second copy of round-half-up Decimal arithmetic would be a number
the surface promises and the engine need not keep.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date as _date
from decimal import Decimal
from uuid import UUID

from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.investment_repository import InvestmentDTO
from services.investments.archetype import Archetype, resolve_archetype
from services.investments.flow_type_invariants import is_overlay_exempt

# `repaced_date` is the executor's own date rule, reused rather than mirrored:
# the readout must state exactly the shift `execute_repace_flows` will perform,
# and a local copy of the round-half-up arithmetic would drift silently and let
# the surface promise a quarter the engine never delivers. ADR-0104 §2 makes the
# mid-position bit-identity a regression anchor for the same reason. It is
# public API of the overlay package (§8.4) precisely because it is shared this
# way — until S34.1 it was reached by a private cross-package import.
from services.overlay import (
    FACTOR_NEUTRAL,
    PlanFlow,
    PlanFrames,
    repaced_date,
)

#: The ``flow_kind`` the called-to-date figure is read from. Only realised
#: calls reduce an unfunded commitment; a *plan* call is precisely the thing
#: the slider is about to re-pace.
_ACTUAL: str = "actual"

#: The ``flow_type`` a capital-account drawdown is booked as (ADR-0043).
_CAPITAL_CALL: str = "capital_call"

#: The plan-source descriptor of a manager-supplied drawdown profile — the
#: mockup's row meta ("reported · unfunded …"). The book's own plan, and the
#: only source that is *in* the book: its sibling below is generated.
PLAN_SOURCE_REPORTED: str = "reported"

#: The plan-source descriptor of an ephemeral Takahashi–Alexander profile
#: (ADR-0105 §4/§5) — generated at the plan-world assembly seam for a
#: capital-account fund the book states no plan for, and written nowhere.
#:
#: **The single formulation of the value**, so the mark
#: :func:`~services.investments.plan_world.assemble_plan_frames` sets on
#: :attr:`~services.overlay.pipeline.PlanFrames.profile_source` and the
#: :attr:`PacingRow.plan_source` this module reads back are the same string by
#: construction rather than by coincidence. The *display* copy ("TA-generated
#: profile") is the surface's, not this constant's: a badge is user-facing text
#: and a plan source is a code value.
PLAN_SOURCE_TA: str = "ta"

#: What a capital-account fund with no remaining profile **at all** is told. It
#: is rendered **disabled with this note, never hidden** (ADR-0104 §4): a fund
#: the operator cannot pace is a fact about the book, and hiding the row would
#: state it as an absence instead.
#:
#: Since ADR-0105 §4 the note names a much narrower case than it used to. A
#: fund with no manager plan now gets a generated one, so a disabled row means
#: the generator found nothing to model either — no commitment stated, or a
#: commitment already fully drawn and realised (the two cases
#: :func:`~services.investments.ta_profile.generate_remaining_profile` returns
#: its empty list for). That is the "genuinely un-modellable" residue ADR-0105
#: §Consequences leaves behind, and it is worth naming precisely: the operator
#: is owed the reason, and "no plan" is no longer it.
NO_PLAN_NOTE: str = "no manager plan — nothing to model (no commitment, or fully realised)"

#: The slider's granularity. Not a contract constant — the bounds are
#: (:data:`services.overlay.contract.FACTOR_MIN` /
#: :data:`~services.overlay.contract.FACTOR_MAX`), the step is a control
#: choice — but it lives here rather than in the template so the route and the
#: markup cannot disagree about what the slider can emit.
FACTOR_STEP: Decimal = Decimal("0.05")


@dataclass(frozen=True)
class PacingRow:
    """One capital-account fund's pacing row (mockup ③).

    Carries what the row *is*, not how it is drawn: the factor in force, the
    slider's links and its readout text depend on the parameter set, which is
    the route's knowledge, not the book's.

    Attributes:
        investment_id: The fund the slider re-paces.
        name: Its display name, from the book.
        currency: Its position currency — the unit :attr:`unfunded` is stated
            in (the row is never converted; the ADR-0099 §4 seam is downstream
            of everything here).
        plan_source: Where the remaining profile comes from —
            :data:`PLAN_SOURCE_REPORTED` for the book's own manager plan,
            :data:`PLAN_SOURCE_TA` for a profile generated at the assembly seam
            (ADR-0105 §4), or ``None`` for a fund that has no remaining profile
            at all. The two sourced values differ in **epistemic status, not in
            mechanics**: a re-pacing treats them identically (ADR-0105 §5), and
            the difference is carried to the operator by the badge alone — which
            is why the surface must always render it.
        unfunded: ``commitment_amount − Σ|realised capital calls|``, or
            ``None`` where the book states no commitment. An unfunded figure
            derived from a commitment nobody stated would be a number invented
            to fill a column.
        profile_end: The **latest repaceable flow date** — the end of the
            remaining drawdown profile, and the input the readout's
            quarter-shift is measured on. ``None`` where the profile is empty.
    """

    investment_id: UUID
    name: str
    currency: str
    plan_source: str | None
    unfunded: Decimal | None
    profile_end: _date | None

    @property
    def enabled(self) -> bool:
        """Whether this fund has a remaining profile to re-pace.

        A property rather than a field: the enablement rule *is* "the
        repaceable set is non-empty", and :attr:`profile_end` is ``None``
        exactly then. Two fields could disagree; one cannot.
        """
        return self.profile_end is not None


def repaceable_flows(frames: PlanFrames, investment_id: UUID) -> tuple[PlanFlow, ...]:
    """Return the remaining plan flows a re-pacing would move.

    **A restatement, and deliberately labelled as one.**
    :func:`services.overlay.executors.execute_repace_flows` selects the same
    set inside its own loop and exposes no reusable selection; ADR-0104 §4's
    enablement rule ("a fund with no remaining profile renders disabled")
    needs the same set *before* any transformation exists. The three
    conditions are therefore repeated here — against the same imported
    exemption predicate, so the one rule that can actually change
    (:data:`services.investments.flow_type_invariants.OVERLAY_EXEMPT_FLOW_TYPES`)
    still has exactly one formulation:

    * the flow belongs to this investment;
    * it lies **strictly after** ``frames.t0`` — realised history is identical
      in every scenario (ADR-0104 §5), so nothing at or before the seam is part
      of a *remaining* profile;
    * it is not overlay-exempt (ADR-0103 §5: an ``investor_flow`` is the
      mandate's own capital and no scenario may move it).

    Args:
        frames: The **baseline** plan frames. The profile is a property of the
            manager plan, so it is read from the book's world, never from an
            already-overlaid one.
        investment_id: The fund whose remaining profile is wanted.

    Returns:
        The repaceable flows, in the frames' own order. Empty where the fund
        has no remaining profile — which is exactly the disabled row.
    """
    return tuple(
        flow
        for flow in frames.plan_flows
        if flow.investment_id == investment_id
        and flow.as_of_date > frames.t0
        and not is_overlay_exempt(flow.flow_type)
    )


def capital_account_ids(
    *,
    frames: PlanFrames,
    investments_by_id: Mapping[UUID, InvestmentDTO],
) -> list[UUID]:
    """Select the funds that get a pacing row — the enablement rule's *scope*.

    The single formulation of "which investments does the pacing block speak
    for", so the route's calls-to-date read and
    :func:`build_pacing_rows` cannot disagree about the set. Two conditions:

    * the archetype is :attr:`~services.investments.archetype.Archetype.CAPITAL_ACCOUNT`
      — the only one with a manager-plan drawdown profile, and the only one
      :func:`services.overlay.executors.execute_repace_flows` accepts;
    * the fund is in the plan world at all. One that is in the book but not in
      the frames would draw
      :class:`~services.overlay.errors.UnknownInvestmentError` from the
      executor, so it gets no slider to draw it with.

    Args:
        frames: The baseline plan frames.
        investments_by_id: The active universe, keyed by id.

    Returns:
        The ids, in ``investments_by_id`` iteration order.
    """
    return [
        investment_id
        for investment_id, investment in investments_by_id.items()
        if resolve_archetype(investment.investment_type) is Archetype.CAPITAL_ACCOUNT
        and investment_id in frames.investments
    ]


def build_pacing_rows(
    *,
    frames: PlanFrames,
    investments_by_id: Mapping[UUID, InvestmentDTO],
    called_by_investment: Mapping[UUID, Decimal],
) -> tuple[PacingRow, ...]:
    """Assemble the pacing rows of the Cash Flow Planning section.

    One row per **active capital-account** investment (:func:`capital_account_ids`),
    and no row for anything else: the executor refuses every other archetype
    (:class:`~services.overlay.errors.NotRepaceableError`), so offering a
    slider for one would be an affordance that only ever produces an error.

    A capital-account fund with no remaining profile still gets a row —
    disabled, with :data:`NO_PLAN_NOTE`. ADR-0104 §4 is explicit that it is not
    hidden: the operator is told *why* they cannot pace it.

    **The plan source is read, never re-derived** (ADR-0105 §4). A fund whose
    remaining profile was generated at the assembly seam is marked in
    :attr:`~services.overlay.pipeline.PlanFrames.profile_source`, and this
    function reads that mark. It does *not* re-run the un-paceable predicate to
    work out which funds were generated — by the time the frames arrive here a
    generated fund carries remaining flows and is no longer un-paceable, so the
    predicate would answer "reported" for every one of them. One marking, set
    once at the seam, is what keeps the surface and the assembly from drifting
    apart (the ADR's same-predicate rule).

    Args:
        frames: The baseline plan frames — the seam, the plan flows the
            remaining profile is read from, and the profile-source marking.
        investments_by_id: The active universe from the book, keyed by id.
            **Iteration order is the row order**: the caller hands over
            :meth:`~core.repositories.investment_repository.InvestmentRepository.list_active`'s
            result, which is name-ascending — the ordering convention every
            investment list on the surface already follows.
        called_by_investment: Realised calls to date per investment, as the
            positive figure :func:`load_called_amounts` produces. A fund absent
            from the mapping has called nothing.

    Returns:
        The rows, in ``investments_by_id`` order.
    """
    rows: list[PacingRow] = []
    for investment_id in capital_account_ids(frames=frames, investments_by_id=investments_by_id):
        investment = investments_by_id[investment_id]
        flows = repaceable_flows(frames, investment_id)
        # A generated profile is marked; anything else with remaining flows is
        # the book's own. The `or` reads in that order because the marking is
        # the exception and the manager plan the rule — and because a marked
        # fund always has flows (the seam marks only where the generator
        # produced some), so the two can never disagree about enablement.
        generated = frames.profile_source.get(investment_id)
        rows.append(
            PacingRow(
                investment_id=investment_id,
                name=investment.name,
                currency=investment.currency.upper(),
                plan_source=generated or (PLAN_SOURCE_REPORTED if flows else None),
                unfunded=unfunded_commitment(investment, called_by_investment.get(investment_id)),
                profile_end=max((flow.as_of_date for flow in flows), default=None),
            )
        )
    return tuple(rows)


def unfunded_commitment(investment: InvestmentDTO, called: Decimal | None) -> Decimal | None:
    """Return the fund's unfunded commitment, or ``None`` if unstatable.

    ``commitment_amount − Σ|realised capital calls|`` — the formulation
    :meth:`services.front_office_charts.archetype_charts_service.ArchetypeChartsService._capital_account_tiles`
    computes for the Front-Office KPI caption, **mirrored rather than reused**:
    that one is a step inside a per-investment charts assembly (rolling
    multiples, rolling IRR, the benchmark series) and cannot be called for a
    scalar without doing all of it. The rule is one line; the seam to reach it
    is not.

    Args:
        investment: The fund. Its ``commitment_amount`` may be ``None``.
        called: Realised calls to date, as a positive figure, or ``None``.

    Returns:
        The unfunded commitment, or ``None`` where the book states no
        commitment — an unfunded figure derived from a commitment nobody
        stated is a number invented to fill a column.
    """
    if investment.commitment_amount is None:
        return None
    return investment.commitment_amount - (called or Decimal(0))


# ---------------------------------------------------------------------------
# The readout — the executor's date rule, in quarters
# ---------------------------------------------------------------------------


def _quarter_index(day: _date) -> int:
    """Return a strictly monotonic index of the calendar quarter ``day`` is in."""
    return 4 * day.year + (day.month - 1) // 3


def quarter_shift(*, t0: _date, profile_end: _date, factor: Decimal) -> int:
    """Return how many **calendar quarters** a re-pacing moves the profile end.

    The convention, stated so it can be relied on: quarters are *calendar*
    quarters — the very grid the timeline's quarterly columns are cut on — and
    the shift is the difference of their indices::

        shift = quarter_index(new_end) − quarter_index(old_end)
        quarter_index(d) = 4 · d.year + (d.month − 1) // 3

    So the readout says how many **columns** the end of the drawdown profile
    moves on the table beside it, which is the question the operator is
    actually asking. It is not a day count divided by ninety-one: a shift of
    eighty days that crosses a quarter boundary moves a column, and one that
    does not, does not.

    ``new_end`` comes from the executor's own date rule
    (:func:`services.overlay.repaced_date`), imported rather than mirrored —
    ``t0 + round_half_up(factor × (profile_end − t0).days)``, in Decimal
    arithmetic. The readout therefore cannot promise a shift the engine will not
    perform.

    Args:
        t0: The plan/actual seam the scaling is measured about.
        profile_end: The latest repaceable flow date of the baseline profile.
        factor: The time-scaling factor, within the ADR-0104 §2 bounds.

    Returns:
        The signed quarter shift. Positive stretches (a slower manager),
        negative compresses, and **zero** for a factor whose shift stays inside
        the old end's own quarter — which is not the same as being on plan.
    """
    new_end = repaced_date(t0, profile_end, factor)
    return _quarter_index(new_end) - _quarter_index(profile_end)


def describe_shift(*, t0: _date, profile_end: _date, factor: Decimal) -> str:
    """Render the row's readout (mockup ③).

    Four outcomes, and the fourth is the one worth naming: a factor off the
    mid-position whose shift does not cross a quarter boundary is **not** "on
    plan" — flows have moved, a chip is in the strip, and the scenario differs
    from the book. It says so.

    Args:
        t0: The plan/actual seam.
        profile_end: The baseline profile's end date.
        factor: The factor in force.

    Returns:
        ``'on plan'`` at :data:`~services.overlay.contract.FACTOR_NEUTRAL`;
        ``'stretch +N quarters'`` / ``'compress −N quarters'`` where the shift
        crosses a boundary; ``'under a quarter'`` where it does not.
    """
    if factor == FACTOR_NEUTRAL:
        return "on plan"

    shift = quarter_shift(t0=t0, profile_end=profile_end, factor=factor)
    if shift == 0:
        return "under a quarter"

    quarters = abs(shift)
    unit = "quarter" if quarters == 1 else "quarters"
    if shift > 0:
        return f"stretch +{quarters} {unit}"
    # A true minus sign, not a hyphen — the mockup's typography, and the
    # character the rest of the surface uses for a negative figure.
    return f"compress −{quarters} {unit}"


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


async def load_called_amounts(
    *,
    cashflows: InvestmentCashflowRepository,
    investment_ids: list[UUID],
) -> dict[UUID, Decimal]:
    """Read realised calls to date, per investment — one batched query.

    The unfunded figure is the row's only input the plan world does not
    already carry: :class:`~services.overlay.pipeline.PlanFrames` holds the
    *plan* flows (ADR-0104 §1), and an unfunded commitment is reduced by
    **realised** calls. Neither the frames nor
    :func:`~services.investments.cash_flow_timeline.load_cash_flow_planning_inputs`
    has them in hand, so this is a genuinely new read — kept to the
    capital-account ids the caller asks for, and batched through the same
    repository method the plan-world assembly uses for its plan flows.

    Calls are booked negative (ADR-0043); the figure returned is their
    magnitude, so a caller subtracts it from a commitment rather than adding a
    negative.

    Args:
        cashflows: Tenant-scoped cashflow repository.
        investment_ids: The capital-account funds to read. Empty in, empty out
            — no query is issued.

    Returns:
        Realised calls to date per investment, as a positive figure. An
        investment that has called nothing is absent from the mapping.
    """
    if not investment_ids:
        return {}

    by_investment = await cashflows.list_by_investments_and_kind(investment_ids, _ACTUAL)
    called: dict[UUID, Decimal] = {}
    for investment_id, flows in by_investment.items():
        total = sum(
            (abs(flow.amount) for flow in flows if flow.flow_type == _CAPITAL_CALL),
            Decimal(0),
        )
        if total:
            called[investment_id] = total
    return called


__all__ = [
    "FACTOR_STEP",
    "NO_PLAN_NOTE",
    "PLAN_SOURCE_REPORTED",
    "PLAN_SOURCE_TA",
    "PacingRow",
    "build_pacing_rows",
    "capital_account_ids",
    "describe_shift",
    "load_called_amounts",
    "quarter_shift",
    "repaceable_flows",
    "unfunded_commitment",
]
