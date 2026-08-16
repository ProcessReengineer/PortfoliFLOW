# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Deterministic RSS clustering orchestration (ADR-0087 Part B).

This is the *impure* half of Irene's RSS delta: it vectorises feed items,
groups semantically similar same-day/same-tag items into clusters, and
forms each cluster's ``subject_key`` — **before any synthesis LLM sees
the items**. It is the DB- and model-aware counterpart to the pure
key-former in :mod:`services.analytics.rss_bucketing`.

The determinism contract (ADR-0087 §Compliance) is delivered by four
rules, in order:

1. **Canonical order.** Incoming items are sorted by ``(published_at,
   url)`` before anything else, so the whole pipeline consumes one fixed
   order regardless of how items arrived. This is what makes greedy
   online clustering deterministic.
2. **Fixed day-buckets.** Each item's ``day_bucket`` is its
   ``published_at`` in **UTC**, truncated to the calendar day (midnight
   UTC). Items only cluster with same-day, same-tag items. Not a rolling
   window. Known, accepted v0 limitation: an event straddling midnight
   UTC splits into two day-buckets → two keys (tested, not "fixed").
3. **Multi-valued tags.** An item with N tags participates in N
   ``(day_bucket, tag)`` dimensions; an untagged item participates in one
   reserved ``("untagged")`` dimension. Cross-source events merge within a
   shared tag dimension — the whole point of tag-scoped (not
   source-scoped) keys.
4. **Freeze on model change.** Existing open ``rss:cluster:*`` findings
   are frozen anchors: their members are absorbed (removed from fresh
   assignment) so the current embedding model never re-forms or re-keys
   them. See :func:`_load_frozen_anchors` and §1.4.

The vectorisation is the *only* model call in the RSS path, and it
happens here (never in the pure key-former): the engine vectorises, groups
by cosine similarity into memberships, and hands the **membership** (stable
item identities, not vectors) to
:func:`services.analytics.rss_bucketing.form_subject_key`.

Layering: lives under ``services/irene`` (imports ``core`` and
``services``), Qt-free.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING
from collections.abc import Sequence

from core.repositories.irene_finding_repository import IreneFindingRepository
from services.analytics.rss_bucketing import (
    SUBJECT_KEY_PREFIX,
    form_subject_key,
    item_identity,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from services.irene.delta_config import DeltaThresholds
    from services.irene.embedding import Embedder
    from services.web_research.models import FeedItem

logger = logging.getLogger(__name__)

_UTC = timezone.utc

# The reserved dimension for items whose source carries no tags. They are
# never dropped — they cluster among themselves under this sentinel.
UNTAGGED_DIMENSION: str = "untagged"


@dataclass(frozen=True)
class RssItemRef:
    """The stable identity view of one clustered feed item.

    Only the fields that identify and attribute an item — never its
    vector. This is what a bucket carries and what the beat renders /
    persists as basis.

    Attributes:
        url: The item's canonical URL.
        title: The item's title (for display / basis, never a key input).
        source_name: The publisher's allowlist display name.
        published_at: The item's timezone-aware UTC publication time.
    """

    url: str
    title: str
    source_name: str
    published_at: datetime


@dataclass(frozen=True)
class RssBucket:
    """One deterministically-formed semantic cluster of feed items.

    Attributes:
        tag: The bucket-dimension tag (a curated tag, or the reserved
            ``untagged`` sentinel).
        day_bucket: The UTC calendar day the cluster belongs to.
        members: The clustered items, in canonical ``(published_at, url)``
            order.
        subject_key: The deterministic ``rss:cluster:<hash>`` key, formed
            from the cluster's membership by
            :func:`services.analytics.rss_bucketing.form_subject_key`.
    """

    tag: str
    day_bucket: date
    members: tuple[RssItemRef, ...]
    subject_key: str


def _item_text(item: FeedItem) -> str:
    """The text vectorised for one item — its title plus any description."""
    return f"{item.title}\n{item.description or ''}".strip()


def _to_ref(item: FeedItem) -> RssItemRef:
    return RssItemRef(
        url=item.url,
        title=item.title,
        source_name=item.source_name,
        published_at=item.published_at,
    )


def _identity(item: FeedItem) -> str:
    return item_identity(published_at=item.published_at, url=item.url)


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 if degenerate)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _dimensions_of(item: FeedItem) -> tuple[str, ...]:
    """The tag dimensions one item participates in (>=1, never empty)."""
    return item.tags if item.tags else (UNTAGGED_DIMENSION,)


