# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the auth-surface layout (Phase 6 Block 1, 6F-1 correction).

The login screen renders with a minimal layout that contains only
the PortfoliFLOW logo, the authentication card, and a minimal
status bar. It must NOT carry the five-area sidebar from the
authenticated shell: at this point the operator has no tenant or
area context, and rendering the area navigation pre-authentication
would either leak the IA or display a disabled-noisy variant.

Three assertions, plus a regression guard:

* ``test_web_login_page_has_no_sidebar`` — ``GET /login`` body
  excludes every marker the area sidebar emits.
* ``test_web_login_page_uses_auth_base`` — the response carries the
  auth-base markers (``data-surface="auth"`` and the
  ``pf-auth-shell`` wrapper class).
* ``test_web_authenticated_pages_have_sidebar`` — guards against
  future templates accidentally extending ``_auth_base.html`` for
  surfaces that need the area sidebar. Drives a real login and then
  fetches ``/front-office``.

The fixture pattern mirrors ``test_login_flow.py`` and
``test_shell_sidebar_and_areas.py``: live compose Postgres,
``ASGITransport``, sentinel-tenant seed.
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


_AREA_LABELS: tuple[str, ...] = (
    "Front Office",
    "Back Office",
    "Admin",
    "Investor Communication",
    "Assistants",
)


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB auth-surface-layout tests.",
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
    email = "auth-layout@example.com"
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


async def _login(client: AsyncClient, email: str, password: str) -> None:
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# /login renders without the area sidebar
# ---------------------------------------------------------------------------


async def test_web_login_page_has_no_sidebar(web_client: AsyncClient) -> None:
    """GET /login must not emit any sidebar marker.

    The area sidebar is gated behind authentication. The pre-session
    surface should not expose it — neither its wrapper id nor any
    area label.
    """
    response = await web_client.get("/login")
    assert response.status_code == 200
    body = response.text

    assert 'id="pf-sidebar"' not in body
    assert "pf-sidebar__item" not in body
    assert "pf-shell" not in body
    for label in _AREA_LABELS:
        assert label not in body, f"Login page leaks area label {label!r}; sidebar must be hidden."


async def test_web_login_page_uses_auth_base(web_client: AsyncClient) -> None:
    """The login response carries the auth-base layout markers."""
    response = await web_client.get("/login")
    assert response.status_code == 200
    body = response.text

    # The auth base sets ``data-surface="auth"`` on <html> and wraps
    # its content in ``pf-auth-shell``. Either token is a reliable
    # distinguisher from ``base.html`` (which sets neither).
    assert 'data-surface="auth"' in body
    assert "pf-auth-shell" in body
    # And the canonical PortfoliFLOW logo is rendered via the shared
    # partial — the ``<img>`` element points at the canonical PNG.
    assert 'src="/static/img/portfoliflow-logo.png"' in body
    assert "pf-logo__img" in body


# ---------------------------------------------------------------------------
# Authenticated surfaces still carry the sidebar
# ---------------------------------------------------------------------------


async def test_web_authenticated_pages_have_sidebar(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Regression guard: authenticated area pages keep the sidebar.

    The split into ``_auth_base.html`` / ``base.html`` must not
    accidentally take the sidebar away from authenticated surfaces.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/front-office", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert 'id="pf-sidebar"' in body
    assert "pf-shell" in body
    # And the auth-shell wrapper is absent — these are different layouts.
    assert "pf-auth-shell" not in body
