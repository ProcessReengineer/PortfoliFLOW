# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the market-data schedule web surface (ADR-0093, #036 slice 5).

ASGI-level tests over a live Postgres, mirroring the fixture pattern in
``tests/web/test_watch_desk.py`` (login helper, superuser-seeded
tenant/user, session-CSRF read from the DB). They cover the deliberately
small surface: the enable/disable + cadence CRUD, and "Refresh now" setting
``next_due_at := now`` on an enabled schedule (and refusing on a disabled
one, without moving the cursor).

The cadence tests keep ``daily`` — still an offered choice — and add the
sub-hourly case ADR-0125 §2 introduced: an ``every_15m`` save must land on
the quarter-hour grid and must render its label, not the raw value.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from pathlib import Path
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
            "skipping live-DB market-data web tests.",
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


_TRUNCATE = text(
    "TRUNCATE TABLE market_data_schedule, login_audit, sessions, audit_log, "
    "users, tenants RESTART IDENTITY CASCADE"
)


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
async def seeded_user(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "md-owner@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital')"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, email, password_hash, roles, is_active) "
                "VALUES (:id, :tid, :email, :hash, "
                "ARRAY['owner']::text[], TRUE)"
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


async def _session_csrf(client: AsyncClient, engine: AsyncEngine) -> str:
    cookie = client.cookies.get("portfoliflow_session")
    assert cookie is not None, "not logged in"
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT csrf_token FROM sessions WHERE session_token = :t"),
                {"t": cookie},
            )
        ).first()
    assert row is not None
    return str(row.csrf_token)


async def _read_schedule(engine: AsyncEngine) -> tuple[bool, datetime] | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT enabled, next_due_at FROM market_data_schedule "
                    "WHERE tenant_id = :t AND user_id IS NULL"
                ),
                {"t": str(SENTINEL_TENANT_ID)},
            )
        ).first()
    if row is None:
        return None
    return bool(row.enabled), row.next_due_at


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_save_schedule_creates_enabled_row(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    _, email, pw = (None, "md-owner@example.com", "correct-horse-battery-staple")
    await _login(web_client, email, pw)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    resp = await web_client.post(
        "/api/market-data/schedule",
        data={
            "cadence": "daily",
            "preferred_hour": "6",
            "timezone": "Europe/Berlin",
            "enabled": "on",
            "csrf_token": csrf,
        },
    )
    assert resp.status_code == 200, resp.text
    assert "Schedule saved." in resp.text

    state = await _read_schedule(fresh_superuser_engine)
    assert state is not None
    enabled, _next_due = state
    assert enabled is True


async def test_disable_via_crud(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    # Enable, then disable (omit the checkbox field).
    await web_client.post(
        "/api/market-data/schedule",
        data={
            "cadence": "daily",
            "preferred_hour": "6",
            "timezone": "Europe/Berlin",
            "enabled": "on",
            "csrf_token": csrf,
        },
    )
    await web_client.post(
        "/api/market-data/schedule",
        data={
            "cadence": "daily",
            "preferred_hour": "6",
            "timezone": "Europe/Berlin",
            "csrf_token": csrf,
        },
    )
    state = await _read_schedule(fresh_superuser_engine)
    assert state is not None
    enabled, _ = state
    assert enabled is False


async def test_refresh_now_sets_due_now_when_enabled(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    # Enable with a far-future preferred hour so next_due_at is well ahead.
    await web_client.post(
        "/api/market-data/schedule",
        data={
            "cadence": "daily",
            "preferred_hour": "6",
            "timezone": "Europe/Berlin",
            "enabled": "on",
            "csrf_token": csrf,
        },
    )
    before = await _read_schedule(fresh_superuser_engine)
    assert before is not None and before[1] > datetime.now(timezone.utc)

    resp = await web_client.post("/api/market-data/refresh-now", data={"csrf_token": csrf})
    assert resp.status_code == 200, resp.text
    assert "queued" in resp.text.lower()

    after = await _read_schedule(fresh_superuser_engine)
    assert after is not None
    # next_due_at was pulled back to ~now (<= now), so the next tick claims it.
    assert after[1] <= datetime.now(timezone.utc)


async def test_refresh_now_disabled_does_not_move_cursor(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    # Save a DISABLED schedule (omit the enabled checkbox).
    await web_client.post(
        "/api/market-data/schedule",
        data={
            "cadence": "daily",
            "preferred_hour": "6",
            "timezone": "Europe/Berlin",
            "csrf_token": csrf,
        },
    )
    before = await _read_schedule(fresh_superuser_engine)
    assert before is not None

    resp = await web_client.post("/api/market-data/refresh-now", data={"csrf_token": csrf})
    assert resp.status_code == 200, resp.text
    assert "enable the schedule first" in resp.text.lower()

    after = await _read_schedule(fresh_superuser_engine)
    assert after is not None
    # A disabled schedule's cursor is untouched.
    assert after[1] == before[1]


async def test_save_schedule_every_15m_lands_on_the_quarter_hour_grid(
    web_client: AsyncClient, fresh_superuser_engine: AsyncEngine
) -> None:
    """A sub-hourly save round-trips through the shared cadence arithmetic.

    ADR-0125 §2 adds ``every_15m`` to what this panel offers; §1 states the
    quarter-hour grid is a *property* of the existing ``anchor + k·step``
    arithmetic rather than a new rule. Asserting the persisted
    ``next_due_at`` (rather than only a 200) is what proves the route feeds
    the new vocabulary through ``compute_next_due_at`` unchanged.

    The render assertions cover the other half of §2: the label map exists
    and the template uses it — a ``|capitalize`` would emit "Every_15m".
    """
    await _login(web_client, "md-owner@example.com", "correct-horse-battery-staple")
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    resp = await web_client.post(
        "/api/market-data/schedule",
        data={
            "cadence": "every_15m",
            "preferred_hour": "0",
            "timezone": "Europe/Berlin",
            "enabled": "on",
            "csrf_token": csrf,
        },
    )
    assert resp.status_code == 200, resp.text
    assert "Schedule saved." in resp.text
    assert "Every 15 minutes" in resp.text, "the cadence label must be rendered, not the raw value."
    assert "Refresh interval" in resp.text, "the panel caption is 'Refresh interval' (ADR-0125 §2)."
    assert "Every_15m" not in resp.text, "a |capitalize fallback would betray a missing label map."

    state = await _read_schedule(fresh_superuser_engine)
    assert state is not None
    enabled, next_due = state
    assert enabled is True
    assert next_due.minute in (0, 15, 30, 45), (
        "an every_15m schedule is due on the quarter-hour grid measured from "
        "the full hour (ADR-0125 §1)."
    )
    assert next_due.second == 0
