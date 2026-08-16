# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-turn voice resolution and per-tenant gating on the web surface (ADR-0118).

The voice twin of ``test_chat_llm_resolution.py``, and built the same way:
live Postgres, the real login flow, real vault rows written through
``ScopedSettingRepository`` — only ``build_provider`` is a double, and it is
one precisely so the test can read *what the request resolved*.

What is pinned here:

* **The rows drive the turn.** A tenant's ``voice_stt`` / ``voice_tts`` keys
  and its model / persona-voice rows reach the provider factory as the
  request's :class:`ResolvedVoice`, with **no restart** between the write and
  the recording.
* **Scope precedence.** A tenant row outranks the environment — the ADR-0112
  §1 chain, observed end to end.
* **Env-only degradation.** With no vault rows the resolution is exactly the
  ``VOICE_*`` environment, which is what the retired process-global singleton
  was built from: single-tenant deployments did not change.
* **Gating is a per-tenant answer.** A ``voice.enabled`` tenant row turns the
  endpoints and the Assistants affordances on with the environment silent,
  and their absence turns them off (ADR-0118 §5).
* **Enabled-but-keyless is loud.** Voice on with no credential anywhere
  answers 503 on both endpoints, naming Providers & Credentials *and*
  ``.env`` — the startup validation ``VoiceConfig.__post_init__`` used to do,
  relocated to first use (ADR-0118 §2).
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
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories import ScopedSettingRepository, tenant_context
from core.tenant_constants import SENTINEL_TENANT_ID
from services.ai_models import ConnectionStatus
from services.ai_service_core import StreamEvent
from services.credential_vault import MASTER_KEY_ENV_VAR, VaultCipher
from services.password_hashing import hash_password
from services.voice import (
    DEFAULT_STT_BASE_URL,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_VOICE,
    DEFAULT_VOICE_PROVIDER,
    ResolvedVoice,
)
from web.main import create_app
from web.routes import chat as chat_routes
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_MASTER_KEY = Fernet.generate_key().decode()

_AUDIO_BYTES = b"OggS" + b"\x00" * 32

#: Every environment variable the voice chains read. Pinned per test (set or
#: deleted) so the repository ``.env`` this module loads cannot decide an
#: outcome — it holds a fully enabled voice deployment on the maintainer's
#: machine, which would make several of these tests pass for the wrong reason.
_VOICE_ENV_VARS = (
    "VOICE_ENABLED",
    "VOICE_STT_PROVIDER",
    "VOICE_STT_MODEL",
    "VOICE_STT_API_KEY",
    "VOICE_STT_BASE_URL",
    "VOICE_TTS_PROVIDER",
    "VOICE_TTS_MODEL",
    "VOICE_TTS_VOICE",
    "VOICE_TTS_API_KEY",
)

#: The application scope, as a deployment's ``.env`` would provide it. Every
#: value is deliberately distinguishable from the code defaults, so a test
#: reading them back cannot confuse "resolved from the environment" with
#: "fell through to the constant".
_ENV_VOICE: dict[str, str] = {
    "VOICE_STT_PROVIDER": "openai",
    "VOICE_STT_MODEL": "env/stt-model",
    "VOICE_STT_API_KEY": "sk-env-stt",
    "VOICE_STT_BASE_URL": "https://env.example/stt/v1",
    "VOICE_TTS_PROVIDER": "openai",
    "VOICE_TTS_MODEL": "env/tts-model",
    "VOICE_TTS_VOICE": "env-voice",
    "VOICE_TTS_API_KEY": "sk-env-tts",
}

# A vision-capable model id, so the mixed-mode gate in ``post_voice`` is a
# non-issue; the LLM chain is not what this module tests.
_LLM_MODEL = "anthropic/claude-sonnet-4.5"


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB voice-resolution tests.",
            allow_module_level=False,
        )


