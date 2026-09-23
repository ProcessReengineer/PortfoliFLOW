# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for Shirley as a shell element — rail, dock and stage (P-UX-A0e).

ASGI-level tests over a live Postgres. What they pin:

* **The hosts are shell furniture.** Every Area page carries the rail,
  one dock host and one stage host, all outside ``#shell-main`` — and
  the HTMX area fragment carries none of them. That is the whole reason
  the conversation (and a running SSE stream) survives navigating from
  one Area to another; ``shirley.js`` only ever *moves* the one
  instance between the two hosts.
* **The conversation is a fragment now.** ``GET /chat/dock`` renders it
  once per page life, on the first open, with the anchor ids
  ``chat-history`` / ``chat-form`` / ``chat-input`` each appearing
  exactly once and no ``Model: …`` line (record §2.10.3 demoted it).
* **The case marker still lands.** ``GET /assistants?case=<open>``
  arrives with the dock open, and the banner it promises renders in the
  dock fragment that follows — the marker sets the session stash on the
  area page, ``/chat/dock`` reads it.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories._session import tenant_context
from core.repositories.case_repository import CaseRepository
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
            "skipping live-DB Shirley-dock tests.",
            allow_module_level=False,
        )


class _StubCore:
    """Minimal :class:`AIServiceCore` stand-in — the shell reads nothing."""

    def get_model(self) -> str:
        return "fake/model"

    def get_status(self) -> ConnectionStatus:
        return ConnectionStatus.CONNECTED


_TRUNCATE = text(
    "TRUNCATE TABLE case_attachments, case_entries, cases, "
    "irene_finding, irene_schedule, irene_watch_state, "
    "login_audit, sessions, audit_log, "
    "data_store_entries, users, tenants RESTART IDENTITY CASCADE"
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
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(_TRUNCATE)
    try:
        yield
    finally:
        async with fresh_superuser_engine.begin() as conn:
            await conn.execute(_TRUNCATE)


@pytest_asyncio.fixture
async def seeded_owner(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    """Seed the primary tenant and one owner."""
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "dock-owner@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital')"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, roles, is_active)
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
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """App-role engine for seeding cases through the repository under RLS."""
    _require_db()
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client_factory(seeded_owner: tuple[UUID, str, str]):
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
        app.state.ai_core = _StubCore()
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


async def _seed_open_case(
    app_engine: AsyncEngine, *, opened_by: UUID, title: str = "Dock target"
) -> tuple[UUID, int]:
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        case = await CaseRepository(session).create(
            title=title,
            opened_by=opened_by,
            opened_actor="pm",
            opened_payload=None,
            now=datetime.now(UTC),
        )
        return case.id, case.case_number


# ---------------------------------------------------------------------------
# 1 / 2 — the hosts are shell furniture, outside the area swap
# ---------------------------------------------------------------------------


async def test_every_area_page_carries_the_rail_and_the_two_hosts(
    client_factory: Any,
    seeded_owner: tuple[UUID, str, str],
) -> None:
    """A non-Assistants Area still reaches Shirley — and carries no chat."""
    _id, email, password = seeded_owner
    client, _app = await client_factory()
    await _login(client, email, password)

    response = await client.get("/front-office", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    assert body.count('id="dock-chat-host"') == 1
    assert body.count('id="stage-chat-host"') == 1
    assert body.count('id="pf-side"') == 1
    assert 'data-shirley="closed"' in body
    assert 'title="Open Shirley  Ctrl J"' in body

    # Closed means closed: the conversation is not in the page at all
    # until the reader opens the dock.
    assert 'id="chat-form"' not in body


async def test_the_area_fragment_carries_none_of_the_hosts(
    client_factory: Any,
    seeded_owner: tuple[UUID, str, str],
) -> None:
    """The HTMX swap targets ``#shell-main``; the hosts live outside it."""
    _id, email, password = seeded_owner
    client, _app = await client_factory()
    await _login(client, email, password)

    response = await client.get(
        "/front-office",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body
    assert 'id="dock-chat-host"' not in body
    assert 'id="stage-chat-host"' not in body
    assert 'id="pf-side"' not in body


# ---------------------------------------------------------------------------
# 3 — GET /chat/dock
# ---------------------------------------------------------------------------


async def test_chat_dock_renders_the_one_conversation_as_a_fragment(
    client_factory: Any,
    seeded_owner: tuple[UUID, str, str],
) -> None:
    """One of each anchor id, the new head, no shell and no model line."""
    _id, email, password = seeded_owner
    client, _app = await client_factory()
    await _login(client, email, password)

    response = await client.get("/chat/dock", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    assert "<html" not in body
    assert body.count('id="pf-chat"') == 1
    assert body.count('id="chat-history"') == 1
    assert body.count('id="chat-form"') == 1
    assert body.count('id="chat-input"') == 1

    # The head, with the three controls.
    assert "New chat" in body
    assert 'aria-label="Open on stage"' in body
    assert 'title="Close  Ctrl J"' in body
    assert "data-toggle-stage" in body

    # Demoted by record §2.10.3 — its home is Providers & Credentials.
    assert "Model: " not in body
    assert "chat-embed__model" not in body


async def test_chat_dock_is_session_gated_like_chat_history(
    client_factory: Any,
    seeded_owner: tuple[UUID, str, str],
) -> None:
    """Unauthenticated: 303 to /login, or 401 + ``HX-Redirect`` for HTMX."""
    _id, _email, _password = seeded_owner
    client, _app = await client_factory()

    plain = await client.get("/chat/dock", follow_redirects=False)
    assert plain.status_code == 303
    assert plain.headers["location"] == "/login"

    htmx = await client.get(
        "/chat/dock",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert htmx.status_code == 401
    assert htmx.headers["HX-Redirect"] == "/login"


# ---------------------------------------------------------------------------
# 4 — the case marker arrives docked, and the banner follows
# ---------------------------------------------------------------------------


async def test_a_case_marker_opens_the_dock_and_banners_in_it(
    client_factory: Any,
    seeded_owner: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """ "Consult Shirley" lands on the dock, open, with the case named."""
    actor_id, email, password = seeded_owner
    case_id, case_number = await _seed_open_case(app_engine, opened_by=actor_id, title="Alpha")

    client, _app = await client_factory()
    await _login(client, email, password)

    page = await client.get(f"/assistants?case={case_id}", follow_redirects=False)
    assert page.status_code == 200
    assert 'data-shirley="docked"' in page.text
    # The banner is not in the area page — it is chat furniture now.
    assert "Consulting for" not in page.text

    dock = await client.get("/chat/dock", follow_redirects=False)
    assert dock.status_code == 200
    assert "Consulting for" in dock.text
    assert f"CASE-{case_number:04d}" in dock.text
    assert "Alpha" in dock.text


async def test_assistants_without_a_marker_stays_closed(
    client_factory: Any,
    seeded_owner: tuple[UUID, str, str],
) -> None:
    """No marker, no brief: the Assistants page opens like any other."""
    _id, email, password = seeded_owner
    client, _app = await client_factory()
    await _login(client, email, password)

    page = await client.get("/assistants", follow_redirects=False)
    assert page.status_code == 200
    assert 'data-shirley="closed"' in page.text
