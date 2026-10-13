# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-turn voice resolution and per-tenant gating on the web surface (ADR-0118, ADR-0136).

The voice twin of ``test_chat_llm_resolution.py``, and built the same way:
live Postgres, the real login flow, real vault rows written through
``ScopedSettingRepository`` — only ``build_stt`` / ``build_tts`` are doubles,
and they are precisely so the test can read *what the request resolved*.

What is pinned here:

* **The rows drive the turn.** A tenant's ``voice_stt`` / ``voice_tts`` keys
  and its model / persona-voice rows reach the two builders as the
  request's :class:`ResolvedSTT` / :class:`ResolvedTTS`, with **no restart**
  between the write and the recording. ``POST /chat/voice`` builds only the
  STT half and ``POST /chat/tts`` only the TTS half, so a test reading a half
  posts to that half's endpoint.
* **Scope precedence.** A tenant row outranks the environment — the ADR-0112
  §1 chain, observed end to end.
* **Env-only degradation.** With no vault rows the resolution is exactly the
  ``VOICE_*`` environment, which is what the retired process-global singleton
  was built from: single-tenant deployments did not change.
* **Gating is a per-tenant answer, per half.** A ``voice.enabled`` tenant row
  turns the endpoints and the Assistants affordances on with the environment
  silent, and a half's ``provider`` row activates that half alone
  (ADR-0118 §5, ADR-0136 §4).
* **Incomplete is loud, per half.** An active half whose adapter needs a key,
  a model or an endpoint it cannot resolve answers 503 on that half's
  endpoint, naming the field, Providers & Credentials *and* ``.env``; an
  unknown provider key is named the same way. A keyless adapter resolves
  without a key (ADR-0136 §4, §5).
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
from services.voice import ADAPTER_DECLARATIONS, ResolvedSTT, ResolvedTTS
from web.main import create_app
from web.routes import chat as chat_routes
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_MASTER_KEY = Fernet.generate_key().decode()

#: The ``openai`` declaration — today's defaults, kept until OpenAI's sunset
#: (ADR-0136 §2). Compared against rather than restated as literals.
_OPENAI = ADAPTER_DECLARATIONS["openai"]

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
    "VOICE_TTS_BASE_URL",
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
    "VOICE_TTS_BASE_URL": "https://env.example/tts/v1",
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
    """Satisfies :class:`SpeechToText` and :class:`TextToSpeech`; always succeeds.

    This module tests resolution, not I/O.
    """

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        return "how is my portfolio doing"

    async def synthesize(self, text: str, *, fmt: str) -> tuple[bytes, str]:
        return b"MP3BYTES", "audio/mpeg"


class _ProviderRecorder:
    """Stands in for :func:`build_stt` and :func:`build_tts` and keeps what each was handed."""

    def __init__(self) -> None:
        self.stt: ResolvedSTT | None = None
        self.tts: ResolvedTTS | None = None
        self.stt_calls = 0
        self.tts_calls = 0

    def build_stt(self, resolved: ResolvedSTT) -> _FakeVoiceProvider:
        self.stt = resolved
        self.stt_calls += 1
        return _FakeVoiceProvider()

    def build_tts(self, resolved: ResolvedTTS) -> _FakeVoiceProvider:
        self.tts = resolved
        self.tts_calls += 1
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
    """Replace ``web.routes.chat.build_stt`` / ``build_tts`` with the recorder."""
    recorder = _ProviderRecorder()
    monkeypatch.setattr(chat_routes, "build_stt", recorder.build_stt)
    monkeypatch.setattr(chat_routes, "build_tts", recorder.build_tts)
    return recorder


