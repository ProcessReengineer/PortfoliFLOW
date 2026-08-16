# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Golden cases for the pure ``liquidity`` producer (ADR-0116 §4).

No database:
:func:`services.analytics.cash_coverage_watch.evaluate_cash_coverage` takes
a balance, a list of projected flows and a floor, and returns an
observation. Everything that knows where a balance comes from — which
positions hold it, which currency it was converted from, whether a plan
path was materialised — lives in :mod:`services.irene.signal_delta`.

The cases that carry an argument:

* **Exactly at the floor scores exactly 100 and fires.** The recorded
  deviation restates the ratio as "percent of the way down to the floor";
  if that arithmetic were off by anything at the boundary, the family's
  one calibrated number would mean something other than what the operator
  set.
* **Approaching is reachable.** Under ADR-0116 §4's literal magnitude
  ("shortfall below the floor, 0 if covered") no value sits between calm
  and triggered, and the band would be dead. This is the test that would
  fail against that version.
* **Only capital calls enter the denominator.** The plan world projects
  distributions, fees and investor flows over the same dates. A ratio that
  netted them would answer a question nobody asked.
* **The three pinned edge rules.** Nothing to cover is covered; nothing to
  cover it *with* is bounded, not infinite; and no forward projection at
  all is silence, not calm.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from services.analytics.cash_coverage_watch import (
    COVERAGE_THRESHOLD_PCT,
    NO_COVERAGE_MAGNITUDE,
    PlannedFlow,
    coverage_horizon_end,
    coverage_ratio,
    evaluate_cash_coverage,
    projected_calls_of,
)
from services.analytics.signal_watch import (
    LIQUIDITY_SUBJECT_KEY,
    NoObservation,
    SignalObservation,
)

_AS_OF = date(2026, 8, 10)
_HORIZON_MONTHS = 12
#: 2027-08-10 — the inclusive end of a twelve-month horizon opened above.
_HORIZON_END = date(2027, 8, 10)
_MIN_RATIO = Decimal("1.2")
_WARN_DEFAULT = Decimal("90.0")
_CALLS_TOTAL = Decimal("1000000")


def _call(amount: str, *, on: date = date(2026, 12, 1)) -> PlannedFlow:
    """One projected capital call — negative, as the importer guarantees."""
    return PlannedFlow(as_of_date=on, flow_type="capital_call", amount=Decimal(amount))


def _evaluate(
    liquid_balance: str,
    *flows: PlannedFlow,
    min_coverage_ratio: Decimal = _MIN_RATIO,
    warn_threshold_pct: Decimal = _WARN_DEFAULT,
    has_forward_plan_path: bool = True,
) -> SignalObservation | NoObservation:
    return evaluate_cash_coverage(
        subject_key=LIQUIDITY_SUBJECT_KEY,
        liquid_balance=Decimal(liquid_balance),
        planned_flows=list(flows),
        horizon_months=_HORIZON_MONTHS,
        min_coverage_ratio=min_coverage_ratio,
        as_of=_AS_OF,
        warn_threshold_pct=warn_threshold_pct,
        has_forward_plan_path=has_forward_plan_path,
    )


# ---------------------------------------------------------------------------
# The scale itself
# ---------------------------------------------------------------------------


def test_a_ratio_exactly_at_the_floor_scores_exactly_one_hundred() -> None:
    """1.20× against a 1.20× floor: the boundary belongs to the trigger."""
    result = _evaluate("1200000", _call("-1000000"))

    assert isinstance(result, SignalObservation)
    assert result.subject_key == LIQUIDITY_SUBJECT_KEY
    assert result.magnitude == COVERAGE_THRESHOLD_PCT
    assert result.threshold_pct == COVERAGE_THRESHOLD_PCT
    assert result.status == "BREACH"
    assert coverage_ratio(
        liquid_balance=result.reference_value, calls=projected_calls_of(result)
    ) == Decimal("1.2")


def test_a_comfortable_book_is_calm() -> None:
    """2.40× against a 1.20× floor is half way down the scale."""
    result = _evaluate("2400000", _call("-1000000"))

    assert isinstance(result, SignalObservation)
    assert result.magnitude == Decimal("50.0000")
    assert result.status == "OK"


def test_the_approaching_band_is_reachable_between_the_floor_and_comfort() -> None:
    """The deviation's whole point, pinned as a boundary pair.

    Under the ADR's literal "shortfall below the floor, 0 if covered" both
    of these books would score zero and read identically calm. On the
    100-scale a 1.30× book is 92.3% of the way down to its floor and reads
    Approaching, while a 1.35× book has not reached the warn fraction.
    """
    approaching = _evaluate("1300000", _call("-1000000"))
    calm = _evaluate("1350000", _call("-1000000"))

    assert isinstance(approaching, SignalObservation)
    assert approaching.magnitude == Decimal("92.3077")
    assert approaching.status == "WARN"

    assert isinstance(calm, SignalObservation)
    assert calm.magnitude == Decimal("88.8889")
    assert calm.status == "OK"


def test_a_deeper_shortfall_is_a_larger_magnitude() -> None:
    """Badness units: a falling ratio must raise the figure, never lower it."""
    shallow = _evaluate("1100000", _call("-1000000"))
    deep = _evaluate("600000", _call("-1000000"))

    assert isinstance(shallow, SignalObservation) and isinstance(deep, SignalObservation)
    assert deep.magnitude > shallow.magnitude > COVERAGE_THRESHOLD_PCT


# ---------------------------------------------------------------------------
# What enters the denominator
# ---------------------------------------------------------------------------


