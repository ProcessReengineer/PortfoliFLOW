# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Stateful edge-gate for Irene's RSS delta (ADR-0087 Part B).

This module turns the deterministically-formed RSS buckets
(:func:`services.irene.rss_clustering.build_rss_buckets`) into a list of
*eligible findings* — the RSS half of Irene's delta layer. It is the
non-scalar sibling of :mod:`services.irene.internal_delta`: it clusters
feed items into keyed buckets, diffs each bucket's ``rss:cluster:*``
subject against ``irene_watch_state``, and writes the acknowledgement that
suppresses a bucket from re-firing on the next beat.

Edge-triggering (ADR-0087): a **newly-formed** bucket (no prior
acknowledged ``rss:cluster:`` state) is a rising edge → an
:class:`RssEligibleFinding`. A bucket already acknowledged does not
re-fire. RSS subjects are non-scalar (``magnitude is None``), so the
magnitude re-trigger path never applies — that falls out of
``re_trigger_delta["rss"] = 0`` already in :mod:`.delta_config`.

An RSS eligible carries only an **edge band** and a non-binding urgency
hint for context. The persisted card's final urgency and band are decided
by the deterministic floor (:mod:`services.analytics.irene_floor`): a
standalone RSS finding is capped at the ``informational`` band (source =
RSS), while an RSS item corroborating an internal edge is merged into that
internal finding upstream (:mod:`services.irene.correlation`) and so is not
RSS-capped.

Because it reads and writes the database it lives here under
``services/irene`` — never under ``services/analytics`` — and imports only
from ``core`` and ``services`` (Qt-free).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING
from collections.abc import Sequence

from core.repositories.irene_watch_state_repository import (
    IreneWatchStateRepository,
)
from services.irene.delta_config import (
    DEFAULT_DELTA_THRESHOLDS,
    DeltaThresholds,
)
from services.irene.rss_clustering import RssBucket, RssItemRef, build_rss_buckets
from services.watch_desk.overlay import WatchDeskResolution, rss_overlay_subject_key

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from services.irene.embedding import Embedder
    from services.web_research.models import FeedItem

_LOG = logging.getLogger(__name__)

# Edge band + non-binding urgency hint for an RSS eligible. Both are the
# lowest stand-ins: RSS carries no scalar magnitude, and the deterministic
# floor caps a standalone RSS finding at the informational band anyway. The
# edge band is written to ``irene_watch_state`` for edge detection; it is
# not the persisted card band (which the floor derives).
_RSS_PROVISIONAL_BAND: str = "note"
_RSS_PROVISIONAL_URGENCY_HINT: int = 1


@dataclass(frozen=True)
class RssEligibleFinding:
    """One RSS bucket the delta layer deems worth showing Irene.

    The non-scalar sibling of
    :class:`services.irene.internal_delta.EligibleFinding`. It carries no
    ``coverage_pct`` / ``max_pct`` / ``headroom_eur`` — RSS is non-scalar —
    only the identity of the bucket and its members, so the beat can render
    grounded context (titles / sources / day-bucket, never invented
    numbers) and persist a finding whose payload lets the bucket be frozen.

    Attributes:
        subject_key: The bucket's ``rss:cluster:<hash>`` key, formed before
            any synthesis LLM ran.
        bucket_members: The clustered items, in canonical order (stable
            identity fields only, no vectors).
        tags: The bucket's single dimension tag, as a one-tuple. A bucket
            is formed within one ``(day_bucket, tag)`` dimension, so this is
            precisely the tag that groups its members — the tag the
            correlation lift keys off (never ``source_name``).
        day_bucket: The bucket's UTC calendar day.
        reason: A deterministic explanation (the finding basis + audit
            trail): what tag, which day, how many items, which sources.
        provisional_band: The edge band (lowest — ``note``), written to
            ``irene_watch_state`` for edge detection. Not the card's final
            band; the floor caps a standalone RSS finding at
            ``informational``.
        provisional_urgency_hint: A non-binding urgency hint; the
            deterministic floor decides the final urgency.
    """

    subject_key: str
    bucket_members: tuple[RssItemRef, ...]
    tags: tuple[str, ...]
    day_bucket: date
    reason: str
    provisional_band: str
    provisional_urgency_hint: int | None


