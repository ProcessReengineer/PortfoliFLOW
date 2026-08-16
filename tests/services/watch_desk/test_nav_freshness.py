# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Golden cases for the pure ``freshness`` producer (ADR-0116 §4).

No database: :func:`services.analytics.nav_freshness.evaluate_nav_freshness`
takes one dated NAV and an age limit and returns an observation, which is
what keeps the measurement on the pure side of the impurity line while the
beat owns the book and the fetch.

The cases that carry an argument, rather than merely exercising a branch:

* **Approaching is reachable at all.** This is the whole point of the
  recorded deviation from ADR-0116 §4's magnitude cell (see the producer's
  module docstring): measuring the *age* rather than the excess over the
  limit is what puts values between calm and triggered on the scale. A test
  that only checked the two ends would pass against the degenerate,
  binary version of this family.
* **Exactly at the limit fires.** ``magnitude >= threshold`` — a
  watchpoint is a trigger, not a ceiling.
* **A NAV that does not exist has no age.** The absence is stated, never
  filled in with a zero that would read as "restated today".
* **The window pair is checkable.** ``magnitude`` is exactly the gap
  between the two dates the observation carries, so nothing in it has to
  be taken on trust.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from services.analytics.nav_freshness import evaluate_nav_freshness
from services.analytics.signal_watch import (
    DatedValue,
    NoObservation,
    SignalObservation,
)

_AS_OF = date(2026, 8, 10)
_MAX_AGE_DAYS = 120
_WARN_DEFAULT = Decimal("90.0")
#: 90% of 120 days — the first age that reads Approaching.
_WARN_AGE = 108
_SUBJECT = "freshness:22222222-2222-2222-2222-222222222222"
_NAV_VALUE = Decimal("1250000.00")


def _nav_aged(days: int) -> DatedValue:
    return DatedValue(as_of_date=_AS_OF - timedelta(days=days), value=_NAV_VALUE)


def _evaluate(
    latest_nav: DatedValue | None,
    *,
    max_age_days: int = _MAX_AGE_DAYS,
    warn_threshold_pct: Decimal = _WARN_DEFAULT,
) -> SignalObservation | NoObservation:
    return evaluate_nav_freshness(
        subject_key=_SUBJECT,
        latest_nav=latest_nav,
        max_age_days=max_age_days,
        as_of=_AS_OF,
        warn_threshold_pct=warn_threshold_pct,
    )


def test_a_nav_exactly_at_the_age_limit_is_triggered() -> None:
    """``age >= max_age_days`` — the boundary belongs to the trigger."""
    result = _evaluate(_nav_aged(_MAX_AGE_DAYS))

    assert isinstance(result, SignalObservation)
    assert result.subject_key == _SUBJECT
    assert result.magnitude == Decimal("120")
    assert result.threshold_pct == Decimal("120")
    assert result.status == "BREACH"


def test_a_nav_one_day_inside_the_limit_is_only_approaching() -> None:
    """119 days against a 120-day limit has not triggered yet."""
    result = _evaluate(_nav_aged(_MAX_AGE_DAYS - 1))

    assert isinstance(result, SignalObservation)
    assert result.magnitude == Decimal("119")
    assert result.status == "WARN"


def test_the_approaching_band_is_reachable_and_starts_at_the_warn_fraction() -> None:
    """The deviation's whole point, pinned as a boundary pair.

    Under ADR-0116 §4's literal magnitude ("days *over* the limit, 0 if
    within") every age below 120 would score zero and this band would be
    empty. Measuring the age itself is what gives Approaching a range to
    occupy — here, the eleven days from 109 to 119.

    The band opens *strictly above* 90% of the limit, not at it: that
    boundary belongs to
    :func:`~services.analytics.limit_coverage.classify_coverage_status`,
    which P4 delegates the OK/WARN split to rather than forking. Only the
    trigger boundary is the signal layer's own, and only that one is
    inclusive.
    """
    calm = _evaluate(_nav_aged(_WARN_AGE))
    approaching = _evaluate(_nav_aged(_WARN_AGE + 1))

    assert isinstance(calm, SignalObservation) and calm.status == "OK"
    assert isinstance(approaching, SignalObservation) and approaching.status == "WARN"
    assert approaching.magnitude == Decimal(_WARN_AGE + 1)


def test_a_warn_override_moves_the_approaching_boundary() -> None:
    """One unchanged NAV age, two classifications.

    60% of 120 days is 72; a 100-day-old NAV is calm under the default
    fraction and approaching under a tenant that watches earlier.
    """
    nav = _nav_aged(100)
    under_default = _evaluate(nav)
    under_override = _evaluate(nav, warn_threshold_pct=Decimal("60"))

    assert isinstance(under_default, SignalObservation) and under_default.status == "OK"
    assert isinstance(under_override, SignalObservation) and under_override.status == "WARN"


def test_a_nav_restated_today_is_calm_at_magnitude_zero() -> None:
    """Zero is a claim about the world here, and a true one."""
    result = _evaluate(_nav_aged(0))

    assert isinstance(result, SignalObservation)
    assert result.magnitude == Decimal("0")
    assert result.status == "OK"


def test_a_future_dated_nav_clamps_to_zero_rather_than_going_negative() -> None:
    """A data error must not read as an improvement.

    The magnitude is a badness scalar the delta layer compares for
    escalation; a negative age would make a corrected date look like a
    subject getting worse.
    """
    result = _evaluate(DatedValue(as_of_date=_AS_OF + timedelta(days=3), value=_NAV_VALUE))

    assert isinstance(result, SignalObservation)
    assert result.magnitude == Decimal("0")
    assert result.status == "OK"


def test_an_investment_with_no_actual_nav_yields_no_observation() -> None:
    """An age since nothing is a guess, so none is offered."""
    result = _evaluate(None)

    assert isinstance(result, NoObservation)
    assert result.subject_key == _SUBJECT
    assert "no actual NAV row" in result.reason


def test_the_window_pair_carries_the_nav_and_the_gap_it_implies() -> None:
    """Nothing in the observation is decorative — the pair proves itself.

    Reference is the newest NAV; latest is the same value carried forward
    to the evaluation date (ADR-0060). The magnitude is exactly the gap
    between the two dates, so a reader can check the figure without the
    producer.
    """
    result = _evaluate(_nav_aged(134))

    assert isinstance(result, SignalObservation)
    assert result.reference_date == _AS_OF - timedelta(days=134)
    assert result.reference_value == _NAV_VALUE
    assert result.latest_date == _AS_OF
    assert result.latest_value == _NAV_VALUE
    assert result.magnitude == Decimal((result.latest_date - result.reference_date).days)
    # The limit is the window: "restated within the last 120 days" is one
    # statement, and the observation states it once.
    assert result.window_days == _MAX_AGE_DAYS
