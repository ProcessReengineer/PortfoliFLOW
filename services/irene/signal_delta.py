# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Stateful evaluation of the defined signal families (ADR-0116 §4).

The third sibling of :mod:`services.irene.internal_delta` (quota) and
:mod:`services.irene.rss_delta` (press). Where those two diff a world
state the platform derives, this one evaluates the subjects the operator
*defined*: a ``price`` watchpoint on one instrument, an ``fx`` watchpoint
on one currency pair, a ``freshness`` limit over the whole book, a
``liquidity`` floor under it. It takes each family's evaluated observations
from
:func:`services.watch_desk.signal_observation.observe_signal_families` and
rides the existing pipeline unchanged — watch-state upsert, acknowledged
capture, :func:`~services.analytics.irene_delta.decide_delta`, mute gate,
eligible finding.

"Rides the existing pipeline" is the design, not a summary
-----------------------------------------------------------
ADR-0116 §4 states the magnitude of every signal family in **badness
units** — a scalar where larger is always worse — precisely so that
ADR-0087's direction-agnostic edge arithmetic applies with no branch. The
threshold plays the part the ceiling plays for a limit, so the
re-classification of an acknowledged magnitude works the same way; the
statuses stay ``OK`` / ``WARN`` / ``BREACH`` internally so
``edge_band_from_status`` is reused verbatim. Nothing here forks a
contract; what is new is the fetch and the wording.

Trigger vocabulary, never breach vocabulary
-------------------------------------------
Every human-facing string this module composes says *triggered* /
*approaching* / *eased* (ADR-0116 §4): "breach" is regulatory language,
reserved for the quota families. The internal ``BREACH`` status never
reaches a note, a reason, or the context the beat renders for Irene — it
is translated at the boundary by
:func:`~services.analytics.signal_watch.signal_status_label`. The delta
layer's own ``reason`` (which spells the raw status) is deliberately not
reused verbatim for that reason; a signal eligible states its own basis in
its own numbers.

Observation is shared; state is not
-----------------------------------
The fetch-and-produce half moved to
:mod:`services.watch_desk.signal_observation` in P6, because the monitor
needs exactly the same answer this module needs and a second fetch path
would be a second answer (ADR-0116 §1, applied one layer down to data
access). What stayed here is everything **stateful**: the watch-state
upsert, the acknowledged capture, the delta decision, the mute gate and
the eligible finding. That division is the point — the monitor renders
observations on every request and must advance no subject's state machine,
while the beat is the one caller that does.

The wording below is stateful too, in the sense that matters: it is written
around a *change*, not around a reading, which is why it lives beside the
decision that detected the change rather than beside the fetch.

Because it reads and writes the database it lives here under
``services/irene/``, never under ``services/analytics/``, and imports only
from ``core`` and ``services`` (Qt-free).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from core.repositories.irene_watch_state_repository import (
    IreneWatchStateDTO,
    IreneWatchStateRepository,
)
from services.analytics.cash_coverage_watch import (
    coverage_ratio,
    projected_calls_of,
)
from services.analytics.irene_delta import (
    KIND_FALLING_EDGE,
    KIND_MAGNITUDE_RETRIGGER,
    KIND_RISING_EDGE,
    AcknowledgedState,
    SubjectObservation,
    decide_delta,
    edge_band_from_status,
    mute_suppresses,
)
from services.analytics.signal_watch import (
    FAMILY_FRESHNESS,
    FAMILY_FX,
    FAMILY_LIQUIDITY,
    FAMILY_PRICE,
    STATUS_TRIGGERED,
    STATUS_WARN,
    NoObservation,
    SignalObservation,
    SignalResult,
    classify_signal_status,
    signal_status_label,
)
from services.watch_desk.overlay import SignalWatchpoint, WatchDeskResolution
from services.watch_desk.signal_observation import observe_signal_families

_LOG = logging.getLogger(__name__)

#: Two decimals for prose, four for arithmetic. The magnitudes the delta
#: layer compares stay at the analytics quantum; a note that reads
#: "fell 6.2000%" reads like a machine wrote it.
_PROSE_QUANTUM: Decimal = Decimal("0.01")

