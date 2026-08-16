# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure truth-table tests for the deterministic urgency floor (ADR-0088).

Exercises :mod:`services.analytics.irene_floor` in isolation — no DB, no
network. The floor is the home of the materiality judgement: the model
*suggests*, deterministic rules *decide*. These tests pin the decision
table (floor raises, source/trigger caps), the band derivation boundaries,
the options gate, the "why an 8?" decomposition, and the ``FloorConfig``
construction invariants that make a silent miscalibration impossible.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from services.analytics.irene_floor import (
    BAND_CRITICAL,
    BAND_INFORMATIONAL,
    BAND_NOTEWORTHY,
    DEFAULT_FLOOR_CONFIG,
    SOURCE_INTERNAL,
    SOURCE_RSS,
    TRIGGER_ALL_CLEAR,
    TRIGGER_FUND_CLOSURE,
    TRIGGER_FX,
    TRIGGER_LIMIT_BREACH,
    TRIGGER_LIMIT_ESCALATION,
    TRIGGER_PRICE,
    TRIGGER_RSS_CLUSTER,
    FloorConfig,
    band_from_final_urgency,
    clamp_suggestion,
    derive_trigger_type,
    explain_urgency,
    final_urgency,
    options_allowed,
)
from services.analytics.irene_delta import (
    KIND_FALLING_EDGE,
    KIND_MAGNITUDE_RETRIGGER,
    KIND_RISING_EDGE,
)

_CFG = DEFAULT_FLOOR_CONFIG


def _final(suggestion: int, trigger: str, source: str) -> int:
    return final_urgency(suggestion=suggestion, trigger_type=trigger, source=source, config=_CFG)


# ---------------------------------------------------------------------------
# final_urgency — the decision table
# ---------------------------------------------------------------------------


def test_limit_breach_floor_raises_a_low_suggestion() -> None:
    # A limit breach floors at 7: a suggestion of 5 is raised to 7.
    assert _final(5, TRIGGER_LIMIT_BREACH, SOURCE_INTERNAL) == 7


def test_limit_breach_honours_a_suggestion_above_the_floor() -> None:
    # 9 is within [7, 10] on a breach, so Irene's suggestion is honoured.
    assert _final(9, TRIGGER_LIMIT_BREACH, SOURCE_INTERNAL) == 9


def test_all_clear_capped_low_regardless_of_a_high_suggestion() -> None:
    # A falling-edge all-clear is capped at the informational top (3) even
    # if the model over-proposes.
    assert _final(9, TRIGGER_ALL_CLEAR, SOURCE_INTERNAL) == 3
    assert (
        band_from_final_urgency(_final(9, TRIGGER_ALL_CLEAR, SOURCE_INTERNAL), _CFG)
        == BAND_INFORMATIONAL
    )


def test_rss_cluster_capped_at_informational_top() -> None:
    # A standalone RSS cluster (source RSS) is capped at 3; a high
    # suggestion cannot lift it out of the informational band.
    assert _final(8, TRIGGER_RSS_CLUSTER, SOURCE_RSS) == 3
    # A modest suggestion within the cap is honoured.
    assert _final(2, TRIGGER_RSS_CLUSTER, SOURCE_RSS) == 2


def test_fund_closure_pinned_to_ten_regardless_of_suggestion() -> None:
    # fund_closure floor = cap = 10: any suggestion collapses to 10.
    assert _final(1, TRIGGER_FUND_CLOSURE, SOURCE_INTERNAL) == 10
    assert _final(10, TRIGGER_FUND_CLOSURE, SOURCE_INTERNAL) == 10
    assert (
        band_from_final_urgency(_final(3, TRIGGER_FUND_CLOSURE, SOURCE_INTERNAL), _CFG)
        == BAND_CRITICAL
    )


def test_limit_escalation_floor_raises_to_the_configured_minimum() -> None:
    # limit_escalation floors at 5 (a WARN-band escalation is at least
    # noteworthy).
    assert _final(2, TRIGGER_LIMIT_ESCALATION, SOURCE_INTERNAL) == 5
    assert _final(8, TRIGGER_LIMIT_ESCALATION, SOURCE_INTERNAL) == 8


# ---------------------------------------------------------------------------
# explain_urgency — the "why an 8?" audit decomposition
# ---------------------------------------------------------------------------


