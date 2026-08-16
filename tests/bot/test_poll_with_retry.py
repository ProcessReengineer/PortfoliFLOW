# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for :func:`bot.telegram_bot._poll_with_retry`.

The retry helper is the network-robustness seam: it supervises one
dispatcher's polling so transient connectivity loss is retried quietly in
the background instead of killing that bot, while a permanently rejected
token stops immediately and genuinely unexpected errors still propagate.
These tests exercise it in isolation — no aiogram, no network — by passing
in plain stand-in exception types, a fake ``run_polling`` coroutine, and
tiny backoff values so the loop runs instantly.

It became a **coroutine** with ADR-0112 §5: one thread now runs N
dispatchers on one loop, so supervision has to be something the loop can
run N of concurrently, which ``run_until_complete`` (its pre-F5 shape)
cannot be. The semantics are unchanged, which is why these tests only had
to grow an ``await``.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from bot.telegram_bot import _poll_with_retry


class _Net(Exception):
    """Stand-in for ``aiogram.exceptions.TelegramNetworkError``."""


class _Unauth(Exception):
    """Stand-in for ``aiogram.exceptions.TelegramUnauthorizedError``."""


async def test_transient_network_failure_retries_then_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two network errors are retried; recovery exits cleanly with ONE warning."""
    stop_event = threading.Event()
    calls = 0

    async def run_polling() -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise _Net("no network")
        # Third attempt: simulate a clean shutdown arriving — set the stop
        # event and return normally, mirroring a real recovery/shutdown.
        stop_event.set()

    with caplog.at_level(logging.DEBUG, logger="bot.telegram_bot"):
        await _poll_with_retry(
            run_polling=run_polling,
            network_error=_Net,
            unauthorized_error=_Unauth,
            stop_event=stop_event,
            backoff_start=0.001,
            backoff_max=0.001,
        )

    assert calls == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, (
        f"expected exactly one WARNING, got {[r.getMessage() for r in warnings]}"
    )
    # No traceback: the warning is logged without ``exc_info``, so the record
    # carries no exception tuple — a full stack trace is never dumped.
    assert warnings[0].exc_info is None
    assert "_Net" in warnings[0].getMessage()


async def test_unauthorized_does_not_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected token logs one ERROR and returns without retrying."""
    stop_event = threading.Event()
    calls = 0

    async def run_polling() -> None:
        nonlocal calls
        calls += 1
        raise _Unauth("bad token")

    with caplog.at_level(logging.DEBUG, logger="bot.telegram_bot"):
        await _poll_with_retry(
            run_polling=run_polling,
            network_error=_Net,
            unauthorized_error=_Unauth,
            stop_event=stop_event,
            label="tenant=t1 source=vault",
            backoff_start=0.001,
            backoff_max=0.001,
        )

    assert calls == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage().lower()
    assert "unauthorized" in message
    # The dispatcher is named so an operator knows *whose* bot died; the
    # token itself never appears.
    assert "tenant=t1" in message


async def test_stop_event_already_set_never_polls() -> None:
    """A pre-set stop event short-circuits — ``run_polling`` is never called."""
    stop_event = threading.Event()
    stop_event.set()
    calls = 0

    async def run_polling() -> None:
        nonlocal calls
        calls += 1

    await _poll_with_retry(
        run_polling=run_polling,
        network_error=_Net,
        unauthorized_error=_Unauth,
        stop_event=stop_event,
        backoff_start=0.001,
        backoff_max=0.001,
    )

    assert calls == 0


async def test_a_stop_during_backoff_returns_promptly() -> None:
    """The backoff wait is interruptible, and does not block the shared loop.

    With N dispatchers on one loop, a blocking ``Event.wait`` in one
    backoff would freeze the other N-1. The wait is a series of short async
    naps instead, so a concurrent task can run — and set the flag.
    """
    stop_event = threading.Event()
    calls = 0

    async def run_polling() -> None:
        nonlocal calls
        calls += 1
        raise _Net("no network")

    async def stop_soon() -> None:
        await asyncio.sleep(0.01)
        stop_event.set()

    await asyncio.gather(
        _poll_with_retry(
            run_polling=run_polling,
            network_error=_Net,
            unauthorized_error=_Unauth,
            stop_event=stop_event,
            backoff_start=5.0,
            backoff_max=5.0,
        ),
        stop_soon(),
    )

    # One attempt, then the backoff was cut short by the stop signal rather
    # than running its full five seconds.
    assert calls == 1


async def test_cancellation_propagates() -> None:
    """A cancelled supervisor really is cancelled — ``stop_bot`` relies on it."""
    stop_event = threading.Event()
    started = asyncio.Event()

    async def run_polling() -> None:
        started.set()
        await asyncio.sleep(60)

    task = asyncio.ensure_future(
        _poll_with_retry(
            run_polling=run_polling,
            network_error=_Net,
            unauthorized_error=_Unauth,
            stop_event=stop_event,
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
