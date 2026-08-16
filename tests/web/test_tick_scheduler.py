# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Loop and shutdown semantics of the built-in tick scheduler (ADR-0117 §1).

Fully offline by construction: both runner entry points are monkeypatched
at the ``web.tick_scheduler`` module level and the engine is an opaque
sentinel, because everything below the two calls is the shared runner's
own contract and is pinned in ``tests/services/scheduler/``. What is
asserted here is only what this module decides — *when* a tick runs, what
survives a failing one, how the credential gate is reported, and how the
task stops.

The intervals are sub-millisecond so a "several ticks later" assertion
costs no wall-clock. That is a test-double liberty:
``TICK_SCHEDULER_INTERVAL_SECONDS`` is an int with a five-second floor in
``WebSettings`` (ADR-0117 §4), and the loop only ever hands the value to
``asyncio.wait_for``, which is happy with a float.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

import web.tick_scheduler as tick_scheduler
from services.scheduler.tick_runner import IreneTickSummary, MarketDataTickSummary
from web.tick_scheduler import (
    TickSchedulerHandle,
    TickSchedulerStatus,
    run_tick_scheduler,
    start_tick_scheduler,
    stop_tick_scheduler,
)

_ENGINE = object()
"""Stands in for the audit engine — the scheduler only passes it on."""


class _FakeSettings:
    """Structural stand-in for :class:`web.tick_scheduler.TickSchedulerSettings`."""

    def __init__(self, *, api_key: str | None = "sk-test", interval: float = 0.005) -> None:
        self.openrouter_api_key = api_key
        self.openrouter_base_url = "https://openrouter.invalid/api/v1"
        self.tick_scheduler_interval_seconds = interval


class _Recorder:
    """Records each domain call in order; optionally fails or blocks."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.market_data_error: Exception | None = None
        self.irene_error: Exception | None = None
        self.block_forever = False

    async def market_data(self, engine: Any, **kwargs: Any) -> MarketDataTickSummary:
        self.calls.append("market_data")
        if self.block_forever:
            await asyncio.Event().wait()
        if self.market_data_error is not None:
            raise self.market_data_error
        return MarketDataTickSummary(due=1, refreshed=1)

    async def irene(self, engine: Any, **kwargs: Any) -> IreneTickSummary:
        self.calls.append("irene")
        if self.irene_error is not None:
            raise self.irene_error
        return IreneTickSummary(due=1, beaten=1, findings_written=2)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Replace both runner entry points with a recording double."""
    rec = _Recorder()
    monkeypatch.setattr(tick_scheduler, "run_market_data_tick", rec.market_data)
    monkeypatch.setattr(tick_scheduler, "run_irene_tick", rec.irene)
    return rec


async def _run_until(
    predicate: Any,
    *,
    timeout: float = 2.0,
) -> None:
    """Yield to the loop until ``predicate()`` holds, or fail the test."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached within the timeout")
        await asyncio.sleep(0.001)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def test_the_first_tick_waits_one_full_interval(recorder: _Recorder) -> None:
    """Nothing ticks during startup — the sleep comes first.

    The lifespan is still bringing engines and the bot up when the task is
    created; a scheduler that ticked immediately would compete with it.
    """
    handle = start_tick_scheduler(_ENGINE, _FakeSettings(interval=30))
    try:
        await asyncio.sleep(0.05)
        assert recorder.calls == [], (
            f"The scheduler ticked before its first interval elapsed: {recorder.calls}"
        )
    finally:
        await stop_tick_scheduler(handle)


async def test_each_interval_runs_market_data_then_irene(recorder: _Recorder) -> None:
    """Both domains run per tick, sequentially, market data first.

    Market data first is deliberate: it needs no LLM, so it is the domain
    that keeps working on a deployment with no credential at all.
    """
    handle = start_tick_scheduler(_ENGINE, _FakeSettings())
    try:
        await _run_until(lambda: recorder.calls.count("irene") >= 2)
    finally:
        await stop_tick_scheduler(handle)

    # Whole ticks only — the loop never starts Irene before market data.
    pairs = [recorder.calls[i : i + 2] for i in range(0, len(recorder.calls) - 1, 2)]
    assert all(pair == ["market_data", "irene"] for pair in pairs), recorder.calls


async def test_a_failing_market_data_tick_never_starves_irene(
    recorder: _Recorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One domain's infrastructure failure is logged; the other still runs."""
    recorder.market_data_error = RuntimeError("provider unreachable (test)")

    with caplog.at_level(logging.ERROR, logger="portfoliflow.web.tick_scheduler"):
        handle = start_tick_scheduler(_ENGINE, _FakeSettings())
        try:
            await _run_until(lambda: recorder.calls.count("irene") >= 2)
        finally:
            await stop_tick_scheduler(handle)

    assert recorder.calls.count("market_data") >= 2, "The loop stopped retrying."
    failures = [r for r in caplog.records if "market-data tick failed" in r.getMessage()]
    assert failures, "The market-data failure was swallowed silently."
    assert failures[0].exc_info is not None, "The failure was logged without a traceback."


