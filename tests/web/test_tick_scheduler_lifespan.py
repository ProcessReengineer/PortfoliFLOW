# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Lifespan wiring and settings for the built-in tick scheduler (ADR-0117).

The loop's own behaviour lives in ``test_tick_scheduler.py``; what is
pinned here is the deployment contract around it — that
``TICK_SCHEDULER_ENABLED`` decides whether the app hosts a tick source at
all, that a bad interval is refused at settings load rather than silently
corrected, and that shutdown quiesces the task **before** the engine it
ticks on is disposed.

Offline: the superuser URL is a syntactically valid one that is never
connected to (``create_async_engine`` builds a lazy pool), and both runner
entry points are replaced, so no tick can reach Postgres even if the
interval elapsed mid-test.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from pydantic import ValidationError

import web.tick_scheduler as tick_scheduler
from web.main import create_app
from web.settings import WebSettings

_UNCONNECTED_SUPERUSER_URL = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/absent"


def _settings(**overrides: Any) -> WebSettings:
    """Web settings for a lifespan that reaches the scheduler branch."""
    values: dict[str, Any] = {
        "web_host": "127.0.0.1",
        "web_port": 8000,
        "database_url": None,
        "database_url_superuser": _UNCONNECTED_SUPERUSER_URL,
        "tick_scheduler_enabled": True,
        "tick_scheduler_interval_seconds": 5,
    }
    values.update(overrides)
    return WebSettings(**values)


@pytest.fixture(autouse=True)
def _no_real_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither domain may reach Postgres from a lifespan test."""

    async def _never(engine: Any, **kwargs: Any) -> None:
        raise AssertionError("a lifespan test must not run a real tick")

    monkeypatch.setattr(tick_scheduler, "run_market_data_tick", _never)
    monkeypatch.setattr(tick_scheduler, "run_irene_tick", _never)


class _RecordingEngine:
    """Stand-in for an engine, recording when the lifespan disposes it.

    ``probe`` is sampled at dispose time, which is how the shutdown-order
    test observes whether the scheduler had already stopped.
    """

    def __init__(
        self,
        events: list[tuple[str, bool | None]],
        label: str,
        probe: Any = None,
    ) -> None:
        self._events = events
        self._label = label
        self._probe = probe

    async def dispose(self) -> None:
        self._events.append((f"dispose:{self._label}", self._probe() if self._probe else None))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


async def test_lifespan_starts_the_scheduler_by_default() -> None:
    """Enabled (the default): the task is running and parked on app.state."""
    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        handle = app.state.tick_scheduler
        assert handle is not None, "The built-in tick scheduler did not start."
        assert not handle.task.done(), "The task ended immediately."
        assert handle.interval_seconds == 5

    assert handle.task.done(), "The task outlived the lifespan."


async def test_lifespan_starts_nothing_when_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Disabled: no task, and the log says an external tick source is expected.

    The log line matters as much as the absent task — a deployment that
    opts out and forgets its systemd timers has no heartbeat at all, so
    startup states the expectation.
    """
    with caplog.at_level(logging.INFO, logger="portfoliflow.web"):
        app = create_app(_settings(tick_scheduler_enabled=False))
        async with app.router.lifespan_context(app):
            assert app.state.tick_scheduler is None

    messages = [r.getMessage() for r in caplog.records]
    disabled = [m for m in messages if "TICK_SCHEDULER_ENABLED=false" in m]
    assert disabled, f"No disabled-mode startup line: {messages}"
    assert "external tick source is expected" in disabled[0]