#: How a family names itself in a sentence. Only ``fx`` needs saying —
#: lower-case "fx watchpoint" reads like a typo in a card a human is meant
#: to act on.
_PROSE_FAMILY_LABEL: dict[str, str] = {
    FAMILY_PRICE: "price",
    FAMILY_FX: "FX",
    FAMILY_FRESHNESS: "freshness",
    FAMILY_LIQUIDITY: "cash coverage",
}


@dataclass(frozen=True)
class SignalEligibleFinding:
    """One signal-family change the delta layer deems worth showing Irene.

    The scalar sibling of
    :class:`services.irene.internal_delta.EligibleFinding`, carrying a
    watchpoint's own figures instead of a limit's: there is no ceiling and
    no headroom here, because the subject has no ceiling — it has a
    threshold somebody chose.

    Attributes:
        subject_key: The watchpoint's subject (``price:{instrument_id}`` /
            ``fx:{BASE}/{QUOTE}``).
        family: ``price`` / ``fx``. The floor's family axis
            (:func:`services.analytics.irene_floor.derive_trigger_type`).
        display_name: The instrument or pair label the note is written
            around — the operator's own words, from the registry.
        kind: The delta kind — ``rising_edge`` / ``falling_edge`` /
            ``magnitude_retrigger``.
        reason: The deterministic basis, in trigger vocabulary.
        note: The human-facing sentence, naming every concrete figure.
        status: The **internal** status (``OK`` / ``WARN`` / ``BREACH``).
            Never rendered raw — see :attr:`status_label`.
        band: The edge band derived from ``status``.
        magnitude: The observed move in badness percentage points.
        threshold_pct: The trigger threshold the move was measured against.
        window_days: The observation window in days.
        acknowledged_magnitude: The previously acknowledged magnitude, or
            ``None`` when there was no acknowledged state.
        provisional_urgency_hint: A deterministic, **non-binding** urgency
            hint the beat may pass as context. The deterministic floor
            decides the final urgency; this hint is not it.
    """

    subject_key: str
    family: str
    display_name: str
    kind: str
    reason: str
    note: str
    status: str
    band: str
    magnitude: Decimal
    threshold_pct: Decimal
    window_days: int
    acknowledged_magnitude: Decimal | None
    provisional_urgency_hint: int | None

    @property
    def status_label(self) -> str:
        """The human-facing status word — Calm / Approaching / Triggered."""
        return signal_status_label(self.status)


async def evaluate_signal_deltas(
    session: AsyncSession,
    *,
    now: datetime,
    resolution: WatchDeskResolution,
) -> list[SignalEligibleFinding]:
    """Return the signal-family eligible findings for the active tenant.

    Observes every effective watchpoint of the four defined families at
    ``now`` through
    :func:`services.watch_desk.signal_observation.observe_signal_families`
    — the same call the monitor renders from — and writes the resulting
    watch-state upserts, acknowledgements and resets. Must run on a
    **tenant-scoped** session (opened by the tick via ``tenant_context``);
    every read and write is RLS-policed for the active tenant.

    A ``freshness`` watchpoint is one row but many subjects — one per
    active investment — so the observation layer also enumerates the book.
    The subjects it produces resolve their mute and their thresholds
    through the singleton's own settings (ADR-0116 §4), which is why muting
    that one row silences the family and not merely a wildcard nobody
    observes.

    Like the other two deltas, the watch-state writes happen **before**
    synthesis runs, so an edge is "consumed" once shown to Irene whether or
    not Irene later phrases a finding for it.

    Silence is a common, correct outcome: a tenant with no watchpoints, a
    calm book, and a subject whose data supply is missing all yield no
    eligible finding. The third of those is logged per subject and writes
    **no** watch-state row — a subject that cannot be evaluated must not
    have its acknowledged state quietly reset by an outage in its data
    (ADR-0116 §4: absence of data is shown, never guessed over).

    Args:
        session: A tenant-scoped session.
        now: The beat clock (timezone-aware UTC), stamped as
            ``last_seen_at`` / ``acknowledged_at`` and taken as the
            evaluation date.
        resolution: This tenant's effective calibration, resolved **once**
            per beat by
            :func:`services.watch_desk.overlay.resolve_watch_desk` and
            threaded in as a plain argument. There is no default: a second
            resolution path is exactly what ADR-0116 §1 forbids. A retired
            watchpoint is absent from it, which is how retirement stops
            evaluation — an already-open finding it raised is never
            deleted, it stays as history (ADR-0085) and is closed by a
            falling edge only while the subject is still watched.

    Returns:
        The eligible findings, in ``(price, fx, freshness, liquidity)``
        family order then registry order — and, within ``freshness``, the
        book's own order. Empty on any silence path.
    """
    evaluated = await observe_signal_families(session, as_of=now.date(), resolution=resolution)
    if not evaluated:
        return []

    watch = IreneWatchStateRepository(session)
    eligible: list[SignalEligibleFinding] = []
    for watchpoint, result in evaluated:
        finding = await _record_observation(
            watchpoint=watchpoint,
            result=result,
            watch=watch,
            now=now,
            resolution=resolution,
        )
        if finding is not None:
            eligible.append(finding)

    return eligible


