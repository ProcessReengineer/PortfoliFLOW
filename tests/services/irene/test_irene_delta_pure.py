# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure truth-table tests for Irene's internal delta core (ADR-0087).

Exercises :mod:`services.analytics.irene_delta` in isolation: no DB, no
network. The edge/re-trigger logic is a deterministic comparison of a
current observation against acknowledged state, so it is unit-tested
directly with fixed ``Decimal`` inputs — the same discipline the
analytics layer is held to everywhere else.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from services.analytics.irene_delta import (
    KIND_FALLING_EDGE,
    KIND_MAGNITUDE_RETRIGGER,
    KIND_NONE,
    KIND_RISING_EDGE,
    AcknowledgedState,
    DeltaDecision,
    SubjectObservation,
    edge_band_from_status,
    decide_delta,
    mute_suppresses,
    subject_type_from_key,
)
from services.irene.delta_config import DEFAULT_DELTA_THRESHOLDS

_SUBJECT = "saa:listed_equity"


def _D(value: str) -> Decimal:
    return Decimal(value)


def _obs(magnitude: str, status: str) -> SubjectObservation:
    return SubjectObservation(
        subject_key=_SUBJECT,
        magnitude=_D(magnitude),
        status=status,
        band=edge_band_from_status(status),
    )


def _ack(magnitude: str, status: str) -> AcknowledgedState:
    return AcknowledgedState(
        magnitude=_D(magnitude),
        band=edge_band_from_status(status),
    )


def _decide(obs: SubjectObservation, ack: AcknowledgedState | None):
    return decide_delta(obs, ack, DEFAULT_DELTA_THRESHOLDS)


# ---------------------------------------------------------------------------
# edge_band_from_status — totality and ordering
# ---------------------------------------------------------------------------


def test_edge_band_from_status_is_total_over_constrained_statuses() -> None:
    assert edge_band_from_status("OK") == "note"
    assert edge_band_from_status("WARN") == "watch"
    assert edge_band_from_status("BREACH") == "act"


def test_edge_band_from_status_rejects_unmapped_status() -> None:
    # UNALLOCATED / NO_LIMIT are filtered out upstream and must never
    # reach edge_band_from_status; an unmapped status is a programming error.
    with pytest.raises(ValueError):
        edge_band_from_status("UNALLOCATED")


def test_band_ordering_is_total_ok_lt_warn_lt_breach() -> None:
    # A worsening across each boundary is a rising edge; the improvement
    # back across it is a falling edge. This pins the OK<WARN<BREACH order.
    assert _decide(_obs("47", "WARN"), _ack("40", "OK")).kind == KIND_RISING_EDGE
    assert _decide(_obs("55", "BREACH"), _ack("47", "WARN")).kind == KIND_RISING_EDGE
    assert _decide(_obs("47", "WARN"), _ack("55", "BREACH")).kind == KIND_FALLING_EDGE
    assert _decide(_obs("40", "OK"), _ack("47", "WARN")).kind == KIND_FALLING_EDGE


# ---------------------------------------------------------------------------
# subject_type_from_key
# ---------------------------------------------------------------------------


def test_subject_type_from_key_uses_prefix() -> None:
    assert subject_type_from_key("saa:private_equity") == "saa"
    assert subject_type_from_key("anlv:16") == "anlv"
    assert subject_type_from_key("rss:cluster:abc123") == "rss"
    assert subject_type_from_key("no-colon") == "no-colon"


# ---------------------------------------------------------------------------
# decide_delta — the prescribed truth table
# ---------------------------------------------------------------------------


def test_no_ack_benign_is_none() -> None:
    decision = _decide(_obs("40", "OK"), None)
    assert decision.kind == KIND_NONE
    assert decision.acknowledged_magnitude is None
    assert decision.acknowledged_band is None


def test_no_ack_breach_is_rising() -> None:
    decision = _decide(_obs("58", "BREACH"), None)
    assert decision.kind == KIND_RISING_EDGE
    assert decision.current_band == "act"
    assert decision.acknowledged_band is None


def test_warn_to_breach_is_rising() -> None:
    decision = _decide(_obs("58", "BREACH"), _ack("46", "WARN"))
    assert decision.kind == KIND_RISING_EDGE


def test_breach_to_warn_is_falling() -> None:
    decision = _decide(_obs("46", "WARN"), _ack("58", "BREACH"))
    assert decision.kind == KIND_FALLING_EDGE


