# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the tool-execution context seam.

Covers :mod:`services.tools._tool_context` (the module-level
``ToolExecutionContext`` holder and the ``resolve_tenant_id`` hardwire
seam) and :mod:`services.tools._async_bridge` (the
``run_async_in_fresh_loop`` async-from-sync helper).

None of these tests need a database — they exercise the seam itself,
not the repository workflows the tools build on top of it. See
``tests/assistants/test_investment_tools.py`` for the DB-backed
happy-path coverage.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

from core.tenant_constants import SENTINEL_TENANT_ID
from services.tools._async_bridge import run_async_in_fresh_loop
from services.tools._tool_context import (
    _DATA_CACHE_LIMIT,
    ToolExecutionContext,
    clear_tool_context,
    clear_tool_data,
    get_tool_context,
    get_tool_data,
    resolve_tenant_id,
    set_tool_context,
    store_tool_data,
)

# A syntactically-valid connection URL. ``ToolExecutionContext`` now
# carries the database URL as a plain string (ADR-0047, amended), so
# the holder tests need no engine object — and never connect.
_DUMMY_DB_URL = "postgresql+asyncpg://localhost/unused"


@pytest.fixture(autouse=True)
def _clean_context() -> Generator[None, None, None]:
    """Ensure each test starts and ends with no context or cached data."""
    clear_tool_context()
    clear_tool_data()
    yield
    clear_tool_context()
    clear_tool_data()


# ---------------------------------------------------------------------------
# ToolExecutionContext holder
# ---------------------------------------------------------------------------


def test_get_returns_none_on_fresh_module() -> None:
    """With nothing set, ``get_tool_context`` reports ``None``."""
    assert get_tool_context() is None


def test_set_then_get_round_trips() -> None:
    """A context that was ``set`` is returned verbatim by ``get``."""
    ctx = ToolExecutionContext(tenant_id=SENTINEL_TENANT_ID, database_url=_DUMMY_DB_URL)
    set_tool_context(ctx)
    retrieved = get_tool_context()
    assert retrieved is ctx
    assert retrieved.tenant_id == SENTINEL_TENANT_ID
    assert retrieved.database_url == _DUMMY_DB_URL
    assert isinstance(retrieved.database_url, str)


def test_clear_resets_to_none() -> None:
    """``clear_tool_context`` returns the holder to the unset state."""
    set_tool_context(ToolExecutionContext(tenant_id=SENTINEL_TENANT_ID, database_url=_DUMMY_DB_URL))
    assert get_tool_context() is not None
    clear_tool_context()
    assert get_tool_context() is None


def test_context_is_frozen() -> None:
    """``ToolExecutionContext`` is immutable — fields cannot be rebound."""
    ctx = ToolExecutionContext(tenant_id=SENTINEL_TENANT_ID, database_url=_DUMMY_DB_URL)
    with pytest.raises(AttributeError):
        ctx.tenant_id = SENTINEL_TENANT_ID  # type: ignore[misc]


# ---------------------------------------------------------------------------
# resolve_tenant_id — the single hardwire seam
# ---------------------------------------------------------------------------


def test_resolve_tenant_id_returns_context_tenant() -> None:
    """resolve_tenant_id reads from the active ToolExecutionContext."""
    from services.tools._tool_context import (
        ToolExecutionContext,
        clear_tool_context,
        set_tool_context,
    )

    set_tool_context(
        ToolExecutionContext(
            tenant_id=SENTINEL_TENANT_ID,
            database_url="postgresql+asyncpg://stub/stub",
        )
    )
    try:
        assert resolve_tenant_id() == SENTINEL_TENANT_ID
    finally:
        clear_tool_context()


def test_resolve_tenant_id_raises_when_context_absent() -> None:
    """Per ADR-0063 §3, no context means a programming error — raise."""
    from services.tools._tool_context import (
        ToolContextNotSetError,
        clear_tool_context,
    )

    clear_tool_context()
    with pytest.raises(ToolContextNotSetError):
        resolve_tenant_id()


# ---------------------------------------------------------------------------
# The turn-scoped data cache — store / get / clear / eviction
# ---------------------------------------------------------------------------


def test_store_tool_data_returns_handle_and_round_trips() -> None:
    """A stored envelope is retrievable by the handle ``store`` returns."""
    envelope = {"__data__": "investment_data", "rows": [[1], [2]]}
    handle = store_tool_data(envelope)
    assert isinstance(handle, str) and handle
    assert get_tool_data(handle) is envelope


def test_get_tool_data_unknown_handle_returns_none() -> None:
    """An unknown handle is the stale/wrong-handle signal — ``None``."""
    assert get_tool_data("not-a-real-handle") is None


def test_store_tool_data_handles_are_distinct() -> None:
    """Two stores get distinct handles even for the same envelope."""
    envelope = {"__data__": "investment_data", "rows": []}
    first = store_tool_data(envelope)
    second = store_tool_data(envelope)
    assert first != second


def test_clear_tool_data_empties_the_cache() -> None:
    """``clear_tool_data`` drops every cached envelope."""
    handle = store_tool_data({"__data__": "investment_data", "rows": []})
    assert get_tool_data(handle) is not None
    clear_tool_data()
    assert get_tool_data(handle) is None


def test_data_cache_evicts_oldest_first_past_the_size_cap() -> None:
    """Past the size cap the oldest entries are evicted, newest survive."""
    handles = [
        store_tool_data({"__data__": "investment_data", "n": i})
        for i in range(_DATA_CACHE_LIMIT + 5)
    ]
    # The first five handles were evicted oldest-first; the rest survive.
    for stale in handles[:5]:
        assert get_tool_data(stale) is None
    for live in handles[5:]:
        assert get_tool_data(live) is not None


# ---------------------------------------------------------------------------
# run_async_in_fresh_loop — async-from-sync bridge
# ---------------------------------------------------------------------------


def test_run_async_in_fresh_loop_without_running_loop() -> None:
    """Called from a thread with no live loop, the direct path runs."""

    async def _work() -> int:
        return 21 * 2

    assert run_async_in_fresh_loop(_work) == 42


async def test_run_async_in_fresh_loop_inside_running_loop() -> None:
    """Called from inside a live loop, the call is dispatched to a thread.

    This is the path the AIServiceCore tool-execution loop and the
    FastAPI chat handler hit. Before the fresh-loop dispatch it would
    raise ``RuntimeError: asyncio.run() cannot be called from a
    running event loop``.
    """

    async def _work() -> str:
        return "ran inside a live loop"

    assert run_async_in_fresh_loop(_work) == "ran inside a live loop"


def test_run_async_in_fresh_loop_reraises_without_loop() -> None:
    """An exception raised inside the coroutine reaches the caller (sync path)."""

    async def _boom() -> None:
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        run_async_in_fresh_loop(_boom)


async def test_run_async_in_fresh_loop_reraises_inside_loop() -> None:
    """An exception raised inside the coroutine reaches the caller (thread path)."""

    async def _boom() -> None:
        raise ValueError("kaboom-in-thread")

    with pytest.raises(ValueError, match="kaboom-in-thread"):
        run_async_in_fresh_loop(_boom)