# ---------------------------------------------------------------------------
# The stateful pipeline: identical for all four families, deliberately so.
# ---------------------------------------------------------------------------


async def _record_observation(
    *,
    watchpoint: SignalWatchpoint,
    result: SignalResult,
    watch: IreneWatchStateRepository,
    now: datetime,
    resolution: WatchDeskResolution,
) -> SignalEligibleFinding | None:
    """Run one evaluated subject through the watch-state / delta pipeline.

    In the order ADR-0087 §1A A2 fixes: capture the acknowledged state
    **before** the upsert overwrites magnitude/band, upsert, decide, then
    acknowledge (rising edge / re-trigger) or reset (falling edge). The
    mute gate runs last, so a muted subject's state machine advances
    exactly as an unmuted one's does and only the finding is withheld.

    A :class:`~services.analytics.signal_watch.NoObservation` returns early
    **before** the upsert: writing a row for a subject that could not be
    evaluated would record a world state nobody observed.
    """
    subject_key = watchpoint.subject_key
    if isinstance(result, NoObservation):
        _LOG.info(
            "irene signal-delta: %s watchpoint %r cannot be evaluated — %s "
            "(no watch-state written).",
            watchpoint.family,
            subject_key,
            result.reason,
        )
        return None

    warn_threshold_pct = resolution.warn_threshold_for(subject_key)
    band = edge_band_from_status(result.status)
    observation = SubjectObservation(
        subject_key=subject_key,
        magnitude=result.magnitude,
        status=result.status,
        band=band,
    )

    prior = await watch.get_by_subject(subject_key)
    acknowledged = _acknowledged_state(
        prior,
        threshold_pct=result.threshold_pct,
        warn_threshold_pct=warn_threshold_pct,
    )

    await watch.upsert(
        subject_key=subject_key,
        magnitude=result.magnitude,
        band=band,
        last_seen_at=now,
    )

    decision = decide_delta(
        observation,
        acknowledged,
        resolution.config,
        re_trigger_delta=resolution.re_trigger_delta_for(subject_key),
    )

    if decision.kind in (KIND_RISING_EDGE, KIND_MAGNITUDE_RETRIGGER):
        await watch.acknowledge(
            subject_key=subject_key,
            acknowledged_at=now,
            acknowledged_magnitude=result.magnitude,
        )
        all_clear = False
    elif decision.kind == KIND_FALLING_EDGE:
        await watch.reset_acknowledgement(subject_key)
        all_clear = True
    else:
        return None

    if resolution.is_muted(subject_key) and mute_suppresses(decision, status=result.status):
        _LOG.info(
            "irene signal-delta: subject %r is muted — %s suppressed (watch-state advanced).",
            subject_key,
            decision.kind,
        )
        return None

    return SignalEligibleFinding(
        subject_key=subject_key,
        family=watchpoint.family,
        display_name=watchpoint.display_name,
        kind=decision.kind,
        reason=_reason(
            watchpoint=watchpoint,
            result=result,
            kind=decision.kind,
            acknowledged_magnitude=decision.acknowledged_magnitude,
        ),
        note=_note(watchpoint=watchpoint, result=result),
        status=result.status,
        band=band,
        magnitude=result.magnitude,
        threshold_pct=result.threshold_pct,
        window_days=result.window_days,
        acknowledged_magnitude=decision.acknowledged_magnitude,
        provisional_urgency_hint=_urgency_hint(
            kind=decision.kind, status=result.status, all_clear=all_clear
        ),
    )


