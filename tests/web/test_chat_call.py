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
* **The call chain resolves end to end.** With the switch on, the key comes
  from the tenant's vault row and from nowhere else — a ``VOICE_CALL_API_KEY``
  variable supplies nothing — the defaults of ``services.voice_call`` fill
  the empty fields, the persona voice ``voice_tts.voice`` stands in for an
  empty call voice, and an incomplete call fails naming the card, not
  ``.env`` (ADR-0137 §7).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories import ScopedSettingRepository, tenant_context
from core.tenant_constants import SENTINEL_TENANT_ID
from services.credential_vault import MASTER_KEY_ENV_VAR, VaultCipher
from services.investments.credential_resolver import CredentialResolver
from services.voice_call import (
    DEFAULT_BASE_URL,
    DEFAULT_CONVERSATION_MODEL,
    DEFAULT_LANGUAGE,
    CallConfigurationError,
    ResolvedCall,
)
from web.routes import chat as chat_routes

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_MASTER_KEY = Fernet.generate_key().decode()
_CALL_KEY = "xi-call-tenant"


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
    # The persona-voice fallback reads ``voice_tts.voice``, which has an env link.
    monkeypatch.delenv("VOICE_TTS_VOICE", raising=False)


@pytest.fixture
def vault_key(monkeypatch: pytest.MonkeyPatch) -> VaultCipher:
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, _MASTER_KEY)
    return VaultCipher(_MASTER_KEY)


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


async def _write_setting(
    engine: AsyncEngine,
    *,
    provider: str,
    key: str,
    value: str,
    cipher: VaultCipher | None = None,
) -> None:
    """Write one tenant-scope row exactly as the admin surface would."""
    async with tenant_context(engine, SENTINEL_TENANT_ID) as session:
        repo = ScopedSettingRepository(session)
        if cipher is not None:
            await repo.upsert(
                scope="tenant",
                provider=provider,
                key=key,
                is_secret=True,
                value_ciphertext=cipher.encrypt(value),
                secret_hint=value[-4:],
            )
        else:
            await repo.upsert(
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


async def _call(engine: AsyncEngine) -> ResolvedCall | None:
    """Resolve the call chain the way a call start does, inside the tenant's session."""
    async with tenant_context(engine, SENTINEL_TENANT_ID) as db:
        return await chat_routes._call_values(
            CredentialResolver(session=db), tenant_id=SENTINEL_TENANT_ID
        )


async def _enable_with_key(engine: AsyncEngine, cipher: VaultCipher) -> None:
    """Switch the call on and store its key in the tenant's vault row."""
    await _write_setting(engine, provider="voice_call", key="enabled", value="true")
    await _write_setting(
        engine, provider="voice_call", key="api_key", value=_CALL_KEY, cipher=cipher
    )


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


# ---------------------------------------------------------------------------
# The call chain (ADR-0137 §7)
# ---------------------------------------------------------------------------


async def test_call_values_are_none_when_off_whatever_else_is_set(
    app_engine: AsyncEngine, vault_key: VaultCipher
) -> None:
    """Switched off: inactive, even with a key and every config row stored."""
    await _write_setting(app_engine, provider="voice_call", key="enabled", value="false")
    await _write_setting(
        app_engine, provider="voice_call", key="api_key", value=_CALL_KEY, cipher=vault_key
    )
    for key, value in (("voice", "v1"), ("model", "m-x"), ("agent_id", "agent_x")):
        await _write_setting(app_engine, provider="voice_call", key=key, value=value)

    assert await _call(app_engine) is None


async def test_call_values_fill_the_package_defaults(
    app_engine: AsyncEngine, vault_key: VaultCipher
) -> None:
    """On, a key and a voice, nothing else: every default from ``services.voice_call``."""
    await _enable_with_key(app_engine, vault_key)
    await _write_setting(app_engine, provider="voice_call", key="voice", value="v1")

    call = await _call(app_engine)

    assert call == ResolvedCall(
        api_key=_CALL_KEY,
        agent_id=None,
        model=DEFAULT_CONVERSATION_MODEL,
        voice="v1",
        language=DEFAULT_LANGUAGE,
        base_url=DEFAULT_BASE_URL,
    )


async def test_call_values_fall_back_to_the_persona_voice(
    app_engine: AsyncEngine, vault_key: VaultCipher
) -> None:
    """No call voice: the tenant's ``voice_tts.voice`` row is the call's voice."""
    await _enable_with_key(app_engine, vault_key)
    await _write_setting(app_engine, provider="voice_tts", key="voice", value="v_tts")

    call = await _call(app_engine)

    assert call is not None
    assert call.voice == "v_tts"


async def test_call_values_without_a_key_name_the_card(
    app_engine: AsyncEngine, vault_key: VaultCipher
) -> None:
    """On without a key: a configuration error naming the card, never ``.env``."""
    await _write_setting(app_engine, provider="voice_call", key="enabled", value="true")
    await _write_setting(app_engine, provider="voice_call", key="voice", value="v1")

    with pytest.raises(CallConfigurationError) as excinfo:
        await _call(app_engine)

    assert "Voice call" in str(excinfo.value)
    assert ".env" not in str(excinfo.value)


async def test_call_values_ignore_a_key_in_the_environment(
    app_engine: AsyncEngine,
    vault_key: VaultCipher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``VOICE_CALL_API_KEY`` supplies nothing: there is no env link, end to end."""
    monkeypatch.setenv("VOICE_CALL_API_KEY", "xi-call-env")
    await _write_setting(app_engine, provider="voice_call", key="enabled", value="true")
    await _write_setting(app_engine, provider="voice_call", key="voice", value="v1")

    with pytest.raises(CallConfigurationError):
        await _call(app_engine)


async def test_call_values_read_the_tenant_rows(
    app_engine: AsyncEngine, vault_key: VaultCipher
) -> None:
    """Agent id, model and base URL come from their rows; the trailing slash is dropped."""
    await _enable_with_key(app_engine, vault_key)
    for key, value in (
        ("voice", "v1"),
        ("agent_id", "agent_x"),
        ("model", "m-x"),
        ("base_url", "https://eu.invalid/"),
    ):
        await _write_setting(app_engine, provider="voice_call", key=key, value=value)

    call = await _call(app_engine)

    assert call is not None
    assert (call.agent_id, call.model, call.base_url) == ("agent_x", "m-x", "https://eu.invalid")
