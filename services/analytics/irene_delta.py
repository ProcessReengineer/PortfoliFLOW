# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure edge/re-trigger comparison for Irene's internal delta (ADR-0087).

This module holds the *deterministic core* of Irene's internal delta:
given a current observation of a monitored subject and the state the
user has already acknowledged, decide whether the change is a rising
edge, a falling edge (all-clear), a magnitude re-trigger within an
unchanged band, or nothing at all.

It lives under :mod:`services.analytics` and is therefore held to the
analytics purity contract (ADR-0013 / ADR-0045 §3), enforced by
``tests/regression/test_analytics_layer_pure.py``: no database, no ORM,
no FastAPI, no Qt. Inputs are plain values (``Decimal`` / ``str``) and
outputs are frozen dataclasses. The *stateful* half — reading the
coverage bundle and the watch-state rows, and writing acknowledgements —
lives outside this root in :mod:`services.irene.internal_delta`.

Determinism is the whole point: every edge decision traces to a
rule-based comparison of numbers against a configured threshold, never
to model discretion (ADR-0087 §Compliance).

The mute rule lives here too
----------------------------
:func:`mute_suppresses` — "does a muted subject's change still become a
finding" (ADR-0116 §3) — is the same kind of object: a pure decision over
a :class:`DeltaDecision`, with no I/O. It sits here rather than inside one
evaluator because *three* callers need the identical answer — the quota
delta, the signal delta, and (from P6) the monitor, which mirrors the rule
in a disabled mute toggle. Two copies of a rule this load-bearing would be
one copy too many.

Edge band vs. card band
-----------------------
The band labels here (``note`` < ``watch`` < ``act``, mapping
OK < WARN < BREACH) are the delta layer's **edge bands**: an internal
coverage-severity ordering used *only* to decide whether a band worsened
(rising edge) or improved (falling edge / all-clear). They are wholly
distinct from the **card's final band** — the canonical ADR-0088 vocabulary
``informational`` / ``noteworthy`` / ``critical`` that the beat persists,
derived deterministically from the final urgency by
:func:`services.analytics.irene_floor.band_from_final_urgency`. Keep the two
separate: this module never produces a card band, and the floor never
produces an edge band.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Imported for typing only. Keeping it under TYPE_CHECKING means this
    # pure analytics module pulls in nothing from services.irene at
    # runtime, so the layer stays "stdlib + third-party only".
    from services.irene.delta_config import DeltaThresholds

# Edge-band labels keyed by coverage status — the delta layer's internal
# severity ordering for edge detection, NOT the card's final band (see the
# module docstring).
_EDGE_BAND_BY_STATUS: dict[str, str] = {
    "OK": "note",
    "WARN": "watch",
    "BREACH": "act",
}

# Total order over the edge bands (benign → severe), so
# "band worsens / improves" is decidable. OK(note) < WARN(watch) <
# BREACH(act).
_EDGE_BAND_RANK: dict[str, int] = {"note": 0, "watch": 1, "act": 2}

# The single benign edge band — the one that means "nothing to flag".
_BENIGN_EDGE_BAND: str = "note"

# The status that means "the ceiling is exceeded" for a quota subject, and
# the edge band it maps to. Named so the mute rule below can ask "was the
# acknowledged state a breach?" without restating the mapping.
_STATUS_BREACH: str = "BREACH"
_BREACH_EDGE_BAND: str = _EDGE_BAND_BY_STATUS[_STATUS_BREACH]

# The families whose subjects carry a *regulatory* ceiling — the ones the
# limit sets enumerate. Only these can be breached in the sense that word
# has here (ADR-0116 §3 scopes the un-mutable-breach rule to saa/anlv), and
# the distinction is load-bearing for :func:`mute_suppresses`.
QUOTA_FAMILIES: frozenset[str] = frozenset({"saa", "anlv"})

# Delta kinds. Kept as module constants so callers compare against a
# single source of truth rather than string literals.
KIND_RISING_EDGE: str = "rising_edge"
KIND_FALLING_EDGE: str = "falling_edge"
KIND_MAGNITUDE_RETRIGGER: str = "magnitude_retrigger"
KIND_NONE: str = "none"