def _acknowledged_state(
    prior: IreneWatchStateDTO | None,
    *,
    threshold_pct: Decimal,
    warn_threshold_pct: Decimal,
) -> AcknowledgedState | None:
    """Reconstruct the acknowledged state for one signal subject, or ``None``.

    The signal-family mirror of
    :func:`services.irene.internal_delta._acknowledged_state`: the
    acknowledged band is derived by re-classifying the acknowledged
    magnitude against the **current** threshold, so lowering a watchpoint's
    ``drop_pct`` does not by itself manufacture an edge out of a figure
    that never moved. The classification uses
    :func:`~services.analytics.signal_watch.classify_signal_status`, the
    same function the live observation was classified with — including its
    ``>=`` trigger boundary, which a coverage classification would get
    wrong by one hair at exactly the threshold.
    """
    if prior is None or prior.acknowledged_at is None:
        return None
    ack_magnitude = prior.acknowledged_magnitude
    if ack_magnitude is None:  # pragma: no cover - signal subjects are scalar
        return AcknowledgedState(magnitude=None, band=edge_band_from_status("OK"))
    ack_status = classify_signal_status(ack_magnitude, threshold_pct, warn_threshold_pct)
    return AcknowledgedState(magnitude=ack_magnitude, band=edge_band_from_status(ack_status))


def _urgency_hint(*, kind: str, status: str, all_clear: bool) -> int:
    """Return a deterministic, **non-binding** urgency hint for context.

    The signal-family counterpart of the quota hint, on the same coarse
    scale so a card's context does not read as more urgent merely because
    of which family raised it. It does **not** influence the persisted
    urgency: the deterministic floor decides that
    (:func:`services.analytics.irene_floor.final_urgency`), the model
    suggests, and this hint is neither.
    """
    if all_clear:
        return 0
    if kind == KIND_MAGNITUDE_RETRIGGER:
        return 3
    if status == STATUS_TRIGGERED:
        return 4
    return 2  # rising edge into Approaching


# ---------------------------------------------------------------------------
# Wording (ADR-0116 §4): triggered / approaching / eased — never "breach".
# ---------------------------------------------------------------------------


def _pct(value: Decimal) -> str:
    """Render a percentage-point figure for prose, at two decimals."""
    return f"{value.quantize(_PROSE_QUANTUM, rounding=ROUND_HALF_EVEN)}%"


def _family_label(family: str) -> str:
    """Render a family name the way prose spells it (``fx`` → ``FX``)."""
    return _PROSE_FAMILY_LABEL.get(family, family)


def _ratio(value: Decimal) -> str:
    """Render a coverage ratio for prose, at two decimals — ``1.08×``."""
    return f"{value.quantize(_PROSE_QUANTUM, rounding=ROUND_HALF_EVEN)}×"


def _plural(count: int, unit: str) -> str:
    """Render ``count`` of ``unit``, pluralising the ordinary way."""
    return f"{count} {unit}{'' if count == 1 else 's'}"


def _movement_phrase(watchpoint: SignalWatchpoint, result: SignalObservation) -> str:
    """State what the subject did, in its family's own terms.

    ``price`` watches declines, so its phrase is about being *below* an
    earlier level and reads honestly at magnitude zero (a rise is not a
    fall of 0.00%). ``fx`` watches both directions, so its phrase is about
    the size of the move and says nothing about which way.
    """
    days = _plural(result.window_days, "day")
    name = watchpoint.display_name
    if watchpoint.family == FAMILY_PRICE:
        if result.magnitude == 0:
            return f"{name} is at or above its price of {days} ago"
        return f"{name} is {_pct(result.magnitude)} below its price of {days} ago"
    return f"{name} moved {_pct(result.magnitude)} over {days}"


