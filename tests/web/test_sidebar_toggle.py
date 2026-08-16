# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the bidirectional sidebar collapse toggle (6F-1 polish).

The toggle button at the bottom of the sidebar must flip the
``pf_sidebar_collapsed`` cookie in BOTH directions. Existing tests in
``test_shell_sidebar_and_areas.py`` exercise a single flip; these
tests cover the bidirectional behaviour, the cookie's round-trip
across requests, and the form's ``redirect_to`` wiring (the prior
defect: the form fell back to ``"/"`` so every toggle bounced the
user to /front-office regardless of where they actually were).
"""

from __future__ import annotations

import os
import re
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
            "skipping live-DB sidebar-toggle tests.",
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
    email = "toggle@example.com"
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


def _csrf_from(body: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', body)
    assert match is not None, "CSRF token missing from rendered page"
    return match.group(1)


def _redirect_to_value(body: str) -> str:
    match = re.search(r'name="redirect_to"\s+value="([^"]+)"', body)
    assert match is not None, "redirect_to hidden input missing"
    return match.group(1)


# ---------------------------------------------------------------------------
# Bidirectional toggle
# ---------------------------------------------------------------------------


async def test_web_sidebar_toggle_bidirectional(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """One click collapses, a second click re-expands.

    The endpoint reads the current cookie value and writes the
    opposite, so the same form submission is the entire affordance
    in both directions.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    page = await web_client.get("/front-office", follow_redirects=False)
    assert 'data-sidebar-collapsed="false"' in page.text
    csrf = _csrf_from(page.text)

    # First toggle — collapses.
    first = await web_client.post(
        "/shell/sidebar/toggle",
        data={"csrf_token": csrf, "redirect_to": "/front-office"},
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert first.cookies.get("pf_sidebar_collapsed") == "true"

    after_first = await web_client.get("/front-office", follow_redirects=False)
    assert 'data-sidebar-collapsed="true"' in after_first.text

    # Second toggle — expands.
    second = await web_client.post(
        "/shell/sidebar/toggle",
        data={"csrf_token": csrf, "redirect_to": "/front-office"},
        follow_redirects=False,
    )
    assert second.status_code == 303
    assert second.cookies.get("pf_sidebar_collapsed") == "false"

    after_second = await web_client.get("/front-office", follow_redirects=False)
    assert 'data-sidebar-collapsed="false"' in after_second.text


async def test_web_sidebar_toggle_cookie_persists(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The cookie survives across requests in both states."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    page = await web_client.get("/front-office", follow_redirects=False)
    csrf = _csrf_from(page.text)

    # Collapse — flag persists across an unrelated GET.
    await web_client.post(
        "/shell/sidebar/toggle",
        data={"csrf_token": csrf, "redirect_to": "/front-office"},
        follow_redirects=False,
    )
    elsewhere = await web_client.get("/back-office", follow_redirects=False)
    assert 'data-sidebar-collapsed="true"' in elsewhere.text

    # Expand — same persistence on the way back.
    await web_client.post(
        "/shell/sidebar/toggle",
        data={"csrf_token": csrf, "redirect_to": "/back-office"},
        follow_redirects=False,
    )
    again = await web_client.get("/admin", follow_redirects=False)
    assert 'data-sidebar-collapsed="false"' in again.text


async def test_web_sidebar_toggle_five_clicks_stable(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Five toggles in a row alternate cleanly — no off-by-one or stuck state."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    page = await web_client.get("/front-office", follow_redirects=False)
    csrf = _csrf_from(page.text)

    expected = ("true", "false", "true", "false", "true")
    for click_index, want in enumerate(expected):
        response = await web_client.post(
            "/shell/sidebar/toggle",
            data={"csrf_token": csrf, "redirect_to": "/front-office"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.cookies.get("pf_sidebar_collapsed") == want, (
            f"click #{click_index + 1}: cookie should be {want!r}, "
            f"got {response.cookies.get('pf_sidebar_collapsed')!r}"
        )


# ---------------------------------------------------------------------------
# redirect_to wiring — the form must point back at the rendered page
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/front-office",
        "/back-office",
        "/admin",
        "/investor-communication",
        "/assistants",
    ],
)
async def test_web_sidebar_form_redirect_to_matches_current_url(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    url: str,
) -> None:
    """The collapse form posts back to the page the user is on.

    Before the polish loop, the shell context processor did not supply
    ``redirect_to``, so every collapse / expand bounced the user to
    /front-office (the / fallback in chat.py:root_redirect). Now the
    processor sets ``redirect_to`` to ``request.url.path`` so the
    affordance preserves the user's location.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(url, follow_redirects=False)
    assert response.status_code == 200
    assert _redirect_to_value(response.text) == url
