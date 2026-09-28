# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Loop-local, tenant-scoped session for the Postgres-native tools.

:func:`tool_session` is the one way an AI-callable tool opens a database
session from inside the fresh event loop
:func:`services.tools._async_bridge.run_async_in_fresh_loop` gives it. It
was a private helper in ``investment_tools.py`` until ADR-0132; the lift
gives it a home that is not a sibling tool module, because three modules
now consume it — ``investment_tools.py``, ``analysis_tools.py`` and
``web_research_tool.py``, which opens one to resolve its per-call
credential — and a tool module importing a private name from another
tool module made the first of them a de-facto library.

It cannot live in :mod:`services.tools._tool_context`: that module is
stdlib-only by contract (its docstring says so, and the tools read the
context on paths that must not pull SQLAlchemy in), while this helper is
SQLAlchemy and ``core.repositories`` by nature.

The user axis (ADR-0132)
------------------------
The session is opened with ``user_id=ctx.user_id``, which sets the
``app.user_id`` GUC the b001 audit trigger reads to attribute a row to
its actor (ADR-0036 §1d). It plays **no** part in RLS — tenant isolation
binds on ``app.tenant_id`` alone, exactly as before — so carrying the
user changes what an audit row records, never what a query can see.

For every call site that exists today the GUC is a no-op: all of them
are ``READ_INTERNAL`` tools that write no audit rows. It is set anyway
so that a future ``WRITE_INTERNAL`` tool running inside a turn attributes
its audit rows to the turn's user rather than leaving ``user_id NULL``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from core.repositories import create_engine_from_url, tenant_context
from services.tools._tool_context import ToolExecutionContext


@asynccontextmanager
async def tool_session(
    ctx: ToolExecutionContext,
) -> AsyncIterator[AsyncSession]:
    """Yield a tenant-scoped session backed by a loop-local engine.

    Constructs a short-lived ``AsyncEngine`` from ``ctx.database_url``
    *inside the caller's event loop* — the fresh loop
    :func:`~services.tools._async_bridge.run_async_in_fresh_loop`
    provides — so no loop-bound object crosses a thread boundary, and
    every asyncpg connection it pools is born and dies on that one
    loop. The engine is disposed when the context exits.

    A fresh engine per tool call is not free — it opens a new pool,
    runs the asyncpg connection handshake, and tears it down — but at
    Shirley's human-paced call volume the cost is negligible, and it
    is the same tradeoff :func:`web.main._read_schema_revision`
    already accepts. A per-thread / per-loop engine cache is a
    possible future optimisation, deliberately not built now. See
    ADR-0047 (amended).

    Args:
        ctx: The per-turn tool-execution context carrying the tenant
            id, the database connection URL, and the turn's user (see
            the module docstring for what the user axis does and does
            not affect).

    Yields:
        An :class:`~sqlalchemy.ext.asyncio.AsyncSession` scoped to
        ``ctx.tenant_id`` via
        :func:`core.repositories.tenant_context`.
    """
    engine = create_engine_from_url(ctx.database_url)
    try:
        async with tenant_context(engine, ctx.tenant_id, user_id=ctx.user_id) as db:
            yield db
    finally:
        await engine.dispose()