def _note(*, watchpoint: SignalWatchpoint, result: SignalObservation) -> str:
    """Compose the human-facing sentence for one signal observation.

    Beat-assembled per the house idiom, naming every concrete figure — the
    subject's own label, what it did, and the threshold it did it against —
    so the card can be read without opening the watchpoint. Every branch
    uses trigger vocabulary; the word "breach" appears in none of them, by
    construction rather than by review (ADR-0116 §4).

    Each family speaks in the unit its operator calibrated: percentage
    points for a move, whole days for an age, and **ratios** for coverage.
    The 100-scale ``liquidity`` computes on is arithmetic, not
    communication, and never reaches a sentence.
    """
    if watchpoint.family == FAMILY_FRESHNESS:
        return _freshness_note(watchpoint, result)
    if watchpoint.family == FAMILY_LIQUIDITY:
        return _coverage_note(watchpoint, result)
    return _move_note(watchpoint, result)


def _move_note(watchpoint: SignalWatchpoint, result: SignalObservation) -> str:
    """The ``price`` / ``fx`` sentence: a move against a move threshold."""
    threshold = _pct(result.threshold_pct)
    family = _family_label(watchpoint.family)
    if result.status == STATUS_TRIGGERED:
        clause = f"{family} watchpoint triggered (threshold {threshold})"
    elif result.status == STATUS_WARN:
        clause = f"approaching the {family} watchpoint threshold of {threshold}"
    else:
        clause = f"{family} watchpoint eased back below its threshold of {threshold}"
    return f"{_movement_phrase(watchpoint, result)} — {clause}."


def _freshness_note(watchpoint: SignalWatchpoint, result: SignalObservation) -> str:
    """The ``freshness`` sentence: one investment's NAV age against the limit.

    Names the investment, because the subject is the investment and a card
    reading "a NAV is 134 days old" would send its reader back to the
    monitor to find out which.
    """
    limit = _plural(result.window_days, "day")
    age = int(result.magnitude)
    aged = (
        f"NAV for {watchpoint.display_name} was restated today"
        if age == 0
        else f"NAV for {watchpoint.display_name} is {_plural(age, 'day')} old"
    )
    if result.status == STATUS_TRIGGERED:
        clause = f"freshness watchpoint triggered (limit {limit})"
    elif result.status == STATUS_WARN:
        clause = f"approaching the freshness watchpoint limit of {limit}"
    else:
        clause = f"freshness watchpoint eased back within its limit of {limit}"
    return f"{aged} — {clause}."


def _coverage_note(watchpoint: SignalWatchpoint, result: SignalObservation) -> str:
    """The ``liquidity`` sentence: coverage as a ratio, against a ratio floor.

    Two figures and no percentages: the ratio the book achieves and the
    floor the operator set. Both are the numbers on the watchpoint's own
    editor, which is what makes the sentence actionable without a
    translation step.
    """
    floor, horizon_months = _coverage_parameters(watchpoint)
    horizon = _plural(horizon_months, "month")
    ratio = coverage_ratio(liquid_balance=result.reference_value, calls=projected_calls_of(result))
    if ratio is None:
        return (
            f"no capital calls are projected over the next {horizon} — cash "
            f"coverage watchpoint calm against your {_ratio(floor)} floor."
        )
    if ratio <= 0:
        return (
            f"cash covers none of the calls projected over the next {horizon} — "
            f"far below your {_ratio(floor)} floor."
        )
    covers = f"cash covers projected calls {_ratio(ratio)} over {horizon}"
    if result.status == STATUS_TRIGGERED:
        return f"{covers} — below your {_ratio(floor)} floor."
    if result.status == STATUS_WARN:
        return f"{covers} — approaching your {_ratio(floor)} floor."
    return f"{covers} — back clear of your {_ratio(floor)} floor."