def test_only_capital_calls_inside_the_horizon_enter_the_denominator() -> None:
    """Mixed types, mixed signs, and two dates outside the window.

    Every flow here is a legitimate plan row the tenant holds. Exactly one
    of them is a promise to pay inside the horizon, and the ratio must be
    formed from that one alone: with a 1,200,000 balance the book sits
    exactly at its 1.20× floor, and any leakage from the others would move
    it off that boundary in a directly visible way.
    """
    result = _evaluate(
        "1200000",
        _call("-1000000"),
        PlannedFlow(
            as_of_date=date(2026, 10, 1), flow_type="distribution", amount=Decimal("500000")
        ),
        PlannedFlow(as_of_date=date(2026, 10, 1), flow_type="fee", amount=Decimal("-50000")),
        PlannedFlow(
            as_of_date=date(2027, 1, 1), flow_type="investor_flow", amount=Decimal("200000")
        ),
        # Dated the evaluation day itself: the horizon is strictly forward.
        _call("-9000000", on=_AS_OF),
        # Beyond the horizon's end.
        _call("-7000000", on=date(2027, 8, 11)),
    )

    assert isinstance(result, SignalObservation)
    assert projected_calls_of(result) == _CALLS_TOTAL
    assert result.magnitude == COVERAGE_THRESHOLD_PCT


def test_a_call_on_the_horizons_last_day_is_inside_it() -> None:
    """The interval is ``(as_of, horizon_end]`` — closed at the far end."""
    result = _evaluate("1200000", _call("-1000000", on=_HORIZON_END))

    assert isinstance(result, SignalObservation)
    assert projected_calls_of(result) == _CALLS_TOTAL


def test_several_calls_sum_before_the_ratio_is_taken() -> None:
    """Coverage is against the horizon's total, never its largest call."""
    result = _evaluate(
        "1200000",
        _call("-400000", on=date(2026, 9, 1)),
        _call("-600000", on=date(2027, 3, 1)),
    )

    assert isinstance(result, SignalObservation)
    assert projected_calls_of(result) == _CALLS_TOTAL
    assert result.magnitude == COVERAGE_THRESHOLD_PCT


# ---------------------------------------------------------------------------
# The three pinned edge rules
# ---------------------------------------------------------------------------


def test_no_projected_calls_is_fully_covered_not_missing_data() -> None:
    """Nothing to cover is covered — a trivially calm book.

    The plan world *was* consulted; it promised nothing inside the horizon.
    That is a positive statement, and the difference between it and
    ``NoObservation`` is the difference between calm and silence.
    """
    result = _evaluate(
        "1200000",
        PlannedFlow(
            as_of_date=date(2026, 12, 1), flow_type="distribution", amount=Decimal("500000")
        ),
    )

    assert isinstance(result, SignalObservation)
    assert result.magnitude == Decimal("0")
    assert result.status == "OK"
    assert projected_calls_of(result) == Decimal("0")
    assert result.latest_value == result.reference_value
    assert coverage_ratio(liquid_balance=result.reference_value, calls=Decimal("0")) is None


def test_no_balance_against_real_calls_clamps_to_the_bounded_magnitude() -> None:
    """A ratio of zero has no finite badness figure, so it is given one."""
    result = _evaluate("0", _call("-1000000"))

    assert isinstance(result, SignalObservation)
    assert result.magnitude == NO_COVERAGE_MAGNITUDE
    assert result.magnitude > COVERAGE_THRESHOLD_PCT
    assert result.status == "BREACH"


def test_an_overdrawn_balance_clamps_to_the_same_magnitude() -> None:
    """Negative balances are legal in the plan world (ADR-0103 §6)."""
    result = _evaluate("-250000", _call("-1000000"))

    assert isinstance(result, SignalObservation)
    assert result.magnitude == NO_COVERAGE_MAGNITUDE
    assert result.latest_value == Decimal("-1250000")


def test_a_vanishing_balance_is_bounded_by_the_same_constant() -> None:
    """The clamp is a bound on the scale, not a special case for zero.

    Without it an arbitrarily small balance would produce an arbitrarily
    large magnitude, and the worst books on the platform would re-trigger
    on every beat as the divisor drifted — the noisiest possible treatment
    of the most serious state.
    """
    result = _evaluate("1", _call("-1000000"))

    assert isinstance(result, SignalObservation)
    assert result.magnitude == NO_COVERAGE_MAGNITUDE


def test_without_a_forward_plan_path_the_subject_reports_no_observation() -> None:
    """Absence of a projection is shown, never guessed over (ADR-0116 §4)."""
    result = _evaluate("1200000", _call("-1000000"), has_forward_plan_path=False)

    assert isinstance(result, NoObservation)
    assert result.subject_key == LIQUIDITY_SUBJECT_KEY
    assert "no materialised cash plan path" in result.reason
    assert _AS_OF.isoformat() in result.reason


# ---------------------------------------------------------------------------
# The horizon itself
# ---------------------------------------------------------------------------


def test_the_horizon_runs_in_calendar_months() -> None:
    """ "The next year" ends on the calendar's answer, not on 365 days."""
    assert coverage_horizon_end(_AS_OF, _HORIZON_MONTHS) == _HORIZON_END
    assert coverage_horizon_end(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert coverage_horizon_end(date(2026, 1, 31), 1) == date(2026, 2, 28)


def test_the_observation_states_the_window_it_measured() -> None:
    """The pair is the horizon's two ends, and both are checkable."""
    result = _evaluate("1200000", _call("-1000000"))

    assert isinstance(result, SignalObservation)
    assert result.reference_date == _AS_OF
    assert result.reference_value == Decimal("1200000")
    assert result.latest_date == _HORIZON_END
    assert result.latest_value == Decimal("200000")
    assert result.window_days == (_HORIZON_END - _AS_OF).days
