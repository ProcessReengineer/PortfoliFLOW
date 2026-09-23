# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Assistants area surface after Shirley left it.

ADR-0051 folded the standalone ``GET /chat`` page into the Assistants
area's ``shirley`` section; P-UX-A0e moved the conversation out again,
into the shell's Shirley column, where every Area reaches it. What this
module pins is what is left behind:

* ``GET /assistants`` renders the ``shirley`` Section as a **pointer** —
  prose plus "Open on stage" — and carries none of the chat's anchor ids.
* The HTMX request branch (``HX-Request: true``) returns the area body
  fragment (no ``<html>`` wrapper) and carries neither the chat nor the
  shell's Shirley column: the hosts live outside ``#shell-main``.
* The Providers & Credentials tile still points at Admin.

The conversation's own render is pinned by ``test_shirley_dock.py``; the
"Model: …" status line has no assertions anywhere, because record §2.10.3
demoted it — the model a tenant runs is configuration, and its home is
Providers & Credentials.
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


async def test_assistants_shirley_section_is_the_pointer(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The section keeps its slug and heading, and points at the dock.

    The conversation is a shell element now (P-UX-A0e): none of its
    anchor ids may appear in the area body, or ``#chat-form`` and
    ``#chat-history`` would stop being unique the moment the dock loads.
    """
    _id, email, password = seeded_user
    client, _app = await web_client_factory(model="anthropic/claude-opus-4-7")
    await _login(client, email, password)

    response = await client.get("/assistants", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    # The section itself is unchanged — same slug, same heading.
    assert 'id="shirley"' in body
    assert ">Shirley<" in body or "Shirley</h2>" in body

    # Its body is a pointer: the prose and the stage button.
    assert "Shirley is in the dock on the right" in body
    assert 'data-set-shirley="stage"' in body
    assert "Open on stage" in body

    # And not the conversation.
    assert 'id="chat-form"' not in body
    assert 'id="chat-history"' not in body
    assert 'id="chat-input"' not in body


async def test_assistants_htmx_fragment_carries_neither_chat_nor_column(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The area swap touches ``#shell-main`` and nothing beside it.

    The rail, the dock host and the stage host live outside the swap
    target, which is exactly why a running conversation survives
    navigating to another Area — so the fragment must not carry them.
    """
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
    assert 'id="chat-history"' not in body
    assert 'id="chat-form"' not in body
    # ``id=``-qualified: ``pf-sidebar`` contains ``pf-side`` as a prefix.
    assert 'id="pf-side"' not in body
    assert 'id="dock-chat-host"' not in body
    assert 'id="stage-chat-host"' not in body


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
