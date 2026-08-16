# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Replay the crash-driven stress day and assert the edge sequence.

ADR-0087 v0 test strategy: a historical stress day is captured as a
deterministic fixture (``tests/services/irene/fixtures/stress_day_replay``)
and the delta layer must reproduce its edge sequence exactly. The driver
here is pure — it replays the fixture beats through
:func:`decide_delta`, carrying acknowledged state forward exactly as
:mod:`services.irene.internal_delta` does (acknowledge on rising /
re-trigger, reset on falling; acknowledged band re-classified against the
current ceiling). No DB, no network.
"""

from __future__ import annotations

from decimal import Decimal

from services.analytics.irene_delta import (
    KIND_FALLING_EDGE,
    KIND_MAGNITUDE_RETRIGGER,
    KIND_RISING_EDGE,
    AcknowledgedState,
    SubjectObservation,
    edge_band_from_status,
    decide_delta,
)
from services.analytics.limit_coverage import classify_coverage_status
from services.irene.delta_config import DEFAULT_DELTA_THRESHOLDS
from tests.services.irene.fixtures.stress_day_replay import (
    STRESS_DAY,
    StressDayScenario,
)

# Matches the WARN floor the internal delta reclassifies against.
_WARN_THRESHOLD_PCT = Decimal("90.0")


def _replay(scenario: StressDayScenario) -> list[str]:
    """Return the delta kind produced for each beat, in order.

    Mirrors the per-subject accounting in
    :func:`services.irene.internal_delta.evaluate_internal_deltas`:
    acknowledge (store magnitude) on rising / re-trigger, reset on
    falling, and reconstruct the acknowledged band by re-classifying the
    stored magnitude against the *current* beat's ceiling.
    """
    acknowledged_magnitude: Decimal | None = None
    kinds: list[str] = []

    for beat in scenario.beats:
        obs = SubjectObservation(
            subject_key=scenario.subject_key,
            magnitude=beat.coverage_pct,
            status=beat.status,
            band=edge_band_from_status(beat.status),
        )
        if acknowledged_magnitude is None:
            acknowledged: AcknowledgedState | None = None
        else:
            ack_status = classify_coverage_status(
                acknowledged_magnitude, beat.max_pct, _WARN_THRESHOLD_PCT
            )
            acknowledged = AcknowledgedState(
                magnitude=acknowledged_magnitude,
                band=edge_band_from_status(ack_status),
            )

        decision = decide_delta(obs, acknowledged, DEFAULT_DELTA_THRESHOLDS)
        kinds.append(decision.kind)

        if decision.kind in (KIND_RISING_EDGE, KIND_MAGNITUDE_RETRIGGER):
            acknowledged_magnitude = beat.coverage_pct
        elif decision.kind == KIND_FALLING_EDGE:
            acknowledged_magnitude = None

    return kinds


def test_stress_day_replay_reproduces_edge_sequence() -> None:
    expected = [beat.expected_kind for beat in STRESS_DAY.beats]
    assert _replay(STRESS_DAY) == expected


def test_stress_day_replay_is_deterministic() -> None:
    # Two replays of the same fixture produce the identical sequence —
    # the determinism ADR-0087 requires for reproducibility/audit.
    assert _replay(STRESS_DAY) == _replay(STRESS_DAY)
