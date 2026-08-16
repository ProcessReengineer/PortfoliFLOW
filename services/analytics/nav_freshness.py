# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The pure ``freshness`` signal producer (ADR-0116 §4).

One singleton watchpoint watches the **whole book**: its ``max_age_days``
applies to every investment, and the producer is called once per active
investment, emitting one ``freshness:{investment_id}`` subject each. That
is the enumeration pattern the quota families already use — the watchpoint
states the rule, the book states the subjects.

The badness magnitude is the age of the newest actual NAV, in days:

    magnitude = max(0, as_of - newest actual NAV date)

The family is deliberately a data-quality watcher: it demonstrates that
the Watch Desk also watches the ground it stands on (ADR-0116 §4). A
portfolio whose numbers are three months stale is not a calm portfolio —
it is one nobody can say anything true about, and that is worth a finding
in its own right.

Recorded deviation from ADR-0116 §4 (operator-approved)
--------------------------------------------------------
The ADR's magnitude cell for this family reads "days the newest NAV
exceeds ``max_age_days`` (0 if within)". That magnitude is **zero right up
until the threshold is crossed**, which makes the WARN band unreachable:
there is no value strictly between 0 and the trigger, so the family would
silently degrade to a binary Calm/Triggered — contradicting the same
section's rule that a signal family's WARN means "within the warn fraction
of the trigger threshold".

This module therefore measures the **age itself** and takes ``max_age_days``
as the threshold, which is the identical machinery ``price`` and ``fx``
run on: Approaching above ``warn_fraction × max_age_days``, Triggered at or
above ``max_age_days``. The two boundaries differ in strictness on purpose
and not by oversight — the trigger belongs to the signal layer and includes
its endpoint, the WARN split is delegated verbatim to
:func:`~services.analytics.limit_coverage.classify_coverage_status` and
excludes its own (P4, and unchanged here). The native re-trigger unit stays
days, so the family's 5.0 default in ``FloorConfig.re_trigger_delta`` means
what its comment already said it means — "5 days is a week of statement
lag".

The deviation was approved with the P5 implementation prompt (2026-08-11)
and its record is the programme's roadmap entry; ADR-0116 is deliberately
left unamended, and there is no successor ADR. The sibling deviation for
``liquidity`` is stated in :mod:`services.analytics.cash_coverage_watch`.

Any actual NAV counts, whoever wrote it
---------------------------------------
The input is the newest ``nav_kind='actual'`` row of **any** origin and
basis — an Excel-imported statement level, a live-refreshed one, and the
``'system'`` row the ADR-0098 service materialises from ``holdings ×
price`` all count identically. For a unitised instrument that means
freshness reads the health of the **price feed** rather than of a
statement, which is exactly the data-quality watching this family exists
for: a computed NAV that stopped moving is a feed that stopped arriving.
Plan rows are excluded — a projection is not an observation, and dating
staleness against one would make an unmaintained plan look like a fresh
book.

Absence of data is shown, never guessed over
--------------------------------------------
An investment with **no** actual NAV row at all yields
:class:`~services.analytics.signal_watch.NoObservation`, never an age. An
age since nothing is a guess, and the number it would have to guess from
(creation date? import date?) is not a statement about the investment's
data quality at all. The beat logs the subject and writes no watch-state
row for it.

Purity: DB-free, held to ADR-0013 / ADR-0045 §3. The impure fetch — the
active book and one batched latest-NAV read — lives in
:mod:`services.irene.signal_delta`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from services.analytics.signal_watch import (
    DatedValue,
    NoObservation,
    SignalObservation,
    SignalResult,
    classify_signal_status,
)


def evaluate_nav_freshness(
    *,
    subject_key: str,
    latest_nav: DatedValue | None,
    max_age_days: int,
    as_of: date,
    warn_threshold_pct: Decimal,
) -> SignalResult:
    """Evaluate one investment's NAV age against the freshness limit.

    Args:
        subject_key: The enumerated subject
            (``freshness:{investment_id}``), formed by
            :func:`~services.analytics.signal_watch.freshness_subject_key`
            so the producer and the sensitivity lookup cannot spell it
            differently.
        latest_nav: The investment's newest **actual** NAV — its date and
            its value — or ``None`` when it carries none at all.
        max_age_days: The singleton watchpoint's age limit in days. A NAV
            at or beyond it is Triggered.
        as_of: The evaluation date (the beat's clock, in UTC).
        warn_threshold_pct: The subject's effective WARN fraction, as a
            percentage of ``max_age_days``. Resolved by the beat through
            the singleton's settings (ADR-0116 §4) — this function never
            reaches for a default.

    Returns:
        A :class:`~services.analytics.signal_watch.SignalObservation`
        whose magnitude is the NAV's age in whole days, else a
        :class:`~services.analytics.signal_watch.NoObservation` when the
        investment has no actual NAV to age.
    """
    if latest_nav is None:
        return NoObservation(
            subject_key=subject_key,
            reason="the investment carries no actual NAV row to measure an age against",
        )

    # A NAV dated in the future is a data error, not a negative age: the
    # magnitude is a badness scalar and must never go below zero, or the
    # delta layer would read a corrected date as an escalation.
    age_days = max(0, (as_of - latest_nav.as_of_date).days)
    magnitude = Decimal(age_days)
    threshold = Decimal(max_age_days)

    return SignalObservation(
        subject_key=subject_key,
        magnitude=magnitude,
        status=classify_signal_status(magnitude, threshold, warn_threshold_pct),
        threshold_pct=threshold,
        # The watchpoint's window *is* its limit: "restated within the last
        # `max_age_days` days" is one statement, and it is both.
        window_days=max_age_days,
        reference_value=latest_nav.value,
        reference_date=latest_nav.as_of_date,
        # The value in force today is that same NAV, carried forward
        # (ADR-0060). Stating it twice against two dates is what makes the
        # magnitude exactly the gap between them, rather than a figure the
        # observation cannot be checked against.
        latest_value=latest_nav.value,
        latest_date=as_of,
    )


__all__ = ["evaluate_nav_freshness"]
