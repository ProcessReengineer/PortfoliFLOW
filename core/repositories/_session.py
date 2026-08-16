# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Async session factory and tenant-context manager.

This module is the single seam between the repository layer and the
SQLAlchemy engine. Two responsibilities:

1. Construct the async engine from a connection URL (typically read
   from ``DATABASE_URL`` in ``.env``).
2. Hand out tenant-scoped sessions through the
   :func:`tenant_context` async context manager, which sets
   ``SET LOCAL app.tenant_id`` on the underlying connection inside the
   transaction the work runs on (per ADR-0035 §4).

Direct ``async_sessionmaker(engine)()`` usage that bypasses
``tenant_context`` is a programming error: any query against a
domain table without a tenant context returns the empty set (RLS
filters everything out) or raises (when an INSERT's WITH CHECK
fails). Audit-relevant code paths must always go through
``tenant_context``.

The ``LOCAL`` keyword on ``SET LOCAL`` is essential. Without it, the
GUC would persist on the pooled connection after release and the next
caller picking up the same connection would inherit the previous
caller's tenant — a cross-tenant leak. ``LOCAL`` ties the GUC to the
transaction lifetime, which is exactly the granularity ADR-0035
requires.

To make RLS enforcement true-by-construction regardless of the
connecting role, ``tenant_context`` also switches to the unprivileged
application role (``APP_DB_ROLE``) for the duration of the transaction
(per ADR-0078). PostgreSQL never enforces RLS for a superuser, so a
``tenant_context`` opened on the superuser CLI engine would otherwise
set ``app.tenant_id`` while no policy ever evaluates it — every
RLS-reliant read would silently see all tenants' rows. Dropping to the
application role closes that gap; the switch is a no-op when the engine
already connects as that role (the web app). Like the GUCs, the role is
set transaction-locally and auto-resets at COMMIT/ROLLBACK.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import get_config


def create_engine_from_url(url: str, **engine_kwargs: object) -> AsyncEngine:
    """Build an async SQLAlchemy engine for the given URL.

    Args:
        url: The connection URL. Must use the ``postgresql+asyncpg://``
            scheme; other dialects are not supported by PortfoliFLOW.
        **engine_kwargs: Additional keyword arguments forwarded to
            :func:`sqlalchemy.ext.asyncio.create_async_engine` (e.g.
            ``echo=True`` for query logging in tests).

    Returns:
        A configured :class:`AsyncEngine`. The caller is responsible
        for ``await engine.dispose()`` at shutdown.
    """
    return create_async_engine(url, **engine_kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return an ``async_sessionmaker`` bound to the given engine.

    The factory is cheap to construct per-request, but in long-running
    processes a single sessionmaker per engine is preferable. The
    repository layer typically caches one sessionmaker for the
    application lifetime.

    Args:
        engine: The async engine the sessionmaker should bind to.

    Returns:
        An ``async_sessionmaker`` configured with
        ``expire_on_commit=False`` so DTOs can be safely returned to
        callers after commit.
    """
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def tenant_context(
    engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID | None = None,
    *,
    is_super_admin: bool = False,
    enforce_rls: bool = True,
) -> AsyncGenerator[AsyncSession, None]:
    """Acquire a tenant-scoped database session.

    Sets ``app.tenant_id`` on the connection inside the transaction
    (per ADR-0035 §4). The setting expires when the transaction ends —
    pooled connections do not leak the tenant to subsequent requests.

    Unless ``enforce_rls`` is False, the session then switches to the
    unprivileged application role (``APP_DB_ROLE``) for the remainder
    of the transaction (per ADR-0078), so RLS is enforced regardless of
    the connecting role. This matters for the superuser CLI engine,
    which bypasses RLS entirely: without the switch, tenant-scoped
    reads on that engine would see every tenant's rows. The switch is a
    no-op when the engine already connects as the application role (the
    web app). It auto-resets at COMMIT/ROLLBACK, like the GUCs above.

    When ``user_id`` is provided, additionally sets ``app.user_id`` so
    the audit trigger from b001 can capture the actor (per ADR-0036
    §1d). Phase-1 call sites (CLI bootstrap, repository tests) leave
    it ``None`` and the resulting audit rows have ``user_id IS NULL``,
    matching the b001 behaviour. Authenticated requests pass the
    session's user id and the audit log becomes complete.

    When ``is_super_admin`` is True, ``app.is_super_admin`` is set to
    ``'true'`` so the ``super_admin_audit`` RLS policy installed by
    migration b013 allows the session to read rows. The flag is
    distinct from ``user.is_super_admin`` to keep responsibility on
    the caller: the route layer reads the user's flag, then asks
    ``tenant_context`` to expose super-admin rows. Per ADR-0064 §4.

    This is the ONLY sanctioned way to obtain a session for
    domain-table access. Direct session acquisition that bypasses this
    manager is a programming error.

    Args:
        engine: The async engine to acquire the connection from.
        tenant_id: The tenant the session should be scoped to. RLS
            policies on every domain table evaluate against this UUID —
            true by construction, since (with ``enforce_rls``) the
            session assumes the RLS-subject application role.
        user_id: Optional UUID of the authenticated user. When set,
            ``app.user_id`` is populated alongside ``app.tenant_id``
            so the audit trigger records the actor.
        is_super_admin: When True, sets ``app.is_super_admin`` to
            ``'true'`` so the ``super_admin_audit`` RLS policy
            evaluates correctly. Defaults to False; the only
            sanctioned True-callers are the super-admin route
            handlers in ``web/routes/super_admin.py``. The GUC-based
            policy survives the application-role switch.
        enforce_rls: When True (the default), switch to the
            application role (``APP_DB_ROLE``) so RLS is enforced even
            on a privileged engine. False is reserved for explicitly
            audited cross-tenant callers that must bypass RLS while
            inside a tenant context; each such use must carry a
            justifying comment (per ADR-0078).

    Yields:
        An ``AsyncSession`` with the tenant context set. The session is
        closed and the transaction is committed when the context block
        exits without exception, or rolled back on exception.
    """
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        # set_config(name, value, is_local) is the parameter-bindable
        # equivalent of SET LOCAL. The literal SET LOCAL statement is
        # a Postgres config command, not regular DML, and the asyncpg
        # protocol cannot bind placeholders into it. Passing the
        # tenant id as a bound parameter (rather than string-
        # interpolating into the SQL) is the safe path: even if a
        # caller ever passes a non-UUID value, asyncpg's type system
        # rejects it before the database sees it.
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        if user_id is not None:
            await session.execute(
                text("SELECT set_config('app.user_id', :uid, true)"),
                {"uid": str(user_id)},
            )
        if is_super_admin:
            await session.execute(text("SELECT set_config('app.is_super_admin', 'true', true)"))
        if enforce_rls:
            # Drop to the unprivileged application role for the rest
            # of the transaction so RLS is enforced regardless of the
            # connecting role (ADR-0078). The GUCs above are set
            # *first*, as the privileged role, then we relinquish the
            # privilege — a superuser would otherwise bypass RLS and
            # every tenant-scoped read would see all tenants' rows.
            #
            # set_config('role', value, true) is the bind-safe,
            # transaction-local equivalent of SET LOCAL ROLE (asyncpg
            # cannot bind into the literal command); like the GUCs it
            # auto-resets at COMMIT/ROLLBACK, so no pooled connection
            # leaks the role. The role name comes from configuration
            # (APP_DB_ROLE), never from caller input.
            await session.execute(
                text("SELECT set_config('role', :app_role, true)"),
                {"app_role": get_config().APP_DB_ROLE},
            )
        yield session
