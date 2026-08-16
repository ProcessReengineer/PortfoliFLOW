# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the bottom status bar (Phase 6 Block 1, 6F-1 polish loop).

ADR-0046 § Status bar specifies a fixed-height bar at the bottom of
the shell grid that carries the active area name, the tenant name,
the Cmd+K palette hint, the build SHA, and a config-status flag.

Coverage:
* The bar renders on every authenticated area page.
* Direct navigation contains the active area label and the tenant.
* The auth surface (login page) does NOT render this bar (auth uses
  a separate minimal status footer in ``_auth_base.html``).
* HTMX area switches receive an out-of-band status-bar fragment so
  the active area updates without a full page reload.
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
from web.shell import all_areas

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


# Derived from ``web.shell`` — the single source of truth for the area
# catalogue (the ``test_section_navigation.py`` idiom). Each row is
# ``(slug, url, label)``; these tests assert that the RENDERED status bar of
# every catalogue area carries its own label and the tenant name. The
# deliberate sidebar-order pin lives once, as the glyph-sequence assert in
# ``tests/web/test_sidebar_glyph_and_auth_polish.py``.
_AREAS: tuple[tuple[str, str, str], ...] = tuple(
    (area.slug, area.url, area.label) for area in all_areas()
)


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB status-bar tests.",
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
    email = "statusbar@example.com"
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
# Renders on every area
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,url,_label", _AREAS)
async def test_web_status_bar_renders_on_each_area(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    slug: str,
    url: str,
    _label: str,
) -> None:
    """``id="pf-statusbar"`` is present on every area page."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert 'id="pf-statusbar"' in body
    assert "pf-statusbar" in body


@pytest.mark.parametrize("slug,url,label", _AREAS)
async def test_web_status_bar_contains_area_name(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    slug: str,
    url: str,
    label: str,
) -> None:
    """The active area's display label appears in the status bar."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    bar_start = body.index('id="pf-statusbar"')
    bar_end = body.index("</footer>", bar_start)
    bar = body[bar_start:bar_end]
    assert label in bar, f"area label {label!r} missing from status bar"


@pytest.mark.parametrize("slug,url,_label", _AREAS)
async def test_web_status_bar_contains_tenant_name(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    slug: str,
    url: str,
    _label: str,
) -> None:
    """The tenant name appears in the status bar's left group."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    bar_start = body.index('id="pf-statusbar"')
    bar_end = body.index("</footer>", bar_start)
    bar = body[bar_start:bar_end]
    assert "Sentinel Tenant" in bar


# ---------------------------------------------------------------------------
# Auth surface does NOT render this bar
# ---------------------------------------------------------------------------


async def test_web_status_bar_does_not_render_on_login(
    web_client: AsyncClient,
) -> None:
    """``GET /login`` uses ``_auth_base.html`` and skips the shell bar.

    The auth-surface footer is a different element (``pf-auth__statusbar``)
    that does not carry ``id="pf-statusbar"`` and does not include the
    area / tenant / palette-hint groups. This regression guards the
    layout split.
    """
    response = await web_client.get("/login")
    assert response.status_code == 200
    body = response.text
    assert 'id="pf-statusbar"' not in body
    assert "pf-statusbar__area" not in body
    assert "pf-statusbar__shortcut" not in body
    # Tenant name must not leak onto the unauthenticated surface either.
    assert "Sentinel Tenant" not in body


# ---------------------------------------------------------------------------
# HTMX area switches update the status bar via OOB swap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,url,label", _AREAS)
async def test_web_htmx_area_swap_includes_oob_statusbar(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    slug: str,
    url: str,
    label: str,
) -> None:
    """HTMX area requests carry an OOB status-bar fragment."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(url, headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    # The OOB status-bar element is in the response and points at the
    # canonical id.
    assert 'id="pf-statusbar"' in body
    bar_start = body.index('id="pf-statusbar"')
    bar_end = body.index("</footer>", bar_start)
    bar = body[bar_start:bar_end]
    assert 'hx-swap-oob="outerHTML"' in bar
    # The OOB fragment shows the new active-area label.
    assert label in bar