def _day_bucket(item: FeedItem) -> date:
    """The item's fixed UTC calendar-day bucket (midnight-UTC truncation)."""
    return item.published_at.astimezone(_UTC).date()


async def _load_frozen_anchors(
    session: AsyncSession,
) -> dict[tuple[date, str], set[str]]:
    """Load open ``rss:cluster:*`` findings as frozen-anchor memberships.

    A surfaced-and-still-open RSS cluster is an immutable identity: its
    members must stay in their bucket and its key must never be recomputed,
    regardless of the current embedding model (§1.4). The membership was
    stashed on the finding ``payload`` (``member_ids`` / ``tag`` /
    ``day_bucket``) when the beat persisted it, so we reconstruct the
    frozen memberships from the open findings here.

    Args:
        session: The tenant-scoped session (RLS-policed reads).

    Returns:
        A mapping ``(day_bucket, tag) -> set(member identity strings)`` —
        the union of every open cluster's members in that dimension. An
        incoming item whose identity is in this set is absorbed (skipped in
        fresh assignment).
    """
    findings = await IreneFindingRepository(session).list_open()
    anchors: dict[tuple[date, str], set[str]] = {}
    for finding in findings:
        if not finding.subject_key.startswith(SUBJECT_KEY_PREFIX):
            continue
        payload = finding.payload or {}
        tag = payload.get("tag")
        day_raw = payload.get("day_bucket")
        member_ids = payload.get("member_ids")
        if not isinstance(tag, str) or not isinstance(member_ids, list):
            continue
        try:
            day = date.fromisoformat(str(day_raw))
        except (TypeError, ValueError):
            continue
        bucket = anchors.setdefault((day, tag), set())
        bucket.update(str(m) for m in member_ids)
    if anchors:
        logger.debug(
            "rss-clustering: %d frozen anchor dimension(s) loaded.",
            len(anchors),
        )
    return anchors


def _cluster_dimension(
    items: list[FeedItem],
    vectors: dict[str, list[float]],
    *,
    threshold: float,
) -> list[list[FeedItem]]:
    """Greedily cluster one dimension's fresh items (canonical order in).

    ``items`` arrives in canonical ``(published_at, url)`` order, so the
    greedy assignment is deterministic. Each item joins the nearest open
    bucket whose cosine similarity to that bucket's centroid is
    ``>= threshold``, else it opens a new bucket.

    Tie-break (the determinism guarantee, not an afterthought): among the
    open buckets at or above threshold, pick the one with the **higher**
    similarity; on an exact similarity tie, pick the bucket whose earliest
    member sorts first by ``(published_at, url)``. Because the earliest
    member is fixed once a bucket opens (items arrive in canonical order,
    so a bucket's seed is its earliest member), this tie-break is total and
    order-stable.
    """
    # Each open bucket: (members, centroid, seed_sort_key).
    buckets: list[tuple[list[FeedItem], list[float], tuple[datetime, str]]] = []
    for item in items:
        vec = vectors[_identity(item)]
        best_idx = -1
        best_sim = -1.0
        for idx, (_members, centroid, seed_key) in enumerate(buckets):
            sim = _cosine(vec, centroid)
            if sim < threshold:
                continue
            if sim > best_sim:
                best_sim = sim
                best_idx = idx
            elif sim == best_sim and best_idx >= 0:
                # Exact tie → the bucket with the earliest-sorting seed.
                if seed_key < buckets[best_idx][2]:
                    best_idx = idx
        if best_idx >= 0:
            members, centroid, seed_key = buckets[best_idx]
            members.append(item)
            n = len(members)
            # Incremental mean keeps the centroid a function of the (fixed)
            # membership, independent of arrival order.
            new_centroid = [(c * (n - 1) + v) / n for c, v in zip(centroid, vec)]
            buckets[best_idx] = (members, new_centroid, seed_key)
        else:
            buckets.append(([item], list(vec), (item.published_at, item.url)))
    return [members for members, _c, _s in buckets]


