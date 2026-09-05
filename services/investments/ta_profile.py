# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure Takahashi–Alexander remaining-profile generator (ADR-0105 §1/§2).

The engine behind the Planning Desk's *generator for missing plans* (ADR-0104
§2, D18). For a capital-account fund with no manager plan there is nothing for
the pacing slider to time-scale; this module synthesises a standard, coarse
drawdown-and-distribution profile in its place — the classic deterministic
Takahashi–Alexander model (:mod:`services.investments.ta_profile_constants`
carries the cited per-type parameters).

**Generator only — nothing is wired in here.** This strand (S34.6) builds the
engine; the seam that runs it at plan-world assembly, labels its flows
``profile_source='ta'``, and enables pacing for the funds it covers is S34.7
(ADR-0105 §4/§5). Nothing outside this module's tests imports it yet. It reads
no repository and writes no book row: every input is a plain value handed in.

**Import-pure** — no database, no repository, no session, no web, no Qt
(ADR-0105 §1). It reuses :class:`~services.overlay.pipeline.PlanFlow` (which
lives in the equally book-free overlay package), so a generated flow *is* a
plan flow and S34.7 folds the output straight into
:attr:`~services.overlay.pipeline.PlanFrames.plan_flows` with no translation.
Guarded by ``tests/regression/test_ta_profile_pure.py``.

The model — classic deterministic Takahashi–Alexander (ADR-0105 §2)
------------------------------------------------------------------
Annual model periods ``t = 1 … L`` run forward from ``t0`` (``L`` the fund
lifetime from the parameters). Each year:

