# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the canonical PortfoliFLOW logo partial.

The brand mark is the canonical PortfoliFLOW PNG asset shipped under
``web/static/img/portfoliflow-logo.png``. The partial renders a single
``<img>`` element so the wordmark stays pixel-perfect across themes.
Both the auth surface (login page) and the authenticated shell sidebar
share the same partial.
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
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "web" / "templates"


def _render_logo_partial(variant: str | None = None) -> str:
    """Render ``_partials/logo.html`` in isolation for unit assertions."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("_partials/logo.html")
    context: dict[str, str] = {}
    if variant is not None:
        context["variant"] = variant
    return template.render(**context)


def test_web_logo_partial_renders_img() -> None:
    """The default partial renders the canonical PNG via an ``<img>`` tag."""
    body = _render_logo_partial()
    assert "<img" in body
    assert 'src="/static/img/portfoliflow-logo.png"' in body
    assert 'alt="PortfoliFLOW"' in body
    assert "pf-logo__img" in body
    assert "pf-logo__img--full" in body


def test_web_logo_partial_mark_only_uses_variant_class() -> None:
    """``variant='mark-only'`` reuses the same image with a variant class."""
    body = _render_logo_partial(variant="mark-only")
    assert "<img" in body
    assert 'src="/static/img/portfoliflow-logo.png"' in body
    assert "pf-logo__img--mark-only" in body
    assert "pf-logo__img--full" not in body


# ---------------------------------------------------------------------------
# End-to-end: login page and area page render the canonical logo
# ---------------------------------------------------------------------------


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; skipping live-DB logo tests.",
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
    email = "logo@example.com"
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


async def test_web_login_page_uses_canonical_logo(
    web_client: AsyncClient,
) -> None:
    """``GET /login`` includes the PortfoliFLOW PNG, not a placeholder square."""
    response = await web_client.get("/login")
    assert response.status_code == 200
    body = response.text
    assert 'src="/static/img/portfoliflow-logo.png"' in body
    assert 'alt="PortfoliFLOW"' in body
    assert "pf-logo__img" in body


async def test_web_area_page_uses_canonical_logo(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An authenticated area page renders the PortfoliFLOW PNG in the sidebar brand."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/front-office", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert 'src="/static/img/portfoliflow-logo.png"' in body
    assert 'alt="PortfoliFLOW"' in body
    assert "pf-logo__img--full" in body
