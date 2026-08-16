# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The pure ``liquidity`` signal producer (ADR-0116 §4).

One singleton watchpoint, one subject — ``liquidity:cash_coverage`` — over
the whole book. It asks the question a treasurer asks: **does what we hold
cover what we have promised to pay?**

    ratio     = liquid balance ÷ |Σ projected capital calls in the horizon|
    magnitude = 100 × min_coverage_ratio ÷ ratio

Both inputs arrive already converted into the tenant's functional currency,
at the ADR-0099 §4 boundary the beat owns. This module divides two numbers
that are already comparable; it reads no rate and knows no currency.

Recorded deviation from ADR-0116 §4 (operator-approved)
--------------------------------------------------------
The ADR's magnitude cell for this family reads "shortfall in coverage-ratio
pp below ``min_coverage_ratio`` (0 if covered)". That magnitude is **zero
right up until the floor is crossed**, leaving nothing between calm and
triggered for the WARN band to occupy — the family would degrade to a
binary, contradicting the same section's rule that WARN means "within the
warn fraction of the trigger threshold".

This module therefore states the magnitude as **percent of the way down to
the floor** and fixes the threshold at :data:`COVERAGE_THRESHOLD_PCT`:

* at ``ratio == min_coverage_ratio`` the magnitude is exactly 100 —
  Triggered, since the trigger boundary belongs to the signal layer and
  includes its endpoint;
* at the default 90% warn fraction, Approaching is the open interval
  ``min_coverage_ratio < ratio < min_coverage_ratio / 0.9`` — a book still
  above its floor but closing on it. Both of its ends are exclusive, and
  for different reasons: the lower one because the trigger already owns
  ``ratio == min``, the upper one because the OK/WARN split is delegated
  verbatim to
  :func:`~services.analytics.limit_coverage.classify_coverage_status`
  rather than forked (P4, unchanged here);
* the scale is direction-correct by construction: a *falling* ratio raises
  the magnitude, which is what "badness units, larger is always worse"
  requires of every family (ADR-0087's delta arithmetic is
  direction-agnostic and would read a signed shortfall backwards).

The 100-scale is arithmetic, not communication. Every human-facing string
the beat composes for this family speaks in **ratios** — "cash covers
projected calls 1.08× over 12 months — below your 1.20× floor" — because
that is the number the operator set and the number they will act on, and a
figure of 111 would mean nothing to them. The deviation was approved
with the P5 implementation prompt (2026-08-11); its record is the
programme's roadmap entry, ADR-0116 is deliberately left unamended, and
there is no successor ADR. The sibling deviation for ``freshness`` is
stated in :mod:`services.analytics.nav_freshness`.

Three pinned edge rules
-----------------------
1. **No projected calls → magnitude 0.** Nothing to cover is fully
   covered. This is a trivially calm book, not missing data: the plan
   world was consulted and it promised nothing inside the horizon.
2. **No liquid balance against real calls → a bounded, very large
   magnitude.** A ratio at or below zero has no finite badness figure, and
   an arbitrarily small positive balance has an arbitrarily large one. Both
   clamp to :data:`NO_COVERAGE_MAGNITUDE`, ten times the threshold. The
   bound matters: an unbounded magnitude would re-trigger on every beat as
   the divisor drifted, turning the worst case into the noisiest one.
3. **No forward plan projection → NoObservation.** The ratio's denominator
   is a claim about the future, and a book with no projection has made no
   such claim. Reporting "fully covered" there would be inventing calm out
   of an empty table (ADR-0116 §4: absence of data is shown, never guessed
   over). The beat decides what counts as a forward projection — it is the
   half that can see the plan path — and passes the answer in.