class _FakeCore:
    """Enough of :class:`AIServiceCore` for ``post_voice`` to begin a turn."""

    def get_status(self) -> ConnectionStatus:
        return ConnectionStatus.CONNECTED

    def get_model(self) -> str:
        return _LLM_MODEL

    def get_system_prompt(self, prompt_name: str = "shirley") -> str:
        return ""

    async def stream_response(
        self,
        conversation: object,
        system_prompt: str = "",
        temperature: float = 0.7,
        tool_context: object = None,
        llm: object = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent("stream_finished", {"message": None, "iterations": 0})


class _FakeVoiceProvider:
    """Succeeds at everything; this module tests resolution, not I/O."""

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        return "how is my portfolio doing"

    async def synthesize(self, text: str, *, fmt: str) -> tuple[bytes, str]:
        return b"MP3BYTES", "audio/mpeg"


class _ProviderRecorder:
    """Stands in for :func:`build_provider` and keeps what it was handed."""

    def __init__(self) -> None:
        self.resolved: ResolvedVoice | None = None
        self.calls = 0

    def __call__(self, resolved: ResolvedVoice) -> _FakeVoiceProvider:
        self.resolved = resolved
        self.calls += 1
        return _FakeVoiceProvider()


@pytest.fixture(autouse=True)
def pinned_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every ``VOICE_*`` variable and pin the LLM chain.

    The LLM chain is pinned rather than cleared because ``post_voice``
    refuses before STT when *Shirley* is unconfigured (a 503 of its own), and
    a test about voice must not be reading that answer.
    """
    for var in _VOICE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-openrouter")
    monkeypatch.setenv("SHIRLEY_MODEL", _LLM_MODEL)


@pytest.fixture
def voice_build(monkeypatch: pytest.MonkeyPatch) -> _ProviderRecorder:
    """Replace ``web.routes.chat.build_provider`` with the recorder."""
    recorder = _ProviderRecorder()
    monkeypatch.setattr(chat_routes, "build_provider", recorder)
    return recorder


@pytest.fixture
def env_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole voice configuration in the environment, and nowhere else."""
    monkeypatch.setenv("VOICE_ENABLED", "true")
    for var, value in _ENV_VOICE.items():
        monkeypatch.setenv(var, value)


@pytest.fixture
def env_enabled_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Voice switched on in the environment, with no keys and no settings."""
    monkeypatch.setenv("VOICE_ENABLED", "true")


@pytest.fixture
def vault_key(monkeypatch: pytest.MonkeyPatch) -> VaultCipher:
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, _MASTER_KEY)
    return VaultCipher(_MASTER_KEY)


@pytest.fixture
def no_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)


@pytest_asyncio.fixture
async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
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
async def seeded_user(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "voice-resolution@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
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
async def app_client(seeded_user: tuple[UUID, str, str]) -> AsyncGenerator[Any, None]:
    """Yield ``(client, app)`` for a fully wired app with a fake AI core."""
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
    try:
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        app.state.ai_core = _FakeCore()
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://testserver")
        await stack.enter_async_context(client)
        yield client, app
    finally:
        await stack.aclose()


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


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """Log in and return the session CSRF token from the composer form."""
    pre = await client.get("/login")
    csrf = pre.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    page = await client.get("/assistants", follow_redirects=False)
    assert page.status_code == 200
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


async def _post_voice(client: AsyncClient, csrf: str) -> Any:
    return await client.post(
        "/chat/voice",
        data={"csrf_token": csrf},
        files={"audio": ("voice.webm", _AUDIO_BYTES, "audio/webm")},
        follow_redirects=False,
    )


async def _post_tts(client: AsyncClient, csrf: str) -> Any:
    return await client.post(
        "/chat/tts",
        data={"text": "Here is your answer.", "csrf_token": csrf},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# The rows drive the request
# ---------------------------------------------------------------------------


async def test_tenant_rows_drive_the_request_without_a_restart(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    vault_key: VaultCipher,
    env_enabled_only: None,
) -> None:
    """Vault rows written after startup reach the very next recording.

    The unset config fields fall through to the ``DEFAULT_*`` constants —
    the tails of the chains, and the reason no resolution site constructs a
    :class:`VoiceConfig` to learn them (ADR-0118 §4).
    """
    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    # Written *after* the app started — the point of per-request resolution.
    await _write_setting(
        app.state.engine,
        provider="voice_stt",
        key="api_key",
        value="sk-tenant-stt",
        cipher=vault_key,
    )
    await _write_setting(
        app.state.engine,
        provider="voice_tts",
        key="api_key",
        value="sk-tenant-tts",
        cipher=vault_key,
    )
    await _write_setting(
        app.state.engine, provider="voice_stt", key="model", value="tenant/stt-model"
    )
    await _write_setting(app.state.engine, provider="voice_tts", key="voice", value="tenant-voice")

    response = await _post_voice(client, csrf)

    assert response.status_code == 200
    resolved = voice_build.resolved
    assert resolved is not None
    assert resolved.stt_api_key == "sk-tenant-stt"
    assert resolved.tts_api_key == "sk-tenant-tts"
    assert resolved.stt_model == "tenant/stt-model"
    assert resolved.tts_voice == "tenant-voice"
    # Unset everywhere: the code defaults are the tails of these two chains.
    assert resolved.stt_base_url == DEFAULT_STT_BASE_URL
    assert resolved.tts_model == DEFAULT_TTS_MODEL
    # Undeclared by design (ADR-0118 §1) — env-only, defaulted here.
    assert resolved.stt_provider == DEFAULT_VOICE_PROVIDER
    assert resolved.tts_provider == DEFAULT_VOICE_PROVIDER


async def test_tenant_rows_outrank_the_environment(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    vault_key: VaultCipher,
    env_voice: None,
) -> None:
    """Keys and settings in both scopes → the tenant's win (ADR-0112 §1)."""
    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    await _write_setting(
        app.state.engine,
        provider="voice_stt",
        key="api_key",
        value="sk-tenant-stt",
        cipher=vault_key,
    )
    await _write_setting(
        app.state.engine,
        provider="voice_tts",
        key="api_key",
        value="sk-tenant-tts",
        cipher=vault_key,
    )
    await _write_setting(
        app.state.engine,
        provider="voice_stt",
        key="base_url",
        value="https://tenant.example/stt/v1",
    )
    await _write_setting(
        app.state.engine, provider="voice_tts", key="model", value="tenant/tts-model"
    )

    response = await _post_voice(client, csrf)

    assert response.status_code == 200
    resolved = voice_build.resolved
    assert resolved is not None
    assert resolved.stt_api_key == "sk-tenant-stt"
    assert resolved.tts_api_key == "sk-tenant-tts"
    assert resolved.stt_base_url == "https://tenant.example/stt/v1"
    assert resolved.tts_model == "tenant/tts-model"
    # The fields the tenant left alone still come from the environment.
    assert resolved.stt_model == _ENV_VOICE["VOICE_STT_MODEL"]
    assert resolved.tts_voice == _ENV_VOICE["VOICE_TTS_VOICE"]


async def test_env_only_resolution_is_the_whole_configuration(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    no_vault: None,
    env_voice: None,
) -> None:
    """No vault, no rows: the request runs on exactly the ``VOICE_*`` env.

    The pre-V3 lifespan built one process-global provider from these eight
    values. A request now resolves its own — and it must come out identical,
    or every existing single-tenant deployment changed behaviour.
    """
    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    response = await _post_voice(client, csrf)

    assert response.status_code == 200
    resolved = voice_build.resolved
    assert resolved is not None
    assert resolved == ResolvedVoice(
        stt_provider=_ENV_VOICE["VOICE_STT_PROVIDER"],
        stt_model=_ENV_VOICE["VOICE_STT_MODEL"],
        stt_api_key=_ENV_VOICE["VOICE_STT_API_KEY"],
        stt_base_url=_ENV_VOICE["VOICE_STT_BASE_URL"],
        tts_provider=_ENV_VOICE["VOICE_TTS_PROVIDER"],
        tts_model=_ENV_VOICE["VOICE_TTS_MODEL"],
        tts_voice=_ENV_VOICE["VOICE_TTS_VOICE"],
        tts_api_key=_ENV_VOICE["VOICE_TTS_API_KEY"],
    )


async def test_the_defaults_are_the_tails_of_every_config_chain(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    no_vault: None,
    env_enabled_only: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keys alone, nothing else: all four settings fall to the constants."""
    monkeypatch.setenv("VOICE_STT_API_KEY", "sk-env-stt")
    monkeypatch.setenv("VOICE_TTS_API_KEY", "sk-env-tts")

    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    assert (await _post_tts(client, csrf)).status_code == 200

    resolved = voice_build.resolved
    assert resolved is not None
    assert resolved.stt_model == DEFAULT_STT_MODEL
    assert resolved.stt_base_url == DEFAULT_STT_BASE_URL
    assert resolved.tts_model == DEFAULT_TTS_MODEL
    assert resolved.tts_voice == DEFAULT_TTS_VOICE


# ---------------------------------------------------------------------------
# Gating is a per-tenant answer (ADR-0118 §5)
# ---------------------------------------------------------------------------


async def test_a_tenant_row_enables_voice_with_the_environment_silent(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    no_vault: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``voice.enabled`` on the tenant turns the endpoints and controls on.

    ``VOICE_ENABLED`` is unset throughout, so nothing but the row can be
    answering. The keys still come from the environment — enablement and
    credential are separate chains, and this test is about the first.
    """
    monkeypatch.setenv("VOICE_STT_API_KEY", "sk-env-stt")
    monkeypatch.setenv("VOICE_TTS_API_KEY", "sk-env-tts")

    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    # Nothing on app.state decides this any more (the V3 startup removal).
    assert getattr(app.state, "voice_enabled", None) is None
    assert getattr(app.state, "voice_provider", None) is None

    # Off first: no row, no environment.
    assert (await _post_tts(client, csrf)).status_code == 404
    off_page = await client.get("/assistants", follow_redirects=False)
    assert "data-pf-voice-toggle" not in off_page.text

    await _write_setting(app.state.engine, provider="voice", key="enabled", value="true")

    assert (await _post_tts(client, csrf)).status_code == 200
    assert (await _post_voice(client, csrf)).status_code == 200
    on_page = await client.get("/assistants", follow_redirects=False)
    assert "data-pf-voice-toggle" in on_page.text


# ---------------------------------------------------------------------------
# Enabled but keyless is loud (ADR-0118 §2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["voice", "tts"])
async def test_enabled_without_a_credential_answers_503_naming_both_scopes(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    no_vault: None,
    env_enabled_only: None,
    endpoint: str,
) -> None:
    """Voice on, no key anywhere → 503 on both endpoints, and no provider.

    This is the startup validation ``VoiceConfig.__post_init__`` performed,
    relocated to first use: a tenant that switched voice on and stopped
    there learns so from a 503 that says where to fix it, not from a
    provider quietly failing upstream.
    """
    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    poster = _post_voice if endpoint == "voice" else _post_tts
    response = await poster(client, csrf)

    assert response.status_code == 503
    body = response.text
    assert "Providers" in body and "Credentials" in body
    assert "VOICE_STT_API_KEY" in body
    assert ".env" in body
    # Tenant rows apply on the next voice message — never a restart.
    assert "restart" not in body.lower()
    assert voice_build.calls == 0


async def test_one_half_of_the_credential_is_still_unconfigured(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    no_vault: None,
    env_enabled_only: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STT keyed, TTS not → 503, even on the endpoint that needs only STT.

    Both halves resolve on every request precisely so a half-configured
    tenant cannot be told it is fine by whichever endpoint it happened to
    hit first (ADR-0118 §2).
    """
    monkeypatch.setenv("VOICE_STT_API_KEY", "sk-env-stt")

    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    assert (await _post_voice(client, csrf)).status_code == 503
    assert voice_build.calls == 0
