# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Compatibility adapter over the unified Floor Config (ADR-0087/0088).

The delta layer (Prompt 3) needed a handful of calibration values — the
magnitude ``re_trigger_delta`` per subject type, the RSS clustering
parameters, and the tag→asset-class map — passed explicitly into the pure
delta functions so they stay pure and testable. Prompt 4 (ADR-0088) folded
those into the single **Floor Config** calibration object
(:class:`services.analytics.irene_floor.FloorConfig`), which also carries
the deterministic-floor axes (floors, caps, band boundaries, options gate).
One coherent object now threads through both the delta layer and the floor.

This module is what remains of the standalone ``DeltaThresholds``: a thin
adapter that re-exports the unified object under its historical names, so
the delta functions and their callers (and the existing tests) keep
importing ``DeltaThresholds`` / ``DEFAULT_DELTA_THRESHOLDS`` unchanged while
the values now live in one place.

``subject_type`` referenced by the delta layer (``"saa"`` / ``"anlv"`` /
``"rss"``) is the rule-formed ``subject_key`` prefix — a deterministic
axis, never any LLM classification.

The pinned ``embedding_model`` and ``similarity_threshold`` remain
*auditable configuration*: changing the embedding model is a logged config
change that **freezes existing open RSS buckets** (the key hashes bucket
membership, not any vector, so a model change can neither re-form nor
re-key a frozen ``rss:cluster:*`` subject). See
:mod:`services.analytics.rss_bucketing`.
"""

from __future__ import annotations

from collections.abc import Mapping

from services.analytics.irene_floor import (
    DEFAULT_FLOOR_CONFIG,
    FloorConfig,
)

# ``DeltaThresholds`` is the historical name for the calibration object the
# delta functions receive; it is now exactly the unified Floor Config. The
# alias keeps ``from services.irene.delta_config import DeltaThresholds``
# (and ``DeltaThresholds(embedding_model=...)`` in tests) working while
# there is a single source of truth.
DeltaThresholds = FloorConfig

# The single default instance threaded into the delta functions and the
# floor (identical object under both names).
DEFAULT_DELTA_THRESHOLDS: FloorConfig = DEFAULT_FLOOR_CONFIG

# Module-level handle on the correlation mapping, so callers that do not
# thread a full config (e.g. the pure correlation unit tests) can still
# reach one auditable source of truth.
DEFAULT_TAG_ASSET_CLASS_MAP: Mapping[str, tuple[str, ...]] = (
    DEFAULT_FLOOR_CONFIG.tag_asset_class_map
)


__all__ = [
    "DEFAULT_DELTA_THRESHOLDS",
    "DEFAULT_TAG_ASSET_CLASS_MAP",
    "DeltaThresholds",
]
