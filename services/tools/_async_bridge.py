# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Async-from-sync bridge for synchronous AI-callable tools.

AI-callable tools registered with the :class:`~services.tool_registry.ToolRegistry`
must be synchronous (the registry contract is ``Callable[..., str]`` and
:meth:`~services.tool_registry.ToolRegistry.execute_tool` invokes them
synchronously inside the already-running event loop that drives
:meth:`services.ai_service_core.AIServiceCore.stream_response`). The
Postgres-native investment tools, however, need to run async repository
workflows (``tenant_context`` and the repository layer are async).

Calling :func:`asyncio.run` from inside that live loop raises
``RuntimeError: asyncio.run() cannot be called from a running event
loop`` — the exact bug fixed for
:meth:`AIServiceCore.send_one_shot_extraction` under ADR-0038. This
module extracts that fix's dispatch pattern into one shared helper so
the three investment tools do not each copy the thread dance.

See :meth:`AIServiceCore.send_one_shot_extraction` for the canonical
copy of the pattern and ADR-0038 / ADR-0047 for the rationale.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


def run_async_in_fresh_loop(coro_factory: Callable[[], Awaitable[T]]) -> T:
    """Run an async coroutine to completion from synchronous code.

    Safe to call whether or not the calling thread already has a
    running event loop: when a loop is live (the ``AIServiceCore``
    tool-execution loop, a FastAPI handler), the coroutine is
    dispatched to a fresh daemon thread with its own loop and the
    caller blocks on the join; when no loop is live, it runs directly
    via :func:`asyncio.run`.

    Mirrors the dispatch logic in
    :meth:`services.ai_service_core.AIServiceCore.send_one_shot_extraction`
    — see that method and ADR-0038 for the rationale. (A future tidy-up
    could refactor ``send_one_shot_extraction`` to call this helper too;
    that is deliberately out of scope here so a just-stabilised method
    is not touched.)

    Args:
        coro_factory: A zero-argument callable returning the coroutine
            to run. A factory rather than a bare coroutine is taken so
            the coroutine is constructed on the thread that will run
            it — a coroutine created on one thread's loop and awaited
            on another's is a programming error.

    Returns:
        The value the coroutine resolves to.

    Raises:
        BaseException: Any exception raised inside the coroutine is
            re-raised verbatim on the caller's thread, so callers see
            the original error class (RLS errors, SDK errors, ...).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No live loop on this thread — the direct path is safe.
        return asyncio.run(coro_factory())

    # A live loop is running on this thread. Scheduling back onto it
    # while we synchronously join would deadlock, so spawn a fresh
    # daemon thread that owns its own loop, capture the result or any
    # raised exception, and re-raise on the caller's thread.
    container: dict[str, Any] = {}

    def _runner() -> None:
        try:
            container["result"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001 — re-raised below
            container["error"] = exc

    worker = threading.Thread(
        target=_runner,
        name="tool-async-bridge",
        daemon=True,
    )
    worker.start()
    worker.join()

    if "error" in container:
        raise container["error"]
    return container["result"]
