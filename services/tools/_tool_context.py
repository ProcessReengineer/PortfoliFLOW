# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tool-execution context — the per-turn tenant + database-URL seam.

The Postgres-native AI tools (``services/tools/investment_tools.py``)
read through the repository layer, which needs two things the
:class:`~services.tool_registry.ToolRegistry` dispatch path does not
carry: the request's tenant identity and a way to reach the database.
``execute_tool`` passes only the arguments the LLM put in the tool
call; the tool functions are plain ``Callable[..., str]`` with no
access to the FastAPI request.

This module is that missing channel, in its minimal form. The chat
route (:func:`web.routes.chat.chat_stream`) populates a module-level
:class:`ToolExecutionContext` before driving a turn and clears it in a
``finally`` afterwards; the Postgres-native tools read it.

Why the context carries a URL string, not an ``AsyncEngine``
------------------------------------------------------------
The context carries the **database connection URL** — a plain,
immutable string — not a live
:class:`~sqlalchemy.ext.asyncio.AsyncEngine`. A SQLAlchemy
``AsyncEngine`` backed by asyncpg holds a connection pool, and every
asyncpg connection is bound to the event loop it was created on. The
tools run their async workflows on a *fresh* event loop on a daemon
thread (via
:func:`services.tools._async_bridge.run_async_in_fresh_loop`), so the
application engine — created on the uvicorn loop and reachable as
``request.app.state.engine`` — must not be handed in: pulling a
connection from its pool on the fresh loop raises ``RuntimeError: ...
got Future ... attached to a different loop``.

The fix is to carry the URL across the thread boundary and have each
tool workflow construct its own short-lived engine *inside* the fresh
loop, then dispose it. An immutable string crosses a loop boundary
safely; a live engine does not. This is the same hazard class as the
``httpx.AsyncClient`` loop-binding note in
:meth:`services.ai_service_core.AIServiceCore.__init__`, and the same
per-job-engine pattern as :func:`web.main._read_schema_revision`. See
ADR-0047 (amended) for the decision record.

Why module-level state is acceptable here
------------------------------------------
:meth:`services.ai_service_core.AIServiceCore._stream_response_locked`
runs under the process-wide ``_TURN_LOCK`` (ADR-0031), so at most one
turn is ever populating this context at a time in a single-worker
deployment — there is never concurrent context population to race on.
A multi-worker deployment (flagged as a Phase-5 follow-up that also
needs Redis for ``pending_turns``), or any future concurrent-turn
design, requires this context to become :mod:`contextvars`-based;
that is the known migration trigger.

The turn-scoped data cache
--------------------------
:func:`store_tool_data` / :func:`get_tool_data` / :func:`clear_tool_data`
back a second piece of per-turn server-side state: a small cache of
the structured-data envelopes ``get_investment_data`` produces, keyed
by an opaque handle. It exists so structured chart data never travels
*through the model*. A tool call's arguments are model-generated
output, so passing a multi-hundred-row envelope as a ``render_chart``
argument would force the model to read every row as input tokens and
then re-emit every row token-by-token as output — slow, expensive, and
corruption-prone. Instead ``get_investment_data`` stores the envelope
here and returns the model only a handle plus a compact summary;
``render_chart`` looks the rows back up by handle. This is the QT
``datastore_key`` principle adapted to the web variant — the model
decides *which* data and *how* to chart it, but never transports the
data. See ADR-0048 (amended).

The cache shares the per-turn lifecycle of ``_context`` above (the
chat route clears both in the same ``finally``) and the same
module-level-state justification — the process-wide ``_TURN_LOCK``
means at most one turn populates it at a time in a single-worker
deployment — and the same multi-worker / :mod:`contextvars` migration
trigger.

The single multi-tenant seam
----------------------------
:func:`resolve_tenant_id` is the per-turn tenant accessor.
Multi-tenant activation (ADR-0063) makes it read from the
:class:`ToolExecutionContext` populated by the chat route from the
authenticated session's ``tenant_id``. An unset context is a
programming error and raises :class:`ToolContextNotSetError` so a
GUI-style caller that never set a context produces a clear
diagnostic instead of leaking onto the primary tenant.

