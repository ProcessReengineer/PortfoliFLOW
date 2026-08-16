# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure tests for the RSS↔internal correlation lift (ADR-0087 §1.5).

No DB, no network: :func:`services.irene.correlation.correlate` is a pure
function over the two eligible lists plus the auditable tag→asset-class
map. The "denominator demo": a coincident internal edge absorbs a
corresponding RSS bucket as corroboration, and the standalone RSS eligible
is suppressed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from services.irene.correlation import correlate
from services.irene.delta_config import DEFAULT_TAG_ASSET_CLASS_MAP
from services.irene.internal_delta import EligibleFinding
from services.irene.rss_clustering import RssItemRef
from services.irene.rss_delta import RssEligibleFinding


def _internal(subject_key: str) -> EligibleFinding:
    return EligibleFinding(
        subject_key=subject_key,
        kind="rising_edge",
        reason="band worsened",
        coverage_pct=Decimal("60"),
        max_pct=Decimal("50"),
        headroom_eur=Decimal("-100000"),
        status="BREACH",
        band="act",
        current_magnitude=Decimal("60"),
        acknowledged_magnitude=None,
        provisional_urgency_hint=4,
    )


def _rss(subject_key: str, tag: str) -> RssEligibleFinding:
    member = RssItemRef(
        url="https://x/1",
        title="RATE: something",
        source_name="ECB",
        published_at=datetime(2026, 6, 30, 9, tzinfo=timezone.utc),
    )
    return RssEligibleFinding(
        subject_key=subject_key,
        bucket_members=(member,),
        tags=(tag,),
        day_bucket=date(2026, 6, 30),
        reason="new RSS cluster",
        provisional_band="note",
        provisional_urgency_hint=1,
    )


def test_coincident_edge_absorbs_matching_rss_bucket() -> None:
    internal = [_internal("saa:equities")]
    rss = [_rss("rss:cluster:aaa", "equities")]

    result = correlate(internal, rss, tag_asset_class_map=DEFAULT_TAG_ASSET_CLASS_MAP)

    # One internal card, carrying the RSS bucket as corroboration.
    assert len(result.merged) == 1
    assert result.merged[0].internal.subject_key == "saa:equities"
    assert len(result.merged[0].corroborating) == 1
    assert result.merged[0].corroborating[0].subject_key == "rss:cluster:aaa"
    # The standalone RSS eligible is suppressed.
    assert result.standalone_rss == ()


def test_broad_tag_never_merges_and_stands_alone() -> None:
    # `macro` maps to no asset class → the bucket always stands alone even
    # with a coincident internal edge.
    internal = [_internal("saa:equities")]
    rss = [_rss("rss:cluster:macro", "macro")]

    result = correlate(internal, rss, tag_asset_class_map=DEFAULT_TAG_ASSET_CLASS_MAP)

    assert result.merged[0].corroborating == ()
    assert [r.subject_key for r in result.standalone_rss] == ["rss:cluster:macro"]


def test_rss_matching_no_internal_stands_alone() -> None:
    internal = [_internal("saa:private_equity")]
    rss = [_rss("rss:cluster:eq", "equities")]

    result = correlate(internal, rss, tag_asset_class_map=DEFAULT_TAG_ASSET_CLASS_MAP)

    # equities → (equities, listed_equity); private_equity is not in that
    # set, so no merge.
    assert result.merged[0].corroborating == ()
    assert [r.subject_key for r in result.standalone_rss] == ["rss:cluster:eq"]


def test_internal_without_corroboration_still_rendered() -> None:
    internal = [_internal("saa:equities"), _internal("anlv:anlv_1")]
    result = correlate(internal, [], tag_asset_class_map=DEFAULT_TAG_ASSET_CLASS_MAP)
    assert len(result.merged) == 2
    assert all(m.corroborating == () for m in result.merged)
    assert result.standalone_rss == ()


def test_rss_attached_to_each_matching_internal() -> None:
    # One RSS bucket, two internal edges both matching `equities`.
    internal = [_internal("saa:equities"), _internal("saa:listed_equity")]
    rss = [_rss("rss:cluster:eq", "equities")]
    result = correlate(internal, rss, tag_asset_class_map=DEFAULT_TAG_ASSET_CLASS_MAP)
    assert all(len(m.corroborating) == 1 for m in result.merged)
    assert result.standalone_rss == ()
