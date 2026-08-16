# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the Shirley-Chat routes.

These tests exercise the auth gating, CSRF protection, and the HTML
fragment shape that ``POST /chat/messages`` returns. The SSE streaming
behaviour itself is covered separately in ``test_chat_sse.py`` so the
two test files can mock the AI core differently without stepping on
each other.

The tests reuse the live-DB fixture pattern from
``test_login_flow.py``; the conftest seeds the sentinel tenant + a
sentinel-tenant user, and the per-test client is bound to the FastAPI
app via ``ASGITransport``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB chat-route tests.",
            allow_module_level=False,
        )


@pytest_asyncio.fixture
async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_schema(
    fresh_superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    """Truncate every domain table before AND after each test.

    The trailing TRUNCATE prevents ``chat@example.com`` from
    persisting in the dev database after the last test (sub-stream
    3a, Task 2 fix).
    """
    truncate_sql = text(
        "TRUNCATE TABLE data_upload_sheets, data_uploads, "
        "login_audit, sessions, audit_log, "
        "data_store_entries, users, tenants "
        "RESTART IDENTITY CASCADE"
    )
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(truncate_sql)
    try:
        yield
    finally:
        async with fresh_superuser_engine.begin() as conn:
            await conn.execute(truncate_sql)


@pytest_asyncio.fixture
async def seeded_user(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "chat@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
            },
        )
    return user_id, email, plaintext


@pytest_asyncio.fixture
async def web_client(
    seeded_user: tuple[UUID, str, str],
) -> AsyncGenerator[AsyncClient, None]:
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
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://testserver") as client,
        app.router.lifespan_context(app),
    ):
        yield client


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """Drive ``GET /login`` + ``POST /login`` and return the session
    CSRF token extracted from the embedded Shirley section on
    ``/assistants`` (ADR-0051 retired the standalone ``GET /chat``).
    """
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    page = await client.get("/assistants", follow_redirects=False)
    assert page.status_code == 200
    import re

    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


# ---------------------------------------------------------------------------
# POST /chat/messages
# ---------------------------------------------------------------------------


async def test_post_messages_without_csrf_returns_403(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.post(
        "/chat/messages",
        data={"message": "hello"},
        follow_redirects=False,
    )
    assert response.status_code == 403


async def test_post_messages_unauthenticated_redirects(
    web_client: AsyncClient,
) -> None:
    response = await web_client.post(
        "/chat/messages",
        data={"message": "hello"},
        follow_redirects=False,
    )
    # Redirect to /login before CSRF check, since require_session runs first.
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_post_messages_with_csrf_returns_user_fragment_and_sse_target(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    csrf = await _login(web_client, email, password)

    response = await web_client.post(
        "/chat/messages",
        data={"message": "Hello Shirley", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # The user's line is rendered inline.
    assert "Hello Shirley" in body
    assert 'class="chat-message chat-message--user"' in body
    # An assistant placeholder bubble is present.
    assert "chat-message--assistant" in body
    # The SSE-bootstrap element exposes the stream URL via a data
    # attribute that ``chat.js`` picks up to open a native EventSource.
    # The HTMX ``sse-connect`` / ``sse-close`` attributes are gone —
    # see HTMX bug #2343 (whitespace trim) and the post-completion
    # 404 reconnect storm noted in the 2026-05-13 smoke test.
    assert 'data-pf-sse-url="/chat/stream/' in body