def test_breach_to_benign_is_falling() -> None:
    decision = _decide(_obs("40", "OK"), _ack("58", "BREACH"))
    assert decision.kind == KIND_FALLING_EDGE


def test_breach_to_breach_within_retrigger_is_none() -> None:
    # |58.4 - 58.0| = 0.4 < re_trigger_delta["saa"] (5.0).
    decision = _decide(_obs("58.4", "BREACH"), _ack("58.0", "BREACH"))
    assert decision.kind == KIND_NONE


def test_breach_to_breach_beyond_retrigger_is_retrigger() -> None:
    # |66.0 - 58.0| = 8.0 >= re_trigger_delta["saa"] (5.0).
    decision = _decide(_obs("66.0", "BREACH"), _ack("58.0", "BREACH"))
    assert decision.kind == KIND_MAGNITUDE_RETRIGGER


def test_retrigger_boundary_is_inclusive() -> None:
    # Exactly at the threshold (>=) re-triggers.
    decision = _decide(_obs("55.0", "BREACH"), _ack("50.0", "BREACH"))
    assert decision.kind == KIND_MAGNITUDE_RETRIGGER


def test_non_scalar_same_band_never_retriggers() -> None:
    # A subject with no magnitude (e.g. a future RSS cluster) cannot
    # magnitude-retrigger even within an unchanged non-benign band.
    obs = SubjectObservation(
        subject_key="rss:cluster:x",
        magnitude=None,
        status="BREACH",
        band="act",
    )
    ack = AcknowledgedState(magnitude=None, band="act")
    assert decide_delta(obs, ack, DEFAULT_DELTA_THRESHOLDS).kind == KIND_NONE


# ---------------------------------------------------------------------------
# mute_suppresses — the two exceptions, and which of them is family-scoped
# ---------------------------------------------------------------------------


def _decision(subject_key: str, *, kind: str, acknowledged_band: str | None):
    """Build a decision directly: the mute rule reads it, never derives it."""
    return DeltaDecision(
        subject_key=subject_key,
        kind=kind,
        current_magnitude=_D("1"),
        acknowledged_magnitude=_D("1"),
        current_band="act",
        acknowledged_band=acknowledged_band,
        reason="fixture",
    )


@pytest.mark.parametrize("subject_key", ["saa:listed_equity", "anlv:anlv_1"])
def test_a_quota_breach_fires_through_a_mute(subject_key: str) -> None:
    """Nervousness can be silenced; a rule violation cannot (ADR-0116 §3)."""
    decision = _decision(subject_key, kind=KIND_RISING_EDGE, acknowledged_band=None)
    assert mute_suppresses(decision, status="BREACH") is False


@pytest.mark.parametrize("subject_key", ["price:11111111", "fx:USD/EUR"])
def test_a_triggered_signal_subject_can_be_muted(subject_key: str) -> None:
    """The exception is quota-only, and this is the asymmetry it creates.

    A ``price`` threshold is one the operator chose; no regulatory floor
    stands behind it, so overriding their mute would make the mute a lie.
    The internal ``BREACH`` here is the spelling of *Triggered*.
    """
    decision = _decision(subject_key, kind=KIND_RISING_EDGE, acknowledged_band=None)
    assert mute_suppresses(decision, status="BREACH") is True


@pytest.mark.parametrize("subject_key", ["saa:listed_equity", "price:11111111", "fx:USD/EUR"])
def test_the_closing_all_clear_of_a_raised_card_passes_in_every_family(
    subject_key: str,
) -> None:
    """Family-agnostic on purpose: a stranded card is the worse failure."""
    decision = _decision(subject_key, kind=KIND_FALLING_EDGE, acknowledged_band="act")
    assert mute_suppresses(decision, status="OK") is False


@pytest.mark.parametrize("subject_key", ["saa:listed_equity", "price:11111111", "fx:USD/EUR"])
def test_an_ordinary_all_clear_is_suppressed_in_every_family(subject_key: str) -> None:
    """A muted subject's calm is muted too — nobody heard the warning."""
    decision = _decision(subject_key, kind=KIND_FALLING_EDGE, acknowledged_band="watch")
    assert mute_suppresses(decision, status="OK") is True


@pytest.mark.parametrize("subject_key", ["saa:listed_equity", "price:11111111"])
def test_a_warn_rising_edge_is_suppressed_in_every_family(subject_key: str) -> None:
    decision = _decision(subject_key, kind=KIND_RISING_EDGE, acknowledged_band=None)
    assert mute_suppresses(decision, status="WARN") is True
