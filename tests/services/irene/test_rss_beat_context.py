# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure rendering tests for the beat's delta context (ADR-0087 Part B).

``services.irene.beat._build_delta_context`` is pure given the eligible
lists, so its silence / internal / RSS / correlation-merge rendering is
tested without a DB or LLM.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from services.irene.beat import _build_delta_context
from services.irene.delta_config import DEFAULT_TAG_ASSET_CLASS_MAP
from services.irene.internal_delta import EligibleFinding
from services.irene.rss_clustering import RssItemRef
from services.irene.rss_delta import RssEligibleFinding


def _now() -> datetime:
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def _internal(subject_key: str = "saa:equities") -> EligibleFinding:
    return EligibleFinding(
        subject_key=subject_key,
        kind="rising_edge",
        reason="band worsened OK → BREACH",
        coverage_pct=Decimal("60"),
        max_pct=Decimal("50"),
        headroom_eur=Decimal("-100000"),
        status="BREACH",
        band="act",
        current_magnitude=Decimal("60"),
        acknowledged_magnitude=None,
        provisional_urgency_hint=4,
    )


def _rss(subject_key: str, tag: str, title: str) -> RssEligibleFinding:
    member = RssItemRef(
        url="https://ecb/1",
        title=title,
        source_name="ECB",
        published_at=datetime(2026, 6, 30, 9, tzinfo=timezone.utc),
    )
    return RssEligibleFinding(
        subject_key=subject_key,
        bucket_members=(member,),
        tags=(tag,),
        day_bucket=date(2026, 6, 30),
        reason=f"new RSS cluster on 2026-06-30 [{tag}] — 1 item(s) from ECB",
        provisional_band="note",
        provisional_urgency_hint=1,
    )


def _content(internal, rss) -> str:
    msgs = _build_delta_context(
        internal, rss, _now(), tag_asset_class_map=DEFAULT_TAG_ASSET_CLASS_MAP
    )
    assert len(msgs) == 1
    return msgs[0]["content"]


def test_silence_when_both_empty() -> None:
    content = _content([], [])
    assert "no material change" in content
    assert "silence is the correct outcome" in content


def test_internal_only_renders_numeric_block() -> None:
    content = _content([_internal()], [])
    assert "subject_key: saa:equities" in content
    assert "60%" in content and "50%" in content  # explicit figures


def test_rss_only_renders_titles_without_numbers() -> None:
    content = _content([], [_rss("rss:cluster:eq", "equities", "RATE: ECB holds")])
    assert "subject_key: rss:cluster:eq" in content
    assert "RATE: ECB holds (ECB)" in content
    assert "new RSS cluster" in content


def test_correlation_merge_folds_rss_into_internal_card() -> None:
    internal = [_internal("saa:equities")]
    rss = [_rss("rss:cluster:eq", "equities", "RATE: ECB holds")]
    content = _content(internal, rss)

    # The internal card is present with its corroboration…
    assert "subject_key: saa:equities" in content
    assert "corroborating external signal(s)" in content
    assert "RATE: ECB holds (ECB)" in content
    # …and the RSS bucket is NOT rendered as a standalone card.
    assert "subject_key: rss:cluster:eq" not in content


def test_broad_tag_rss_renders_standalone_alongside_internal() -> None:
    internal = [_internal("saa:equities")]
    rss = [_rss("rss:cluster:macro", "macro", "RATE: policy update")]
    content = _content(internal, rss)
    # macro maps to no class → standalone, not corroboration.
    assert "subject_key: saa:equities" in content
    assert "subject_key: rss:cluster:macro" in content
    assert "corroborating external signal(s)" not in content
