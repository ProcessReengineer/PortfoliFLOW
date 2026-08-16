# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Deterministic replay fixture: a crash-driven limit breach (ADR-0087).

ADR-0087's v0 test strategy is to replay a historical stress day — an
urgency-10 closure / crash-driven breach — as a **deterministic fixture**
and assert the *edge sequence* it reproduces. This module is that
fixture. It is data-only (no pytest collection: the filename does not
match ``test_*``); the driver and assertions live in
``tests/services/irene/test_replay.py``.

Fixture shape
-------------
A :class:`StressDayScenario` is a single monitored subject observed over
an ordered list of beats. Each :class:`ReplayBeat` carries the raw
inputs the internal delta consumes for that beat:

* ``coverage_pct`` — the measured magnitude at the beat's Stichtag (pp).
* ``max_pct`` — the ceiling in force (pp); constant here, but modelled
  per-beat so a ceiling change could be replayed later.
* ``status`` — the coverage status (``OK`` / ``WARN`` / ``BREACH``) the
  limit-coverage engine would emit for ``(coverage_pct, max_pct)``.
* ``expected_kind`` — the delta kind the edge logic must reproduce for
  this beat, given the acknowledged state carried forward from the prior
  beats.

The driver replays the beats through :func:`decide_delta`, carrying an
in-memory acknowledged state forward exactly as
:mod:`services.irene.internal_delta` would (acknowledge on rising /
re-trigger, reset on falling), and reconstructing the acknowledged band
by re-classifying the acknowledged magnitude against the current beat's
ceiling. The whole sequence is pure and deterministic — no DB, no
network — which is precisely what makes the stress day reproducible
before any live feed exists.

The narrative (one asset-class SAA limit, 50% ceiling, 45% WARN floor):
a warning builds (OK→WARN), a market crash breaches it (WARN→BREACH),
the breach persists (noise, suppressed), then escalates materially
(re-trigger), then the position is unwound back to calm (all-clear),
after which the book is quiet again (silence).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from services.analytics.irene_delta import (
    KIND_FALLING_EDGE,
    KIND_MAGNITUDE_RETRIGGER,
    KIND_NONE,
    KIND_RISING_EDGE,
)

SUBJECT_KEY: str = "saa:listed_equity"


@dataclass(frozen=True)
class ReplayBeat:
    """One beat's raw inputs plus the delta kind it must reproduce."""

    coverage_pct: Decimal
    max_pct: Decimal
    status: str
    expected_kind: str


@dataclass(frozen=True)
class StressDayScenario:
    """A single subject observed across an ordered list of beats."""

    subject_key: str
    beats: tuple[ReplayBeat, ...]


def _beat(coverage: str, status: str, expected_kind: str) -> ReplayBeat:
    return ReplayBeat(
        coverage_pct=Decimal(coverage),
        max_pct=Decimal("50.0"),
        status=status,
        expected_kind=expected_kind,
    )


# The crash-driven stress day. Ceiling constant at 50.0% (WARN floor
# 45.0% at the default 90% threshold); re_trigger_delta["saa"] = 5.0.
STRESS_DAY: StressDayScenario = StressDayScenario(
    subject_key=SUBJECT_KEY,
    beats=(
        # Calm book — benign, never acknowledged.
        _beat("40.0", "OK", KIND_NONE),
        # A warning builds — first non-benign observation.
        _beat("47.0", "WARN", KIND_RISING_EDGE),
        # The crash breaches the ceiling — band worsens WARN → BREACH.
        _beat("58.0", "BREACH", KIND_RISING_EDGE),
        # The breach persists with only noise (|60.0-58.0|=2.0 < 5.0).
        _beat("60.0", "BREACH", KIND_NONE),
        # Material escalation within the breach (|66.0-58.0|=8.0 >= 5.0).
        _beat("66.0", "BREACH", KIND_MAGNITUDE_RETRIGGER),
        # The position is unwound — all-clear back to benign.
        _beat("40.0", "OK", KIND_FALLING_EDGE),
        # Quiet book again — benign, acknowledgement already reset.
        _beat("40.0", "OK", KIND_NONE),
    ),
)


__all__ = ["STRESS_DAY", "SUBJECT_KEY", "ReplayBeat", "StressDayScenario"]