@pytest.fixture
def env_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole voice configuration in the environment, and nowhere else."""
    monkeypatch.setenv("VOICE_ENABLED", "true")
    for var, value in _ENV_VOICE.items():
        monkeypatch.setenv(var, value)


@pytest.fixture
def env_enabled_with_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Voice on and both providers ``openai`` in the environment; nothing else.

    Today's ``.env.example`` deployment minus its keys and settings — the
    fixture for "tenant rows supply the rest".
    """
    monkeypatch.setenv("VOICE_ENABLED", "true")
    monkeypatch.setenv("VOICE_STT_PROVIDER", "openai")
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "openai")


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
    # The composer moved to ``GET /chat/dock`` in P-UX-A0e — Shirley is a
    # shell element now, loaded on first open.
    page = await client.get("/chat/dock", follow_redirects=False)
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
    env_enabled_with_providers: None,
) -> None:
    """Vault rows written after startup reach the very next recording.

    The unset config fields fall through to the ``openai`` declaration's
    defaults (ADR-0136 §2).
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

    assert (await _post_voice(client, csrf)).status_code == 200
    assert (await _post_tts(client, csrf)).status_code == 200

    stt, tts = voice_build.stt, voice_build.tts
    assert stt is not None and tts is not None
    assert stt.api_key == "sk-tenant-stt"
    assert tts.api_key == "sk-tenant-tts"
    assert stt.model == "tenant/stt-model"
    assert tts.voice == "tenant-voice"
    # Unset everywhere: the declaration fills them.
    assert stt.base_url == _OPENAI.base_url
    assert tts.model == _OPENAI.tts_model
    assert stt.provider == "openai"
    assert tts.provider == "openai"


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

    assert (await _post_voice(client, csrf)).status_code == 200
    assert (await _post_tts(client, csrf)).status_code == 200

    stt, tts = voice_build.stt, voice_build.tts
    assert stt is not None and tts is not None
    assert stt.api_key == "sk-tenant-stt"
    assert tts.api_key == "sk-tenant-tts"
    assert stt.base_url == "https://tenant.example/stt/v1"
    assert tts.model == "tenant/tts-model"
    # The fields the tenant left alone still come from the environment.
    assert stt.model == _ENV_VOICE["VOICE_STT_MODEL"]
    assert tts.voice == _ENV_VOICE["VOICE_TTS_VOICE"]


async def test_env_only_resolution_is_the_whole_configuration(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    no_vault: None,
    env_voice: None,
) -> None:
    """No vault, no rows: the request runs on exactly the ``VOICE_*`` env.

    The pre-V3 lifespan built one process-global provider from these values.
    A request now resolves its own — and it must come out identical, or every
    existing single-tenant deployment changed behaviour. ``VOICE_TTS_BASE_URL``
    joined the chain with ADR-0136 §3 and is observed end to end here.
    """
    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    assert (await _post_voice(client, csrf)).status_code == 200
    assert (await _post_tts(client, csrf)).status_code == 200

    assert voice_build.stt == ResolvedSTT(
        provider=_ENV_VOICE["VOICE_STT_PROVIDER"],
        model=_ENV_VOICE["VOICE_STT_MODEL"],
        api_key=_ENV_VOICE["VOICE_STT_API_KEY"],
        base_url=_ENV_VOICE["VOICE_STT_BASE_URL"],
    )
    assert voice_build.tts == ResolvedTTS(
        provider=_ENV_VOICE["VOICE_TTS_PROVIDER"],
        model=_ENV_VOICE["VOICE_TTS_MODEL"],
        voice=_ENV_VOICE["VOICE_TTS_VOICE"],
        api_key=_ENV_VOICE["VOICE_TTS_API_KEY"],
        base_url=_ENV_VOICE.get("VOICE_TTS_BASE_URL") or "https://api.openai.com/v1",
    )


async def test_the_legacy_openai_declaration_supplies_todays_defaults(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    no_vault: None,
    env_enabled_with_providers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Providers and keys alone: every setting falls to the ``openai`` declaration.

    The values ADR-0076 wrote as code defaults, now read from the declaration
    that keeps them until OpenAI's sunset (ADR-0136 §2).
    """
    monkeypatch.setenv("VOICE_STT_API_KEY", "sk-env-stt")
    monkeypatch.setenv("VOICE_TTS_API_KEY", "sk-env-tts")

    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    assert (await _post_voice(client, csrf)).status_code == 200
    assert (await _post_tts(client, csrf)).status_code == 200

    stt, tts = voice_build.stt, voice_build.tts
    assert stt is not None and tts is not None
    assert stt.model == _OPENAI.stt_model
    assert stt.base_url == _OPENAI.base_url
    assert tts.model == _OPENAI.tts_model
    assert tts.voice == _OPENAI.tts_voice
    assert tts.base_url == _OPENAI.base_url


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
    answering. The providers and keys still come from the environment —
    enablement and the rest are separate chains, and this test is about the
    first.
    """
    monkeypatch.setenv("VOICE_STT_PROVIDER", "openai")
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "openai")
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
    off_page = await client.get("/chat/dock", follow_redirects=False)
    assert "data-pf-voice-toggle" not in off_page.text

    await _write_setting(app.state.engine, provider="voice", key="enabled", value="true")

    assert (await _post_tts(client, csrf)).status_code == 200
    assert (await _post_voice(client, csrf)).status_code == 200
    on_page = await client.get("/chat/dock", follow_redirects=False)
    assert "data-pf-voice-toggle" in on_page.text


# ---------------------------------------------------------------------------
# Incomplete is loud, per half (ADR-0136 §4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["voice", "tts"])
async def test_enabled_without_a_credential_answers_503_naming_both_scopes(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    no_vault: None,
    env_enabled_with_providers: None,
    endpoint: str,
) -> None:
    """An active ``openai`` half with no key anywhere → 503 on that half's endpoint.

    The start-up validation ADR-0076 had, relocated to first use and judged
    per half: each endpoint names its own half's variable, and where to fix
    it, rather than a provider quietly failing upstream.
    """
    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    poster = _post_voice if endpoint == "voice" else _post_tts
    response = await poster(client, csrf)

    assert response.status_code == 503
    body = response.text
    assert "Providers" in body and "Credentials" in body
    variable = "VOICE_STT_API_KEY" if endpoint == "voice" else "VOICE_TTS_API_KEY"
    assert variable in body
    assert ".env" in body
    # Tenant rows apply on the next voice message — never a restart.
    assert "restart" not in body.lower()
    assert voice_build.stt_calls == 0
    assert voice_build.tts_calls == 0


async def test_each_half_is_judged_on_its_own(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    no_vault: None,
    env_enabled_with_providers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STT keyed, TTS not → ``/chat/voice`` works and only ``/chat/tts`` is 503.

    Each endpoint resolves only the half it needs (ADR-0136 §5): one half
    complete and the other incomplete is loud on the incomplete half alone.
    """
    monkeypatch.setenv("VOICE_STT_API_KEY", "sk-env-stt")

    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    assert (await _post_voice(client, csrf)).status_code == 200
    tts = await _post_tts(client, csrf)
    assert tts.status_code == 503
    assert "VOICE_TTS_API_KEY" in tts.text
    assert voice_build.stt_calls == 1
    assert voice_build.tts_calls == 0