async def build_rss_buckets(
    session: AsyncSession | None,
    embedder: Embedder,
    items: Sequence[FeedItem],
    *,
    now: datetime,
    thresholds: DeltaThresholds,
) -> list[RssBucket]:
    """Cluster feed items into keyed RSS buckets, deterministically.

    See the module docstring for the four determinism rules. The returned
    buckets are the **fresh** clusters formed this beat (frozen anchors are
    absorbed, not re-emitted); each carries a ``subject_key`` formed before
    any synthesis LLM runs.

    Args:
        session: The tenant-scoped session, used only to load frozen
            anchors (open ``rss:cluster:*`` findings). ``None`` skips
            anchor loading — for pure clustering unit tests and the very
            first run where nothing is persisted yet.
        embedder: The injected vectorisation seam (the only model call in
            the RSS path).
        items: The feed items to cluster (each carrying its source's tags).
        now: The beat clock (timezone-aware UTC); recorded for tracing. The
            day-bucket is derived per item from its own ``published_at``,
            not from ``now`` (v0 fixed-window semantics).
        thresholds: The delta calibration (embedding model + similarity
            threshold live here).

    Returns:
        The fresh buckets, sorted by ``(day_bucket, tag, subject_key)`` for
        a stable, reproducible order.
    """
    ordered = sorted(items, key=lambda it: (it.published_at, it.url))
    if not ordered:
        return []

    frozen = await _load_frozen_anchors(session) if session is not None else {}

    # Group into (day_bucket, tag) dimensions, dropping frozen-anchor
    # members (absorbed: their bucket is immutable, key fixed).
    dimensions: dict[tuple[date, str], list[FeedItem]] = {}
    for item in ordered:  # canonical order preserved into each dimension
        day = _day_bucket(item)
        ident = _identity(item)
        for tag in _dimensions_of(item):
            if ident in frozen.get((day, tag), ()):  # absorbed by a frozen anchor
                continue
            dimensions.setdefault((day, tag), []).append(item)

    if not dimensions:
        logger.debug(
            "rss-clustering: %d item(s) at %s — all absorbed by frozen anchors; no fresh buckets.",
            len(ordered),
            now.isoformat(),
        )
        return []

    # Vectorise every fresh item exactly once, in canonical order.
    fresh_items: list[FeedItem] = []
    seen: set[str] = set()
    for members in dimensions.values():
        for item in members:
            ident = _identity(item)
            if ident not in seen:
                seen.add(ident)
                fresh_items.append(item)
    fresh_items.sort(key=lambda it: (it.published_at, it.url))

    raw_vectors = await embedder.embed(
        [_item_text(it) for it in fresh_items],
        model=thresholds.embedding_model,
    )
    vectors = {_identity(it): vec for it, vec in zip(fresh_items, raw_vectors)}

    buckets: list[RssBucket] = []
    for (day, tag), dim_items in dimensions.items():
        clusters = _cluster_dimension(dim_items, vectors, threshold=thresholds.similarity_threshold)
        for members in clusters:
            members_sorted = sorted(members, key=lambda it: (it.published_at, it.url))
            membership = frozenset(_identity(it) for it in members_sorted)
            subject_key = form_subject_key(day_bucket=day, tag=tag, membership=membership)
            buckets.append(
                RssBucket(
                    tag=tag,
                    day_bucket=day,
                    members=tuple(_to_ref(it) for it in members_sorted),
                    subject_key=subject_key,
                )
            )

    buckets.sort(key=lambda b: (b.day_bucket, b.tag, b.subject_key))
    logger.debug(
        "rss-clustering: %d fresh item(s) → %d bucket(s) at %s.",
        len(fresh_items),
        len(buckets),
        now.isoformat(),
    )
    return buckets


__all__ = [
    "UNTAGGED_DIMENSION",
    "RssBucket",
    "RssItemRef",
    "build_rss_buckets",
]
