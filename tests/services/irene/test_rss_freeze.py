# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Freeze-on-model-change regression (ADR-0087 Part B, checkpoint 4).

An open ``rss:cluster:*`` finding is a frozen anchor: its members are
absorbed from fresh assignment, so a change of the pinned embedding model
can neither re-form nor re-key it, and produces no new findings for it (no
re-alarm storm). Live-DB, because the freeze anchor is loaded from open
``irene_finding`` rows under a tenant context. The embedder is a stub whose
clustering deliberately *changes* between two model ids — the counterfactual
that makes the freeze necessary, not incidental.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import tenant_context
from core.repositories.irene_finding_repository import IreneFindingRepository
from core.repositories.irene_watch_state_repository import (
    IreneWatchStateRepository,
)
from services.irene.beat import _augment_rss_payload
from services.irene.delta_config import DeltaThresholds
from services.irene.rss_clustering import build_rss_buckets
from services.irene.rss_delta import evaluate_rss_deltas
from tests.services.irene._rss_fixtures import make_item

_MODEL_A = "embed-model-A"
_MODEL_B = "embed-model-B"


def _dt(hour: int) -> datetime:
    return datetime(2026, 6, 30, hour, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


class _SplitByModelEmbedder:
    """Clusters items into ONE bucket under model A, N buckets under B.

    Under ``_MODEL_A`` every item gets the same vector (cosine ``1.0`` ⇒ a
    single bucket). Under ``_MODEL_B`` every item gets a distinct one-hot
    vector (cosine ``0.0`` ⇒ one bucket each). So absent freeze, switching
    A→B would shatter one acknowledged cluster into several new keys — the
    storm the freeze must prevent.
    """

    async def embed(self, texts, *, model: str) -> list[list[float]]:
        texts = list(texts)
        if model == _MODEL_A:
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]
        vectors: list[list[float]] = []
        for i, _ in enumerate(texts):
            vec = [0.0, 0.0, 0.0, 0.0]
            vec[i % 4] = 1.0
            vectors.append(vec)
        return vectors


def _items() -> list:
    return [
        make_item("https://a/1", "one", _dt(9), tags=("macro",)),
        make_item("https://a/2", "two", _dt(10), tags=("macro",)),
        make_item("https://a/3", "three", _dt(11), tags=("macro",)),
    ]


async def test_model_change_freezes_open_bucket_no_storm(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("rss-freeze")
    items = _items()
    embedder = _SplitByModelEmbedder()
    thresholds_a = DeltaThresholds(embedding_model=_MODEL_A)
    thresholds_b = DeltaThresholds(embedding_model=_MODEL_B)

    # Beat 1 (model A): the three items form ONE bucket. Simulate the model
    # surfacing it by persisting an open finding with the membership payload
    # (exactly what run_beat does), so the bucket becomes a frozen anchor.
    async with tenant_context(app_engine, tenant_id) as session:
        eligible = await evaluate_rss_deltas(
            session, embedder, items, now=_now(), thresholds=thresholds_a
        )
        assert len(eligible) == 1
        key_a = eligible[0].subject_key
        payload = _augment_rss_payload({"finding": "cluster"}, eligible[0])
        await IreneFindingRepository(session).append(
            subject_key=key_a, payload=payload, urgency=1, band="note"
        )

    # Counterfactual: absent freeze (session=None), model B shatters the
    # same items into THREE new buckets with keys != key_a. This is the
    # storm the freeze must suppress.
    naive_b = await build_rss_buckets(None, embedder, items, now=_now(), thresholds=thresholds_b)
    assert len(naive_b) == 3
    assert all(b.subject_key != key_a for b in naive_b)

    # Beat 2 (model B), WITH the open finding present: every item is
    # absorbed by the frozen anchor → no fresh buckets, no new eligibles.
    async with tenant_context(app_engine, tenant_id) as session:
        eligible_b = await evaluate_rss_deltas(
            session, embedder, items, now=_now(), thresholds=thresholds_b
        )
        assert eligible_b == []

        # The open bucket's key is untouched, and it is still the only
        # finding — no re-alarm storm.
        open_findings = await IreneFindingRepository(session).list_open()
        assert [f.subject_key for f in open_findings] == [key_a]

        finding_count = (
            await session.execute(text("SELECT count(*) FROM irene_finding"))
        ).scalar_one()
        assert finding_count == 1

        # Its acknowledged watch-state is untouched, too.
        state = await IreneWatchStateRepository(session).get_by_subject(key_a)
        assert state is not None
        assert state.acknowledged_at is not None
