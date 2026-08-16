# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Determinism tests for RSS bucketing (ADR-0087 Part B, checkpoint 3).

Exercises the pure key-former (:mod:`services.analytics.rss_bucketing`)
and the clustering orchestration
(:func:`services.irene.rss_clustering.build_rss_buckets`) with a fixed
stub embedder and ``session=None`` — no network, no DB. The whole point is
determinism: the same items in any input order produce identical buckets
and identical ``subject_key``s.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

from services.analytics.rss_bucketing import form_subject_key, item_identity
from services.irene.delta_config import DEFAULT_DELTA_THRESHOLDS
from services.irene.rss_clustering import build_rss_buckets
from tests.services.irene._rss_fixtures import StubEmbedder, make_item


def _dt(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Pure key-former
# ---------------------------------------------------------------------------


def test_form_subject_key_is_order_independent() -> None:
    from datetime import date

    a = form_subject_key(
        day_bucket=date(2026, 6, 30),
        tag="macro",
        membership=frozenset({"x", "y", "z"}),
    )
    b = form_subject_key(
        day_bucket=date(2026, 6, 30),
        tag="macro",
        membership=frozenset({"z", "x", "y"}),
    )
    assert a == b
    assert a.startswith("rss:cluster:")


def test_form_subject_key_varies_with_membership_and_dimension() -> None:
    from datetime import date

    base = form_subject_key(day_bucket=date(2026, 6, 30), tag="macro", membership=frozenset({"x"}))
    # Different membership → different key.
    assert base != form_subject_key(
        day_bucket=date(2026, 6, 30),
        tag="macro",
        membership=frozenset({"x", "y"}),
    )
    # Different tag → different key.
    assert base != form_subject_key(
        day_bucket=date(2026, 6, 30), tag="equities", membership=frozenset({"x"})
    )
    # Different day → different key.
    assert base != form_subject_key(
        day_bucket=date(2026, 6, 29), tag="macro", membership=frozenset({"x"})
    )


# ---------------------------------------------------------------------------
# Clustering determinism
# ---------------------------------------------------------------------------


def _sample_items() -> list:
    # Three "RATE" items (one macro topic) + two "MERGER" items, all same
    # UTC day, all tagged `macro`.
    return [
        make_item("https://a/1", "RATE: ECB holds", _dt(30, 9), tags=("macro",)),
        make_item("https://b/2", "RATE: Fed pause", _dt(30, 10), tags=("macro",)),
        make_item("https://c/3", "RATE: BoE steady", _dt(30, 11), tags=("macro",)),
        make_item("https://d/4", "MERGER: bank tie-up", _dt(30, 8), tags=("macro",)),
        make_item("https://e/5", "MERGER: fund deal", _dt(30, 12), tags=("macro",)),
    ]


async def _buckets(items: list) -> list:
    return await build_rss_buckets(
        None,
        StubEmbedder(),
        items,
        now=_now(),
        thresholds=DEFAULT_DELTA_THRESHOLDS,
    )


async def test_same_items_any_order_identical_buckets_and_keys() -> None:
    items = _sample_items()
    base = await _buckets(items)

    # Two topics under `macro` → two buckets.
    assert len(base) == 2
    base_keys = sorted(b.subject_key for b in base)

    for seed in range(5):
        shuffled = items[:]
        random.Random(seed).shuffle(shuffled)
        again = await _buckets(shuffled)
        assert sorted(b.subject_key for b in again) == base_keys
        # Membership is identical bucket-for-bucket, regardless of order.
        by_key = {b.subject_key: b for b in again}
        for b in base:
            members_a = {m.url for m in b.members}
            members_b = {m.url for m in by_key[b.subject_key].members}
            assert members_a == members_b


async def test_cross_source_same_tag_same_day_forms_one_bucket() -> None:
    # One event ("RATE") reported by three different sources, all sharing
    # the `macro` tag on the same UTC day → exactly one bucket.
    items = [
        make_item("https://ecb/x", "RATE: rate move", _dt(30, 9), tags=("macro",), source="ECB"),
        make_item(
            "https://ft/y", "RATE: rate move covered", _dt(30, 10), tags=("macro",), source="FT"
        ),
        make_item(
            "https://finews/z",
            "RATE: rate move analysis",
            _dt(30, 11),
            tags=("macro",),
            source="finews",
        ),
    ]
    buckets = await _buckets(items)
    assert len(buckets) == 1
    bucket = buckets[0]
    assert bucket.tag == "macro"
    assert {m.source_name for m in bucket.members} == {"ECB", "FT", "finews"}


async def test_midnight_utc_split_is_two_day_buckets() -> None:
    # A single event straddling midnight UTC (23:50 → 00:10) splits into two
    # calendar-day buckets → two keys. This is the documented, accepted v0
    # limitation — asserted, not "fixed".
    items = [
        make_item("https://a/late", "RATE: late break", _dt(29, 23, 50), tags=("macro",)),
        make_item("https://a/early", "RATE: early follow", _dt(30, 0, 10), tags=("macro",)),
    ]
    buckets = await _buckets(items)
    assert len(buckets) == 2
    assert {b.day_bucket.isoformat() for b in buckets} == {
        "2026-06-29",
        "2026-06-30",
    }
    assert len({b.subject_key for b in buckets}) == 2


async def test_multi_valued_tags_place_item_in_each_dimension() -> None:
    # An item tagged (macro, regulator) participates in both dimensions →
    # two single-item buckets, one per tag.
    items = [
        make_item("https://ecb/1", "RATE: ECB decision", _dt(30, 9), tags=("macro", "regulator")),
    ]
    buckets = await _buckets(items)
    assert {b.tag for b in buckets} == {"macro", "regulator"}
    assert all(len(b.members) == 1 for b in buckets)
    # The two dimension buckets have distinct keys (tag is key material).
    assert len({b.subject_key for b in buckets}) == 2


async def test_untagged_source_clusters_under_untagged_dimension() -> None:
    items = [
        make_item("https://x/1", "RATE: no tags", _dt(30, 9), tags=()),
        make_item("https://x/2", "RATE: no tags either", _dt(30, 10), tags=()),
    ]
    buckets = await _buckets(items)
    assert len(buckets) == 1
    assert buckets[0].tag == "untagged"


async def test_subject_key_matches_pure_key_former() -> None:
    # The orchestrator's key equals the pure key-former over the same
    # membership — the key really is formed from membership, nothing else.
    items = _sample_items()
    buckets = await _buckets(items)
    for b in buckets:
        membership = frozenset(
            item_identity(published_at=m.published_at, url=m.url) for m in b.members
        )
        assert b.subject_key == form_subject_key(
            day_bucket=b.day_bucket, tag=b.tag, membership=membership
        )