def test_why_an_eight_is_reconstructable_from_the_decomposition() -> None:
    # A breach floor of 7, suggestion 8, honoured: 8 > 7, final 8.
    decision = explain_urgency(
        suggestion=8,
        trigger_type=TRIGGER_LIMIT_BREACH,
        source=SOURCE_INTERNAL,
        config=_CFG,
    )
    assert decision.suggestion == 8
    assert decision.floor == 7
    assert decision.cap == 10
    assert decision.final == 8
    # The decomposition reproduces the final exactly.
    assert decision.final == max(decision.floor, min(decision.suggestion, decision.cap))
    assert "honoured" in decision.reason


def test_decomposition_names_the_floor_when_it_binds() -> None:
    decision = explain_urgency(
        suggestion=5,
        trigger_type=TRIGGER_LIMIT_BREACH,
        source=SOURCE_INTERNAL,
        config=_CFG,
    )
    assert decision.final == 7
    assert "floor" in decision.reason


def test_decomposition_names_the_cap_when_it_binds() -> None:
    decision = explain_urgency(
        suggestion=9,
        trigger_type=TRIGGER_RSS_CLUSTER,
        source=SOURCE_RSS,
        config=_CFG,
    )
    assert decision.final == 3
    assert "capped" in decision.reason


# ---------------------------------------------------------------------------
# clamp_suggestion — the raw-model guard
# ---------------------------------------------------------------------------


def test_clamp_suggestion_bounds_to_one_through_ten() -> None:
    assert clamp_suggestion(0) == 1
    assert clamp_suggestion(-4) == 1
    assert clamp_suggestion(1) == 1
    assert clamp_suggestion(10) == 10
    assert clamp_suggestion(11) == 10
    assert clamp_suggestion(5) == 5


# ---------------------------------------------------------------------------
# derive_trigger_type — the (source, kind, status) → trigger mapping
# ---------------------------------------------------------------------------


def test_derive_trigger_rss_is_always_rss_cluster() -> None:
    assert derive_trigger_type(source=SOURCE_RSS, kind=None, status=None) == TRIGGER_RSS_CLUSTER


def test_derive_trigger_falling_edge_is_all_clear() -> None:
    # A falling edge is an all-clear regardless of its improved status.
    assert (
        derive_trigger_type(source=SOURCE_INTERNAL, kind=KIND_FALLING_EDGE, status="OK")
        == TRIGGER_ALL_CLEAR
    )
    assert (
        derive_trigger_type(source=SOURCE_INTERNAL, kind=KIND_FALLING_EDGE, status="WARN")
        == TRIGGER_ALL_CLEAR
    )


def test_derive_trigger_breach_status_is_limit_breach() -> None:
    # A rising edge into BREACH, and a re-trigger *within* a breach, both
    # floor as limit_breach — status is prioritised over kind so a
    # within-breach move is not downgraded.
    assert (
        derive_trigger_type(source=SOURCE_INTERNAL, kind=KIND_RISING_EDGE, status="BREACH")
        == TRIGGER_LIMIT_BREACH
    )
    assert (
        derive_trigger_type(
            source=SOURCE_INTERNAL,
            kind=KIND_MAGNITUDE_RETRIGGER,
            status="BREACH",
        )
        == TRIGGER_LIMIT_BREACH
    )


def test_derive_trigger_non_breach_internal_is_escalation() -> None:
    # A rising edge into WARN, or a within-WARN re-trigger, is an escalation.
    assert (
        derive_trigger_type(source=SOURCE_INTERNAL, kind=KIND_RISING_EDGE, status="WARN")
        == TRIGGER_LIMIT_ESCALATION
    )
    assert (
        derive_trigger_type(
            source=SOURCE_INTERNAL,
            kind=KIND_MAGNITUDE_RETRIGGER,
            status="WARN",
        )
        == TRIGGER_LIMIT_ESCALATION
    )


def test_derive_trigger_signal_families_carry_their_own_trigger() -> None:
    """A watchpoint is never described as a limit (ADR-0116 §4).

    Its internal ``BREACH`` is the spelling of *Triggered*, so the status
    that would floor a quota subject to ``limit_breach`` must not do so
    here — the family axis is consulted first.
    """
    for family, trigger in (("price", TRIGGER_PRICE), ("fx", TRIGGER_FX)):
        for kind in (KIND_RISING_EDGE, KIND_MAGNITUDE_RETRIGGER):
            for status in ("WARN", "BREACH"):
                assert (
                    derive_trigger_type(
                        source=SOURCE_INTERNAL, kind=kind, status=status, family=family
                    )
                    == trigger
                )