def edge_band_from_status(status: str) -> str:
    """Map a coverage status to a delta **edge band** label.

    The edge band is the delta layer's internal severity ordering used for
    edge detection only; it is not the card's final band (see the module
    docstring — the final band is
    :func:`services.analytics.irene_floor.band_from_final_urgency`).

    The map is total over the three constrained-row statuses the delta
    layer ever sees (``OK`` / ``WARN`` / ``BREACH``); ``UNALLOCATED`` and
    ``NO_LIMIT`` rows carry no ceiling and are filtered out upstream, so
    they never reach this function.

    Args:
        status: One of ``OK`` / ``WARN`` / ``BREACH``.

    Returns:
        The edge band label (``note`` / ``watch`` / ``act``).

    Raises:
        ValueError: If ``status`` is not a constrained-row status.
    """
    try:
        return _EDGE_BAND_BY_STATUS[status]
    except KeyError:
        raise ValueError(
            f"edge_band_from_status: unmapped status {status!r}; expected one "
            f"of {sorted(_EDGE_BAND_BY_STATUS)}."
        ) from None


def subject_type_from_key(subject_key: str) -> str:
    """Return the delta layer's ``subject_type`` axis for a subject key.

    The subject type is the rule-formed prefix before the first ``:``
    (``saa:private_equity`` → ``saa``). Pure and deterministic — no DB,
    no LLM. Used only to look up the per-type ``re_trigger_delta``.

    Args:
        subject_key: A rule-formed subject key (e.g. ``anlv:16``).

    Returns:
        The prefix before the first ``:``; the whole string if there is
        no ``:``.
    """
    return subject_key.split(":", 1)[0]


@dataclass(frozen=True)
class SubjectObservation:
    """One monitored subject's current, deterministically derived state.

    Attributes:
        subject_key: The rule-formed subject identifier
            (``saa:{class_key}`` / ``anlv:{class_key}``).
        magnitude: The measured quantity at the latest Stichtag
            (``coverage_pct`` for limit subjects), or ``None`` for
            non-scalar subjects.
        status: The raw coverage status (``OK`` / ``WARN`` / ``BREACH``).
        band: The edge band derived from ``status`` via
            :func:`edge_band_from_status`.
    """

    subject_key: str
    magnitude: Decimal | None
    status: str
    band: str


@dataclass(frozen=True)
class AcknowledgedState:
    """The state the user has already seen for one subject.

    Reconstructed by the orchestration from
    ``irene_watch_state.acknowledged_magnitude`` (ADR-0085): its band is
    derived by re-classifying that magnitude against the *current*
    ceiling, so a ceiling change alone does not manufacture a spurious
    edge. ``None`` (rather than an instance) is passed when
    ``acknowledged_at IS NULL`` — the subject has never been acknowledged
    at a non-benign state, or was reset on a prior falling edge.

    Attributes:
        magnitude: The acknowledged measured quantity, or ``None`` for
            non-scalar subjects.
        band: The edge band the acknowledged magnitude maps to
            under the current ceiling.
    """

    magnitude: Decimal | None
    band: str


@dataclass(frozen=True)
class DeltaDecision:
    """The outcome of comparing an observation against acknowledged state.

    Attributes:
        subject_key: The subject the decision is about.
        kind: One of ``rising_edge`` / ``falling_edge`` /
            ``magnitude_retrigger`` / ``none`` (the ``KIND_*`` constants).
        current_magnitude: The observation's magnitude.
        acknowledged_magnitude: The acknowledged magnitude, or ``None``
            when there was no acknowledged state.
        current_band: The observation's edge band.
        acknowledged_band: The acknowledged edge band, or ``None``
            when there was no acknowledged state.
        reason: A short, deterministic human-readable explanation of the
            decision, used for the finding basis and the audit trail.
    """

    subject_key: str
    kind: str
    current_magnitude: Decimal | None
    acknowledged_magnitude: Decimal | None
    current_band: str
    acknowledged_band: str | None
    reason: str