* **Contribution** ``C_t = RC_t × (commitment − called_cum_{t-1})`` — the
  rate-of-contribution schedule applied to the **remaining** uncalled balance.
  ``called_to_date`` seeds ``called_cum``, so a mid-life fund is picked up
  where it stands and the schedule only ever draws the still-unfunded
  remainder — never a re-call of capital already drawn (the analogue of
  ``repace_flows``' remaining-profile semantics, ADR-0105 §2). A commitment
  already fully drawn contributes nothing.
* **NAV recursion (internal machinery only, ADR-0105 §5)**
  ``NAV_t = NAV_{t-1} × (1 + G) + C_t − D_t``, seeded with ``current_nav``.
  This series exists **solely to size the distributions** and is *never*
  returned or exposed: v1 surfaces TA *flows* only, and asserts no plan-NAV
  path (the fund's plan NAV stays ADR-0060 carry-forward, ADR-0105 §5). A
  reader looking for a NAV trajectory out of this module will not find one, by
  design.
* **Distribution** ``D_t = d_t × (NAV_{t-1} × (1 + G))`` with the bow rate
  ``d_t = (t / L) ** B`` — the classic form, distributions taken from the
  grown prior-year NAV. (ADR-0105 §2 writes this ``d_t × NAV_t × (1 + G)``; the
  non-circular reading, and the one Takahashi–Alexander state, is the grown
  *prior* NAV — ``NAV_{t-1} × (1 + G)`` — since ``NAV_t`` itself depends on
  ``D_t``.) The final model year ``L`` is **terminal**: it liquidates the
  entire residual NAV instead of applying the bow, driving the modelled NAV to
  zero.
* **Flows are signed in the fund's position currency**, with the canonical
  ``investment_cashflows`` sign convention (ADR-0043): a ``capital_call`` is
  **negative** (it debits the settling cash path), a ``distribution`` is
  **positive** (it credits it) — matching
  :class:`~services.overlay.pipeline.PlanFlow`'s documented convention exactly.
  Settlement against the currency's cash path is S34.7's / ADR-0103 §6's, not
  this module's; the generator only emits correctly-typed, correctly-signed,
  correctly-currencied flows.
* **Deterministic, single path** (ADR-0105 §2) — no Monte-Carlo, no
  randomness, no clock. The arithmetic runs in a fixed :mod:`decimal` context
  so identical inputs yield bit-identical output.

Periodisation mapping (ADR-0105 §2)
-----------------------------------
The model is annual; the requested periodisation only re-cuts *when* each
year's two flows land, so a generated profile sits on the same period-end grid
a manager plan would. Each model year's **contribution** is dated at the first
period end of that year and its **distribution** at the year's last period end
(the annual boundary), both snapped to the quarterly (3-month) or monthly
(1-month) grid with the same last-day-of-month convention the Cash Flow
Planning timeline uses (:mod:`services.investments.cash_flow_timeline` — whose
period helpers cannot be imported here without dragging its repositories into
this pure module, so the convention is mirrored, not reused). Under monthly
periodisation a year's contribution lands a month after the year's start;
under quarterly, a quarter after — so the two periodisations produce genuinely
different flow dates, while the annual model economics are identical. Placing a
whole year's contribution and distribution as single lump flows is deliberately
coarse (ADR-0105 §3); a finer intra-year spread is a non-goal for v1.

Signals for the un-modellable case (ADR-0105 §Consequences)
-----------------------------------------------------------
A fund whose ``commitment`` is ``None`` — the book states none — has no
uncalled balance to draw and cannot be TA-modelled. The generator returns an
**empty list**, never a profile fabricated from an invented commitment,
mirroring :func:`services.investments.pacing_rows.unfunded_commitment`'s
``None`` posture for the same missing datum: S34.7 reads the empty result
as "stay disabled".
An unknown or non-capital-account ``investment_type`` is the loud case instead
— it raises
:class:`~services.investments.ta_profile_constants.TAProfileUnsupportedTypeError`
rather than defaulting, because a wrong *type* is a routing error, whereas a
missing *commitment* is a legitimately un-modellable fund.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date as _date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import TYPE_CHECKING
from uuid import UUID

from services.investments.ta_profile_constants import (
    TAProfileUnsupportedTypeError,
    parameters_for,
)
from services.overlay import PlanFlow

if TYPE_CHECKING:  # pragma: no cover - type-only import kept out of runtime
    # `Periodisation` lives in `cash_flow_timeline`, which imports repositories
    # and so cannot be imported at runtime without breaking this module's
    # purity guard. It is a `StrEnum`, so a member *is* its string value; this
    # module accepts one and reads it as a string key, and the type hint is
    # resolved statically only.
    from services.investments.cash_flow_timeline import Periodisation

#: A generated flow is exactly a plan flow (ADR-0105 §2a decision): it carries
#: the same five fields (``investment_id``, ``as_of_date``, ``amount``,
#: ``currency``, ``flow_type``), and the ``profile_source='ta'`` label is
#: applied at the *frames* level in S34.7 (ADR-0105 §5), never on the flow. So
#: the generator emits :class:`~services.overlay.pipeline.PlanFlow` unchanged
#: and S34.7's fold into :attr:`~services.overlay.pipeline.PlanFrames.plan_flows`
#: is a concatenation, with no translation step.
GeneratedPlanFlow = PlanFlow

#: The canonical ``investment_cashflows.flow_type`` values the generator emits
#: (ADR-0043). Only these two — never an ``investor_flow`` or any other type,
#: which is what makes the overlay-exemption invariant (ADR-0103 §5) hold for
#: generated flows by construction.
_CAPITAL_CALL: str = "capital_call"
_DISTRIBUTION: str = "distribution"

#: Months per period, by periodisation string value. Mirrors
#: :data:`services.investments.cash_flow_timeline._MONTHS_PER_PERIOD` (which
#: cannot be imported here — see the ``Periodisation`` note above). A
#: :class:`~services.investments.cash_flow_timeline.Periodisation` member keys
#: this directly, being a ``StrEnum``.
_MONTHS_PER_PERIOD: dict[str, int] = {"quarterly": 3, "monthly": 1}

#: The working precision for the model arithmetic, fixed so the recursion and
#: the non-integer bow power ``(t / L) ** B`` are reproducible regardless of
#: the caller's ambient :mod:`decimal` context.
_PRECISION: int = 28

#: The scale emitted flow amounts are quantised to — ``Numeric(20, 4)``, the
#: scale of ``investment_cashflows.amount``
#: (:class:`core.models.investment_cashflow.InvestmentCashflow`), so a
#: generated amount is stated at the precision the book would carry it at.
_AMOUNT_QUANTUM: Decimal = Decimal("0.0001")


def generate_remaining_profile(
    *,
    investment_id: UUID,
    commitment: Decimal | None,
    called_to_date: Decimal,
    current_nav: Decimal,
    t0: _date,
    investment_type: str,
    currency: str,
    periodisation: Periodisation,
) -> list[GeneratedPlanFlow]:
    """Generate the remaining Takahashi–Alexander cash-flow profile of a fund.

    The pure engine of ADR-0105 §1/§2: a deterministic classic-TA drawdown and
    distribution profile for a capital-account fund that has no manager plan.
    Identical inputs yield bit-identical output (fixed :mod:`decimal` context,
    no clock, no randomness). See the module docstring for the full model.

    Args:
        investment_id: The fund the generated flows belong to. Every emitted
            :class:`~services.overlay.pipeline.PlanFlow` carries it, so S34.7
            folds the result straight into
            :attr:`~services.overlay.pipeline.PlanFrames.plan_flows` per
            investment. (ADR-0105 §1's illustrative signature omits it; a plan
            flow requires it, and emitting fully-formed flows keeps S34.7's
            fold a concatenation.)
        commitment: The fund's total commitment, in ``currency``. ``None`` —
            the book states no commitment — makes the fund un-modellable and
            returns ``[]`` (see Returns).
        called_to_date: Realised capital calls to date, as a **positive**
            magnitude (as :func:`services.investments.pacing_rows.load_called_amounts`
            produces). Seeds the cumulative-called balance so the schedule
            draws only the still-uncalled remainder.
        current_nav: The fund's last actual NAV, in ``currency``, seeding the
            internal NAV recursion. ``Decimal(0)`` is valid — a just-started
            fund builds NAV from its calls.
        t0: The plan/actual seam (ADR-0060) the annual model periods run
            forward from.
        investment_type: The fund's ``investments.investment_type``. Must be a
            capital-account type (see Raises).
        currency: The fund's position currency; every emitted flow is stated in
            it (upper-cased).
        periodisation: The Planning Desk period length
            (:class:`~services.investments.cash_flow_timeline.Periodisation`) —
            quarterly or monthly. Determines the grid the annual flows are
            dated on; the annual model economics are unaffected by it.

    Returns:
        The generated flows, **ascending by date**, one lump ``capital_call``
        (negative) and/or one lump ``distribution`` (positive) per model year
        that carries a non-zero amount at the emitted scale. An **empty list**
        when ``commitment`` is ``None`` (un-modellable — no invented profile)
        or when a fully-realised, fully-called fund with no NAV has nothing
        left to project; S34.7 reads either as "stay disabled".

    Raises:
        TAProfileUnsupportedTypeError: If ``investment_type`` is not one of the
            four capital-account types — a routing error, surfaced loudly
            rather than defaulted (ADR-0105 §3).
        ValueError: If ``periodisation`` is neither quarterly nor monthly.
    """
    months_per_period = _MONTHS_PER_PERIOD.get(periodisation)
    if months_per_period is None:
        raise ValueError(
            f"unknown periodisation {periodisation!r}: the TA generator dates "
            f"flows on the quarterly or monthly grid only "
            f"({sorted(_MONTHS_PER_PERIOD)})."
        )

    # A routing error is loud; a missing commitment is a legitimately
    # un-modellable fund and returns empty — the two are distinct on purpose
    # (module docstring). `parameters_for` raises for a non-capital-account
    # type before any commitment check, so a mis-routed type never slips
    # through as an empty "nothing to pace".
    parameters = parameters_for(investment_type)

    if commitment is None:
        return []

    currency = currency.upper()
    periods_per_year = 12 // months_per_period
    base_period_end = _containing_period_end(t0, months_per_period)

    flows: list[GeneratedPlanFlow] = []
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        ctx.rounding = ROUND_HALF_UP

        growth_factor = Decimal(1) + parameters.growth
        called_cum = called_to_date
        nav = current_nav

        for year in range(1, parameters.lifetime_years + 1):
            unfunded = commitment - called_cum
            if unfunded < 0:
                # An over-called book (calls exceeding commitment) has nothing
                # left to draw; the schedule never contributes negatively — a
                # negative contribution would be a phantom distribution.
                unfunded = Decimal(0)

            contribution = _rate_for_year(parameters.rate_of_contribution, year) * unfunded
            called_cum += contribution

            grown_nav = nav * growth_factor
            if year < parameters.lifetime_years:
                distribution = (
                    _bow_rate(year, parameters.lifetime_years, parameters.bow) * grown_nav
                )
                nav = grown_nav + contribution - distribution
            else:
                # Terminal year: liquidate the entire residual NAV rather than
                # applying the bow, so the modelled fund winds down to zero.
                distribution = grown_nav + contribution
                nav = Decimal(0)

            _emit_year_flows(
                flows,
                investment_id=investment_id,
                currency=currency,
                contribution=contribution,
                distribution=distribution,
                base_period_end=base_period_end,
                year=year,
                periods_per_year=periods_per_year,
                months_per_period=months_per_period,
            )

    return flows


def _rate_for_year(schedule: tuple[Decimal, ...], year: int) -> Decimal:
    """Return the rate of contribution for model ``year`` (1-based).

    A year beyond the schedule's length reuses its final entry — the
    steady-state rate — so the drawdown tapers with the shrinking uncalled
    balance rather than stopping at a cliff (see
    :attr:`~services.investments.ta_profile_constants.TAParameters.rate_of_contribution`).
    """
    return schedule[year - 1] if year <= len(schedule) else schedule[-1]


def _bow_rate(year: int, lifetime_years: int, bow: Decimal) -> Decimal:
    """Return the distribution bow rate ``d_t = (t / L) ** B`` for ``year``.

    Computed in the caller's fixed :mod:`decimal` context (see
    :func:`generate_remaining_profile`), so the non-integer power is
    deterministic.
    """
    return (Decimal(year) / Decimal(lifetime_years)) ** bow


def _emit_year_flows(
    flows: list[GeneratedPlanFlow],
    *,
    investment_id: UUID,
    currency: str,
    contribution: Decimal,
    distribution: Decimal,
    base_period_end: _date,
    year: int,
    periods_per_year: int,
    months_per_period: int,
) -> None:
    """Append a model year's contribution and distribution flows to ``flows``.

    The year's contribution lands at its **first** period end and its
    distribution at its **last** (the annual boundary), both on the requested
    periodisation grid. A flow whose amount quantises to zero at the emitted
    scale is skipped — an early year with a negligible distribution, or a
    late year whose uncalled balance is already drawn, adds no phantom row.
    """
    contribution_date = _shift(
        base_period_end,
        (year - 1) * periods_per_year + 1,
        months_per_period,
    )
    distribution_date = _shift(base_period_end, year * periods_per_year, months_per_period)
    _append_flow(
        flows,
        investment_id=investment_id,
        as_of_date=contribution_date,
        # ADR-0043 sign convention: a capital call debits cash, so it is
        # emitted negative — matching PlanFlow's documented convention.
        amount=-contribution,
        flow_type=_CAPITAL_CALL,
        currency=currency,
    )
    _append_flow(
        flows,
        investment_id=investment_id,
        as_of_date=distribution_date,
        amount=distribution,
        flow_type=_DISTRIBUTION,
        currency=currency,
    )


def _append_flow(
    flows: list[GeneratedPlanFlow],
    *,
    investment_id: UUID,
    as_of_date: _date,
    amount: Decimal,
    flow_type: str,
    currency: str,
) -> None:
    """Quantise ``amount`` and append a flow, skipping a zero-at-scale amount.

    Amounts are quantised to :data:`_AMOUNT_QUANTUM` — the book's own
    ``investment_cashflows.amount`` scale — so a generated figure is stated at
    the precision the book would carry it at, and a value that rounds to zero
    there contributes no flow.
    """
    quantised = amount.quantize(_AMOUNT_QUANTUM)
    if quantised == 0:
        return
    flows.append(
        GeneratedPlanFlow(
            investment_id=investment_id,
            as_of_date=as_of_date,
            amount=quantised,
            currency=currency,
            flow_type=flow_type,
        )
    )


# ---------------------------------------------------------------------------
# Period-end grid — mirrors services/investments/cash_flow_timeline.py
# ---------------------------------------------------------------------------
# The timeline module owns these conventions but cannot be imported here: it
# pulls in repositories and would break this module's purity guard. The
# helpers below reproduce its `_month_end`, `_containing_period_end`, and
# `_shift` exactly, so a generated flow lands on the same grid the timeline
# samples balances on. "One line; the seam to reach it is not" — the same
# reason `pacing_rows.unfunded_commitment` mirrors a formula rather than
# importing it.


def _month_end(year: int, month: int) -> _date:
    """Return the last calendar day of ``(year, month)``."""
    return _date(year, month, monthrange(year, month)[1])


def _containing_period_end(day: _date, months_per_period: int) -> _date:
    """Return the end of the period ``day`` falls in — at or after ``day``.

    For monthly periods the containing month end; for quarterly periods the
    containing calendar-quarter end (31 Mar / 30 Jun / 30 Sep / 31 Dec).
    """
    if months_per_period == 1:
        return _month_end(day.year, day.month)
    quarter_month = ((day.month - 1) // 3 + 1) * 3
    return _month_end(day.year, quarter_month)


def _shift(end: _date, periods: int, months_per_period: int) -> _date:
    """Return the period end ``periods`` periods away from ``end``.

    ``end`` is a period end (a month's last day); the result is snapped to its
    own month's last day, so a 31 Mar + 1 quarter lands on 30 Jun, not an
    invalid 31 Jun.
    """
    total = end.year * 12 + (end.month - 1) + periods * months_per_period
    year, month = divmod(total, 12)
    return _month_end(year, month + 1)


__all__ = [
    "GeneratedPlanFlow",
    "TAProfileUnsupportedTypeError",
    "generate_remaining_profile",
]