def test_derive_trigger_a_signal_falling_edge_is_still_an_all_clear() -> None:
    """The family is checked *after* the falling edge, deliberately.

    ADR-0116 §4: "all-clear falling edges reuse the existing ``all_clear``
    semantics". That ordering is what keeps the pinned invariant "an
    all-clear is never itself urgent" (ADR-0116 §7) true for every family,
    rather than for the quota families alone.
    """
    for family in ("price", "fx"):
        assert (
            derive_trigger_type(
                source=SOURCE_INTERNAL, kind=KIND_FALLING_EDGE, status="OK", family=family
            )
            == TRIGGER_ALL_CLEAR
        )


def test_derive_trigger_without_a_family_behaves_exactly_as_before() -> None:
    """The quota path is byte-identical with the new axis left unset."""
    assert (
        derive_trigger_type(
            source=SOURCE_INTERNAL, kind=KIND_RISING_EDGE, status="BREACH", family=None
        )
        == TRIGGER_LIMIT_BREACH
    )
    assert (
        derive_trigger_type(source=SOURCE_INTERNAL, kind=KIND_RISING_EDGE, status="BREACH")
        == TRIGGER_LIMIT_BREACH
    )


def test_a_price_signal_floors_at_four_and_an_all_clear_caps_at_three() -> None:
    """The two ends of a signal card's urgency, on the v1 defaults."""
    assert (
        final_urgency(suggestion=1, trigger_type=TRIGGER_PRICE, source=SOURCE_INTERNAL, config=_CFG)
        == 4
    )
    assert (
        final_urgency(
            suggestion=9, trigger_type=TRIGGER_ALL_CLEAR, source=SOURCE_INTERNAL, config=_CFG
        )
        == 3
    )


# ---------------------------------------------------------------------------
# band_from_final_urgency — the boundary cases at every cut point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "urgency, band",
    [
        (1, BAND_INFORMATIONAL),
        (3, BAND_INFORMATIONAL),  # top of informational
        (4, BAND_NOTEWORTHY),  # first noteworthy
        (6, BAND_NOTEWORTHY),  # top of noteworthy
        (7, BAND_CRITICAL),  # first critical
        (10, BAND_CRITICAL),
    ],
)
def test_band_boundaries_at_every_cut_point(urgency: int, band: str) -> None:
    assert band_from_final_urgency(urgency, _CFG) == band


# ---------------------------------------------------------------------------
# options_allowed — the advise-half gate
# ---------------------------------------------------------------------------


def test_options_gated_below_the_threshold_band() -> None:
    # Default options_min_band is noteworthy: informational drops options,
    # noteworthy and critical keep them.
    assert options_allowed(BAND_INFORMATIONAL, _CFG) is False
    assert options_allowed(BAND_NOTEWORTHY, _CFG) is True
    assert options_allowed(BAND_CRITICAL, _CFG) is True


def test_options_allowed_rejects_an_unknown_band() -> None:
    with pytest.raises(ValueError):
        options_allowed("note", _CFG)  # an edge band is not a final band


# ---------------------------------------------------------------------------
# FloorConfig validation — a miscalibration cannot silently invert
# ---------------------------------------------------------------------------


def test_floor_above_cap_raises_at_construction() -> None:
    # A trigger whose floor exceeds its cap would invert the clamp.
    bad_floor = {**dict(_CFG.floor), TRIGGER_LIMIT_BREACH: 11}
    with pytest.raises(ValueError, match="invert"):
        FloorConfig(floor=bad_floor)


def test_floor_without_a_cap_entry_raises() -> None:
    bad_cap = {k: v for k, v in _CFG.cap.items() if k != TRIGGER_ALL_CLEAR}
    with pytest.raises(ValueError, match="no cap entry"):
        FloorConfig(cap=bad_cap)


@pytest.mark.parametrize(
    "boundaries",
    [
        (6, 3),  # not monotonic
        (3, 3),  # not strictly increasing (noteworthy band empty)
        (0, 6),  # informational band empty
        (3, 10),  # critical band empty
    ],
)
def test_non_covering_or_non_monotonic_band_boundaries_raise(
    boundaries: tuple[int, int],
) -> None:
    with pytest.raises(ValueError, match="band_boundaries"):
        FloorConfig(band_boundaries=boundaries)


def test_invalid_options_min_band_raises() -> None:
    with pytest.raises(ValueError, match="options_min_band"):
        FloorConfig(options_min_band="watch")  # an edge band, not a final band


def test_default_config_is_valid_and_carries_the_delta_thresholds() -> None:
    # The single calibration object also carries the ADR-0087 delta values,
    # unchanged, so the delta functions keep receiving what they need.
    assert _CFG.re_trigger_delta["saa"] == Decimal("5.0")
    assert _CFG.rss_time_window_hours == 24
    assert _CFG.similarity_threshold == 0.83
    assert "equities" in _CFG.tag_asset_class_map
