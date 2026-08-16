# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Fixed-income analytics — pure calculation layer.

The first module of a fixed-income analytics domain seeded by ADR-0082
(the Front-Office universe-charts Fixed-Income archetype) and expected to
grow with duration- and YTM-aggregation primitives as later surfaces need
them. For now it carries the single figure the triplet's Fixed-Income KPI
caption needs beyond what the existing benchmark/statistics analytics
already provide: the notch-weighted average credit rating.

Per ADR-0013 / ADR-0045 §3 the module is pure: stdlib plus the analytics
DTOs, no database, FastAPI, Qt, or matplotlib coupling. Functions take
plain in-memory inputs (a bucket → weight mapping) and return frozen
dataclasses.

The notch scale is the seven rated buckets of ADR-0079 §2:

    AAA=1, AA=2, A=3, BBB=4, BB=5, B=6, CCC_and_below=7

``NR`` (not rated) and any out-of-taxonomy bucket carry no notch and are
excluded from the weighted mean.
"""

from __future__ import annotations

from collections.abc import Mapping

from services.analytics._dtos import NotchWeightedRating

# The seven rated buckets of ADR-0079 §2 mapped to contiguous notches.
# ``NR`` is deliberately absent — it has no notch and never participates
# in the weighted mean.
_NOTCH_BY_BUCKET: dict[str, int] = {
    "AAA": 1,
    "AA": 2,
    "A": 3,
    "BBB": 4,
    "BB": 5,
    "B": 6,
    "CCC_and_below": 7,
}
_BUCKET_BY_NOTCH: dict[int, str] = {notch: bucket for bucket, notch in _NOTCH_BY_BUCKET.items()}


def compute_notch_weighted_average_rating(
    rating_weights: Mapping[str, float],
) -> NotchWeightedRating:
    """Notch-weighted average credit rating of a bucket distribution.

    The headline average rating shown on the Fixed-Income KPI caption
    (ADR-0082 §5). The distribution is mapped bucket → notch on the
    ADR-0079 §2 scale, averaged by weight over the rated buckets, and
    mapped back to a bucket. A naive (unweighted) mean of the buckets is
    explicitly **not** used — the difference is meaningful and tested
    (ADR-0079 Test 4).

    The input is the bucket → ``weight_pct`` mapping for a single
    ``as_of_date`` (the caller selects the date — typically the latest).
    Weights need not sum to 100.

    Method:

    - ``NR`` and any unknown bucket are excluded from the weighted mean;
      the remaining rated weights are renormalised implicitly by dividing
      by their own sum.
    - ``average_notch = Σ(notch_i · w_i) / Σ(w_i)`` over the rated buckets.
    - ``average_bucket`` is the rated bucket whose notch is nearest the
      ``average_notch`` rounded to the nearest integer.
    - ``rated_weight_pct = Σ`` of the rated weights *before* renormalisation
      — effectively total weight minus ``NR`` and unknown buckets.

    Edge cases are explicit (no silent fallback): an empty mapping, an
    ``NR``-only mapping, or any input with no rated weight ``> 0`` returns
    ``average_notch = float('nan')``, ``average_bucket = 'NR'``, and
    ``rated_weight_pct = 0.0``.

    Args:
        rating_weights: Mapping of rating bucket → ``weight_pct`` for one
            ``as_of_date``. Recognised rated buckets are the seven of
            ADR-0079 §2; ``NR`` and unknown keys are excluded from the mean.

    Returns:
        A frozen :class:`~services.analytics._dtos.NotchWeightedRating`.
    """
    rated: list[tuple[int, float]] = []
    for bucket, weight in rating_weights.items():
        notch = _NOTCH_BY_BUCKET.get(bucket)
        if notch is None:
            # NR or out-of-taxonomy bucket — no notch, excluded.
            continue
        weight_value = float(weight)
        if weight_value <= 0.0:
            continue
        rated.append((notch, weight_value))

    rated_weight_total = sum(weight for _, weight in rated)
    if not rated or rated_weight_total <= 0.0:
        return NotchWeightedRating(
            average_notch=float("nan"),
            average_bucket="NR",
            rated_weight_pct=0.0,
        )

    average_notch = sum(notch * weight for notch, weight in rated) / rated_weight_total
    rounded_notch = round(average_notch)
    nearest_notch = min(_BUCKET_BY_NOTCH, key=lambda notch: abs(notch - rounded_notch))
    return NotchWeightedRating(
        average_notch=average_notch,
        average_bucket=_BUCKET_BY_NOTCH[nearest_notch],
        rated_weight_pct=rated_weight_total,
    )
