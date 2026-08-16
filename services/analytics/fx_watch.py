# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The pure ``fx`` signal producer (ADR-0116 §4).

The structural twin of :mod:`services.analytics.price_watch`, differing in
exactly one expression. One watchpoint watches one explicit currency pair
over the watchpoint's window, and its badness magnitude is the
**absolute** move in percentage points,

    magnitude = |(latest - reference) / reference| * 100

where ``reference`` is the latest pair rate at or before ``as_of -
window_days`` and ``latest`` the latest at or before ``as_of``.

Either direction counts
-----------------------
ADR-0116 §4: FX pain is book-dependent. A pair that moved against one
tenant's book moved in favour of another's, and the registry does not
know which — the watchpoint says "tell me when this pair moves", not
"tell me when it hurts". A signed magnitude would also break the delta
layer's direction-agnostic escalation arithmetic (ADR-0087), which is the
same reason ``price`` clamps at zero rather than going negative.

Orientation is fixed before this seam, and must be
--------------------------------------------------
The series arrives already in the watchpoint's own ``BASE/QUOTE``
orientation. This is not a convenience: **a percentage move is not
inversion-symmetric**. A pair that goes 1.00 → 1.25 has moved +25.0%;
inverted, the same fact reads 1.00 → 0.80, a move of 20.0%. Both numbers
are correct about the world and only one of them is correct about the
watchpoint, so the orientation has to be settled before any percentage is
taken — which means before this function, in the impure fetch that knows
how the rate is stored (``fx_rates`` holds legs quoted against the
dataset's reference currency, never pairs, so serving ``BASE/QUOTE`` is
always a derivation and sometimes an inversion). See
:mod:`services.irene.signal_delta`.

Purity: DB-free, held to ADR-0013 / ADR-0045 §3.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from services.analytics.signal_watch import (
    DatedValue,
    NoObservation,
    SignalObservation,
    SignalResult,
    classify_signal_status,
    percentage_move,
    quantize_pp,
    resolve_window,
)


def evaluate_fx_watchpoint(
    *,
    subject_key: str,
    rates: Sequence[DatedValue],
    move_pct: Decimal,
    window_days: int,
    as_of: date,
    warn_threshold_pct: Decimal,
) -> SignalResult:
    """Evaluate one ``fx`` watchpoint over its window.

    Args:
        subject_key: The watchpoint's subject (``fx:{BASE}/{QUOTE}``),
            taken from the registry (ADR-0116 §1).
        rates: The pair's rate series, **already in the watchpoint's
            ``BASE/QUOTE`` orientation** — each value is the price of one
            unit of BASE in QUOTE. May be unsorted; may be empty.
        move_pct: The trigger threshold in percentage points — an absolute
            move at or beyond it is Triggered.
        window_days: The observation window in days.
        as_of: The evaluation date (the beat's clock, in UTC).
        warn_threshold_pct: The subject's effective WARN fraction, as a
            percentage of ``move_pct``.

    Returns:
        A :class:`~services.analytics.signal_watch.SignalObservation` when
        the window resolves, else a
        :class:`~services.analytics.signal_watch.NoObservation` naming
        what was missing.
    """
    window = resolve_window(
        rates,
        as_of=as_of,
        window_days=window_days,
        subject_key=subject_key,
        quantity="rate",
    )
    if isinstance(window, NoObservation):
        return window
    reference, latest = window

    move: Decimal = percentage_move(reference.value, latest.value)
    # The one expression that differs from the price twin: both directions
    # are badness, so the sign is dropped rather than clamped.
    magnitude = quantize_pp(abs(move))

    return SignalObservation(
        subject_key=subject_key,
        magnitude=magnitude,
        status=classify_signal_status(magnitude, move_pct, warn_threshold_pct),
        threshold_pct=move_pct,
        window_days=window_days,
        reference_value=reference.value,
        reference_date=reference.as_of_date,
        latest_value=latest.value,
        latest_date=latest.as_of_date,
    )


__all__ = ["evaluate_fx_watchpoint"]
