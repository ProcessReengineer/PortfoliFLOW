# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure tests for ``compose_floor_config`` and the pinned invariants.

No database: :func:`services.analytics.floor_composition.compose_floor_config`
takes a defaults object and a DTO and returns a ``FloorConfig``, which is
what lets the beat's per-run composition (ADR-0116 §5) stay on the pure
side of the impurity line.

Two behaviours are pinned here:

* **Merging, not replacing.** A tenant that overrode one floor keeps the
  defaults for every other. That is what makes "NULL means code default"
  a live property rather than a storage detail — a later change to a
  default reaches every tenant that never overrode it.
* **Re-validation.** Composition is where a *stored* revision meets
  *current* code defaults, and the pair can be individually fine yet
  jointly wrong. The pinned invariants (ADR-0116 §7) are checked on the
  composed result, so an inverted configuration fails loudly instead of
  reaching the beat.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from core.repositories.floor_calibration_repository import FloorCalibrationDTO
from services.analytics.floor_composition import compose_floor_config
from services.analytics.irene_floor import (
    DEFAULT_FLOOR_CONFIG,
    SOURCE_RSS,
    TRIGGER_ALL_CLEAR,
    TRIGGER_FRESHNESS,
    TRIGGER_FUND_CLOSURE,
    TRIGGER_FX,
    TRIGGER_LIMIT_BREACH,
    TRIGGER_LIQUIDITY,
    TRIGGER_PRICE,
    FloorConfig,
    validate_pinned_invariants,
)

_EFFECTIVE_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _calibration(**overrides) -> FloorCalibrationDTO:
    """Build a calibration DTO carrying only the given deviations."""
    return FloorCalibrationDTO(
        id=UUID(int=1),
        tenant_id=UUID(int=2),
        effective_from=_EFFECTIVE_FROM,
        **overrides,
    )


# ---------------------------------------------------------------------------
# The four new trigger types (ADR-0116 §4)
# ---------------------------------------------------------------------------


def test_the_four_signal_trigger_types_carry_their_v1_levels() -> None:
    assert DEFAULT_FLOOR_CONFIG.floor[TRIGGER_PRICE] == 4
    assert DEFAULT_FLOOR_CONFIG.floor[TRIGGER_FX] == 4
    assert DEFAULT_FLOOR_CONFIG.floor[TRIGGER_FRESHNESS] == 3
    assert DEFAULT_FLOOR_CONFIG.floor[TRIGGER_LIQUIDITY] == 6
    # A stale NAV never outranks a breach, however long the staleness runs.
    assert DEFAULT_FLOOR_CONFIG.cap[TRIGGER_FRESHNESS] == 5


def test_every_family_has_a_re_trigger_delta_so_a_lookup_never_raises() -> None:
    assert set(DEFAULT_FLOOR_CONFIG.re_trigger_delta) == {
        "saa",
        "anlv",
        "rss",
        "price",
        "fx",
        "freshness",
        "liquidity",
    }


def test_the_defaults_satisfy_the_pinned_invariants() -> None:
    validate_pinned_invariants(DEFAULT_FLOOR_CONFIG)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_absent_calibration_composes_to_the_defaults_themselves() -> None:
    assert compose_floor_config(DEFAULT_FLOOR_CONFIG, None) is DEFAULT_FLOOR_CONFIG


def test_a_single_floor_override_leaves_every_other_field_on_the_default() -> None:
    composed = compose_floor_config(DEFAULT_FLOOR_CONFIG, _calibration(floor={TRIGGER_PRICE: 6}))

    assert composed.floor[TRIGGER_PRICE] == 6
    assert composed.floor[TRIGGER_FX] == DEFAULT_FLOOR_CONFIG.floor[TRIGGER_FX]
    assert composed.cap == DEFAULT_FLOOR_CONFIG.cap
    assert composed.band_boundaries == DEFAULT_FLOOR_CONFIG.band_boundaries
    assert composed.options_min_band == DEFAULT_FLOOR_CONFIG.options_min_band
    assert composed.re_trigger_delta == DEFAULT_FLOOR_CONFIG.re_trigger_delta