Layering: this module sits under ``services/`` (ADR-0038 — no PyQt6),
imports only the stdlib and :mod:`core.exceptions`, and must not
import from ``web/`` — the chat route imports *from* here, not the
other way round. See ADR-0047 and ADR-0063 §3.
"""

from __future__ import annotations

import secrets
from collections import OrderedDict
from dataclasses import dataclass
from uuid import UUID

from core.exceptions import PortfoliFlowError


class ToolContextNotSetError(PortfoliFlowError):
    """The Postgres-native tools were invoked outside a chat turn.

    Per ADR-0063 §3 there is no fallback tenant — the chat route
    must populate the :class:`ToolExecutionContext` (with the
    authenticated session's ``tenant_id``) before driving
    :meth:`AIServiceCore.stream_response`. Reaching this exception
    means a code path called a Postgres-native tool without going
    through a chat turn.
    """


@dataclass(frozen=True)
class ToolExecutionContext:
    """Per-turn context the Postgres-native tools read.

    Attributes:
        tenant_id: The tenant the turn's tool reads are scoped to.
            Passed to :func:`core.repositories.tenant_context` so RLS
            evaluates against it.
        database_url: The asyncpg connection URL. The tools construct
            a short-lived ``AsyncEngine`` from this *inside their own
            event loop* — see the module docstring for why the engine
            object itself must not be carried here. Never empty — the
            chat route does not construct a context when the database
            URL is unconfigured (see ADR-0047 §Decision).
    """

    tenant_id: UUID
    database_url: str


_context: ToolExecutionContext | None = None


def set_tool_context(ctx: ToolExecutionContext) -> None:
    """Store the per-turn tool-execution context.

    Called by the chat route immediately before it drives
    :meth:`AIServiceCore.stream_response`.

    Args:
        ctx: The context for the turn about to start.
    """
    global _context
    _context = ctx


def get_tool_context() -> ToolExecutionContext | None:
    """Return the current tool-execution context, or ``None`` if unset.

    The Postgres-native tools call this first; a ``None`` return is the
    graceful-degradation signal (the GUI never populates the context,
    so the tools explain that the data is unavailable rather than
    raising).

    Returns:
        The context set for the current turn, or ``None`` when no turn
        has populated it (GUI path, or between turns).
    """
    return _context


def clear_tool_context() -> None:
    """Reset the tool-execution context to ``None``.

    Called by the chat route in a ``finally`` after a turn ends —
    whether it completed, errored, or the client disconnected — so the
    next turn starts with a clean context and a failed turn cannot leak
    its context forward.
    """
    global _context
    _context = None


def resolve_tenant_id() -> UUID:
    """Resolve the tenant id for the current chat turn.

    Per ADR-0063 §3 this reads directly from the
    :class:`ToolExecutionContext` the chat route populates with the
    authenticated session's ``tenant_id``.

    Returns:
        The active tenant id for the current chat turn.

    Raises:
        ToolContextNotSetError: No turn has set a context. This is
            a programming error — every Postgres-native tool call
            must run inside a chat turn whose route populates the
            context.
    """
    ctx = get_tool_context()
    if ctx is None:
        raise ToolContextNotSetError(
            "Tool context is not set — a Postgres-native tool was "
            "invoked outside a tenant-scoped chat turn."
        )
    return ctx.tenant_id


# ---------------------------------------------------------------------------
# The turn-scoped data cache
# ---------------------------------------------------------------------------

# Defensive cap on how many envelopes the cache holds at once. A single
# turn realistically calls ``get_investment_data`` a handful of times;
# this limit is a guard, not a feature — it stops a pathological tool
# loop from growing the cache without bound. Past the cap the oldest
# entry is evicted first (the ``OrderedDict`` preserves insertion
# order). See the module docstring "The turn-scoped data cache".
_DATA_CACHE_LIMIT = 32

_data_cache: OrderedDict[str, dict] = OrderedDict()


def store_tool_data(envelope: dict) -> str:
    """Store a structured-data envelope and return an opaque handle.

    The handle pattern keeps structured chart data out of the model's
    token stream: ``get_investment_data`` stores the rows here and
    hands the model only the handle, and ``render_chart`` resolves the
    rows back by handle. The model decides *which* data and *how* to
    chart it, but never transports the data itself. See the module
    docstring "The turn-scoped data cache" and ADR-0048 (amended).

    Args:
        envelope: The structured-data envelope to cache, verbatim.

    Returns:
        A freshly generated, short, opaque handle the caller surfaces
        to the model; ``render_chart`` later resolves it via
        :func:`get_tool_data`.
    """
    handle = secrets.token_hex(6)
    _data_cache[handle] = envelope
    while len(_data_cache) > _DATA_CACHE_LIMIT:
        _data_cache.popitem(last=False)
    return handle


def get_tool_data(handle: str) -> dict | None:
    """Return the cached envelope for ``handle``, or ``None`` if absent.

    A ``None`` return is the stale-or-wrong-handle signal —
    ``render_chart`` turns it into a clear explanatory string rather
    than raising.

    Args:
        handle: The handle :func:`store_tool_data` returned.

    Returns:
        The cached envelope, or ``None`` when no entry matches (a
        stale handle, a handle from a previous turn, an evicted entry,
        or a typo).
    """
    return _data_cache.get(handle)


def clear_tool_data() -> None:
    """Empty the turn-scoped data cache.

    Called by the chat route in the same ``finally`` as
    :func:`clear_tool_context` — a turn that completes, errors, or is
    abandoned must leave no cached envelope visible to the next turn.
    """
    _data_cache.clear()
