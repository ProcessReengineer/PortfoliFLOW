# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Live-DB tests for the RSS edge-gate (ADR-0087 Part B, checkpoint 5).

Exercises :func:`services.irene.rss_delta.evaluate_rss_deltas` against the
compose Postgres via the shared ``app_engine`` / ``seed_tenant`` fixtures:
the edge gate reads/writes ``irene_watch_state``, so a live tenant context
is required. The embedder is the offline stub — no network.

Coverage:

* A newly-formed cluster ⇒ one rising-edge RSS eligible; watch-state
  acknowledged so it does not re-fire.
* A cross-source, same-tag, same-UTC-day event ⇒ one bucket, one eligible.
* A second identical beat ⇒ silence (the bucket is already acknowledged).
* No items ⇒ silence.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import tenant_context
from core.repositories.irene_watch_state_repository import (
    IreneWatchStateRepository,
)
from services.irene.rss_delta import evaluate_rss_deltas
from tests.services.irene._rss_fixtures import StubEmbedder, make_item


def _dt(day: int, hour: int) -> datetime:
    return datetime(2026, 6, day, hour, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def _event_items() -> list:
    # One event ("RATE") reported by three sources sharing the `macro` tag,
    # same UTC day → must form exactly one bucket.
    return [
        make_item("https://ecb/x", "RATE: move", _dt(30, 9), tags=("macro",), source="ECB"),
        make_item("https://ft/y", "RATE: move covered", _dt(30, 10), tags=("macro",), source="FT"),
        make_item(
            "https://finews/z", "RATE: move analysis", _dt(30, 11), tags=("macro",), source="finews"
        ),
    ]


async def test_cross_source_event_one_bucket_one_eligible(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("rss-edge")
    items = _event_items()

    async with tenant_context(app_engine, tenant_id) as session:
        eligible = await evaluate_rss_deltas(session, StubEmbedder(), items, now=_now())

        assert len(eligible) == 1
        finding = eligible[0]
        assert finding.subject_key.startswith("rss:cluster:")
        assert finding.tags == ("macro",)
        assert {m.source_name for m in finding.bucket_members} == {
            "ECB",
            "FT",
            "finews",
        }
        assert finding.provisional_band == "note"

        # The bucket is acknowledged so it does not re-fire.
        watch = IreneWatchStateRepository(session)
        state = await watch.get_by_subject(finding.subject_key)
        assert state is not None
        assert state.magnitude is None
        assert state.acknowledged_at is not None


async def test_second_identical_beat_is_silent(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("rss-repeat")
    items = _event_items()

    async with tenant_context(app_engine, tenant_id) as session:
        first = await evaluate_rss_deltas(session, StubEmbedder(), items, now=_now())
        assert len(first) == 1

    # Same items again: the bucket key is identical and already
    # acknowledged in watch-state, so the edge does not re-fire.
    async with tenant_context(app_engine, tenant_id) as session:
        second = await evaluate_rss_deltas(session, StubEmbedder(), items, now=_now())
        assert second == []


async def test_no_items_is_silent(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("rss-empty")
    async with tenant_context(app_engine, tenant_id) as session:
        eligible = await evaluate_rss_deltas(session, StubEmbedder(), [], now=_now())
        assert eligible == []