async def test_a_tenant_provider_row_activates_a_half_with_the_environment_silent(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    no_vault: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``voice.enabled`` and ``voice_stt.provider`` as tenant rows → STT alone is active.

    No ``VOICE_*_PROVIDER`` in the environment: the rows are the only choice
    made, and the half without one stays inactive (404), not misconfigured.
    """
    monkeypatch.setenv("VOICE_STT_API_KEY", "sk-env-stt")

    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    await _write_setting(app.state.engine, provider="voice", key="enabled", value="true")
    await _write_setting(app.state.engine, provider="voice_stt", key="provider", value="openai")

    assert (await _post_voice(client, csrf)).status_code == 200
    assert (await _post_tts(client, csrf)).status_code == 404
    assert voice_build.stt is not None
    assert voice_build.stt.provider == "openai"


async def test_openai_compatible_resolves_keyless(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    no_vault: None,
) -> None:
    """A keyless adapter with its model and endpoint set resolves with no key anywhere."""
    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    engine = app.state.engine
    await _write_setting(engine, provider="voice", key="enabled", value="true")
    await _write_setting(engine, provider="voice_stt", key="provider", value="openai_compatible")
    await _write_setting(engine, provider="voice_stt", key="model", value="local/whisper")
    await _write_setting(
        engine, provider="voice_stt", key="base_url", value="http://localhost:8000/v1"
    )

    assert (await _post_voice(client, csrf)).status_code == 200
    assert voice_build.stt == ResolvedSTT(
        provider="openai_compatible",
        model="local/whisper",
        api_key="",
        base_url="http://localhost:8000/v1",
    )


async def test_openai_compatible_without_a_model_is_503(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    no_vault: None,
) -> None:
    """The wire-compatible adapter defaults nothing: a missing model is named."""
    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    engine = app.state.engine
    await _write_setting(engine, provider="voice", key="enabled", value="true")
    await _write_setting(engine, provider="voice_stt", key="provider", value="openai_compatible")
    await _write_setting(
        engine, provider="voice_stt", key="base_url", value="http://localhost:8000/v1"
    )

    response = await _post_voice(client, csrf)
    assert response.status_code == 503
    assert "VOICE_STT_MODEL" in response.text
    assert voice_build.stt_calls == 0


async def test_a_provider_outside_the_vocabulary_is_503(
    app_client: Any,
    seeded_user: Any,
    voice_build: _ProviderRecorder,
    no_vault: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An env typo is not a silent fallback: 503 naming the variable and the legal keys."""
    monkeypatch.setenv("VOICE_ENABLED", "true")
    monkeypatch.setenv("VOICE_STT_PROVIDER", "deepgram")

    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    response = await _post_voice(client, csrf)
    assert response.status_code == 503
    assert "VOICE_STT_PROVIDER" in response.text
    assert "openai, openai_compatible" in response.text
    assert voice_build.stt_calls == 0