def test_scalars_and_maps_compose_together() -> None:
    composed = compose_floor_config(
        DEFAULT_FLOOR_CONFIG,
        _calibration(
            band_boundaries=(2, 5),
            options_min_band="critical",
            re_trigger_delta={"saa": Decimal("2.5")},
            floor={TRIGGER_LIMIT_BREACH: 8},
            cap={SOURCE_RSS: 2, TRIGGER_ALL_CLEAR: 2},
        ),
    )

    assert composed.band_boundaries == (2, 5)
    assert composed.options_min_band == "critical"
    assert composed.re_trigger_delta["saa"] == Decimal("2.5")
    assert composed.re_trigger_delta["anlv"] == DEFAULT_FLOOR_CONFIG.re_trigger_delta["anlv"]
    assert composed.floor[TRIGGER_LIMIT_BREACH] == 8


def test_the_rss_clustering_parameters_are_carried_not_calibrated() -> None:
    """Changing the pinned embedding model is application-wide, not a knob."""
    composed = compose_floor_config(
        DEFAULT_FLOOR_CONFIG, _calibration(warn_default_pct=Decimal("80"))
    )

    assert composed.embedding_model == DEFAULT_FLOOR_CONFIG.embedding_model
    assert composed.similarity_threshold == DEFAULT_FLOOR_CONFIG.similarity_threshold
    assert composed.rss_time_window_hours == DEFAULT_FLOOR_CONFIG.rss_time_window_hours
    assert composed.tag_asset_class_map == DEFAULT_FLOOR_CONFIG.tag_asset_class_map


def test_warn_default_is_not_folded_into_the_config() -> None:
    """The WARN threshold is an engine parameter, not a ``FloorConfig`` field.

    Stated as a test because the calibration row carries it, so "why is it
    not on the composed object?" is a fair question to answer once.
    """
    assert not hasattr(compose_floor_config(DEFAULT_FLOOR_CONFIG, None), "warn_threshold_pct")


# ---------------------------------------------------------------------------
# Re-validation on the composed result
# ---------------------------------------------------------------------------


def test_a_boundary_edit_that_strands_the_breach_floor_is_rejected() -> None:
    """The coupling ADR-0116 §7 invariant 2 exists to catch.

    Raising the upper boundary to 8 puts the critical band at 9–10, which
    leaves the default ``limit_breach`` floor of 7 below it — a regulatory
    breach rendering as merely noteworthy. Individually the revision and
    the default are both fine; together they are not.
    """
    with pytest.raises(ValueError, match="below the critical band"):
        compose_floor_config(DEFAULT_FLOOR_CONFIG, _calibration(band_boundaries=(3, 8)))


def test_the_same_edit_is_accepted_when_the_breach_floor_moves_with_it() -> None:
    composed = compose_floor_config(
        DEFAULT_FLOOR_CONFIG,
        _calibration(band_boundaries=(3, 8), floor={TRIGGER_LIMIT_BREACH: 9}),
    )
    assert composed.band_boundaries == (3, 8)
    assert composed.floor[TRIGGER_LIMIT_BREACH] == 9


def test_an_rss_cap_above_the_informational_band_is_rejected() -> None:
    with pytest.raises(ValueError, match="never outranks an internal finding"):
        compose_floor_config(DEFAULT_FLOOR_CONFIG, _calibration(cap={SOURCE_RSS: 5}))


def test_lowering_the_lower_boundary_can_strand_the_rss_cap() -> None:
    """Same coupling, from the other side: the boundary moved, the cap did not."""
    with pytest.raises(ValueError, match="never outranks an internal finding"):
        compose_floor_config(DEFAULT_FLOOR_CONFIG, _calibration(band_boundaries=(2, 6)))


def test_an_all_clear_cap_above_the_informational_band_is_rejected() -> None:
    with pytest.raises(ValueError, match="never itself urgent"):
        compose_floor_config(DEFAULT_FLOOR_CONFIG, _calibration(cap={TRIGGER_ALL_CLEAR: 6}))


def test_the_constructor_validation_still_applies_to_the_composed_result() -> None:
    """A floor above its cap would invert the clamp (ADR-0088)."""
    with pytest.raises(ValueError, match="the clamp would invert"):
        compose_floor_config(DEFAULT_FLOOR_CONFIG, _calibration(floor={TRIGGER_FRESHNESS: 9}))


def test_an_unpinned_fund_closure_default_is_caught() -> None:
    """The one invariant no revision can violate — only a bad default can."""
    tampered = FloorConfig(
        floor={**DEFAULT_FLOOR_CONFIG.floor, TRIGGER_FUND_CLOSURE: 8},
        cap=DEFAULT_FLOOR_CONFIG.cap,
    )
    with pytest.raises(ValueError, match="pinned level"):
        compose_floor_config(tampered, None)
