# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard for ADR-0065 request-scoped transaction lifetime.

The retired ``get_authenticated_session`` yield-dependency held a
tenant-scoped transaction — with a ``sessions``-row lock and a pooled
connection — open for the entire request. This module asserts the
structural property that replaced it:

* While a deliberately slow authenticated handler runs, no connection
  in the app database sits ``idle in transaction`` against the auth
  tables. The old dependency held exactly such a connection for the
  whole request (its last statement the ``SELECT ... FROM users`` of
  the user load).
* Two concurrent requests on the same session do not serialise on the
  ``sessions`` row. With the per-request touch lock gone, two slow
  requests overlap and finish in roughly one slow span rather than two.

A throwaway slow route is mounted on a freshly-built app; it is gated
by ``require_role`` so it exercises the full dependency chain
(``require_role`` → ``get_authenticated_user`` →
``require_authenticated_session``). ``pg_stat_activity`` is read from a
separate superuser connection while the slow handler is parked in its
``asyncio.sleep``.

Live-DB only; skips cleanly when ``DATABASE_URL`` (or the superuser URL
needed to observe other backends' queries) is unset.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import time
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories.user_repository import UserDTO
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.permissions import require_role
from web.settings import WebSettings

load_dotenv(pathlib.Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

# Long enough to observe the parked handler reliably, short enough to
# keep the suite snappy.
_SLOW_SECONDS = 1.0
_SLOW_ROUTE = "/__test__/slow-authenticated"


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB transaction-lifetime regression test.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def observer_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Superuser engine used purely to read ``pg_stat_activity``."""
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded_owner(
    observer_engine: AsyncEngine,
) -> tuple[str, str]:
    """Seed the primary tenant plus an owner user; return (email, pw)."""
    async with observer_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE sessions, login_audit, audit_log, "
                "users, tenants RESTART IDENTITY CASCADE"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Primary Tenant"},
        )
        email = f"owner-{uuid4().hex}@example.com"
        plaintext = "correct-horse-battery-staple"
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash,
                     ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(uuid4()),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
            },
        )
    return email, plaintext


def _build_app_with_slow_route() -> FastAPI:
    settings = WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        csrf_cookie_name="portfoliflow_csrf_pre_session",
        database_url=DATABASE_URL,
        database_url_superuser=DATABASE_URL_SUPERUSER,
        session_cookie_secure=False,
    )
    app = create_app(settings)

    @app.get(_SLOW_ROUTE)
    async def _slow_authenticated(
        _user: UserDTO = Depends(require_role("owner")),
    ) -> dict[str, bool]:
        # By the time control reaches here the auth dependency chain —
        # including the throttled touch and the user load — has fully
        # committed and released its connections. The sleep therefore
        # holds no transaction and no sessions-row lock.
        await asyncio.sleep(_SLOW_SECONDS)
        return {"ok": True}

    return app


@pytest_asyncio.fixture
async def slow_client(
    seeded_owner: tuple[str, str],
) -> AsyncGenerator[AsyncClient, None]:
    app = _build_app_with_slow_route()
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://testserver") as client,
        app.router.lifespan_context(app),
    ):
        yield client


async def _login(client: AsyncClient, email: str, password: str) -> None:
    """Establish a session cookie via GET /login + POST /login."""
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    resp = await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def _count_idle_in_transaction_auth(engine: AsyncEngine) -> int:
    """Connections parked ``idle in transaction`` on the auth tables.

    Restricted to the auth-path statements (``sessions`` / ``users``)
    and excluding the observer's own backend, so unrelated activity on
    the shared dev database cannot make the assertion flaky while still
    catching the exact regression pattern the old yield-dependency
    produced.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND state = 'idle in transaction' "
                "AND pid <> pg_backend_pid() "
                "AND (query ILIKE '%from users%' "
                "     OR query ILIKE '%sessions%')"
            )
        )
        return int(result.scalar_one())


async def test_no_idle_in_transaction_across_slow_handler(
    slow_client: AsyncClient,
    seeded_owner: tuple[str, str],
    observer_engine: AsyncEngine,
) -> None:
    email, password = seeded_owner
    await _login(slow_client, email, password)

    task = asyncio.create_task(slow_client.get(_SLOW_ROUTE))
    # Let the request pass through the auth chain and reach the sleep.
    await asyncio.sleep(_SLOW_SECONDS / 2)
    idle = await _count_idle_in_transaction_auth(observer_engine)

    response = await task
    assert response.status_code == 200
    assert idle == 0, (
        "an auth-path connection was idle in transaction across the "
        "slow handler — the request-scoped transaction was reintroduced"
    )


async def test_concurrent_same_session_not_serialised(
    slow_client: AsyncClient,
    seeded_owner: tuple[str, str],
) -> None:
    email, password = seeded_owner
    await _login(slow_client, email, password)

    start = time.monotonic()
    first, second = await asyncio.gather(
        slow_client.get(_SLOW_ROUTE),
        slow_client.get(_SLOW_ROUTE),
    )
    elapsed = time.monotonic() - start

    assert first.status_code == 200
    assert second.status_code == 200
    # Overlapping (no lock held across the sleep) ≈ one slow span.
    # Serialised on the sessions-row lock would be ≈ 2× _SLOW_SECONDS.
    assert elapsed < _SLOW_SECONDS * 1.5, (
        f"two same-session requests took {elapsed:.2f}s (~2× the "
        f"{_SLOW_SECONDS}s span) — they serialised on the sessions row"
    )
