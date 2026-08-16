# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Golden cases for the pure ``price`` producer (ADR-0116 §4).

No database: :func:`services.analytics.price_watch.evaluate_price_watchpoint`
takes a series and a threshold and returns an observation, which is what
lets the measurement stay on the pure side of the impurity line while the
beat owns the fetch.

The cases that carry an argument, rather than merely exercising a branch:

* **Exactly at the threshold fires.** A watchpoint is a trigger, not a
  ceiling: ADR-0116 §4 says ``move >= drop_pct``. The coverage classifier
  it borrows the WARN fraction from is strict in the other direction, and
  the difference is one hair wide and entirely intentional.
* **An upward move is magnitude 0, never negative.** The delta layer
  compares magnitudes for escalation, so a signed magnitude would make a
  recovering instrument look like a worsening one.
* **Sparse data still answers.** Weekend gaps are the normal shape of a
  price series; carry-forward on both ends is what makes a Sunday beat
  legitimate.
* **Missing data is not calm.** A window with no reference returns
  :class:`NoObservation`, so nobody can read "0.00% — calm" off a subject
  nothing was known about.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from services.analytics.price_watch import evaluate_price_watchpoint
from services.analytics.signal_watch import (
    DatedValue,
    NoObservation,
    SignalObservation,
)

_AS_OF = date(2026, 8, 10)
_WINDOW = 5
#: Window start: 2026-08-05.
_WINDOW_START = date(2026, 8, 5)
_DROP_PCT = Decimal("5.0")
_WARN_DEFAULT = Decimal("90.0")
_SUBJECT = "price:11111111-1111-1111-1111-111111111111"


def _d(value: str) -> Decimal:
    return Decimal(value)


def _series(*points: tuple[date, str]) -> list[DatedValue]:
    return [DatedValue(as_of_date=day, value=_d(value)) for day, value in points]


def _evaluate(
    series: list[DatedValue],
    *,
    drop_pct: Decimal = _DROP_PCT,
    warn_threshold_pct: Decimal = _WARN_DEFAULT,
    window_days: int = _WINDOW,
) -> SignalObservation | NoObservation:
    return evaluate_price_watchpoint(
        subject_key=_SUBJECT,
        prices=series,
        drop_pct=drop_pct,
        window_days=window_days,
        as_of=_AS_OF,
        warn_threshold_pct=warn_threshold_pct,
    )


def test_a_decline_at_exactly_the_threshold_is_triggered() -> None:
    """``move >= drop_pct`` — the boundary belongs to the trigger."""
    result = _evaluate(_series((_WINDOW_START, "100"), (_AS_OF, "95")))

    assert isinstance(result, SignalObservation)
    assert result.magnitude == _d("5.0000")
    assert result.status == "BREACH"
    assert result.threshold_pct == _DROP_PCT
    assert result.reference_value == _d("100")
    assert result.reference_date == _WINDOW_START
    assert result.latest_value == _d("95")
    assert result.latest_date == _AS_OF


def test_a_decline_just_under_the_threshold_is_only_approaching() -> None:
    """4.99 pp against a 5.0 pp trigger is Approaching, not Triggered."""
    result = _evaluate(_series((_WINDOW_START, "100"), (_AS_OF, "95.01")))

    assert isinstance(result, SignalObservation)
    assert result.magnitude == _d("4.9900")
    assert result.status == "WARN"


def test_the_warn_fraction_boundary_is_exclusive() -> None:
    """Exactly at the warn floor is Calm; a hair above it is Approaching.

    The same boundary the coverage engine uses (``> max * warn / 100``),
    which is the point of borrowing it: a signal subject and a limit
    subject read their gauges the same way.
    """
    at_the_floor = _evaluate(_series((_WINDOW_START, "100"), (_AS_OF, "95.5")))
    just_above = _evaluate(_series((_WINDOW_START, "100"), (_AS_OF, "95.49")))

    assert isinstance(at_the_floor, SignalObservation)
    assert at_the_floor.magnitude == _d("4.5000")  # 90% of 5.0
    assert at_the_floor.status == "OK"

    assert isinstance(just_above, SignalObservation)
    assert just_above.magnitude == _d("4.5100")
    assert just_above.status == "WARN"


