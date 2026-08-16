# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""End-to-end tests for the login / logout / protected-page flow.

Live-DB tests against the compose Postgres. The fixtures seed the
sentinel tenant and a user under it, then drive the FastAPI app via
``httpx.AsyncClient`` over ``ASGITransport`` (no live uvicorn).
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
            "skipping live-DB login flow tests.",
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
async def reset_login_flow_schema(
    fresh_superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    """Truncate every domain table before AND after each test.

    The trailing TRUNCATE was added in sub-stream 3a, Task 2: the
    pre-only variant left the user row inserted by the last test in
    this module sitting in the dev database, polluting it with
    ``login@example.com``. Truncating in a ``finally`` block keeps
    the dev DB clean even if a test crashes mid-way.
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
    reset_login_flow_schema: None,
) -> tuple[UUID, str, str]:
    """Insert sentinel tenant + a sentinel-tenant user; return ids and password."""
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "login@example.com"
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
    """Client whose engines are bound to the live DB."""
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_login_renders_form_with_pre_session_csrf(
    web_client: AsyncClient,
) -> None:
    response = await web_client.get("/login")
    assert response.status_code == 200
    assert "<form" in response.text
    assert "csrf_token" in response.text
    assert "portfoliflow_csrf_pre_session" in response.cookies


async def test_post_login_with_correct_credentials_sets_session_cookie(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user

    # Mint a pre-session CSRF token via GET first.
    get_response = await web_client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None

    response = await web_client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "portfoliflow_session" in response.cookies


async def test_post_login_with_wrong_credentials_re_renders_form(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, _password = seeded_user

    get_response = await web_client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None

    response = await web_client.post(
        "/login",
        data={"email": email, "password": "wrong", "csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "Invalid credentials" in response.text
    assert "portfoliflow_session" not in response.cookies


async def test_post_login_without_csrf_returns_403(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user

    response = await web_client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 403


async def test_get_root_without_session_redirects_to_login(
    web_client: AsyncClient,
) -> None:
    response = await web_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_get_root_with_session_redirects_to_front_office(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Sub-stream 6F-1 of Phase 6 Block 1 (ADR-0046) re-points the
    root redirect from ``/chat`` to ``/front-office`` — the area
    sidebar is now the canonical navigation surface and Front Office
    is the default landing area.
    """
    _user_id, email, password = seeded_user

    get_response = await web_client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    await web_client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )

    response = await web_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/front-office"

    area_response = await web_client.get("/front-office", follow_redirects=False)
    assert area_response.status_code == 200
    assert email in area_response.text


async def test_post_logout_clears_session_cookie(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user

    get_response = await web_client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    await web_client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )

    # ADR-0051: the standalone /chat page was retired; the embedded
    # Shirley section on /assistants now carries the session CSRF
    # token in its composer form.
    page = await web_client.get("/assistants", follow_redirects=False)
    import re

    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None
    session_csrf = match.group(1)

    response = await web_client.post(
        "/logout",
        data={"csrf_token": session_csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    # Cookie cleared (set with empty value or expired).
    cookie = response.headers.get("set-cookie", "")
    assert "portfoliflow_session=" in cookie


async def test_post_logout_without_csrf_returns_403(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user

    get_response = await web_client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    await web_client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )

    response = await web_client.post("/logout", follow_redirects=False)
    assert response.status_code == 403