def decide_delta(
    obs: SubjectObservation,
    acknowledged: AcknowledgedState | None,
    thresholds: DeltaThresholds,
    *,
    re_trigger_delta: Decimal | None = None,
) -> DeltaDecision:
    """Decide the edge/re-trigger kind for one subject observation.

    Deterministic core of the internal delta (ADR-0087 §Internal delta).
    No DB, no LLM: the decision is a pure comparison of the current band
    and magnitude against the acknowledged band and magnitude, using the
    per-subject-type ``re_trigger_delta`` from ``thresholds`` — or the
    per-subject override the caller resolved, when it supplies one.

    Rules:

    * No acknowledged state and current band benign ⇒ ``none``.
    * No acknowledged state and current band non-benign ⇒ ``rising_edge``.
    * Current band worsens vs acknowledged ⇒ ``rising_edge``.
    * Current band improves vs acknowledged ⇒ ``falling_edge`` (all-clear).
    * Same band and ``abs(current - acknowledged) >=
      re_trigger_delta[subject_type]`` ⇒ ``magnitude_retrigger``;
      otherwise ``none``.

    Args:
        obs: The current observation.
        acknowledged: The acknowledged state, or ``None`` when the
            subject has no acknowledged non-benign state.
        thresholds: The delta calibration values
            (:class:`~services.irene.delta_config.DeltaThresholds`).
        re_trigger_delta: The subject's own magnitude threshold, when the
            caller resolved one (a watchpoint overlay, ADR-0116 §3).
            ``None`` — the ordinary case — falls back to the per-subject-
            type value in ``thresholds``. Taken as a plain argument rather
            than read from a config object, which is what lets a
            per-subject value reach this function without the pure layer
            learning where overlays are stored (ADR-0116 §5).

    Returns:
        The :class:`DeltaDecision` for this subject.
    """
    subject_type = subject_type_from_key(obs.subject_key)
    current_band = obs.band
    current_mag = obs.magnitude

    if acknowledged is None:
        if current_band == _BENIGN_EDGE_BAND:
            return DeltaDecision(
                subject_key=obs.subject_key,
                kind=KIND_NONE,
                current_magnitude=current_mag,
                acknowledged_magnitude=None,
                current_band=current_band,
                acknowledged_band=None,
                reason=(f"benign ({obs.status}) and never acknowledged — nothing material"),
            )
        return DeltaDecision(
            subject_key=obs.subject_key,
            kind=KIND_RISING_EDGE,
            current_magnitude=current_mag,
            acknowledged_magnitude=None,
            current_band=current_band,
            acknowledged_band=None,
            reason=(
                f"first non-benign observation ({obs.status}) with no prior "
                "acknowledgement — rising edge"
            ),
        )

    ack_band = acknowledged.band
    ack_mag = acknowledged.magnitude
    current_rank = _EDGE_BAND_RANK[current_band]
    ack_rank = _EDGE_BAND_RANK[ack_band]

    if current_rank > ack_rank:
        return DeltaDecision(
            subject_key=obs.subject_key,
            kind=KIND_RISING_EDGE,
            current_magnitude=current_mag,
            acknowledged_magnitude=ack_mag,
            current_band=current_band,
            acknowledged_band=ack_band,
            reason=(f"band worsened {ack_band} → {current_band} ({obs.status}) — rising edge"),
        )

    if current_rank < ack_rank:
        return DeltaDecision(
            subject_key=obs.subject_key,
            kind=KIND_FALLING_EDGE,
            current_magnitude=current_mag,
            acknowledged_magnitude=ack_mag,
            current_band=current_band,
            acknowledged_band=ack_band,
            reason=(
                f"band improved {ack_band} → {current_band} "
                f"({obs.status}) — falling edge (all-clear)"
            ),
        )

    # Same band: a fresh finding only when the magnitude escalated
    # materially. Non-scalar subjects (magnitude None) never re-trigger.
    if current_mag is not None and ack_mag is not None:
        move = abs(current_mag - ack_mag)
        re_trigger = (
            re_trigger_delta
            if re_trigger_delta is not None
            else thresholds.re_trigger_delta[subject_type]
        )
        if move >= re_trigger:
            return DeltaDecision(
                subject_key=obs.subject_key,
                kind=KIND_MAGNITUDE_RETRIGGER,
                current_magnitude=current_mag,
                acknowledged_magnitude=ack_mag,
                current_band=current_band,
                acknowledged_band=ack_band,
                reason=(
                    f"magnitude moved {ack_mag} → {current_mag} "
                    f"(|Δ|={move} ≥ {re_trigger}) within {current_band} — "
                    "re-trigger"
                ),
            )

    return DeltaDecision(
        subject_key=obs.subject_key,
        kind=KIND_NONE,
        current_magnitude=current_mag,
        acknowledged_magnitude=ack_mag,
        current_band=current_band,
        acknowledged_band=ack_band,
        reason=(f"unchanged band {current_band} within noise — nothing material"),
    )