def test_a_lower_warn_fraction_moves_the_approaching_band() -> None:
    """The same figure, a different subject threshold, a different status."""
    result = _evaluate(
        _series((_WINDOW_START, "100"), (_AS_OF, "97")),
        warn_threshold_pct=Decimal("55"),  # warn floor 2.75 pp
    )

    assert isinstance(result, SignalObservation)
    assert result.magnitude == _d("3.0000")
    assert result.status == "WARN"


def test_an_upward_move_is_magnitude_zero_not_a_negative_badness() -> None:
    """v1 watches declines only — a rise is calm, never a negative move."""
    result = _evaluate(_series((_WINDOW_START, "100"), (_AS_OF, "112")))

    assert isinstance(result, SignalObservation)
    assert result.magnitude == _d("0.0000")
    assert result.status == "OK"
    # The endpoints are still reported honestly: the move happened, it was
    # simply not adverse.
    assert result.latest_value == _d("112")


def test_a_sparse_series_carries_forward_across_a_weekend_gap() -> None:
    """Neither endpoint needs a row on its own date.

    The window starts on a Wednesday with no quote (2026-08-05) and ends
    on a Monday (2026-08-10); the reference is Friday the 31st of July and
    the latest is Friday the 7th. A hard-edged lookup would report no data
    over a series that plainly answers.
    """
    result = _evaluate(
        _series(
            (date(2026, 7, 31), "200"),
            (date(2026, 8, 7), "184"),
        )
    )

    assert isinstance(result, SignalObservation)
    assert result.reference_date == date(2026, 7, 31)
    assert result.latest_date == date(2026, 8, 7)
    assert result.magnitude == _d("8.0000")
    assert result.status == "BREACH"


def test_a_series_that_starts_inside_the_window_cannot_be_evaluated() -> None:
    """No reference means no answer — and says so, rather than saying zero."""
    result = _evaluate(
        _series(
            (date(2026, 8, 6), "100"),
            (_AS_OF, "80"),
        )
    )

    assert isinstance(result, NoObservation)
    assert result.subject_key == _SUBJECT
    assert "2026-08-05" in result.reason
    assert "window" in result.reason


def test_a_single_observation_before_the_window_reads_as_calm() -> None:
    """One row serves both ends: the price has not moved since it was set.

    Distinct from the case above, and the distinction is the whole point of
    the type — here the instrument genuinely has not moved, there nothing
    is known about whether it has.
    """
    result = _evaluate(_series((date(2026, 7, 20), "42")))

    assert isinstance(result, SignalObservation)
    assert result.magnitude == _d("0.0000")
    assert result.status == "OK"
    assert result.reference_date == result.latest_date == date(2026, 7, 20)


def test_a_single_observation_inside_the_window_cannot_be_evaluated() -> None:
    """A freshly-priced instrument has no history to be measured against."""
    result = _evaluate(_series((_AS_OF, "42")))

    assert isinstance(result, NoObservation)


def test_an_empty_series_cannot_be_evaluated() -> None:
    """An unpriced instrument is silence, not calm."""
    result = _evaluate([])

    assert isinstance(result, NoObservation)
    assert "no price observations at all" in result.reason


def test_an_unsorted_series_is_evaluated_as_if_sorted() -> None:
    """Order-blind by construction — a fetch cannot make the producer lie."""
    ordered = _evaluate(
        _series(
            (date(2026, 8, 3), "100"),
            (date(2026, 8, 4), "99"),
            (_AS_OF, "90"),
        )
    )
    shuffled = _evaluate(
        _series(
            (_AS_OF, "90"),
            (date(2026, 8, 3), "100"),
            (date(2026, 8, 4), "99"),
        )
    )

    assert ordered == shuffled
    assert isinstance(ordered, SignalObservation)
    # The reference is the 4th (the latest at or before the 5th), not the 3rd.
    assert ordered.reference_date == date(2026, 8, 4)
