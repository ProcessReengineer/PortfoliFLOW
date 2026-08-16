# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the tenant-scoped Irene beat handler (ADR-0086).

Live-DB tests (against the compose Postgres, via the shared
``app_engine`` / ``seed_tenant`` fixtures) because the beat's whole job on
the finding path is to persist ``irene_finding`` rows under a tenant
context. The AI core is stubbed — no live network — mirroring the
``configured_core`` / mock-tools style used elsewhere in the suite.

Coverage:

* Silence path — ``run_synthesis`` returns zero tool calls ⇒ nothing is
  written, ``BeatResult.silence`` is True.
* Grounding guard — a stubbed ``surface_finding`` for a subject the delta
  layer did not make eligible is dropped with a warning, not persisted at a
  fabricated urgency. The floor-applied happy paths (seeded breach, options
  gate, RSS cap) live in ``test_beat_floor.py``.
* RSS path — a surfaced RSS bucket key persists its membership for a later
  freeze, at the deterministically RSS-capped ``informational`` band.
* Error isolation — a raising synthesis is caught, ``BeatResult.error`` is
  set, and no exception escapes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import tenant_context
from core.repositories.irene_finding_repository import IreneFindingRepository
from services.ai_service_core import ResolvedLLM, SynthesisResult
from services.irene.beat import BeatResult, run_beat
from services.irene.delta_config import DEFAULT_DELTA_THRESHOLDS
from services.irene.rss_clustering import build_rss_buckets
from tests.services.irene._book_fixtures import (
    CALM_NAV,
    seed_book,
    seed_user,
)
from tests.services.irene._rss_fixtures import StubEmbedder, make_item


#: The per-tenant resolution the tick threads into every beat since
#: ADR-0112 §4b. The beat no longer takes a bare model string.
_TEST_LLM = ResolvedLLM(
    base_url="https://openrouter.test/api/v1",
    api_key="sk-beat-test",
    model="test-model",
)


