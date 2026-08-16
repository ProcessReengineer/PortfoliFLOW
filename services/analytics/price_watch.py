# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The pure ``price`` signal producer (ADR-0116 §4).

One watchpoint watches one instrument for an **adverse move**: how far its
price has fallen over the watchpoint's window. The badness magnitude is
the decline in percentage points,

    magnitude = max(0, (reference - latest) / reference * 100)

where ``reference`` is the latest price at or before ``as_of -
window_days`` and ``latest`` is the latest price at or before ``as_of``
(carry-forward on both ends, so a sparse or weekend-gapped series still
answers).

Declines only, and the ``max(0, ...)`` is the reason
-----------------------------------------------------
ADR-0116 §4 fixes v1 at declines under the long-book assumption: a
position that rose is not a finding waiting to happen. An upward move
therefore reports magnitude **0** — "no adverse move" — rather than a
negative number. That is not a rounding convenience: the delta layer's
arithmetic is direction-agnostic and compares magnitudes for escalation
(ADR-0087), so a signed magnitude would make a recovering instrument look
like a worsening one the moment it crossed zero. Direction
configurability is a commissioned successor (ADR-0116 §Commissions), not
a hidden option here.

Zero is a claim; missing data is not
------------------------------------
A window that cannot be resolved returns
:class:`~services.analytics.signal_watch.NoObservation`. See that
module's docstring — the distinction is the whole reason the type exists.

The move is FX-free by construction: it is measured within one
instrument's own price series, which is quoted in that instrument's own
currency (ADR-0097 §3), so the ratio is currency-invariant. Watching what
the *currency* did is a different family, and it has its own watchpoint.

Purity: DB-free, held to ADR-0013 / ADR-0045 §3 like the rest of
``services/analytics/``. The impure fetch lives in
:mod:`services.irene.signal_delta`.
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

_ZERO: Decimal = Decimal("0")


def evaluate_price_watchpoint(
    *,
    subject_key: str,
    prices: Sequence[DatedValue],
    drop_pct: Decimal,
    window_days: int,
    as_of: date,
    warn_threshold_pct: Decimal,
) -> SignalResult:
    """Evaluate one ``price`` watchpoint over its window.

    Args:
        subject_key: The watchpoint's subject (``price:{instrument_id}``).
            Taken from the registry rather than re-derived: ADR-0116 §1
            defines the stored ``subject_key`` as "the key the producer
            will emit", so the registry's answer is the one the monitor
            and the beat must agree on.
        prices: The instrument's price series, in its own currency. May be
            unsorted; may be empty.
        drop_pct: The trigger threshold in percentage points — a decline
            at or beyond it is Triggered.
        window_days: The observation window in days.
        as_of: The evaluation date (the beat's clock, in UTC).
        warn_threshold_pct: The subject's effective WARN fraction, as a
            percentage of ``drop_pct``. Resolved per subject by the beat
            (ADR-0116 §3) — this function never reaches for a default.

    Returns:
        A :class:`~services.analytics.signal_watch.SignalObservation` when
        the window resolves, else a
        :class:`~services.analytics.signal_watch.NoObservation` naming
        what was missing.
    """
    window = resolve_window(
        prices,
        as_of=as_of,
        window_days=window_days,
        subject_key=subject_key,
        quantity="price",
    )
    if isinstance(window, NoObservation):
        return window
    reference, latest = window

    move = percentage_move(reference.value, latest.value)
    # A rise is magnitude 0, never a negative badness — see the module
    # docstring. `max` over the quantised move keeps the two branches on
    # one scale.
    magnitude = quantize_pp(max(_ZERO, -move))

    return SignalObservation(
        subject_key=subject_key,
        magnitude=magnitude,
        status=classify_signal_status(magnitude, drop_pct, warn_threshold_pct),
        threshold_pct=drop_pct,
        window_days=window_days,
        reference_value=reference.value,
        reference_date=reference.as_of_date,
        latest_value=latest.value,
        latest_date=latest.as_of_date,
    )


__all__ = ["evaluate_price_watchpoint"]
