# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Shirley-embedded Assistants area surface (ADR-0051).

Three assertions:

* ``GET /assistants`` renders the chat shell (composer form, history
  div, "New chat" button) inside the ``shirley`` section.
* ``model_id`` is rendered when the AI core has a model set, and the
  "Model: …" status line is omitted otherwise.
* The HTMX request branch (``HX-Request: true``) returns the area
  body fragment (no ``<html>`` wrapper) and still renders the
  embedded Shirley shell intact.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
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
from services.ai_models import ConnectionStatus
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
            "skipping live-DB assistants-embedding tests.",
            allow_module_level=False,
        )


class _StubCore:
    """Tiny stand-in for :class:`AIServiceCore` exposing only the
    accessors the area handler and the embedded section consume.
    """

    def __init__(self, model: str = "") -> None:
        self._model = model
        self._status = ConnectionStatus.CONNECTED

    def get_model(self) -> str:
        return self._model

    def get_status(self) -> ConnectionStatus:
        return self._status


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
    email = "embed@example.com"
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

    async def _make(*, model: str = "fake/model") -> tuple[AsyncClient, Any]:
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        app.state.ai_core = _StubCore(model=model)
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://testserver")
        await stack.enter_async_context(client)
        return client, app

    try:
        yield _make
    finally:
        await stack.aclose()


async def _login(client: AsyncClient, email: str, password: str) -> None:
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )


async def test_assistants_renders_chat_shell_inside_shirley_section(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory(model="anthropic/claude-opus-4-7")
    await _login(client, email, password)

    response = await client.get("/assistants", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    # The shirley section opens the chat shell.
    assert 'id="shirley"' in body
    # The composer form, history div, and "New chat" button live inside
    # the embedded shell — anchor ids unchanged so chat.js still wires.
    assert 'id="chat-history"' in body
    assert 'id="chat-form"' in body
    assert 'id="chat-input"' in body
    assert 'hx-post="/chat/messages"' in body
    assert 'hx-get="/chat/history"' in body
    assert "New chat" in body
    # Section heading is "Shirley" (no longer "Shirley (Chat)").
    assert ">Shirley<" in body or "Shirley</h2>" in body
    # The embedded shell's order: history pane, then controls row,
    # then composer (controls sit above the composer, not the history).
    history_pos = body.index('id="chat-history"')
    controls_pos = body.index("chat-controls__new")
    composer_pos = body.index("chat-composer__row")
    assert history_pos < controls_pos < composer_pos


async def test_assistants_renders_model_status_line_when_set(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory(model="anthropic/claude-opus-4-7")
    await _login(client, email, password)

    response = await client.get("/assistants", follow_redirects=False)
    body = response.text
    assert "Model: anthropic/claude-opus-4-7" in body


async def test_assistants_omits_model_line_when_unset(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory(model="")
    await _login(client, email, password)

    response = await client.get("/assistants", follow_redirects=False)
    body = response.text
    # When the core has no model set, the embedded shell skips the
    # "Model: …" status line entirely (no empty label leaks through).
    assert "chat-embed__model" not in body


async def test_assistants_htmx_request_returns_fragment_with_chat_shell(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, _app = await web_client_factory(model="fake/model")
    await _login(client, email, password)

    response = await client.get(
        "/assistants",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # HTMX path returns the area body fragment — no full <html> wrapper.
    assert "<html" not in body
    # The Shirley shell still renders inside the fragment.
    assert 'id="chat-history"' in body
    assert 'id="chat-form"' in body


async def test_assistants_provider_credentials_section_links_to_admin(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The pointer tile in Assistants points at the Admin surface.

    Retitled with the tile when ADR-0112 §6 (strand F3) replaced the
    ADR-0052 AI Settings surface with Providers & Credentials; the tile
    itself is unchanged in kind — still a "moved" pointer, never a
    second write surface.
    """
    _id, email, password = seeded_user
    client, _app = await web_client_factory(model="fake/model")
    await _login(client, email, password)

    response = await client.get("/assistants", follow_redirects=False)
    body = response.text
    # The Assistants providers-credentials tile redirects the operator
    # to the live surface under Admin.
    assert re.search(
        r'<a href="/admin#providers-credentials">[^<]*Admin[^<]*'
        r"Providers[^<]*Credentials[^<]*</a>",
        body,
    )
