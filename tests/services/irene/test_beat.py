# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the tenant-scoped Irene beat handler (ADR-0086).

Live-DB tests (against the compose Postgres, via the shared
``app_engine`` / ``seed_tenant`` fixtures) because the beat's whole job on
the finding path is to persist ``irene_finding`` rows under a tenant
context. The AI core is stubbed — no live network — mirroring the
``configured_core`` / mock-tools style used elsewhere in the suite.

Coverage:

* Silence path — ``run_synthesis`` returns zero tool calls ⇒ no finding is
  written, ``BeatResult.silence`` is True, and one briefing row is.
* Grounding guard — a stubbed ``surface_finding`` for a subject the delta
  layer did not make eligible is dropped with a warning, not persisted at a
  fabricated urgency. The floor-applied happy paths (seeded breach, options
  gate, RSS cap) live in ``test_beat_floor.py``.
* RSS path — a surfaced RSS bucket key persists its membership for a later
  freeze, at the deterministically RSS-capped ``informational`` band.
* Error isolation — a raising synthesis is caught, ``BeatResult.error`` is
  set, and no exception escapes.
* Briefing step (ADR-0133 §1.3–§1.4) — a raising, empty or malformed
  writer leaves a ``skipped`` row and the check intact; nothing to write
  makes no second call; a failing row write rolls back only its savepoint.
"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import tenant_context
from core.repositories.irene_briefing_repository import IreneBriefingRepository
from core.repositories.irene_finding_repository import IreneFindingRepository
from core.models.watchpoint import WATCHPOINT_FAMILIES
from core.repositories.watch_family_scope_repository import WatchFamilyScopeRepository
from services.ai_service_core import ResolvedLLM, SynthesisResult
from services.irene.beat import BeatResult, run_beat
from services.irene.briefing_tool import (
    WRITE_BRIEFING_TOOL,
    WRITE_BRIEFING_TOOL_CHOICE,
    WRITE_BRIEFING_TOOL_NAME,
)
from services.irene.delta_config import DEFAULT_DELTA_THRESHOLDS
from services.irene.rss_clustering import build_rss_buckets
from services.irene.synthesis_tool import SURFACE_FINDING_TOOL
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

#: What the stub records when the caller passes no ``tool_choice`` — so a
#: test can pin that the finding call leaves ``run_synthesis``'s default.
_NO_TOOL_CHOICE = object()

#: The stub's default briefing answer: one valid ``write_briefing`` call.
_DEFAULT_BRIEFING_CALL: dict[str, Any] = {
    "name": "write_briefing",
    "arguments": {
        "text_markets": "Markets were quiet.",
        "text_watch": "All watch points are calm.",
    },
    "id": "call_brief",
}


class _StubCore:
    """Duck-typed stand-in for AIServiceCore in the beat handler.

    Records every ``run_synthesis`` call so tests can assert on the tool and
    model passed through, and returns a canned :class:`SynthesisResult` (or
    raises, to exercise error isolation). The briefing call — the one
    offering ``write_briefing`` — is answered from ``briefing_calls`` (one
    valid call by default) or raises ``briefing_raise``; the finding call is
    answered from ``tool_calls`` or raises ``raise_exc``.
    """

    def __init__(
        self,
        *,
        tool_calls: list[dict[str, Any]] | None = None,
        raise_exc: Exception | None = None,
        briefing_calls: list[dict[str, Any]] | None = None,
        briefing_raise: Exception | None = None,
    ) -> None:
        self._tool_calls = tool_calls or []
        self._raise = raise_exc
        self._briefing_calls = (
            [_DEFAULT_BRIEFING_CALL] if briefing_calls is None else briefing_calls
        )
        self._briefing_raise = briefing_raise
        self.calls: list[dict[str, Any]] = []
        self.prompt_names: list[str] = []
        self.briefing_prompt_calls = 0

    def get_system_prompt(self, prompt_name: str = "irene") -> str:
        self.prompt_names.append(prompt_name)
        return "You are Irene."

    def get_briefing_prompt(self) -> str:
        self.briefing_prompt_calls += 1
        return "You are Irene, writing the briefing."

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
        tool_choice: Any = _NO_TOOL_CHOICE,
    ) -> SynthesisResult:
        self.calls.append(
            {
                "tool": tool,
                "llm": llm,
                "context": context_messages,
                "system_prompt": system_prompt,
                "model": model,
                "temperature": temperature,
                "timeout": timeout,
                "tool_choice": tool_choice,
            }
        )
        if tool["function"]["name"] == WRITE_BRIEFING_TOOL_NAME:
            if self._briefing_raise is not None:
                raise self._briefing_raise
            return SynthesisResult(tool_calls=self._briefing_calls, raw_text="")
        if self._raise is not None:
            raise self._raise
        return SynthesisResult(tool_calls=self._tool_calls, raw_text="")


