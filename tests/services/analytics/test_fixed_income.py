# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.analytics.fixed_income``.

Pure-function tests — no DB, no Qt, no FastAPI. Each test builds a
deterministic rating-bucket distribution and asserts the notch-weighted
average against hand-computed values. The notch scale is the seven rated
buckets of ADR-0079 §2 (``AAA=1 … CCC_and_below=7``); ``NR`` and unknown
buckets carry no notch.
"""

from __future__ import annotations

import math

import pytest

from services.analytics._dtos import NotchWeightedRating
from services.analytics.fixed_income import (
    compute_notch_weighted_average_rating,
)


def test_known_distribution_maps_to_expected_notch_and_bucket() -> None:
    """{AAA:50, AA:30, A:20} → notch 1.7, nearest bucket AA."""
    result = compute_notch_weighted_average_rating({"AAA": 50.0, "AA": 30.0, "A": 20.0})
    assert isinstance(result, NotchWeightedRating)
    # (1·50 + 2·30 + 3·20) / 100 = 170 / 100 = 1.7
    assert result.average_notch == pytest.approx(1.7, abs=1e-12)
    assert result.average_bucket == "AA"  # round(1.7) = 2 → AA
    assert result.rated_weight_pct == pytest.approx(100.0, abs=1e-12)


def test_nr_excluded_and_rated_weights_renormalised() -> None:
    """NR is dropped from the mean; the rated weights renormalise."""
    result = compute_notch_weighted_average_rating({"AAA": 30.0, "BBB": 50.0, "NR": 20.0})
    # Mean over rated only: (1·30 + 4·50) / 80 = 230 / 80 = 2.875.
    assert result.average_notch == pytest.approx(230.0 / 80.0, abs=1e-12)
    assert result.average_bucket == "A"  # round(2.875) = 3 → A
    # Rated weight is total minus NR (before renormalisation).
    assert result.rated_weight_pct == pytest.approx(80.0, abs=1e-12)


def test_weights_need_not_sum_to_100() -> None:
    """Renormalisation divides by the rated-weight sum, not by 100."""
    result = compute_notch_weighted_average_rating({"A": 10.0, "BBB": 30.0})
    # (3·10 + 4·30) / 40 = 150 / 40 = 3.75 → round 4 → BBB.
    assert result.average_notch == pytest.approx(3.75, abs=1e-12)
    assert result.average_bucket == "BBB"
    assert result.rated_weight_pct == pytest.approx(40.0, abs=1e-12)


def test_unknown_bucket_excluded_like_nr() -> None:
    result = compute_notch_weighted_average_rating({"AAA": 50.0, "ZZZ": 50.0})
    assert result.average_notch == pytest.approx(1.0, abs=1e-12)
    assert result.average_bucket == "AAA"
    # Only the AAA weight is rated.
    assert result.rated_weight_pct == pytest.approx(50.0, abs=1e-12)


def test_only_nr_returns_sentinel() -> None:
    result = compute_notch_weighted_average_rating({"NR": 100.0})
    assert math.isnan(result.average_notch)
    assert result.average_bucket == "NR"
    assert result.rated_weight_pct == 0.0


def test_empty_mapping_returns_sentinel() -> None:
    result = compute_notch_weighted_average_rating({})
    assert math.isnan(result.average_notch)
    assert result.average_bucket == "NR"
    assert result.rated_weight_pct == 0.0


def test_zero_weight_rated_buckets_return_sentinel() -> None:
    """A rated bucket present but with zero weight is no rated weight > 0."""
    result = compute_notch_weighted_average_rating({"AAA": 0.0, "BBB": 0.0, "NR": 50.0})
    assert math.isnan(result.average_notch)
    assert result.average_bucket == "NR"
    assert result.rated_weight_pct == 0.0


def test_naive_unweighted_mean_diverges_from_notch_weighted() -> None:
    """ADR-0079 Test 4: a naive bucket mean differs from the weighted one.

    A barbell {AAA:90, CCC_and_below:10} is dominated by the AAA weight.
    The notch-weighted mean (1.6 → AA) differs sharply from a naive mean
    of the present buckets ((1 + 7) / 2 = 4.0 → BBB). Asserting the gap
    guards against a regression that silently drops the weighting.
    """
    weights = {"AAA": 90.0, "CCC_and_below": 10.0}
    result = compute_notch_weighted_average_rating(weights)
    # Notch-weighted: (1·90 + 7·10) / 100 = 160 / 100 = 1.6.
    assert result.average_notch == pytest.approx(1.6, abs=1e-12)
    assert result.average_bucket == "AA"  # round(1.6) = 2 → AA

    # Naive unweighted mean of the present rated buckets' notches.
    naive_notch = (1.0 + 7.0) / 2.0  # = 4.0 → would map to BBB
    assert abs(result.average_notch - naive_notch) > 1.0
    assert result.average_bucket != "BBB"