def _make_rss_eligible(bucket: RssBucket) -> RssEligibleFinding:
    """Assemble an :class:`RssEligibleFinding` from a formed bucket."""
    sources = sorted({m.source_name for m in bucket.members})
    reason = (
        f"new RSS cluster on {bucket.day_bucket.isoformat()} "
        f"[{bucket.tag}] — {len(bucket.members)} item(s) from "
        f"{', '.join(sources)}"
    )
    return RssEligibleFinding(
        subject_key=bucket.subject_key,
        bucket_members=bucket.members,
        tags=(bucket.tag,),
        day_bucket=bucket.day_bucket,
        reason=reason,
        provisional_band=_RSS_PROVISIONAL_BAND,
        provisional_urgency_hint=_RSS_PROVISIONAL_URGENCY_HINT,
    )


async def evaluate_rss_deltas(
    session: AsyncSession,
    embedder: Embedder,
    items: Sequence[FeedItem],
    *,
    now: datetime,
    thresholds: DeltaThresholds = DEFAULT_DELTA_THRESHOLDS,
    resolution: WatchDeskResolution | None = None,
) -> list[RssEligibleFinding]:
    """Return the RSS eligible findings for the active tenant.

    Clusters ``items`` into keyed buckets (freeze-aware), then edge-gates
    each bucket against ``irene_watch_state``: a newly-formed / never
    acknowledged bucket is a rising edge and yields an
    :class:`RssEligibleFinding` (and is acknowledged so it does not
    re-fire); an already-acknowledged bucket stays silent. Must run on a
    **tenant-scoped** session (opened by the tick via ``tenant_context``);
    every read and write is RLS-policed for the active tenant.

    Like the internal delta, the watch-state acknowledgement is written
    **before** synthesis runs, so the edge is "consumed" once shown to
    Irene, independent of whether Irene later phrases a finding for it.

    Args:
        session: A tenant-scoped session.
        embedder: The injected vectorisation seam (the only model call in
            the RSS path).
        items: The feed items to cluster (each carrying its source's tags).
        now: The beat clock (timezone-aware UTC), stamped as
            ``last_seen_at`` / ``acknowledged_at``.
        thresholds: The delta calibration values — the clustering
            parameters (window, embedding model, similarity threshold) the
            bucket former needs.
        resolution: This tenant's effective calibration, for the ``rss``
            overlays. ``None`` means "no overlays apply", which is what a
            caller that is not the beat wants. An ``rss`` overlay carries
            mute alone (schema-enforced): a cluster subject is non-scalar,
            so there is no threshold to move.

    Returns:
        The RSS eligible findings, in ``(day_bucket, tag, subject_key)``
        bucket order. Empty when nothing is fresh.
    """
    buckets = await build_rss_buckets(session, embedder, items, now=now, thresholds=thresholds)
    if not buckets:
        return []

    watch = IreneWatchStateRepository(session)
    eligible: list[RssEligibleFinding] = []
    for bucket in buckets:
        # Capture the acknowledged state BEFORE the upsert (order matches
        # the internal delta). The upsert leaves acknowledged_* untouched.
        prior = await watch.get_by_subject(bucket.subject_key)
        await watch.upsert(
            subject_key=bucket.subject_key,
            magnitude=None,
            band=_RSS_PROVISIONAL_BAND,
            last_seen_at=now,
        )
        if prior is None or prior.acknowledged_at is None:
            # Acknowledge the surfaced bucket so it does not re-fire; RSS is
            # non-scalar, so acknowledged_magnitude is None. Written before
            # the mute gate, exactly as on the internal side: mute suppresses
            # finding creation, never the state that records what was seen.
            await watch.acknowledge(
                subject_key=bucket.subject_key,
                acknowledged_at=now,
                acknowledged_magnitude=None,
            )
            # The mute is carried by the *tag*, not by this bucket: bucket
            # keys are hashes of membership and last a day, while the tag is
            # the enumerated subject the monitor shows and the operator mutes
            # (ADR-0116 §3). There is no breach exception here — an RSS
            # cluster has no ceiling to violate, so a muted press dimension
            # is simply silent.
            if resolution is not None and resolution.is_muted(rss_overlay_subject_key(bucket.tag)):
                _LOG.info(
                    "irene rss-delta: press dimension %r is muted — cluster %r "
                    "suppressed (watch-state advanced).",
                    bucket.tag,
                    bucket.subject_key,
                )
                continue
            eligible.append(_make_rss_eligible(bucket))
        # else: already acknowledged → no re-fire (edge-triggered).

    _LOG.debug(
        "irene rss-delta: %d bucket(s), %d rising edge(s).",
        len(buckets),
        len(eligible),
    )
    return eligible


__all__ = ["RssEligibleFinding", "evaluate_rss_deltas"]