class _StubCore:
    """Duck-typed stand-in for AIServiceCore in the beat handler.

    Records the ``run_synthesis`` call so tests can assert on the tool and
    model passed through, and returns a canned :class:`SynthesisResult` (or
    raises, to exercise error isolation).
    """

    def __init__(
        self,
        *,
        tool_calls: list[dict[str, Any]] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._tool_calls = tool_calls or []
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def get_system_prompt(self, prompt_name: str = "irene") -> str:
        return "You are Irene."

    async def run_synthesis(
        self,
        *,
        system_prompt: str,
        context_messages: list[dict[str, Any]],
        tool: dict[str, Any],
        model: str | None = None,
        temperature: float = 0.0,
        timeout: float = 120.0,
        llm: Any = None,
    ) -> SynthesisResult:
        self.calls.append({"tool": tool, "llm": llm, "context": context_messages})
        if self._raise is not None:
            raise self._raise
        return SynthesisResult(tool_calls=self._tool_calls, raw_text="")


async def test_beat_silence_writes_nothing(app_engine: AsyncEngine, seed_tenant) -> None:
    """Zero tool calls ⇒ silence, no findings written."""
    tenant_id = await seed_tenant("beat-silence")
    core = _StubCore(tool_calls=[])
    now = datetime.now(timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(session, core, llm=_TEST_LLM, now=now)

        assert isinstance(result, BeatResult)
        assert result.silence is True
        assert result.findings_written == 0
        assert result.error is None
        assert result.tenant_id == tenant_id

        count = (await session.execute(text("SELECT count(*) FROM irene_finding"))).scalar_one()
        assert count == 0

    # The beat offered the surface_finding tool with the right name.
    assert core.calls[0]["tool"]["function"]["name"] == "surface_finding"
    # The model reaches synthesis on the resolution, not as a second argument.
    assert core.calls[0]["llm"] is _TEST_LLM
    assert core.calls[0]["llm"].model == "test-model"


async def test_beat_uneligible_subject_dropped_and_warned(
    app_engine: AsyncEngine, seed_tenant, caplog
) -> None:
    """A surfaced subject not in the eligible set is dropped with a warning.

    Irene may only surface what the delta layer made eligible (ADR-0088
    §0.3). With no book seeded the eligible set is empty, so a
    ``surface_finding`` for an arbitrary subject_key is a grounding
    violation: it is dropped (no row, no fabricated urgency) and logged as a
    warning. ``silence`` is False — the model did call the tool — but
    nothing grounded was written.
    """
    tenant_id = await seed_tenant("beat-uneligible")
    core = _StubCore(
        tool_calls=[
            {
                "name": "surface_finding",
                "arguments": {
                    "subject_key": "saa:private_equity",
                    "trigger": "hallucinated",
                    "finding": "PE allocation nearing its cap.",
                    "basis": "invented",
                    "urgency_suggestion": 3,
                },
                "id": "call_1",
            }
        ]
    )
    now = datetime.now(timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        with caplog.at_level(logging.WARNING, logger="services.irene.beat"):
            result = await run_beat(session, core, llm=_TEST_LLM, now=now)

        assert result.silence is False
        assert result.findings_written == 0
        assert result.error is None

        count = (await session.execute(text("SELECT count(*) FROM irene_finding"))).scalar_one()
        assert count == 0

    assert any(
        "not in the eligible set" in rec.message and "saa:private_equity" in rec.message
        for rec in caplog.records
    )


async def test_beat_calm_book_stays_silent_end_to_end(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """A calm book (real AUM/NAV/limits, all OK) still yields silence.

    Exercises the wired internal delta end-to-end: the delta finds no
    material change, so the beat synthesises against the "nothing
    material" context and the stubbed core surfaces nothing. The delta
    still ran — the watch-state rows are upserted — but no findings are
    written.
    """
    tenant_id = await seed_tenant("beat-calm")
    user_id = await seed_user(superuser_engine, tenant_id)
    await seed_book(app_engine, tenant_id, user_id, latest_nav=CALM_NAV)

    core = _StubCore(tool_calls=[])
    now = datetime.now(timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(session, core, llm=_TEST_LLM, now=now)

        assert result.silence is True
        assert result.findings_written == 0
        assert result.error is None

        finding_count = (
            await session.execute(text("SELECT count(*) FROM irene_finding"))
        ).scalar_one()
        assert finding_count == 0

        # The delta ran end-to-end: the constrained subjects were
        # recorded in watch-state even though nothing was material.
        watch_count = (
            await session.execute(text("SELECT count(*) FROM irene_watch_state"))
        ).scalar_one()
        assert watch_count == 2


async def test_beat_rss_finding_persists_membership_for_freeze(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A surfaced RSS finding persists its bucket membership + provisional band.

    Drives the RSS path end-to-end: the beat clusters the feed items, the
    stubbed core surfaces the (deterministic) bucket key, and the beat
    appends a finding whose payload carries ``member_ids`` / ``tag`` /
    ``day_bucket`` (so the bucket can be frozen later) with the RSS
    provisional band.
    """
    tenant_id = await seed_tenant("beat-rss")
    published = datetime(2026, 6, 30, 9, tzinfo=timezone.utc)
    items = [
        make_item("https://a/1", "RATE: one", published, tags=("macro",), source="ECB"),
        make_item("https://a/2", "RATE: two", published, tags=("macro",), source="FT"),
    ]
    now = datetime.now(timezone.utc)

    # The bucket key is deterministic (membership-hashed), so precompute it
    # to configure the stub core's surface_finding call.
    buckets = await build_rss_buckets(
        None,
        StubEmbedder(),
        items,
        now=now,
        thresholds=DEFAULT_DELTA_THRESHOLDS,
    )
    assert len(buckets) == 1
    key = buckets[0].subject_key

    core = _StubCore(
        tool_calls=[
            {
                "name": "surface_finding",
                "arguments": {
                    "subject_key": key,
                    "urgency_suggestion": 2,
                    "finding": "Rate-move press cluster forming.",
                },
                "id": "call_rss",
            }
        ]
    )

    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(
            session,
            core,
            llm=_TEST_LLM,
            now=now,
            rss_items=items,
            embedder=StubEmbedder(),
        )
        assert result.findings_written == 1
        assert result.error is None

        openf = await IreneFindingRepository(session).list_open()
        assert len(openf) == 1
        finding = openf[0]
        assert finding.subject_key == key
        # Standalone RSS is deterministically capped at informational by the
        # floor (source = RSS); suggestion 2 clamps to the informational
        # band's top of 3, so final urgency 2, band informational.
        assert finding.urgency == 2
        assert finding.band == "informational"
        assert finding.payload["tag"] == "macro"
        assert finding.payload["day_bucket"] == "2026-06-30"
        assert len(finding.payload["member_ids"]) == 2
        assert len(finding.payload["members"]) == 2


async def test_beat_error_is_isolated_and_reported(app_engine: AsyncEngine, seed_tenant) -> None:
    """A raising synthesis is caught; BeatResult.error is set, no raise."""
    tenant_id = await seed_tenant("beat-error")
    core = _StubCore(raise_exc=RuntimeError("LLM down"))
    now = datetime.now(timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        # Must not raise.
        result = await run_beat(session, core, llm=_TEST_LLM, now=now)

        assert result.error is not None
        assert "LLM down" in result.error
        assert result.silence is False
        assert result.findings_written == 0

        count = (await session.execute(text("SELECT count(*) FROM irene_finding"))).scalar_one()
        assert count == 0