def _coverage_parameters(watchpoint: SignalWatchpoint) -> tuple[Decimal, int]:
    """Return the ``liquidity`` watchpoint's floor and horizon.

    Both are present by construction: the family's evaluator returns before
    producing any observation when either column is absent, so reaching
    here without them would mean an observation nothing measured. Raising
    is the honest answer to that state, and the beat's per-tenant guard
    contains it — inventing a floor to print would not.
    """
    if watchpoint.min_coverage_ratio is None or watchpoint.horizon_months is None:
        raise ValueError(  # pragma: no cover - evaluator-guarded
            f"liquidity watchpoint {watchpoint.subject_key!r} carries no coverage "
            "floor or horizon; it cannot have produced an observation."
        )
    return watchpoint.min_coverage_ratio, watchpoint.horizon_months


def _reason(
    *,
    watchpoint: SignalWatchpoint,
    result: SignalObservation,
    kind: str,
    acknowledged_magnitude: Decimal | None,
) -> str:
    """State the deterministic basis for the finding, in trigger vocabulary.

    Deliberately *not* ``DeltaDecision.reason``: that string spells the raw
    ``BREACH`` status, which is regulatory language a signal family must
    never use (ADR-0116 §4). The decision's substance — which edge, off
    which acknowledged magnitude — is restated here in the family's own
    words and its own numbers, so the audit trail loses nothing.
    """
    observed = _observed_phrase(watchpoint, result)
    family = _family_label(watchpoint.family)
    label = signal_status_label(result.status).lower()
    if kind == KIND_RISING_EDGE:
        return f"{family} watchpoint {label} — rising edge: {observed}"
    if kind == KIND_MAGNITUDE_RETRIGGER:
        moved_from = (
            ""
            if acknowledged_magnitude is None
            else f" (was {_magnitude_figure(watchpoint.family, acknowledged_magnitude)})"
        )
        # "deepened" carries every family: a move, a staleness and a
        # shortfall all deepen, and the badness scale is why one word can.
        return f"{family} watchpoint still {label}, deepened{moved_from} — re-trigger: {observed}"
    # A falling edge need not land at Calm — Triggered → Approaching is one
    # too — so the basis names where it landed rather than implying zero.
    return f"{family} watchpoint eased — all-clear (now {label}): {observed}"


def _magnitude_figure(family: str, magnitude: Decimal) -> str:
    """Render a magnitude in its family's own unit.

    The re-trigger delta is compared in these units, so an audit line that
    printed a day count as a percentage would make the comparison
    unreadable — and the delta is the one number an operator tunes when a
    subject is too noisy.
    """
    if family == FAMILY_FRESHNESS:
        return _plural(int(magnitude), "day")
    return _pct(magnitude)


def _observed_phrase(watchpoint: SignalWatchpoint, result: SignalObservation) -> str:
    """Restate the observation as figures, in the family's own units.

    ``liquidity`` states the two amounts its ratio was formed from, in the
    tenant's functional currency — the currency every converted aggregate
    on the platform is published in (ADR-0099 §2) — so the ratio in the
    note can be checked rather than taken on trust.
    """
    if watchpoint.family == FAMILY_LIQUIDITY:
        floor, horizon_months = _coverage_parameters(watchpoint)
        calls = projected_calls_of(result)
        ratio = coverage_ratio(liquid_balance=result.reference_value, calls=calls)
        achieved = "undefined (no projected calls)" if ratio is None else _ratio(ratio)
        return (
            f"{achieved} coverage against a {_ratio(floor)} floor over "
            f"{_plural(horizon_months, 'month')}, {result.reference_value} available "
            f"against {calls} of projected calls (functional currency), horizon to "
            f"{result.latest_date.isoformat()}"
        )
    if watchpoint.family == FAMILY_FRESHNESS:
        return (
            f"{_plural(int(result.magnitude), 'day')} against a "
            f"{_plural(result.window_days, 'day')} limit, newest actual NAV "
            f"{result.reference_value} on {result.reference_date.isoformat()}, "
            f"evaluated {result.latest_date.isoformat()}"
        )
    return (
        f"{_pct(result.magnitude)} against a {_pct(result.threshold_pct)} threshold "
        f"over {result.window_days} day(s), "
        f"{result.reference_value} on {result.reference_date.isoformat()} → "
        f"{result.latest_value} on {result.latest_date.isoformat()}"
    )


__all__ = ["SignalEligibleFinding", "evaluate_signal_deltas"]
