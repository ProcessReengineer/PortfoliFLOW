# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Golden cases for the pure ``fx`` producer (ADR-0116 §4).

The structural twin of ``test_price_watch.py``, and the cases are
deliberately parallel — the two producers differ in exactly one
expression, so the tests should differ in exactly one claim:

* **Both directions count.** ADR-0116 §4: FX pain is book-dependent, so a
  pair that moved up and a pair that moved down by the same size produce
  the *same* magnitude. That is pinned here as an equality between two
  evaluations, not as two separate assertions that happen to agree.
* **Orientation is not this function's problem — but it is somebody's.**
  A percentage move is not inversion-symmetric, so the same fact yields
  different magnitudes in the two orientations. The test below states that
  asymmetry, which is why the impure caller fixes the orientation before
  this seam (the wiring itself is pinned in
  ``tests/services/irene/test_signal_delta.py``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from services.analytics.fx_watch import evaluate_fx_watchpoint
from services.analytics.signal_watch import (
    DatedValue,
    NoObservation,
    SignalObservation,
)

_AS_OF = date(2026, 8, 10)
_WINDOW = 5
#: Window start: 2026-08-05.
_WINDOW_START = date(2026, 8, 5)
_MOVE_PCT = Decimal("3.0")
_WARN_DEFAULT = Decimal("90.0")
_SUBJECT = "fx:USD/EUR"


def _d(value: str) -> Decimal:
    return Decimal(value)


def _series(*points: tuple[date, str]) -> list[DatedValue]:
    return [DatedValue(as_of_date=day, value=_d(value)) for day, value in points]


def _evaluate(
    series: list[DatedValue],
    *,
    move_pct: Decimal = _MOVE_PCT,
    warn_threshold_pct: Decimal = _WARN_DEFAULT,
    window_days: int = _WINDOW,
) -> SignalObservation | NoObservation:
    return evaluate_fx_watchpoint(
        subject_key=_SUBJECT,
        rates=series,
        move_pct=move_pct,
        window_days=window_days,
        as_of=_AS_OF,
        warn_threshold_pct=warn_threshold_pct,
    )


def test_a_move_at_exactly_the_threshold_is_triggered() -> None:
    """``|move| >= move_pct`` — the boundary belongs to the trigger."""
    result = _evaluate(_series((_WINDOW_START, "1.00"), (_AS_OF, "0.97")))

    assert isinstance(result, SignalObservation)
    assert result.magnitude == _d("3.0000")
    assert result.status == "BREACH"
    assert result.threshold_pct == _MOVE_PCT


def test_an_upward_and_a_downward_move_of_equal_size_are_equally_bad() -> None:
    """The one claim that separates this family from its twin."""
    weaker = _evaluate(_series((_WINDOW_START, "1.00"), (_AS_OF, "0.96")))
    stronger = _evaluate(_series((_WINDOW_START, "1.00"), (_AS_OF, "1.04")))

    assert isinstance(weaker, SignalObservation)
    assert isinstance(stronger, SignalObservation)
    assert weaker.magnitude == stronger.magnitude == _d("4.0000")
    assert weaker.status == stronger.status == "BREACH"
    # The direction is not lost, it is simply not the magnitude: the
    # endpoints still say which way the pair went.
    assert weaker.latest_value < weaker.reference_value
    assert stronger.latest_value > stronger.reference_value


def test_a_percentage_move_is_not_inversion_symmetric() -> None:
    """Why the orientation is settled before this function is called.

    1.00 → 1.25 is a 25% move. The very same fact, quoted the other way
    round, is 1.00 → 0.80 — a 20% move. Both are true about the world and
    only one is true about a watchpoint on ``BASE/QUOTE``.
    """
    as_quoted = _evaluate(_series((_WINDOW_START, "1.00"), (_AS_OF, "1.25")))
    inverted = _evaluate(_series((_WINDOW_START, "1.00"), (_AS_OF, "0.80")))

    assert isinstance(as_quoted, SignalObservation)
    assert isinstance(inverted, SignalObservation)
    assert as_quoted.magnitude == _d("25.0000")
    assert inverted.magnitude == _d("20.0000")


def test_the_warn_fraction_boundary_is_exclusive() -> None:
    """Exactly at the warn floor is Calm; a hair above it is Approaching."""
    at_the_floor = _evaluate(_series((_WINDOW_START, "1.00"), (_AS_OF, "1.027")))
    just_above = _evaluate(_series((_WINDOW_START, "1.00"), (_AS_OF, "1.0271")))

    assert isinstance(at_the_floor, SignalObservation)
    assert at_the_floor.magnitude == _d("2.7000")  # 90% of 3.0
    assert at_the_floor.status == "OK"

    assert isinstance(just_above, SignalObservation)
    assert just_above.status == "WARN"


def test_a_lower_warn_fraction_moves_the_approaching_band() -> None:
    """The same figure, a different subject threshold, a different status."""
    result = _evaluate(
        _series((_WINDOW_START, "1.00"), (_AS_OF, "1.02")),
        warn_threshold_pct=Decimal("55"),  # warn floor 1.65 pp
    )

    assert isinstance(result, SignalObservation)
    assert result.magnitude == _d("2.0000")
    assert result.status == "WARN"


def test_a_sparse_series_carries_forward_across_a_weekend_gap() -> None:
    """ECB-style series have gaps; both endpoints carry forward."""
    result = _evaluate(
        _series(
            (date(2026, 7, 31), "1.10"),
            (date(2026, 8, 7), "1.045"),
        )
    )

    assert isinstance(result, SignalObservation)
    assert result.reference_date == date(2026, 7, 31)
    assert result.latest_date == date(2026, 8, 7)
    assert result.magnitude == _d("5.0000")
    assert result.status == "BREACH"


def test_a_series_that_starts_inside_the_window_cannot_be_evaluated() -> None:
    """A newly-covered pair has no reference yet, and says so."""
    result = _evaluate(_series((date(2026, 8, 6), "1.00"), (_AS_OF, "1.20")))

    assert isinstance(result, NoObservation)
    assert result.subject_key == _SUBJECT
    assert "2026-08-05" in result.reason


def test_a_single_observation_before_the_window_reads_as_calm() -> None:
    """One row serves both ends: the pair has not been re-quoted."""
    result = _evaluate(_series((date(2026, 7, 20), "0.86")))

    assert isinstance(result, SignalObservation)
    assert result.magnitude == _d("0.0000")
    assert result.status == "OK"


def test_a_single_observation_inside_the_window_cannot_be_evaluated() -> None:
    """One row inside the window is a start date, not a measurement."""
    result = _evaluate(_series((_AS_OF, "0.86")))

    assert isinstance(result, NoObservation)


def test_an_empty_series_cannot_be_evaluated() -> None:
    """An uncovered pair is silence, not a stable rate."""
    result = _evaluate([])

    assert isinstance(result, NoObservation)
    assert "no rate observations at all" in result.reason


def test_an_unsorted_series_is_evaluated_as_if_sorted() -> None:
    """Order-blind by construction, exactly as the price twin is."""
    ordered = _evaluate(
        _series(
            (date(2026, 8, 3), "1.00"),
            (date(2026, 8, 4), "1.01"),
            (_AS_OF, "1.10"),
        )
    )
    shuffled = _evaluate(
        _series(
            (_AS_OF, "1.10"),
            (date(2026, 8, 4), "1.01"),
            (date(2026, 8, 3), "1.00"),
        )
    )

    assert ordered == shuffled
    assert isinstance(ordered, SignalObservation)
    assert ordered.reference_date == date(2026, 8, 4)