async def test_beat_silence_writes_nothing(app_engine: AsyncEngine, seed_tenant) -> None:
    """Zero tool calls ⇒ silence: no findings, one written briefing."""
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
        assert result.briefing_status == "written"

        count = (await session.execute(text("SELECT count(*) FROM irene_finding"))).scalar_one()
        assert count == 0

        # The silence path still reaches the briefing step (ADR-0133 §1.3).
        briefing_count = (
            await session.execute(text("SELECT count(*) FROM irene_briefing"))
        ).scalar_one()
        assert briefing_count == 1
        row = await IreneBriefingRepository(session).newest()
        assert row is not None
        assert row.status == "written"
        assert row.beat_at == now
        assert row.finding_ids == ()
        assert row.model_id == "test-model"
        assert row.text_markets == "Markets were quiet."
        assert row.text_watch == "All watch points are calm."
        assert row.inputs["version"] == 1

    # The beat offered the surface_finding tool with the right name.
    assert core.calls[0]["tool"]["function"]["name"] == "surface_finding"
    # The model reaches synthesis on the resolution, not as a second argument.
    assert core.calls[0]["llm"] is _TEST_LLM
    assert core.calls[0]["llm"].model == "test-model"
    # The finding call is unchanged by the persona split (ADR-0133 §1.6):
    # Irene's prompt, exactly one tool, and run_synthesis's defaults —
    # `tool_choice` defaults to "auto" (pinned on the wire by
    # tests/assistants/test_run_synthesis.py); the stub records a sentinel
    # when no `tool_choice` is passed, so the finding call passes none.
    assert core.prompt_names == ["irene"]
    assert core.calls[0]["tool_choice"] is _NO_TOOL_CHOICE
    assert core.calls[0]["system_prompt"] == "You are Irene."
    assert core.calls[0]["tool"] is SURFACE_FINDING_TOOL
    assert (core.calls[0]["model"], core.calls[0]["temperature"], core.calls[0]["timeout"]) == (
        None,
        0.0,
        120.0,
    )

    # The second call is the briefing: the same resolution, the briefing
    # prompt, the one forced write_briefing tool.
    briefing_call = core.calls[1]
    assert briefing_call["tool"] is WRITE_BRIEFING_TOOL
    assert briefing_call["tool_choice"] == WRITE_BRIEFING_TOOL_CHOICE
    assert briefing_call["llm"] is _TEST_LLM
    assert briefing_call["system_prompt"] == "You are Irene, writing the briefing."
    # One user message; its JSON is exactly what was persisted (D-B-120).
    assert len(briefing_call["context"]) == 1
    message = briefing_call["context"][0]
    assert message["role"] == "user"
    assert json.loads(message["content"].split("\n\n", 1)[1]) == row.inputs


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

        # The briefing references the finding and hands it to the writer
        # with its final band; the cluster reaches Part 1.
        row = await IreneBriefingRepository(session).newest()
        assert row is not None
        assert row.finding_ids == (finding.id,)
        this_check = row.inputs["watch"]["findings_this_check"]
        assert this_check[0]["id"] == str(finding.id)
        assert this_check[0]["band"] == finding.band
        assert key in [c["id"] for c in row.inputs["markets"]["press_clusters"]]