async def test_lifespan_declines_to_start_without_a_superuser_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No RLS-bypassing engine, no scheduler — and a warning saying which."""
    with caplog.at_level(logging.WARNING, logger="portfoliflow.web"):
        app = create_app(_settings(database_url_superuser=None))
        async with app.router.lifespan_context(app):
            assert app.state.audit_engine is None
            assert app.state.tick_scheduler is None

    assert any("DATABASE_URL_SUPERUSER" in r.getMessage() for r in caplog.records)


async def test_the_scheduler_runs_on_the_audit_engine_itself() -> None:
    """No third superuser engine: the task gets ``app.state.audit_engine``.

    ADR-0117 §3 rejected a dedicated engine (same URL, same privileges,
    extra pool) in favour of sanctioning path 5 on the existing one, so the
    identity is the decision, not an implementation detail.
    """
    captured: list[Any] = []

    def _capture(engine: Any, settings: Any) -> None:
        captured.append(engine)

    import web.main as web_main

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(web_main, "start_tick_scheduler", _capture)
        app = create_app(_settings())
        async with app.router.lifespan_context(app):
            assert captured and captured[0] is app.state.audit_engine


# ---------------------------------------------------------------------------
# Shutdown ordering
# ---------------------------------------------------------------------------


async def test_shutdown_stops_the_task_before_disposing_the_engines() -> None:
    """The task is quiesced first — it may be holding a transaction.

    Disposing the audit engine under a running tick would tear the
    connection out from beneath an open advisory-lock transaction. The
    order is asserted by sampling, at dispose time, whether the task had
    already finished.
    """
    events: list[tuple[str, bool | None]] = []
    app = create_app(_settings())

    async with app.router.lifespan_context(app):
        handle = app.state.tick_scheduler
        assert handle is not None
        task = handle.task
        # Swap in recorders *after* startup: the task already holds the
        # real engine, which is never connected to in this test.
        app.state.audit_engine = _RecordingEngine(events, "audit", task.done)
        app.state.engine = _RecordingEngine(events, "app", task.done)

    assert events == [("dispose:app", True), ("dispose:audit", True)], (
        f"An engine was disposed while the tick scheduler was still running: {events}"
    )
    assert not task.cancelled(), "A sleeping task should stop cleanly, not by cancellation."


async def test_shutdown_survives_a_scheduler_that_raises_on_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken teardown must not mask shutdown — the engines still dispose."""
    events: list[tuple[str, bool | None]] = []

    async def _explode(handle: Any, **kwargs: Any) -> None:
        raise RuntimeError("stop blew up (test)")

    import web.main as web_main

    monkeypatch.setattr(web_main, "stop_tick_scheduler", _explode)

    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        handle = app.state.tick_scheduler
        assert handle is not None
        app.state.audit_engine = _RecordingEngine(events, "audit")

    assert events == [("dispose:audit", None)]
    # The real task is still parked on the loop; stop it so the test leaves
    # nothing running.
    handle.stop_event.set()
    await asyncio.wait_for(handle.task, timeout=2.0)


# ---------------------------------------------------------------------------
# Settings (ADR-0117 §4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("interval", [5, 60, 3600])
def test_in_bounds_intervals_load(interval: int) -> None:
    """The documented range loads unchanged."""
    assert _settings(tick_scheduler_interval_seconds=interval).tick_scheduler_interval_seconds == (
        interval
    )


@pytest.mark.parametrize("interval", [0, 4, -60, 3601, 86400])
def test_out_of_bounds_intervals_are_refused_at_settings_load(interval: int) -> None:
    """Rejected, never clamped — the process fails to start with the reason.

    A silently corrected interval would leave the deployment ticking at a
    rhythm nobody configured and nobody can see.
    """
    with pytest.raises(ValidationError) as excinfo:
        _settings(tick_scheduler_interval_seconds=interval)

    assert "TICK_SCHEDULER_INTERVAL_SECONDS" in str(excinfo.value)


def test_web_settings_satisfies_the_scheduler_settings_protocol() -> None:
    """``WebSettings`` structurally satisfies what the task reads.

    The protocol is what keeps ``services/`` free of a ``web`` import
    (CLAUDE.md § Dependency rules), so the satisfying end of it is worth
    pinning: a renamed field would otherwise only fail at runtime, in a
    background task, on someone's deployment.
    """
    settings = _settings()
    for attribute in (
        "openrouter_api_key",
        "openrouter_base_url",
        "tick_scheduler_interval_seconds",
    ):
        assert hasattr(settings, attribute), attribute


def test_the_scheduler_is_enabled_by_default() -> None:
    """ADR-0117 §1: the built-in scheduler is the *default* tick source.

    Constructed with the field left unset so the class default answers
    rather than the environment; the package's autouse fixture turns it
    off for every other test, which would otherwise hide a flipped default.
    """
    assert WebSettings.model_fields["tick_scheduler_enabled"].default is True
    assert WebSettings.model_fields["tick_scheduler_interval_seconds"].default == 60
