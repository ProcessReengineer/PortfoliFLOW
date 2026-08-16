# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ADR-0051: ``GET /chat`` page route is retired; backend endpoints survive.

Two assertions:

* ``GET /chat`` returns 404 — the page handler is gone.
* ``POST /chat/messages``, ``POST /chat/new`` and ``GET /chat/history``
  still respond on a valid session — the backend wire surface that the
  embedded Shirley shell consumes from ``/assistants#shirley`` is
  unchanged.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.tenant_constants import SENTINEL_TENANT_ID
from services.ai_models import ConnectionStatus, Message, MessageRole
from services.ai_service_core import StreamEvent
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
            "skipping live-DB chat-page-removed tests.",
            allow_module_level=False,
        )


class _FakeCore:
    """Minimal scripted core — enough for the surviving routes."""

    def __init__(self) -> None:
        self._status = ConnectionStatus.CONNECTED

    def get_status(self) -> ConnectionStatus:
        return self._status

    def set_status(self, status: ConnectionStatus) -> None:
        self._status = status

    def get_model(self) -> str:
        return "fake/model"

    def get_system_prompt(self, prompt_name: str = "shirley") -> str:
        return ""

    async def stream_response(
        self,
        conversation: object,
        system_prompt: str = "",
        temperature: float = 0.7,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(
            "stream_finished",
            {
                "message": Message(role=MessageRole.ASSISTANT, content="ok", tool_calls=[]),
                "iterations": 0,
            },
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
    email = "retire@example.com"
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
async def web_client_factory(
    seeded_user: tuple[UUID, str, str],
):
    settings = WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        csrf_cookie_name="portfoliflow_csrf_pre_session",
        database_url=DATABASE_URL,
        database_url_superuser=DATABASE_URL_SUPERUSER,
        session_cookie_secure=False,
    )

    stack = AsyncExitStack()
    await stack.__aenter__()

    async def _make() -> tuple[AsyncClient, Any]:
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        app.state.ai_core = _FakeCore()
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://testserver")
        await stack.enter_async_context(client)
        return client, app

    try:
        yield _make
    finally:
        await stack.aclose()


async def _login_and_get_csrf(client: AsyncClient, email: str, password: str) -> str:
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    page = await client.get("/assistants", follow_redirects=False)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


async def test_get_chat_returns_404(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The standalone /chat page route was retired in ADR-0051."""
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    await _login_and_get_csrf(client, email, password)

    response = await client.get("/chat", follow_redirects=False)
    assert response.status_code == 404


async def test_post_chat_messages_still_responds(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The backend wire surface for the embedded shell is unchanged."""
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/messages",
        data={"message": "Hello Shirley", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert 'data-pf-sse-url="/chat/stream/' in response.text


async def test_post_chat_new_still_responds(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/new",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert "chat-empty" in response.text


async def test_get_chat_history_still_responds(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory()
    await _login_and_get_csrf(client, email, password)

    response = await client.get("/chat/history")
    assert response.status_code == 200
    # Empty history renders the placeholder.
    assert "chat-empty" in response.text
