# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Compose a tenant's effective ``FloorConfig`` from defaults ⊕ deviations.

ADR-0116 §5 makes materiality calibration per-tenant: the beat composes
the effective config per run as **defaults ⊕ the tenant's latest
effective ``floor_calibration`` row ⊕ per-subject overlay values**. This
module owns the middle step — the one that can be pure, and therefore
should be:

    compose_floor_config(DEFAULT_FLOOR_CONFIG, calibration) -> FloorConfig

Per-subject overlays (a watchpoint's ``warn_threshold_pct`` /
``re_trigger_delta`` for one subject) are *not* composed here. They are
resolved subject by subject and handed to the pure layers as plain
arguments alongside the config — which is how the coverage engine and
the delta layer already take them, and why extending them was never
necessary (ADR-0116 §Context).

Purity
------
Lives under ``services/analytics/`` and is held to the ADR-0013 /
ADR-0045 §3 purity contract enforced by
``tests/regression/test_analytics_layer_pure.py``: no database, no
session, no FastAPI, no Qt. It imports one repository *DTO* — the
documented allowance for this root, since repository modules co-locate
their DTO with a class that imports SQLAlchemy — and touches nothing
else. The impure beat fetches the row; this function just folds it.

Why it re-validates
-------------------
Composition is where a *stored* revision meets *current* code defaults,
and the pair can be individually fine yet jointly wrong: a revision that
raised the upper band boundary is valid on its own, but combined with a
``limit_breach`` floor left at its default it would put a regulatory
breach below the critical band. So the composed result goes through the
full ``FloorConfig`` constructor **and** the pinned invariants
(:func:`services.analytics.irene_floor.validate_pinned_invariants`).

The write path prevents such a pair from being created in the first
place, so this check should never fire in practice. It is here for the
case the write path cannot cover: a revision that was valid when written
and that a later change to a *code default* invalidated. Failing loudly
at composition is the correct outcome there — silently running an
inverted configuration is not (ADR-0116 §5).
"""

from __future__ import annotations

from core.repositories.floor_calibration_repository import FloorCalibrationDTO
from services.analytics.irene_floor import FloorConfig, validate_pinned_invariants

__all__ = ["compose_floor_config"]


def compose_floor_config(
    defaults: FloorConfig,
    calibration: FloorCalibrationDTO | None,
) -> FloorConfig:
    """Fold a tenant's stored deviations over the code defaults.

    Field-by-field: the three maps (``floor``, ``cap``,
    ``re_trigger_delta``) are merged key-wise, so a tenant that overrode
    one floor keeps the defaults for every other; the scalar fields
    (``band_boundaries``, ``options_min_band``) are replaced when present.
    The RSS clustering parameters — window, embedding model, similarity
    threshold, tag map — are carried from ``defaults`` untouched: they are
    not part of the calibration surface ADR-0116 §7 opens, because
    changing the pinned embedding model freezes open buckets and is an
    application-wide decision, not a tenant knob.

    ``warn_default_pct`` is deliberately **not** folded in: the WARN
    threshold is not a ``FloorConfig`` field — the coverage engine has
    always taken it as a call parameter — so the beat resolves it
    separately from the same calibration row.

    Args:
        defaults: The code defaults, normally
            :data:`services.analytics.irene_floor.DEFAULT_FLOOR_CONFIG`.
        calibration: The tenant's effective revision, or ``None`` for a
            tenant that never customised anything.

    Returns:
        The effective :class:`FloorConfig` — ``defaults`` itself when
        ``calibration`` is ``None``.

    Raises:
        ValueError: If the composed configuration fails the ``FloorConfig``
            constructor validation or any pinned invariant.
    """
    if calibration is None:
        # Still validated: a broken default set must not slip through just
        # because no tenant customised it.
        validate_pinned_invariants(defaults)
        return defaults

    composed = FloorConfig(
        floor={**defaults.floor, **calibration.floor},
        cap={**defaults.cap, **calibration.cap},
        band_boundaries=(
            calibration.band_boundaries
            if calibration.band_boundaries is not None
            else defaults.band_boundaries
        ),
        options_min_band=(
            calibration.options_min_band
            if calibration.options_min_band is not None
            else defaults.options_min_band
        ),
        re_trigger_delta={**defaults.re_trigger_delta, **calibration.re_trigger_delta},
        rss_time_window_hours=defaults.rss_time_window_hours,
        embedding_model=defaults.embedding_model,
        similarity_threshold=defaults.similarity_threshold,
        tag_asset_class_map=defaults.tag_asset_class_map,
    )
    validate_pinned_invariants(composed)
    return composed