async def test_a_failing_irene_tick_never_ends_the_loop(
    recorder: _Recorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An Irene tick that raises is logged with its traceback and retried."""
    recorder.irene_error = RuntimeError("synthesis blew up (test)")

    with caplog.at_level(logging.ERROR, logger="portfoliflow.web.tick_scheduler"):
        handle = start_tick_scheduler(_ENGINE, _FakeSettings())
        try:
            await _run_until(lambda: recorder.calls.count("irene") >= 2)
        finally:
            await stop_tick_scheduler(handle)

    failures = [r for r in caplog.records if "Irene tick failed" in r.getMessage()]
    assert failures, "The Irene failure was swallowed silently."
    assert failures[0].exc_info is not None, "The failure was logged without a traceback."


# ---------------------------------------------------------------------------
# The credential gate (anti-spam)
# ---------------------------------------------------------------------------


async def test_the_closed_gate_is_reported_once_not_per_tick(
    recorder: _Recorder,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credential anywhere: Irene is skipped quietly, market data keeps ticking.

    A self-hosted deployment with no LLM key is an ordinary ADR-0117 state,
    and the loop asks the gate once a minute forever — so both this
    module's warning and the shared runner's own (which the gate emits
    under ``portfoliflow.scheduler``) must appear once, not once per tick.
    """
    monkeypatch.delenv("CREDENTIAL_VAULT_MASTER_KEY", raising=False)

    with caplog.at_level(logging.DEBUG):
        handle = start_tick_scheduler(_ENGINE, _FakeSettings(api_key=None))
        try:
            await _run_until(lambda: recorder.calls.count("market_data") >= 4)
        finally:
            await stop_tick_scheduler(handle)

    assert "irene" not in recorder.calls, (
        "The Irene domain ran although no scope can resolve a credential."
    )
    ours = [r for r in caplog.records if "skipping the Irene domain" in r.getMessage()]
    assert len(ours) == 1, f"Expected exactly one gate warning, got {len(ours)}."
    assert ours[0].levelno == logging.WARNING

    # The runner's own gate warning: emitted once, on the un-muted
    # evaluation at task start, and muted on every steady-state tick after.
    runner_lines = [
        r
        for r in caplog.records
        if r.name == "portfoliflow.scheduler" and "no credential vault" in r.getMessage()
    ]
    assert len(runner_lines) == 1, (
        "The shared runner's gate warning was repeated per tick; the mute in "
        f"_credentials_reachable is not holding ({len(runner_lines)} lines)."
    )


async def test_a_credential_configured_later_reopens_the_gate(
    recorder: _Recorder,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate is re-evaluated per tick — no restart to pick a key up."""
    monkeypatch.delenv("CREDENTIAL_VAULT_MASTER_KEY", raising=False)
    settings = _FakeSettings(api_key=None)

    with caplog.at_level(logging.INFO, logger="portfoliflow.web.tick_scheduler"):
        handle = start_tick_scheduler(_ENGINE, settings)
        try:
            await _run_until(lambda: recorder.calls.count("market_data") >= 2)
            settings.openrouter_api_key = "sk-configured-at-runtime"
            await _run_until(lambda: "irene" in recorder.calls)
        finally:
            await stop_tick_scheduler(handle)

    resumed = [r for r in caplog.records if "resolvable again" in r.getMessage()]
    assert len(resumed) == 1, f"Expected one resume line, got {len(resumed)}."


# ---------------------------------------------------------------------------
# The status the health surfaces read (ADR-0117 §5)
# ---------------------------------------------------------------------------


async def test_the_handle_carries_the_status_the_loop_writes(recorder: _Recorder) -> None:
    """One object, shared: the reader sees the loop's own record.

    A snapshot copied onto the handle would go stale the moment the next
    tick ran, which is the one thing a health surface must not do.
    """
    handle = start_tick_scheduler(_ENGINE, _FakeSettings())
    try:
        assert handle.status.last_tick_at is None, "A tick was stamped before one ran."
        assert handle.status.started_at is not None
        await _run_until(lambda: handle.status.last_tick_at is not None)
    finally:
        await stop_tick_scheduler(handle)

    assert handle.status.last_tick_at >= handle.status.started_at


async def test_a_completed_tick_records_both_domain_summaries(recorder: _Recorder) -> None:
    """The summaries the runner returns are kept, not discarded."""
    status = TickSchedulerStatus()
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_tick_scheduler(_ENGINE, _FakeSettings(), stop_event=stop, status=status),
    )
    try:
        await _run_until(lambda: status.last_tick_at is not None)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert status.last_market_data_summary == MarketDataTickSummary(due=1, refreshed=1)
    assert status.last_irene_summary == IreneTickSummary(due=1, beaten=1, findings_written=2)
    assert status.last_error_at is None
    assert status.consecutive_failures == 0


async def test_a_failing_domain_still_completes_the_tick(recorder: _Recorder) -> None:
    """A tick counts as completed even when a domain raised.

    "The loop is alive and getting through its work" is the question the
    health surface asks; a failure is recorded alongside that answer, not
    instead of it — otherwise a deployment whose provider is down looks
    identical to one whose scheduler died.
    """
    recorder.market_data_error = RuntimeError("provider unreachable (test)")
    status = TickSchedulerStatus()
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_tick_scheduler(_ENGINE, _FakeSettings(), stop_event=stop, status=status),
    )
    try:
        await _run_until(lambda: status.consecutive_failures >= 2)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert status.last_tick_at is not None, "A failing domain suppressed the tick stamp."
    assert status.last_error_at is not None
    assert status.last_market_data_summary is None, "The failed domain reported a summary."
    assert status.last_irene_summary is not None, "The other domain's summary was lost."


async def test_a_healthy_tick_clears_the_failure_streak(recorder: _Recorder) -> None:
    """The counter measures the present, not the deployment's history."""
    recorder.irene_error = RuntimeError("synthesis blew up (test)")
    status = TickSchedulerStatus()
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_tick_scheduler(_ENGINE, _FakeSettings(), stop_event=stop, status=status),
    )
    try:
        await _run_until(lambda: status.consecutive_failures >= 2)
        recorder.irene_error = None
        await _run_until(lambda: status.consecutive_failures == 0)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert status.last_error_at is not None, "The last failure's instant was forgotten."


async def test_a_gated_off_irene_domain_leaves_no_stale_summary(
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipped is not succeeded — the fields describe the last tick only.

    Every field but the failure record answers "what did the *last* tick
    do", so a domain that did not run this tick must read ``None`` rather
    than leave an older tick's summary standing.
    """
    monkeypatch.delenv("CREDENTIAL_VAULT_MASTER_KEY", raising=False)
    settings = _FakeSettings(api_key="sk-test")
    status = TickSchedulerStatus()
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_tick_scheduler(_ENGINE, settings, stop_event=stop, status=status),
    )
    try:
        await _run_until(lambda: status.last_irene_summary is not None)
        settings.openrouter_api_key = None
        await _run_until(lambda: status.last_irene_summary is None)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert status.last_market_data_summary is not None, "Market data stopped with the gate."
    assert status.consecutive_failures == 0, "A closed gate was counted as a failure."


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_the_stop_event_ends_the_loop_cleanly(
    recorder: _Recorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A task between ticks stops at once and says so."""
    with caplog.at_level(logging.INFO, logger="portfoliflow.web.tick_scheduler"):
        handle = start_tick_scheduler(_ENGINE, _FakeSettings(interval=30))
        await stop_tick_scheduler(handle)

    assert handle.task.done() and not handle.task.cancelled()
    messages = [r.getMessage() for r in caplog.records]
    assert any("started" in m for m in messages), messages
    assert any("stopped" in m for m in messages), messages


async def test_a_tick_that_overruns_the_grace_period_is_cancelled(
    recorder: _Recorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shutdown is bounded; the rollback is what protects the in-flight beat.

    ADR-0117 §1: cancelling mid-beat rolls the advisory-lock transaction
    back, so ``next_due_at`` stays unadvanced and the beat is retried after
    restart. Waiting indefinitely instead would hold the whole process
    hostage to one slow synthesis call.
    """
    recorder.block_forever = True

    with caplog.at_level(logging.WARNING, logger="portfoliflow.web.tick_scheduler"):
        handle = start_tick_scheduler(_ENGINE, _FakeSettings())
        await _run_until(lambda: recorder.calls == ["market_data"])
        await stop_tick_scheduler(handle, grace_seconds=0.05)

    assert handle.task.cancelled(), "The blocked tick was not cancelled."
    assert any("cancelling" in r.getMessage() for r in caplog.records)


async def test_stop_reports_a_task_that_died_on_its_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A loop that ended with an unhandled error is reported, not swallowed.

    Nothing inside the loop should raise past the per-domain handlers, so
    this is the "cannot happen" path — which is exactly why shutdown has to
    say something when it does.
    """

    async def _explode() -> None:
        raise RuntimeError("loop died (test)")

    task = asyncio.create_task(_explode())
    await asyncio.sleep(0)
    handle = TickSchedulerHandle(
        task=task,
        stop_event=asyncio.Event(),
        interval_seconds=60,
        status=TickSchedulerStatus(),
    )

    with caplog.at_level(logging.ERROR, logger="portfoliflow.web.tick_scheduler"):
        await stop_tick_scheduler(handle)

    assert any("unhandled error" in r.getMessage() for r in caplog.records)


async def test_run_tick_scheduler_stops_on_a_caller_supplied_event(
    recorder: _Recorder,
) -> None:
    """The coroutine is drivable without :func:`start_tick_scheduler`."""
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_tick_scheduler(_ENGINE, _FakeSettings(), stop_event=stop),
    )
    await _run_until(lambda: recorder.calls.count("irene") >= 1)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done() and task.exception() is None
