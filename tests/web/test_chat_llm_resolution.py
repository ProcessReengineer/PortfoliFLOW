# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-turn LLM resolution on the chat surface (ADR-0112 §4b).

Live Postgres, real login flow, real vault rows — only the AI core is a
fake, and it is a fake precisely so the test can read *what the route
resolved* off the turn it was handed.

What is pinned here:

* **The rows drive the turn.** A tenant's ``openrouter`` key and model reach
  ``stream_response`` as the turn's :class:`ResolvedLLM`, written through the
  same vault the admin surface writes, with **no restart** between the write
  and the turn.
* **Scope precedence.** A user's own model outranks the tenant's, which
  outranks the environment — the ADR-0112 §1 chain, observed end to end
  rather than at the resolver's unit boundary.
* **Loud failure.** With nothing resolvable the POST answers 503 and the SSE
  stream answers an ``error`` frame; both messages point at Providers &
  Credentials *and* ``.env``, and neither tells the operator to restart —
  tenant and user rows apply on the next turn.
* **D7, the no-regression obligation.** With no vault rows and only the
  environment set, the turn's resolution is byte-for-byte what the pre-F4
  singleton would have been configured with. This is the test that says
  "single-tenant deployments did not change".
* **No key in the stash.** The pending-turn store between the POST and the
  SSE GET must never carry the plain credential.
"""

from __future__ import annotations

import os
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
from services.ai_models import (
    ConnectionStatus,
    Conversation,
    Message,
    MessageRole,
)
from services.ai_service_core import StreamEvent
from services.credential_vault import MASTER_KEY_ENV_VAR, VaultCipher
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_MASTER_KEY = Fernet.generate_key().decode()

_ENV_KEY = "sk-env-application-scope"
_ENV_MODEL = "env/shirley-model"
_ENV_BASE_URL = "https://env.example/api/v1"


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB chat-resolution tests.",
            allow_module_level=False,
        )


class _FakeCore:
    """Records the turn it is handed, including the resolution driving it."""

    def __init__(self) -> None:
        self.last_llm: Any = None
        self.calls = 0

    def get_status(self) -> ConnectionStatus:
        # Deliberately DISCONNECTED: the route must never consult this again
        # (ADR-0112 §4b). A turn that runs anyway proves the gate moved.
        return ConnectionStatus.DISCONNECTED

    def get_model(self) -> str:
        return ""

    def get_system_prompt(self, prompt_name: str = "shirley") -> str:
        return ""

    async def stream_response(
        self,
        conversation: Conversation,
        system_prompt: str = "",
        temperature: float = 0.7,
        tool_context: object = None,
        llm: object = None,
    ) -> AsyncIterator[StreamEvent]:
        self.last_llm = llm
        self.calls += 1
        final = Message(role=MessageRole.ASSISTANT, content="ok")
        yield StreamEvent("chunk", {"text": "ok"})
        yield StreamEvent("stream_finished", {"message": final, "iterations": 0})


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
    email = "f4@example.com"
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


@pytest.fixture
def env_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The application scope, as a deployment's ``.env`` would provide it."""
    monkeypatch.setenv("OPENROUTER_API_KEY", _ENV_KEY)
    monkeypatch.setenv("SHIRLEY_MODEL", _ENV_MODEL)
    monkeypatch.setenv("OPENROUTER_BASE_URL", _ENV_BASE_URL)


