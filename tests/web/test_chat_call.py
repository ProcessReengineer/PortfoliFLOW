# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The voice call surface on the web (ADR-0137).

The call twin of the gating section of ``test_chat_voice_resolution.py``,
and built the same way: live Postgres, real vault rows written through
``ScopedSettingRepository``, a real :class:`CredentialResolver` over the
tenant session.

What is pinned here:

* **The switch is the tenant row, and only the tenant row.** The
  ``voice_call`` declaration has no environment link (ADR-0137 §7), so a
  ``VOICE_CALL_ENABLED`` variable switches nothing on; the row's ``true``
  (case-insensitive) is on and every other value is off.
* **The switch alone offers the call.** No key is probed to decide whether
  the call is offered — the ADR-0118 §5 posture the voice controls already
  follow; a missing key is judged when a call is started (ADR-0137 §4).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories import ScopedSettingRepository, tenant_context
from core.tenant_constants import SENTINEL_TENANT_ID
from services.investments.credential_resolver import CredentialResolver
from web.routes import chat as chat_routes

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB voice-call tests.",
            allow_module_level=False,
        )


@pytest.fixture(autouse=True)
def silent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the variables a ``voice_call`` environment link would read, were there one."""
    monkeypatch.delenv("VOICE_CALL_ENABLED", raising=False)
    monkeypatch.delenv("VOICE_CALL_API_KEY", raising=False)


@pytest_asyncio.fixture
async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    assert DATABASE_URL_SUPERUSER is not None
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_schema(fresh_superuser_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    truncate_sql = text(
        "TRUNCATE TABLE scoped_settings, data_upload_sheets, data_uploads, "
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
async def seeded_tenant(fresh_superuser_engine: AsyncEngine, reset_schema: None) -> None:
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )


@pytest_asyncio.fixture
async def app_engine(seeded_tenant: None) -> AsyncGenerator[AsyncEngine, None]:
    """The application-role engine, so row-level security applies as in a request."""
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _write_setting(engine: AsyncEngine, *, provider: str, key: str, value: str) -> None:
    """Write one tenant-scope config row exactly as the admin surface would."""
    async with tenant_context(engine, SENTINEL_TENANT_ID) as session:
        await ScopedSettingRepository(session).upsert(
            scope="tenant",
            provider=provider,
            key=key,
            is_secret=False,
            value_plain=value,
        )


async def _switch(engine: AsyncEngine) -> bool:
    """Read the call switch the way the dock does, inside the tenant's session."""
    async with tenant_context(engine, SENTINEL_TENANT_ID) as db:
        return await chat_routes._call_switch(CredentialResolver(session=db))


# ---------------------------------------------------------------------------
# The switch (ADR-0137 §4, §7)
# ---------------------------------------------------------------------------


async def test_call_switch_is_off_with_no_row_even_when_the_environment_says_on(
    app_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No tenant row: off, whatever the environment holds — there is no env link."""
    monkeypatch.setenv("VOICE_CALL_ENABLED", "true")

    assert await _switch(app_engine) is False


async def test_call_switch_follows_the_tenant_row(app_engine: AsyncEngine) -> None:
    """``true`` in any case is on; anything else is off; an upsert applies at the next read."""
    for value, expected in (("true", True), ("TRUE", True), ("yes", False), ("false", False)):
        await _write_setting(app_engine, provider="voice_call", key="enabled", value=value)
        assert await _switch(app_engine) is expected, value


async def test_call_switch_offers_the_call_without_a_key(app_engine: AsyncEngine) -> None:
    """The switch alone offers the call; no ``api_key`` row is stored."""
    await _write_setting(app_engine, provider="voice_call", key="enabled", value="true")

    # The key is judged when a call is started, not here (ADR-0118 §5 posture).
    assert await _switch(app_engine) is True
