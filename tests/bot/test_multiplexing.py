# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""One thread, one loop, N dispatchers (ADR-0112 §5, strand F5).

:func:`bot.telegram_bot._run_dispatchers` is the multiplexing seam: it
turns the discovered token set into one supervised polling task per
tenant, all on the bot's single event loop. These tests drive it with
**fake dispatchers** — a plain coroutine factory per runner — so the
concurrency contract is asserted without aiogram, a token or a network:

* two tenants really do poll concurrently;
* a permanently rejected token ends **that** task and no other;
* an unexpected error is contained and logged against its dispatcher;
* :func:`bot.telegram_bot.stop_bot` tears every dispatcher down and the
  one worker thread unwinds.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any
from uuid import uuid4

import pytest

import bot.telegram_bot as tb


class _Net(Exception):
    """Stand-in for ``aiogram.exceptions.TelegramNetworkError``."""


class _Unauth(Exception):
    """Stand-in for ``aiogram.exceptions.TelegramUnauthorizedError``."""


def _binding(source: str = "vault") -> tb._BotBinding:
    tenant_id = uuid4()
    return tb._BotBinding(
        tenant_id=tenant_id,
        source=source,
        label=tb._binding_label(tenant_id, source),
    )


@pytest.fixture(autouse=True)
def reset_module_state() -> Any:
    """Keep the module-level task list and stop signal clean per test."""
    tb._bot_tasks.clear()
    tb._bot_stop_event.clear()
    tb._bot_thread = None
    tb._bot_loop = None
    yield
    tb._bot_tasks.clear()
    tb._bot_stop_event.clear()
    tb._bot_thread = None
    tb._bot_loop = None


async def _drive(runners: list[Any]) -> None:
    await tb._run_dispatchers(
        runners,
        network_error=_Net,
        unauthorized_error=_Unauth,
        stop_event=tb._bot_stop_event,
    )


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


async def test_two_tenants_get_two_concurrent_dispatchers() -> None:
    """Both bots poll at once — not one after the other."""
    both_started = asyncio.Event()
    release = asyncio.Event()
    started: list[str] = []

    def _runner(name: str) -> Any:
        async def _poll() -> None:
            started.append(name)
            if len(started) == 2:
                both_started.set()
            await release.wait()

        return _poll

    runners = [(_binding(), _runner("a")), (_binding(), _runner("b"))]
    task = asyncio.ensure_future(_drive(runners))

    await asyncio.wait_for(both_started.wait(), timeout=1)
    assert len(tb._bot_tasks) == 2

    release.set()
    await asyncio.wait_for(task, timeout=1)
    assert sorted(started) == ["a", "b"]
    # The published task list is cleared once the gather returns.
    assert tb._bot_tasks == []


async def test_a_rejected_token_ends_only_its_own_dispatcher(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One dead bot must not take the other tenants' bots with it (D1)."""
    doomed = _binding()
    healthy = _binding()
    b_running = asyncio.Event()
    release = asyncio.Event()

    async def _poll_doomed() -> None:
        raise _Unauth("revoked at BotFather")

    async def _poll_healthy() -> None:
        b_running.set()
        await release.wait()

    with caplog.at_level(logging.ERROR, logger="bot.telegram_bot"):
        task = asyncio.ensure_future(_drive([(doomed, _poll_doomed), (healthy, _poll_healthy)]))
        await asyncio.wait_for(b_running.wait(), timeout=1)
        # Let the doomed supervisor run to completion.
        for _ in range(5):
            await asyncio.sleep(0)

        assert tb._bot_tasks[0].done(), "the rejected dispatcher should have ended"
        assert not tb._bot_tasks[1].done(), "the healthy dispatcher must still poll"

        release.set()
        await asyncio.wait_for(task, timeout=1)

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert doomed.label in errors[0]
    assert healthy.label not in errors[0]


async def test_an_unexpected_error_is_contained_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bug in one dispatcher is reported against it, not raised at the thread."""
    broken = _binding()
    healthy = _binding()
    release = asyncio.Event()

    async def _poll_broken() -> None:
        raise ValueError("a genuine bug")

    async def _poll_healthy() -> None:
        await release.wait()

    with caplog.at_level(logging.ERROR, logger="bot.telegram_bot"):
        task = asyncio.ensure_future(_drive([(broken, _poll_broken), (healthy, _poll_healthy)]))
        for _ in range(5):
            await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(task, timeout=1)

    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(records) == 1
    assert broken.label in records[0].getMessage()
    # The traceback is kept — this is a bug, not an operational condition.
    assert records[0].exc_info is not None


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def test_stop_bot_tears_every_dispatcher_down() -> None:
    """``stop_bot`` cancels all N tasks and the single worker thread unwinds.

    Runs the dispatchers the way production does — on their own loop in
    their own thread — so the cross-thread cancellation path is the one
    under test, not a stand-in for it.
    """
    polled: list[str] = []
    both_started = threading.Event()
    thread_finished = threading.Event()

    def _runner(name: str) -> Any:
        async def _poll() -> None:
            polled.append(name)
            if len(polled) == 2:
                both_started.set()
            await asyncio.sleep(30)

        return _poll

    def _worker() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        tb._bot_loop = loop
        try:
            loop.run_until_complete(
                _drive([(_binding(), _runner("a")), (_binding(), _runner("b"))])
            )
        finally:
            loop.close()
            thread_finished.set()

    thread = threading.Thread(target=_worker, name="TelegramBotTest", daemon=True)
    tb._bot_thread = thread
    thread.start()

    assert both_started.wait(timeout=2), "both dispatchers should have started"
    # Give the tasks a moment to reach their await before cancelling.
    time.sleep(0.05)

    tb.stop_bot()

    assert thread_finished.wait(timeout=2), "the worker thread should have unwound"
    assert not thread.is_alive()
    assert sorted(polled) == ["a", "b"]
    assert tb._bot_tasks == []
