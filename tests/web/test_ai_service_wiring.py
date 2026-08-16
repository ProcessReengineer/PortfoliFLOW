# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the FastAPI AIService wiring (sub-stream 3a, Task 1).

The PyQt6 GUI configures Shirley through ``QSettings`` via the AI
Settings widget; the web variant has no settings UI yet, so the
FastAPI lifespan reads ``OPENROUTER_API_KEY``, ``OPENROUTER_BASE_URL``,
and ``SHIRLEY_MODEL`` from ``.env`` and configures the
:class:`AIServiceCore` singleton at startup.

These tests exercise three contracts:

1. The lifespan configures the core when credentials are supplied.
2. The lifespan resets the core when credentials are missing — so a
   prior CONNECTED state cannot leak into a no-credentials app instance.
3. ``POST /chat/messages`` returns 503 with a clear pointer to ``.env``
   when the core is not configured.

The tests do not exercise a real OpenRouter endpoint — that is what
the manual smoke test from the sub-stream acceptance criteria covers.
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
from services.ai_models import ConnectionStatus
from services.ai_service_core import get_ai_service_core
from services.credential_vault import MASTER_KEY_ENV_VAR
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
            "skipping live-DB AI-wiring tests.",
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
    """Truncate before AND after each test.

    The trailing TRUNCATE prevents the user row from leaking into the
    dev database after the last test in this module — see sub-stream
    3a, Task 2 (Phase-3 test-hygiene fix).
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
    """Insert sentinel tenant + a user for chat-endpoint tests."""
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "ai-wire@example.com"
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


def _build_settings(
    *,
    api_key: str | None,
    model: str | None = "anthropic/claude-haiku-4.5",
    base_url: str = "https://openrouter.ai/api/v1",
) -> WebSettings:
    return WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        csrf_cookie_name="portfoliflow_csrf_pre_session",
        database_url=DATABASE_URL,
        database_url_superuser=DATABASE_URL_SUPERUSER,
        session_cookie_secure=False,
        openrouter_base_url=base_url,
        openrouter_api_key=api_key,
        shirley_model=model,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_lifespan_configures_core_when_api_key_set(
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A configured key + model leaves the core CONNECTED with the model set."""
    settings = _build_settings(
        api_key="sk-or-v1-test-key-not-real",
        model="anthropic/claude-haiku-4.5",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        core = app.state.ai_core
        assert core is not None
        assert core.get_status() == ConnectionStatus.CONNECTED
        assert core.get_model() == "anthropic/claude-haiku-4.5"


async def test_lifespan_resets_core_when_api_key_missing(
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An unset key leaves the core DISCONNECTED — even if a prior
    instance had configured the singleton in the same process.
    """
    # Pre-pollute: configure the singleton as if a prior app had set
    # it up. The lifespan must reset this back to a clean state.
    pre = get_ai_service_core()
    pre.configure("https://example.invalid/api/v1", "stale-key")
    pre.set_model("stale/model")
    pre.set_status(ConnectionStatus.CONNECTED)

    settings = _build_settings(api_key=None)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        core = app.state.ai_core
        assert core is not None
        assert core.get_status() == ConnectionStatus.DISCONNECTED
        assert core.get_model() == ""


async def test_post_messages_returns_503_when_nothing_resolves(
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat endpoint surfaces "no LLM for this tenant" as 503.

    Since ADR-0112 §4b the gate is no longer the singleton's connection
    status but the *resolution*: the turn 503s only when no scope can serve
    it. So the environment key has to be cleared too — a stale
    ``OPENROUTER_API_KEY`` in the process environment would legitimately
    serve the turn even with ``WebSettings.openrouter_api_key`` unset.
    """
    _id, email, password = seeded_user

    # No application-scope key, and no vault for tenant rows to live in.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)

    settings = _build_settings(api_key=None)
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://testserver") as client,
        app.router.lifespan_context(app),
    ):
        # Log in so CSRF + session are minted; the 503 must fire
        # despite a valid session, on the basis of core state alone.
        get_response = await client.get("/login")
        csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
        assert csrf is not None
        await client.post(
            "/login",
            data={
                "email": email,
                "password": password,
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
        # ADR-0051: the standalone /chat page was retired; the
        # embedded Shirley section on /assistants now carries the
        # session CSRF token in its composer form.
        page = await client.get("/assistants", follow_redirects=False)
        assert page.status_code == 200
        import re

        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
        assert match is not None
        session_csrf = match.group(1)

        response = await client.post(
            "/chat/messages",
            data={"message": "hello", "csrf_token": session_csrf},
            follow_redirects=False,
        )
        assert response.status_code == 503
        # Both scopes an operator can fix it in are named...
        assert "Providers &amp; Credentials" in response.text or (
            "Providers & Credentials" in response.text
        )
        assert "OPENROUTER_API_KEY" in response.text
        assert ".env" in response.text
        # ...and the retired restart instruction is gone: tenant and user
        # rows apply on the next turn, with no restart (ADR-0112 §4b).
        assert "restart" not in response.text.lower()