async def test_press_coverage_off_skips_the_rss_delta(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """Press coverage switched off: no RSS watch state, no cluster shown to Irene.

    ADR-0133 §2.4 — the RSS delta is skipped, so the same two ``macro``
    items that form a cluster above leave no trace in ``irene_watch_state``
    and never reach the synthesis context.
    """
    tenant_id = await seed_tenant("beat-rss-off")
    user_id = await seed_user(superuser_engine, tenant_id)
    now = datetime.now(timezone.utc)
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        await WatchFamilyScopeRepository(session).insert_revision(
            family="rss",
            effective_from=now - timedelta(hours=1),
            enabled=False,
            changed_by=user_id,
        )

    published = datetime(2026, 6, 30, 9, tzinfo=timezone.utc)
    items = [
        make_item("https://a/1", "RATE: one", published, tags=("macro",), source="ECB"),
        make_item("https://a/2", "RATE: two", published, tags=("macro",), source="FT"),
    ]
    core = _StubCore()
    embedder = StubEmbedder()

    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(
            session,
            core,
            llm=_TEST_LLM,
            now=now,
            rss_items=items,
            embedder=embedder,
        )
        assert result.error is None

        rss_rows = (
            await session.execute(
                text("SELECT count(*) FROM irene_watch_state WHERE subject_key LIKE 'rss:%'")
            )
        ).scalar_one()
        assert rss_rows == 0

    assert "rss:cluster:" not in str(core.calls[0]["context"])
    # The buckets are still formed while Press coverage is off: only the
    # edge gate is switch-bound (ADR-0133 §1.2).
    assert embedder.calls

    # A macro cluster is briefed whatever the Press switch says (D-WD-2):
    # the briefing call is shown it and the persisted material holds it.
    buckets = await build_rss_buckets(
        None, StubEmbedder(), items, now=now, thresholds=DEFAULT_DELTA_THRESHOLDS
    )
    assert len(buckets) == 1
    key = buckets[0].subject_key
    assert key in str(core.calls[1]["context"])
    async with tenant_context(app_engine, tenant_id) as session:
        row = await IreneBriefingRepository(session).newest()
    assert row is not None
    assert key in [c["id"] for c in row.inputs["markets"]["press_clusters"]]


def test_run_beat_has_one_success_exit() -> None:
    """The silence path no longer returns early (ADR-0133 §1.3).

    One success exit and two failure exits remain, and a step that must run
    on every completed check sits before the success exit.
    """
    source = inspect.getsource(run_beat)
    assert source.count("return BeatResult(") == 3
    assert "silence=not surfacing" in source


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
        # A failed beat writes no briefing row.
        assert result.briefing_status is None

        count = (await session.execute(text("SELECT count(*) FROM irene_finding"))).scalar_one()
        assert count == 0
        briefing_count = (
            await session.execute(text("SELECT count(*) FROM irene_briefing"))
        ).scalar_one()
        assert briefing_count == 0


async def test_a_raising_writer_skips_the_briefing_and_keeps_the_check(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A raising briefing call is ``skipped / llm_error``; the check stands."""
    tenant_id = await seed_tenant("beat-brief-raise")
    core = _StubCore(briefing_raise=RuntimeError("writer down"))
    now = datetime.now(timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(session, core, llm=_TEST_LLM, now=now)

        assert result.error is None
        assert result.briefing_status == "skipped"
        row = await IreneBriefingRepository(session).newest()
        assert row is not None
        assert row.status == "skipped"
        assert row.skip_reason == "llm_error"
        assert (row.text_markets, row.text_watch) == (None, None)
        # A call was attempted, so the model is recorded (D-B-118).
        assert row.model_id == "test-model"


async def test_a_writer_without_one_valid_call_is_skipped_as_invalid_output(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """No call, a call missing a text, or two calls: each is ``invalid_output``."""
    tenant_id = await seed_tenant("beat-brief-invalid")
    now = datetime.now(timezone.utc)
    missing_watch = {
        "name": "write_briefing",
        "arguments": {"text_markets": "Markets were quiet."},
        "id": "call_brief",
    }
    answers: list[list[dict[str, Any]]] = [
        [],
        [missing_watch],
        [_DEFAULT_BRIEFING_CALL, _DEFAULT_BRIEFING_CALL],
    ]

    for offset, briefing_calls in enumerate(answers):
        core = _StubCore(briefing_calls=briefing_calls)
        async with tenant_context(app_engine, tenant_id) as session:
            result = await run_beat(
                session, core, llm=_TEST_LLM, now=now + timedelta(seconds=offset)
            )
        assert result.error is None
        assert result.briefing_status == "skipped"

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await IreneBriefingRepository(session).list_between(
            start=now, end=now + timedelta(seconds=3)
        )
    assert len(rows) == 3
    for row in rows:
        assert row.status == "skipped"
        assert row.skip_reason == "invalid_output"
        assert (row.text_markets, row.text_watch) == (None, None)


async def test_nothing_to_write_makes_no_second_call(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """Every family off, no market, no press: no briefing call, one skipped row."""
    tenant_id = await seed_tenant("beat-brief-nothing")
    user_id = await seed_user(superuser_engine, tenant_id)
    now = datetime.now(timezone.utc)
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        scope = WatchFamilyScopeRepository(session)
        for family in WATCHPOINT_FAMILIES:
            await scope.insert_revision(
                family=family,
                effective_from=now - timedelta(hours=1),
                enabled=False,
                changed_by=user_id,
            )

    core = _StubCore()
    async with tenant_context(app_engine, tenant_id) as session:
        result = await run_beat(session, core, llm=_TEST_LLM, now=now)

        assert result.error is None
        assert result.briefing_status == "skipped"
        row = await IreneBriefingRepository(session).newest()
        assert row is not None
        assert row.status == "skipped"
        assert row.skip_reason == "nothing_to_write"
        assert row.model_id is None

    # Only the finding call reached the core; the briefing prompt was not read.
    assert len(core.calls) == 1
    assert core.briefing_prompt_calls == 0


async def test_a_failing_briefing_write_rolls_back_only_its_savepoint(
    app_engine: AsyncEngine, seed_tenant, caplog
) -> None:
    """A failing row write rolls back the savepoint; the finding survives.

    The duplicate ``(tenant, beat_at)`` cannot arise in production (D-B-125);
    here it is the real constraint violation inside a real savepoint.
    """
    tenant_id = await seed_tenant("beat-brief-savepoint")
    now = datetime.now(timezone.utc)
    async with tenant_context(app_engine, tenant_id) as session:
        await IreneBriefingRepository(session).append(
            beat_at=now,
            written_at=now,
            status="skipped",
            skip_reason="nothing_to_write",
            inputs={},
            finding_ids=[],
        )

    published = datetime(2026, 6, 30, 9, tzinfo=timezone.utc)
    items = [
        make_item("https://a/1", "RATE: one", published, tags=("macro",), source="ECB"),
        make_item("https://a/2", "RATE: two", published, tags=("macro",), source="FT"),
    ]
    buckets = await build_rss_buckets(
        None, StubEmbedder(), items, now=now, thresholds=DEFAULT_DELTA_THRESHOLDS
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
        with caplog.at_level(logging.ERROR, logger="services.irene.briefing_step"):
            result = await run_beat(
                session,
                core,
                llm=_TEST_LLM,
                now=now,
                rss_items=items,
                embedder=StubEmbedder(),
            )
        assert result.error is None
        assert result.findings_written == 1
        assert result.briefing_status is None

    async with tenant_context(app_engine, tenant_id) as session:
        openf = await IreneFindingRepository(session).list_open()
        assert len(openf) == 1
        briefing_count = (
            await session.execute(text("SELECT count(*) FROM irene_briefing"))
        ).scalar_one()
        assert briefing_count == 1
        row = await IreneBriefingRepository(session).newest()
        assert row is not None
        assert row.skip_reason == "nothing_to_write"

    assert any(
        "briefing not recorded; the check stands." in rec.getMessage() for rec in caplog.records
    )