def mute_suppresses(decision: DeltaDecision, *, status: str) -> bool:
    """Decide whether a muted subject's delta still becomes a finding.

    **Load-bearing rule (ADR-0116 §3), enforced beat-side and not in the
    UI.** Mute suppresses *finding creation* only: by the time this is
    asked, the subject's ``irene_watch_state`` row has already been
    upserted and its acknowledgement written, so the state machine
    advances exactly as it would unmuted. What is decided here is whether
    Irene is shown the change at all.

    Two changes pass the gate regardless of mute:

    1. **A quota breach cannot be muted.** Nervousness can be silenced; a
       rule violation cannot. Any rising edge or magnitude re-trigger on
       an ``saa`` / ``anlv`` subject whose *live* status is ``BREACH``
       still fires. The mute toggle is disabled at BREACH in the monitor
       too, but that is a mirror of this rule, never its enforcement — a
       subject muted while calm and later breaching must still raise, and
       only the beat sees that moment.

       **The exception is quota-only, and deliberately so** (ADR-0116 §3
       scopes it to ``saa``/``anlv``). A *signal* family's subject exists
       because the operator defined a watchpoint for it: no regulatory
       floor stands behind a price or FX threshold somebody chose last
       Tuesday, and overriding their mute would make the mute a lie. A
       triggered ``price`` or ``fx`` subject can be muted, and is.
    2. **A raised non-benign state must be allowed to resolve.** An
       all-clear whose *acknowledged* band was the top band closes out an
       open card, so suppressing it would strand that card with no
       counterpart. This rule stays **family-agnostic**: the stranding is
       the same failure whatever raised the card, and the acknowledged
       state records the level, not the mute history — so it holds whether
       or not the mute was in force when the card was raised.

    Every other change for a muted subject — a rising edge into WARN, a
    re-trigger within WARN, and the ordinary all-clear off a WARN — is
    suppressed. The subject is muted; its calm is muted too, because an
    all-clear for something the operator never heard about is noise.

    Pure by construction (ADR-0013): the family is read off the subject
    key, which is rule-formed, so a caller cannot pass a family that
    disagrees with the subject it is deciding about.

    Args:
        decision: The delta decision already taken for the subject.
        status: The live status (``OK`` / ``WARN`` / ``BREACH``). For a
            signal family this is the internal vocabulary — ``BREACH``
            there means *Triggered* (ADR-0116 §4).

    Returns:
        ``True`` when the finding must not be created.
    """
    family = subject_type_from_key(decision.subject_key)
    a_quota_breach_cannot_be_muted = family in QUOTA_FAMILIES and status == _STATUS_BREACH
    a_raised_card_may_be_closed = (
        decision.kind == KIND_FALLING_EDGE and decision.acknowledged_band == _BREACH_EDGE_BAND
    )
    return not (a_quota_breach_cannot_be_muted or a_raised_card_may_be_closed)


__all__ = [
    "KIND_FALLING_EDGE",
    "KIND_MAGNITUDE_RETRIGGER",
    "KIND_NONE",
    "KIND_RISING_EDGE",
    "QUOTA_FAMILIES",
    "AcknowledgedState",
    "DeltaDecision",
    "SubjectObservation",
    "decide_delta",
    "edge_band_from_status",
    "mute_suppresses",
    "subject_type_from_key",
]