Purity: DB-free, held to ADR-0013 / ADR-0045 §3, and blind to investment
types by the same contract — which is why the liquid balance arrives as a
single number rather than as a book to filter. Deciding which positions
hold it is the data-assembly seam's job (ADR-0103 §8), in
:mod:`services.irene.signal_delta`. The plan path is **read** there, never
re-materialised (ADR-0116 §4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from services.analytics.signal_watch import (
    NoObservation,
    SignalObservation,
    SignalResult,
    classify_signal_status,
    quantize_pp,
)

#: The trigger threshold on the coverage scale. Fixed, not configurable:
#: the operator calibrates ``min_coverage_ratio``, and 100 is what "at the
#: floor" is worth once the ratio has been restated as a badness figure.
COVERAGE_THRESHOLD_PCT: Decimal = Decimal("100")

#: The clamp for a coverage ratio that is zero, negative or vanishingly
#: small — ten times the threshold, unambiguously Triggered and far off the
#: 0–100 scale, but finite and *stable*, so a book that stays uncovered
#: re-triggers no more often than any other unchanged subject.
NO_COVERAGE_MAGNITUDE: Decimal = Decimal("1000")

#: The one flow type that funds the denominator (ADR-0009 / ADR-0043 §3
#: vocabulary). Distributions, fees, coupons and investor flows are all
#: projected in the same plan world and none of them is a payment the book
#: has promised to make, so none of them belongs in "what must be covered".
FLOW_TYPE_CAPITAL_CALL: str = "capital_call"

_ZERO: Decimal = Decimal("0")


@dataclass(frozen=True)
class PlannedFlow:
    """One projected flow inside the evaluated horizon.

    A deliberately thin projection of an ``investment_cashflows`` plan row:
    the producer needs a date, a type and a signed amount, and nothing that
    would tell it which investment, which currency or which ledger the flow
    came from.

    Attributes:
        as_of_date: The flow's projected settlement date.
        flow_type: The canonical flow type. Only
            :data:`FLOW_TYPE_CAPITAL_CALL` enters the denominator.
        amount: The signed amount, **already in the functional currency**.
            A capital call is negative (the importer's Cash-Flow-Out
            guard, ADR-0043 §3); the sign is respected rather than assumed,
            and the sum is taken in absolute value.
    """

    as_of_date: date
    flow_type: str
    amount: Decimal


def coverage_horizon_end(as_of: date, horizon_months: int) -> date:
    """Return the last date the coverage horizon includes.

    Calendar months, not a 30-day approximation: a twelve-month horizon
    opened on 29 February ends on 28 February, which is what an operator
    means by "the next year" and what every other forward window in the
    Planning Desk means by it.

    Shared with the beat so the window bounding the *fetch* and the window
    filtering the *measurement* are the same interval by construction —
    two definitions of one horizon would eventually disagree by a day and
    the disagreement would be invisible.

    Args:
        as_of: The evaluation date — the horizon opens the day after.
        horizon_months: The watchpoint's ``horizon_months``.

    Returns:
        The horizon's inclusive end date.
    """
    return as_of + relativedelta(months=horizon_months)


def evaluate_cash_coverage(
    *,
    subject_key: str,
    liquid_balance: Decimal,
    planned_flows: Sequence[PlannedFlow],
    horizon_months: int,
    min_coverage_ratio: Decimal,
    as_of: date,
    warn_threshold_pct: Decimal,
    has_forward_plan_path: bool,
) -> SignalResult:
    """Evaluate the book's coverage of its projected calls over the horizon.

    Args:
        subject_key: The subject
            (:data:`~services.analytics.signal_watch.LIQUIDITY_SUBJECT_KEY`).
        liquid_balance: The balance available today, in the functional
            currency. Negative is legal and means what it says.
        planned_flows: The plan world's projected flows. May carry every
            type and either sign; only capital calls dated inside the
            horizon reach the denominator, and the golden tests pin that.
        horizon_months: The watchpoint's forward horizon in months.
        min_coverage_ratio: The operator's floor — the ratio at or below
            which the watchpoint fires.
        as_of: The evaluation date (the beat's clock, in UTC). The horizon
            is strictly forward: a flow dated today or earlier has settled
            or is settling and is not something the balance must still
            cover.
        warn_threshold_pct: The subject's effective WARN fraction, as a
            percentage of :data:`COVERAGE_THRESHOLD_PCT`.
        has_forward_plan_path: Whether the book holds any materialised
            forward cash projection at all. ``False`` yields a
            :class:`~services.analytics.signal_watch.NoObservation` — see
            the module docstring's third edge rule.

    Returns:
        A :class:`~services.analytics.signal_watch.SignalObservation` on
        the 100-scale, or a
        :class:`~services.analytics.signal_watch.NoObservation` when the
        plan world has projected nothing to measure against.
    """
    if not has_forward_plan_path:
        return NoObservation(
            subject_key=subject_key,
            reason=(
                "no materialised cash plan path projects past "
                f"{as_of.isoformat()} — the book holds no forward cash "
                "projection to measure coverage against"
            ),
        )

    horizon_end = coverage_horizon_end(as_of, horizon_months)
    calls = _projected_calls(planned_flows, opens_after=as_of, closes_on=horizon_end)
    magnitude = _coverage_magnitude(
        liquid_balance=liquid_balance,
        calls=calls,
        min_coverage_ratio=min_coverage_ratio,
    )

    return SignalObservation(
        subject_key=subject_key,
        magnitude=magnitude,
        status=classify_signal_status(magnitude, COVERAGE_THRESHOLD_PCT, warn_threshold_pct),
        threshold_pct=COVERAGE_THRESHOLD_PCT,
        window_days=(horizon_end - as_of).days,
        reference_value=liquid_balance,
        reference_date=as_of,
        # The window's far end: what is left once the horizon's calls have
        # settled. Only the calls — this is the coverage view, not the plan
        # path, and distributions inside the horizon are deliberately not
        # netted off (a promise to pay is not covered by a hoped-for
        # receipt). Negative is the funding gap, stated rather than hidden.
        latest_value=liquid_balance - calls,
        latest_date=horizon_end,
    )


def _projected_calls(
    planned_flows: Sequence[PlannedFlow],
    *,
    opens_after: date,
    closes_on: date,
) -> Decimal:
    """Return the absolute total of the capital calls inside the horizon.

    Two filters and an absolute value. The type filter is the load-bearing
    one: the plan world projects distributions, fees and investor flows
    over the same dates, and every one of them would flatter or wreck the
    ratio if it were allowed into a figure that means "what we have
    promised to pay".
    """
    total = sum(
        (
            flow.amount
            for flow in planned_flows
            if flow.flow_type == FLOW_TYPE_CAPITAL_CALL
            and opens_after < flow.as_of_date <= closes_on
        ),
        _ZERO,
    )
    return abs(total)


def _coverage_magnitude(
    *,
    liquid_balance: Decimal,
    calls: Decimal,
    min_coverage_ratio: Decimal,
) -> Decimal:
    """Restate the coverage ratio as a badness figure on the 100-scale.

    The three edge rules of the module docstring, in the order they can
    occur. The ordinary branch is the last one, and it is one division:
    ``100 × min × calls / balance`` — algebraically ``100 × min / ratio``,
    computed without forming the intermediate ratio so no rounding happens
    twice.
    """
    if calls == _ZERO:
        return _ZERO
    if liquid_balance <= _ZERO:
        return NO_COVERAGE_MAGNITUDE
    magnitude = quantize_pp(COVERAGE_THRESHOLD_PCT * min_coverage_ratio * calls / liquid_balance)
    return min(magnitude, NO_COVERAGE_MAGNITUDE)


def projected_calls_of(observation: SignalObservation) -> Decimal:
    """Recover the horizon's projected-call total from an observation.

    The observation's window pair is ``(balance at the open, balance after
    the calls)``, so their difference is the calls — stated here rather
    than open-coded in the beat, because it is the one place the identity
    that makes the pair readable is written down.

    Args:
        observation: A ``liquidity`` observation.

    Returns:
        The absolute total of the horizon's projected calls.
    """
    return observation.reference_value - observation.latest_value


def coverage_ratio(*, liquid_balance: Decimal, calls: Decimal) -> Decimal | None:
    """Return the coverage ratio the magnitude was formed from, or ``None``.

    The inverse the beat's prose needs: the operator calibrated a ratio and
    must read one back, never the internal 100-scale. ``None`` means the
    ratio is undefined because there is nothing to cover — the caller says
    "no projected calls", which is a different sentence from any number.

    Args:
        liquid_balance: The balance at the horizon's open, in the
            functional currency.
        calls: The absolute total of the horizon's projected calls.

    Returns:
        ``liquid_balance / calls``, or ``None`` when ``calls`` is zero.
    """
    if calls == _ZERO:
        return None
    return liquid_balance / calls


__all__ = [
    "COVERAGE_THRESHOLD_PCT",
    "FLOW_TYPE_CAPITAL_CALL",
    "NO_COVERAGE_MAGNITUDE",
    "PlannedFlow",
    "coverage_horizon_end",
    "coverage_ratio",
    "evaluate_cash_coverage",
    "projected_calls_of",
]