@pytest.fixture
def no_env_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """No application scope at all — the environment cannot serve a turn."""
    for var in ("OPENROUTER_API_KEY", "SHIRLEY_MODEL", "OPENROUTER_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def no_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``OPENROUTER_BASE_URL`` anywhere — not even when the app is built.

    A fixture rather than an in-body ``delenv`` because the fallback under
    test is ``settings.openrouter_base_url``, and those settings are
    constructed inside ``app_client``. Deleting the variable in the test
    body would come too late: the app would already have captured whatever
    the environment held, and the assertion would be comparing two paths
    that merely happen to agree.
    """
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)


@pytest.fixture
def vault_key(monkeypatch: pytest.MonkeyPatch) -> VaultCipher:
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, _MASTER_KEY)
    return VaultCipher(_MASTER_KEY)


@pytest.fixture
def no_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)


@pytest_asyncio.fixture
async def app_client(
    seeded_user: tuple[UUID, str, str],
) -> AsyncGenerator[Any, None]:
    """Yield ``(client, fake_core, app)`` for a fully wired app."""
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
        core = _FakeCore()
        app.state.ai_core = core
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://testserver")
        await stack.enter_async_context(client)
        yield client, core, app
    finally:
        await stack.aclose()


async def _write_setting(
    engine: AsyncEngine,
    *,
    scope: str,
    key: str,
    value: str,
    cipher: VaultCipher | None = None,
    user_id: UUID | None = None,
) -> None:
    """Write one ``openrouter`` row exactly as the admin surface would."""
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
        repo = ScopedSettingRepository(session)
        if cipher is not None:
            await repo.upsert(
                scope=scope,
                provider="openrouter",
                key=key,
                is_secret=True,
                value_ciphertext=cipher.encrypt(value),
                secret_hint=value[-4:],
                user_id=user_id,
            )
        else:
            await repo.upsert(
                scope=scope,
                provider="openrouter",
                key=key,
                is_secret=False,
                value_plain=value,
                user_id=user_id,
            )


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """Log in and return the session CSRF token from the composer form."""
    import re

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


async def _run_turn(client: AsyncClient, csrf: str) -> tuple[Any, str]:
    """Open a turn and consume its SSE stream. Returns (post, sse_text)."""
    import re

    post = await client.post(
        "/chat/messages",
        data={"message": "hello", "csrf_token": csrf},
        follow_redirects=False,
    )
    if post.status_code != 200:
        return post, ""
    match = re.search(r"/chat/stream/([0-9a-f]+)", post.text)
    assert match is not None, post.text
    sse = await client.get(f"/chat/stream/{match.group(1)}")
    return post, sse.text


# ---------------------------------------------------------------------------
# The rows drive the turn
# ---------------------------------------------------------------------------


async def test_tenant_rows_drive_the_turn_without_a_restart(
    app_client: Any, seeded_user: Any, vault_key: VaultCipher, no_env_scope: None
) -> None:
    client, core, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    # Written *after* the app started — the point of per-turn resolution.
    await _write_setting(
        app.state.engine, scope="tenant", key="api_key", value="sk-tenant", cipher=vault_key
    )
    await _write_setting(app.state.engine, scope="tenant", key="model", value="tenant/model")

    _post, sse = await _run_turn(client, csrf)

    assert "event: done" in sse
    assert core.last_llm is not None
    assert core.last_llm.api_key == "sk-tenant"
    assert core.last_llm.model == "tenant/model"


async def test_a_user_model_outranks_the_tenant_model(
    app_client: Any, seeded_user: Any, vault_key: VaultCipher, no_env_scope: None
) -> None:
    client, core, app = app_client
    user_id, email, password = seeded_user
    csrf = await _login(client, email, password)

    await _write_setting(
        app.state.engine, scope="tenant", key="api_key", value="sk-tenant", cipher=vault_key
    )
    await _write_setting(app.state.engine, scope="tenant", key="model", value="tenant/model")
    await _write_setting(
        app.state.engine, scope="user", key="model", value="user/model", user_id=user_id
    )

    await _run_turn(client, csrf)

    assert core.last_llm.model == "user/model"
    # The credential still came from the tenant — only the model is the
    # user's, since that is the only row the user scope holds here.
    assert core.last_llm.api_key == "sk-tenant"


async def test_a_user_key_outranks_the_tenant_key(
    app_client: Any, seeded_user: Any, vault_key: VaultCipher, no_env_scope: None
) -> None:
    client, core, app = app_client
    user_id, email, password = seeded_user
    csrf = await _login(client, email, password)

    await _write_setting(
        app.state.engine, scope="tenant", key="api_key", value="sk-tenant", cipher=vault_key
    )
    await _write_setting(app.state.engine, scope="tenant", key="model", value="tenant/model")
    await _write_setting(
        app.state.engine,
        scope="user",
        key="api_key",
        value="sk-user",
        cipher=vault_key,
        user_id=user_id,
    )

    await _run_turn(client, csrf)

    assert core.last_llm.api_key == "sk-user"


async def test_a_tenant_row_outranks_the_environment(
    app_client: Any, seeded_user: Any, vault_key: VaultCipher, env_scope: None
) -> None:
    client, core, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    await _write_setting(
        app.state.engine, scope="tenant", key="api_key", value="sk-tenant", cipher=vault_key
    )
    await _write_setting(app.state.engine, scope="tenant", key="model", value="tenant/model")
    await _write_setting(
        app.state.engine, scope="tenant", key="base_url", value="https://tenant.example/api/v1"
    )

    await _run_turn(client, csrf)

    assert core.last_llm.api_key == "sk-tenant"
    assert core.last_llm.model == "tenant/model"
    assert core.last_llm.base_url == "https://tenant.example/api/v1"


# ---------------------------------------------------------------------------
# D7 — the single-tenant, env-only deployment is unchanged
# ---------------------------------------------------------------------------


async def test_env_only_resolution_matches_the_pre_f4_singleton_triple(
    app_client: Any, seeded_user: Any, no_vault: None, env_scope: None
) -> None:
    """No vault, no rows: the turn runs on exactly the ``.env`` triple.

    The pre-F4 lifespan configured the singleton with
    ``(openrouter_base_url, OPENROUTER_API_KEY)`` and ``SHIRLEY_MODEL``. A
    turn now resolves its own — and it must come out identical, or every
    existing single-tenant deployment changed behaviour under them.
    """
    client, core, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    _post, sse = await _run_turn(client, csrf)

    assert "event: done" in sse
    assert core.last_llm.api_key == _ENV_KEY
    assert core.last_llm.model == _ENV_MODEL
    assert core.last_llm.base_url == _ENV_BASE_URL


async def test_base_url_falls_back_to_the_settings_default(
    no_base_url: None,
    app_client: Any,
    seeded_user: Any,
    no_vault: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``base_url`` is the one field that can never fail a turn.

    ``no_base_url`` is requested first on purpose: same-scope fixtures are
    set up in the order the signature requests them, so the variable is
    gone before ``app_client`` constructs the app's settings.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", _ENV_KEY)
    monkeypatch.setenv("SHIRLEY_MODEL", _ENV_MODEL)

    client, core, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    await _run_turn(client, csrf)

    assert core.last_llm.base_url == WebSettings().openrouter_base_url


# ---------------------------------------------------------------------------
# Loud failure (D5)
# ---------------------------------------------------------------------------


async def test_no_resolvable_credential_answers_503_with_both_scopes(
    app_client: Any, seeded_user: Any, no_vault: None, no_env_scope: None
) -> None:
    client, core, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    post = await client.post(
        "/chat/messages",
        data={"message": "hello", "csrf_token": csrf},
        follow_redirects=False,
    )

    assert post.status_code == 503
    body = post.text
    assert "Providers" in body and "Credentials" in body
    assert "OPENROUTER_API_KEY" in body
    assert ".env" in body
    # Tenant and user rows apply on the next turn — never a restart.
    assert "restart" not in body.lower()
    assert core.calls == 0


async def test_a_credential_without_a_model_also_503s(
    app_client: Any, seeded_user: Any, no_vault: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key alone cannot drive a turn; the model chain must serve too."""
    monkeypatch.setenv("OPENROUTER_API_KEY", _ENV_KEY)
    monkeypatch.delenv("SHIRLEY_MODEL", raising=False)

    client, core, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    post = await client.post(
        "/chat/messages",
        data={"message": "hello", "csrf_token": csrf},
        follow_redirects=False,
    )

    assert post.status_code == 503
    assert core.calls == 0


async def test_the_sse_stream_reports_the_same_failure_as_an_error_frame(
    app_client: Any, seeded_user: Any, vault_key: VaultCipher, no_env_scope: None
) -> None:
    """The POST passes, the rows vanish, and the SSE turn fails cleanly.

    The authoritative resolution is the SSE one (binding decision D3), so
    the stream has to be able to fail on its own — with an ``error`` frame
    and a ``done``, never a half-open connection.
    """
    import re

    client, core, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    await _write_setting(
        app.state.engine, scope="tenant", key="api_key", value="sk-tenant", cipher=vault_key
    )
    await _write_setting(app.state.engine, scope="tenant", key="model", value="tenant/model")

    post = await client.post(
        "/chat/messages",
        data={"message": "hello", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert post.status_code == 200

    # The tenant's rows are withdrawn between the POST and the SSE GET.
    async with tenant_context(app.state.engine, SENTINEL_TENANT_ID) as session:
        await ScopedSettingRepository(session).delete(
            scope="tenant", provider="openrouter", key="api_key"
        )

    match = re.search(r"/chat/stream/([0-9a-f]+)", post.text)
    assert match is not None
    sse = await client.get(f"/chat/stream/{match.group(1)}")

    assert "event: error" in sse.text
    assert "event: done" in sse.text
    assert "Credentials" in sse.text
    assert core.calls == 0


# ---------------------------------------------------------------------------
# The key never enters the stash (D3)
# ---------------------------------------------------------------------------


async def test_the_pending_turn_stash_never_carries_the_key(
    app_client: Any, seeded_user: Any, vault_key: VaultCipher, no_env_scope: None
) -> None:
    client, _core, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    await _write_setting(
        app.state.engine, scope="tenant", key="api_key", value="sk-tenant", cipher=vault_key
    )
    await _write_setting(app.state.engine, scope="tenant", key="model", value="tenant/model")

    post = await client.post(
        "/chat/messages",
        data={"message": "hello", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert post.status_code == 200

    # Inspect the store between the POST and the SSE GET — the window in
    # which a stashed credential would exist.
    stash = dict(app.state.pending_turns)
    assert stash, "the turn should be pending at this point"
    for entry in stash.values():
        assert "sk-tenant" not in repr(entry)
        assert "api_key" not in entry
        # The resolved *model* is legitimate turn metadata and is kept.
        assert entry["model_id"] == "tenant/model"
    # And it is nowhere in the rendered fragment either.
    assert "sk-tenant" not in post.text
