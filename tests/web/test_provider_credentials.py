# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Providers & Credentials surface (ADR-0112 §6, strands F3/F5).

Follows the idiom of the retired ``test_ai_settings.py`` it replaces —
live Postgres, real login flow, ``AsyncClient`` over ``ASGITransport`` —
but asserts against persisted ``scoped_settings`` rows rather than a
mutated in-process singleton.

Ten groups:

* **Authz matrix** — owner sees and writes both panels; member and
  auditor see their own panel only and are 403 on the tenant endpoint;
  unauthenticated redirects; missing CSRF is rejected.
* **Card copy** — each card states its provider's purpose, each hinted
  field explains itself, the two OpenRouter model rows carry distinct
  labels, and no rendered *copy* names the internal agent (ADR-0115) —
  only the wire values do.
* **Taxonomy validation** — an undeclared provider, an undeclared key, a
  key not writable at the requested scope, and a field the user panel
  excludes are each refused inline **without touching the repository**.
* **Secret round-trip** — the stored ciphertext decrypts to the input
  under the test master key, the hint is the last four characters, and
  the rendered HTML carries the hint and a "set" status but never the
  value.
* **Write-only semantics** — an empty secret save leaves the row
  untouched; an empty config save is a validation error.
* **Enable / disable / delete** round-trips.
* **User self-service** — a user's row is invisible to another user, and
  a smuggled ``user_id`` form field cannot redirect the write.
* **Vault unconfigured** — the banner renders, a secret write is refused
  inline, and config writes keep working.
* **Telegram pairing** (F5, ADR-0112 §5) — the block renders per state for
  every role, a generated code is shown exactly once and never logged,
  re-issuing invalidates the previous code, the code binds the *session's*
  tenant and user, revoking deletes the row and voids pending codes, and
  both endpoints demand a session and a CSRF token.
* **Model catalog** — the OpenRouter ``/models`` datalist: it renders for
  the field that asked and no other, a failed fetch says so inline at
  HTTP 200 rather than swapping in an empty list, the tenant scope carries
  the owner gate and an unoffered scope/field is refused before anything
  leaves the machine, the resolved key reaches the fetch but never the
  response body, a keyless tenant still gets a list, and the section wires
  the ``list=`` attribute onto the three model inputs and nothing else.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncGenerator
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

from core.repositories._session import tenant_context
from core.repositories.scoped_setting_repository import (
    ScopedSettingDTO,
    ScopedSettingRepository,
)
from core.tenant_constants import SENTINEL_TENANT_ID
from services import telegram_pairing
from services.credential_vault import MASTER_KEY_ENV_VAR, VaultCipher
from services.openrouter_catalog import CatalogFetchError, CatalogModel
from services.password_hashing import hash_password
from web.main import create_app
from web.routes import provider_credentials
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_PASSWORD = "correct-horse-battery-staple"

#: A real Fernet key, generated per test session rather than committed —
#: a key literal in the repository reads like a leaked secret even when
#: it is not.
_TEST_MASTER_KEY = Fernet.generate_key().decode()

#: Deliberately long enough (>= 8) to earn a hint, with a distinctive
#: last four so "is the hint rendered" and "is the value rendered" are
#: two different assertions.
_SECRET_VALUE = "sk-or-v1-do-not-echo-this-back-9Q7X"
_SECRET_HINT = "9Q7X"

_SECTION_URL = "/admin/providers-credentials/section"
_TENANT_URL = "/admin/providers-credentials/tenant"
_USER_URL = "/admin/providers-credentials/user"
_PAIR_URL = "/admin/providers-credentials/telegram/pair"
_UNPAIR_URL = "/admin/providers-credentials/telegram/unpair"
_MODELS_URL = "/admin/providers-credentials/openrouter/models"


@pytest.fixture(autouse=True)
def _clean_pairing_store() -> Any:
    """Empty the in-process pairing store around every test.

    The store is module-level and process-lifetime by design (ADR-0112 §5,
    D4), so one test's code would otherwise still be pending in the next.
    """
    telegram_pairing.reset_store()
    yield
    telegram_pairing.reset_store()


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB provider-credentials tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """An RLS-subject engine for reading rows back the way the app writes them."""
    _require_db()
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_schema(
    fresh_superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
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
async def seeded_users(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> dict[str, tuple[UUID, str]]:
    """Seed one user per role plus a second member, all in the Sentinel tenant.

    Returns:
        Role key → ``(user_id, email)``. The password is
        :data:`_PASSWORD` for all of them.
    """
    accounts: dict[str, tuple[UUID, str]] = {
        "owner": (uuid4(), "pc-owner@example.com"),
        "member": (uuid4(), "pc-member@example.com"),
        "auditor": (uuid4(), "pc-auditor@example.com"),
        "other_member": (uuid4(), "pc-other-member@example.com"),
    }
    roles = {
        "owner": "owner",
        "member": "member",
        "auditor": "auditor",
        "other_member": "member",
    }
    password_hash = hash_password(_PASSWORD)

    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        for key, (user_id, email) in accounts.items():
            await conn.execute(
                text(
                    """
                    INSERT INTO users
                        (id, tenant_id, email, password_hash, roles, is_active)
                    VALUES
                        (:id, :tid, :email, :hash, ARRAY[:role]::text[], TRUE)
                    """
                ),
                {
                    "id": str(user_id),
                    "tid": str(SENTINEL_TENANT_ID),
                    "email": email,
                    "hash": password_hash,
                    "role": roles[key],
                },
            )
    return accounts


@pytest_asyncio.fixture
async def client_factory(seeded_users: dict[str, tuple[UUID, str]]):
    """Yield a factory returning a logged-in ``AsyncClient`` for one role."""
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

    async def _make(role: str | None = None) -> AsyncClient:
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://testserver")
        await stack.enter_async_context(client)
        if role is not None:
            await _login(client, seeded_users[role][1])
        return client

    try:
        yield _make
    finally:
        await stack.aclose()


@pytest.fixture
def vault_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure the vault with a real Fernet key for the duration of a test."""
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, _TEST_MASTER_KEY)
    return _TEST_MASTER_KEY


@pytest.fixture
def vault_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the master key so the surface must degrade visibly."""
    monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login(client: AsyncClient, email: str) -> None:
    get_response = await client.get("/login")
    pre_csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert pre_csrf is not None
    response = await client.post(
        "/login",
        data={"email": email, "password": _PASSWORD, "csrf_token": pre_csrf},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303), response.text


async def _section_csrf(client: AsyncClient) -> str:
    """Read the session CSRF token out of the rendered section."""
    response = await client.get(_SECTION_URL, follow_redirects=False)
    assert response.status_code == 200, response.text
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None, "section rendered no CSRF-bearing form"
    return match.group(1)


def _visible_text(html: str) -> str:
    """Return the rendered copy with every tag — and so every attribute — gone.

    Lets a test assert on what an operator *reads* without tripping over
    wire values that legitimately carry a taxonomy key (``name="key"
    value="irene_model"``, the matching input id, the hint's
    ``aria-describedby``).
    """
    return re.sub(r"<[^>]+>", " ", html)


async def _read_row(
    engine: AsyncEngine,
    *,
    scope: str,
    provider: str,
    key: str,
    user_id: UUID | None = None,
) -> ScopedSettingDTO | None:
    async with tenant_context(engine, SENTINEL_TENANT_ID) as session:
        return await ScopedSettingRepository(session).get(scope, provider, key, user_id=user_id)


# ---------------------------------------------------------------------------
# Authz matrix
# ---------------------------------------------------------------------------


async def test_owner_sees_both_panels_with_consumer_pills(
    client_factory: Any,
    vault_key: str,
) -> None:
    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    # Both ADR-0112 §6 scope indicators, and no retired process banner.
    assert "applies to: this tenant" in body
    assert "applies to: you" in body
    assert "Settings apply to the running process" not in body

    # Every declared tenant-scope provider is offered.
    assert "OpenFIGI" in body
    assert "OpenRouter" in body
    assert "Telegram" in body

    # Consumer honesty: all three are consumed as of F5, and the Telegram
    # pill carries the one caveat the others do not — the dispatcher set is
    # discovered once at bot start (ADR-0112 §5).
    assert "live" in body
    assert "consumed when the LLM strand (F4) lands" not in body
    assert "consumed when the multi-bot strand (F5) lands" not in body
    assert "token changes apply after a bot restart" in body

    # The three ADR-0118 voice cards are offered too, each carrying §8's
    # caveat-free pill: the configuration is resolved per voice interaction,
    # so a save applies on the next message without a restart.
    for voice_label in ("Voice", "Voice — speech-to-text", "Voice — text-to-speech"):
        assert re.search(
            rf">{re.escape(voice_label)}</h4>\s*<span[^>]*>live — saves apply instantly</span>",
            body,
        ), voice_label

    # The user panel offers exactly one editable field in v1.
    assert "My model" in body
    # ...and never a telegram.chat_id input: that row is the pairing flow's
    # output, never typed by hand (ADR-0112 §5, D7).
    assert "Chat id" not in body


@pytest.mark.parametrize("role", ["member", "auditor"])
async def test_non_owner_sees_only_the_user_panel(
    client_factory: Any,
    vault_key: str,
    role: str,
) -> None:
    client = await client_factory(role)
    response = await client.get(_SECTION_URL, follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    assert "applies to: you" in body
    assert "applies to: this tenant" not in body
    assert "owner-managed" in body
    # No tenant-scope provider card leaks through.
    assert "OpenFIGI" not in body


@pytest.mark.parametrize("role", ["member", "auditor"])
async def test_non_owner_tenant_write_is_forbidden(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
    role: str,
) -> None:
    client = await client_factory(role)
    csrf = await _section_csrf(client)

    response = await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": "openrouter",
            "key": "model",
            "value": "anthropic/claude-opus-4-7",
            "action": "save",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert await _read_row(app_engine, scope="tenant", provider="openrouter", key="model") is None


async def test_unauthenticated_write_redirects_to_login(
    client_factory: Any,
) -> None:
    client = await client_factory(None)
    response = await client.post(
        _TENANT_URL,
        data={"csrf_token": "x", "provider": "openrouter", "key": "model", "value": "y"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_write_without_csrf_is_rejected(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    client = await client_factory("owner")
    response = await client.post(
        _TENANT_URL,
        data={"provider": "openrouter", "key": "model", "value": "anthropic/x"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert await _read_row(app_engine, scope="tenant", provider="openrouter", key="model") is None


# ---------------------------------------------------------------------------
# Card copy — purpose, hints, and the two disambiguated model rows
# ---------------------------------------------------------------------------


async def test_cards_carry_purpose_copy_and_disambiguate_the_three_model_rows(
    client_factory: Any,
    vault_key: str,
) -> None:
    """An operator can tell what a provider is for, and which model is which.

    OpenRouter declares three model rows at tenant scope (ADR-0123 added the
    third); all read "Model" until the label distinguishes them. The
    distinguishing word is the **Area** or the surface — the internal agent
    name never reaches a user-facing string (ADR-0115); only the wire value
    keeps it.
    """
    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)

    assert response.status_code == 200
    body = response.text

    # Purpose copy under the card title, and the per-field hint under the input.
    assert (
        "The LLM provider behind Shirley, the Report Scraper and the Watch Desk monitoring notes."
        in body
    )
    assert "The model Shirley uses." in body
    assert (
        "The model that extracts figures from uploaded GP reports. "
        "Must be an Anthropic model (PDF input)." in body
    )
    # The key hint names every surface the key is spent on.
    assert (
        "Used for every Shirley turn, Report Scraper run and Watch Desk beat in this tenant."
        in body
    )

    # The three OpenRouter model rows no longer all read "Model"...
    assert "Watch Desk model" in body
    assert "Report Scraper model" in body
    assert 'value="scraper_model"' in body
    # ...and the pill says a save is live, not merely stored.
    assert "live — saves apply instantly" in body

    # The three voice cards state their purpose as well (ADR-0118 §7).
    assert "Voice input and spoken replies for Shirley — the on/off switch for this tenant." in body
    assert "Transcribes recorded questions, on the web chat and in Telegram voice messages." in body
    assert "The voice Shirley answers with, as browser audio and Telegram voice notes." in body

    # The TTS card's persona-voice row reads "Voice" — the same word as the
    # card family, so the hint carries the distinction.
    assert '<span class="pf-credentials__label">Voice</span>' in body
    assert 'id="tenant-voice_tts-voice-hint">The voice Shirley speaks with.</span>' in body

    # The wire value keeps the taxonomy key; nothing an operator reads does.
    assert 'value="irene_model"' in body
    assert "irene" not in _visible_text(body).lower()


# ---------------------------------------------------------------------------
# Taxonomy validation — nothing invalid reaches the repository
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "key", "expected_fragment"),
    [
        ("bloomberg_serverapi", "api_key", "Unknown provider"),
        ("openrouter", "temperature", "declares no field"),
        # Declared, but only at user scope (the F5 pairing flow writes it).
        ("telegram", "chat_id", "may not be written at tenant scope"),
    ],
)
async def test_tenant_write_rejects_undeclared_shapes(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
    provider: str,
    key: str,
    expected_fragment: str,
) -> None:
    client = await client_factory("owner")
    csrf = await _section_csrf(client)

    response = await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": provider,
            "key": key,
            "value": "something",
            "action": "save",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert expected_fragment in response.text
    assert await _read_row(app_engine, scope="tenant", provider=provider, key=key) is None


@pytest.mark.parametrize(
    ("provider", "key", "expected_fragment"),
    [
        # Declared only at tenant scope.
        ("openfigi", "api_key", "may not be written at user scope"),
        # Declared at user scope, but a secret — no user-scope secret UI in v1.
        ("openrouter", "api_key", "user-scope secrets have no write surface"),
        # Declared at user scope and non-secret, but panel-excluded.
        ("telegram", "chat_id", "not editable here"),
        # Declared only at tenant scope (ADR-0123): the Report Scraper is a
        # tenant tool, and a user-scope model would only widen the surface on
        # which a non-PDF-capable model can be chosen.
        ("openrouter", "scraper_model", "may not be written at user scope"),
    ],
)
async def test_user_write_rejects_fields_the_panel_does_not_offer(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
    vault_key: str,
    provider: str,
    key: str,
    expected_fragment: str,
) -> None:
    client = await client_factory("member")
    csrf = await _section_csrf(client)

    response = await client.post(
        _USER_URL,
        data={
            "csrf_token": csrf,
            "provider": provider,
            "key": key,
            "value": "something",
            "action": "save",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert expected_fragment in response.text
    user_id = seeded_users["member"][0]
    assert (
        await _read_row(app_engine, scope="user", provider=provider, key=key, user_id=user_id)
        is None
    )


async def test_unknown_action_is_rejected(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    """``enable`` exists on the tenant panel only; the user panel refuses it."""
    client = await client_factory("member")
    csrf = await _section_csrf(client)

    response = await client.post(
        _USER_URL,
        data={
            "csrf_token": csrf,
            "provider": "openrouter",
            "key": "model",
            "value": "",
            "action": "enable",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Unknown action" in response.text


# ---------------------------------------------------------------------------
# Secret round-trip
# ---------------------------------------------------------------------------


async def test_secret_write_encrypts_hints_and_never_echoes_the_value(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    client = await client_factory("owner")
    csrf = await _section_csrf(client)

    response = await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": "openrouter",
            "key": "api_key",
            "value": _SECRET_VALUE,
            "action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200

    row = await _read_row(app_engine, scope="tenant", provider="openrouter", key="api_key")
    assert row is not None
    assert row.is_secret is True
    assert row.value_plain is None
    assert row.value_ciphertext is not None
    assert VaultCipher(vault_key).decrypt(row.value_ciphertext) == _SECRET_VALUE
    assert row.secret_hint == _SECRET_HINT

    body = response.text
    # The hint and the set-status are displayed by design (ADR-0112 §6)...
    assert _SECRET_HINT in body
    assert ">set<" in body
    # ...the value and its ciphertext are not, anywhere.
    assert _SECRET_VALUE not in body
    assert row.value_ciphertext.decode("utf-8") not in body


async def test_short_secret_gets_no_hint(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    """Below eight characters, the last four would be most of the secret."""
    client = await client_factory("owner")
    csrf = await _section_csrf(client)

    response = await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": "openfigi",
            "key": "api_key",
            "value": "abc123",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200

    row = await _read_row(app_engine, scope="tenant", provider="openfigi", key="api_key")
    assert row is not None
    assert row.secret_hint is None
    assert "abc123" not in response.text
    assert "no hint stored" in response.text


# ---------------------------------------------------------------------------
# Write-only semantics
# ---------------------------------------------------------------------------


async def test_empty_secret_save_leaves_the_stored_row_untouched(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    client = await client_factory("owner")
    csrf = await _section_csrf(client)

    await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": "openrouter",
            "key": "api_key",
            "value": _SECRET_VALUE,
            "action": "save",
        },
        follow_redirects=False,
    )
    before = await _read_row(app_engine, scope="tenant", provider="openrouter", key="api_key")
    assert before is not None

    response = await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": "openrouter",
            "key": "api_key",
            "value": "",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "left unchanged" in response.text

    after = await _read_row(app_engine, scope="tenant", provider="openrouter", key="api_key")
    assert after is not None
    assert after.value_ciphertext == before.value_ciphertext
    assert after.secret_hint == before.secret_hint
    assert after.updated_at == before.updated_at


async def test_empty_config_save_is_a_validation_error(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    """Delete is how a config field is unset — an empty value is not."""
    client = await client_factory("owner")
    csrf = await _section_csrf(client)

    response = await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": "openrouter",
            "key": "model",
            "value": "   ",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "needs a value" in response.text
    assert await _read_row(app_engine, scope="tenant", provider="openrouter", key="model") is None


# ---------------------------------------------------------------------------
# Enable / disable / delete
# ---------------------------------------------------------------------------


async def test_enable_disable_and_delete_round_trip(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    client = await client_factory("owner")
    csrf = await _section_csrf(client)
    base = {"csrf_token": csrf, "provider": "openrouter", "key": "base_url"}

    saved = await client.post(
        _TENANT_URL,
        data={**base, "value": "https://openrouter.ai/api/v1", "action": "save"},
        follow_redirects=False,
    )
    assert saved.status_code == 200
    row = await _read_row(app_engine, scope="tenant", provider="openrouter", key="base_url")
    assert row is not None
    assert row.value_plain == "https://openrouter.ai/api/v1"
    assert row.enabled is True

    disabled = await client.post(
        _TENANT_URL,
        data={**base, "value": "", "action": "disable"},
        follow_redirects=False,
    )
    assert disabled.status_code == 200
    row = await _read_row(app_engine, scope="tenant", provider="openrouter", key="base_url")
    assert row is not None and row.enabled is False
    # The value survives a disable — it is suspended, not erased.
    assert row.value_plain == "https://openrouter.ai/api/v1"

    enabled = await client.post(
        _TENANT_URL,
        data={**base, "value": "", "action": "enable"},
        follow_redirects=False,
    )
    assert enabled.status_code == 200
    row = await _read_row(app_engine, scope="tenant", provider="openrouter", key="base_url")
    assert row is not None and row.enabled is True

    removed = await client.post(
        _TENANT_URL,
        data={**base, "value": "", "action": "delete"},
        follow_redirects=False,
    )
    assert removed.status_code == 200
    assert (
        await _read_row(app_engine, scope="tenant", provider="openrouter", key="base_url") is None
    )


async def test_scraper_model_saves_and_deletes_at_tenant_scope(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    """ADR-0123's field is an ordinary tenant config row, end to end.

    No migration and no code path of its own: it round-trips through the same
    write surface every other config field uses, which is the whole point of a
    taxonomy-driven form.
    """
    client = await client_factory("owner")
    csrf = await _section_csrf(client)
    base = {"csrf_token": csrf, "provider": "openrouter", "key": "scraper_model"}

    saved = await client.post(
        _TENANT_URL,
        data={**base, "value": "anthropic/claude-opus-4-7", "action": "save"},
        follow_redirects=False,
    )
    assert saved.status_code == 200
    assert "Report Scraper model saved." in saved.text
    row = await _read_row(app_engine, scope="tenant", provider="openrouter", key="scraper_model")
    assert row is not None
    assert row.value_plain == "anthropic/claude-opus-4-7"
    assert row.is_secret is False
    assert row.enabled is True

    removed = await client.post(
        _TENANT_URL,
        data={**base, "value": "", "action": "delete"},
        follow_redirects=False,
    )
    assert removed.status_code == 200
    assert (
        await _read_row(app_engine, scope="tenant", provider="openrouter", key="scraper_model")
        is None
    )


async def test_delete_of_an_absent_row_is_refused_inline(
    client_factory: Any,
    vault_key: str,
) -> None:
    client = await client_factory("owner")
    csrf = await _section_csrf(client)

    response = await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": "openrouter",
            "key": "irene_model",
            "value": "",
            "action": "delete",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "No stored value" in response.text


# ---------------------------------------------------------------------------
# User self-service
# ---------------------------------------------------------------------------


async def test_user_scope_write_is_private_to_its_user(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
    vault_key: str,
) -> None:
    member_client = await client_factory("member")
    csrf = await _section_csrf(member_client)

    response = await member_client.post(
        _USER_URL,
        data={
            "csrf_token": csrf,
            "provider": "openrouter",
            "key": "model",
            "value": "anthropic/claude-my-own-model",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "anthropic/claude-my-own-model" in response.text

    member_id = seeded_users["member"][0]
    row = await _read_row(
        app_engine, scope="user", provider="openrouter", key="model", user_id=member_id
    )
    assert row is not None
    assert row.value_plain == "anthropic/claude-my-own-model"

    # A second user's panel is blind to it (the repository's user filter).
    other_client = await client_factory("other_member")
    other_section = await other_client.get(_SECTION_URL, follow_redirects=False)
    assert other_section.status_code == 200
    assert "anthropic/claude-my-own-model" not in other_section.text


async def test_user_write_ignores_a_smuggled_user_id(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
    vault_key: str,
) -> None:
    """The user id comes from the session, never from the form."""
    member_client = await client_factory("member")
    csrf = await _section_csrf(member_client)
    victim_id = seeded_users["other_member"][0]

    response = await member_client.post(
        _USER_URL,
        data={
            "csrf_token": csrf,
            "provider": "openrouter",
            "key": "model",
            "value": "anthropic/smuggled",
            "action": "save",
            "user_id": str(victim_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 200

    # The row landed on the caller...
    assert (
        await _read_row(
            app_engine,
            scope="user",
            provider="openrouter",
            key="model",
            user_id=seeded_users["member"][0],
        )
        is not None
    )
    # ...and nowhere near the smuggled id.
    assert (
        await _read_row(
            app_engine, scope="user", provider="openrouter", key="model", user_id=victim_id
        )
        is None
    )


# ---------------------------------------------------------------------------
# Vault unconfigured — degrade visibly, never silently
# ---------------------------------------------------------------------------


async def test_unconfigured_vault_banners_and_refuses_secret_writes(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_unconfigured: None,
) -> None:
    client = await client_factory("owner")
    section = await client.get(_SECTION_URL, follow_redirects=False)

    assert section.status_code == 200
    assert "not configured" in section.text
    assert "docs/deploy/credential-vault.md" in section.text
    # Secret inputs are disabled; config inputs are untouched.
    assert "disabled" in section.text

    csrf = await _section_csrf(client)
    refused = await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": "openrouter",
            "key": "api_key",
            "value": _SECRET_VALUE,
            "action": "save",
        },
        follow_redirects=False,
    )
    assert refused.status_code == 400
    assert "CREDENTIAL_VAULT_MASTER_KEY" in refused.text
    assert _SECRET_VALUE not in refused.text
    assert await _read_row(app_engine, scope="tenant", provider="openrouter", key="api_key") is None


async def test_unconfigured_vault_still_saves_config_fields(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_unconfigured: None,
) -> None:
    client = await client_factory("owner")
    csrf = await _section_csrf(client)

    response = await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": "openrouter",
            "key": "model",
            "value": "anthropic/claude-opus-4-7",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200

    row = await _read_row(app_engine, scope="tenant", provider="openrouter", key="model")
    assert row is not None
    assert row.value_plain == "anthropic/claude-opus-4-7"


# ---------------------------------------------------------------------------
# Telegram pairing block (ADR-0112 §5, strand F5)
# ---------------------------------------------------------------------------


async def _write_chat_binding(engine: AsyncEngine, user_id: UUID, chat_id: str) -> None:
    """Write the row the bot writes when a code is redeemed."""
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
        await ScopedSettingRepository(session).upsert(
            scope="user",
            provider="telegram",
            key="chat_id",
            is_secret=False,
            value_plain=chat_id,
            user_id=user_id,
        )


@pytest.mark.parametrize("role", ["owner", "member", "auditor"])
async def test_the_pairing_block_renders_unpaired_for_every_role(
    client_factory: Any,
    vault_key: str,
    role: str,
) -> None:
    """Pairing is self-service: every authenticated role gets the block."""
    client = await client_factory(role)
    response = await client.get(_SECTION_URL, follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    assert "not paired" in body
    assert "Generate pairing code" in body
    assert "Revoke pairing" not in body


async def test_generating_a_code_shows_it_once_and_never_logs_it(
    client_factory: Any,
    vault_key: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The code is a bearer token for five minutes: rendered once, logged never."""
    client = await client_factory("member")
    csrf = await _section_csrf(client)

    with caplog.at_level(logging.DEBUG):
        response = await client.post(_PAIR_URL, data={"csrf_token": csrf}, follow_redirects=False)

    assert response.status_code == 200
    match = re.search(r"/pair ([A-Z0-9]{8})", response.text)
    assert match is not None, "the rendered block should carry the code"
    code = match.group(1)
    assert telegram_pairing.pending_code_count() == 1

    # The log line records that a code was issued, and nothing more.
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "telegram pairing: code issued" in logged
    assert code not in logged

    # A re-render (no POST) does not show it again.
    again = await client.get(_SECTION_URL, follow_redirects=False)
    assert code not in again.text


async def test_a_second_code_invalidates_the_first(
    client_factory: Any,
    vault_key: str,
) -> None:
    client = await client_factory("member")
    csrf = await _section_csrf(client)

    first = await client.post(_PAIR_URL, data={"csrf_token": csrf}, follow_redirects=False)
    second = await client.post(_PAIR_URL, data={"csrf_token": csrf}, follow_redirects=False)

    first_code = re.search(r"/pair ([A-Z0-9]{8})", first.text).group(1)  # type: ignore[union-attr]
    second_code = re.search(r"/pair ([A-Z0-9]{8})", second.text).group(1)  # type: ignore[union-attr]

    assert first_code != second_code
    assert telegram_pairing.pending_code_count() == 1
    assert telegram_pairing.redeem_code(first_code, tenant_id=SENTINEL_TENANT_ID) is None


async def test_a_generated_code_binds_the_issuing_user_in_their_tenant(
    client_factory: Any,
    seeded_users: dict[str, tuple[UUID, str]],
    vault_key: str,
) -> None:
    """What the bot redeems must be exactly the session's tenant and user."""
    client = await client_factory("member")
    csrf = await _section_csrf(client)

    response = await client.post(_PAIR_URL, data={"csrf_token": csrf}, follow_redirects=False)
    code = re.search(r"/pair ([A-Z0-9]{8})", response.text).group(1)  # type: ignore[union-attr]

    assert (
        telegram_pairing.redeem_code(code, tenant_id=SENTINEL_TENANT_ID)
        == (seeded_users["member"][0])
    )


async def test_a_paired_block_offers_revoke_and_revoking_deletes_the_row(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
    vault_key: str,
) -> None:
    member_id = seeded_users["member"][0]
    await _write_chat_binding(app_engine, member_id, "555001")

    client = await client_factory("member")
    section = await client.get(_SECTION_URL, follow_redirects=False)
    assert "paired" in section.text
    assert "555001" in section.text
    assert "Revoke pairing" in section.text

    csrf = await _section_csrf(client)
    response = await client.post(_UNPAIR_URL, data={"csrf_token": csrf}, follow_redirects=False)

    assert response.status_code == 200
    assert "not paired" in response.text
    assert (
        await _read_row(
            app_engine, scope="user", provider="telegram", key="chat_id", user_id=member_id
        )
        is None
    )


async def test_revoking_also_drops_a_pending_code(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
    vault_key: str,
) -> None:
    """An outstanding code must not re-bind the chat a moment after a revoke."""
    member_id = seeded_users["member"][0]
    await _write_chat_binding(app_engine, member_id, "555001")

    client = await client_factory("member")
    csrf = await _section_csrf(client)
    issued = await client.post(_PAIR_URL, data={"csrf_token": csrf}, follow_redirects=False)
    code = re.search(r"/pair ([A-Z0-9]{8})", issued.text).group(1)  # type: ignore[union-attr]

    await client.post(_UNPAIR_URL, data={"csrf_token": csrf}, follow_redirects=False)

    assert telegram_pairing.redeem_code(code, tenant_id=SENTINEL_TENANT_ID) is None


async def test_revoking_without_a_pairing_is_refused_inline(
    client_factory: Any,
    vault_key: str,
) -> None:
    client = await client_factory("member")
    csrf = await _section_csrf(client)

    response = await client.post(_UNPAIR_URL, data={"csrf_token": csrf}, follow_redirects=False)

    assert response.status_code == 400
    assert "No Telegram chat is paired" in response.text


async def test_one_users_pairing_is_invisible_to_another(
    client_factory: Any,
    app_engine: AsyncEngine,
    seeded_users: dict[str, tuple[UUID, str]],
    vault_key: str,
) -> None:
    """The row is user-filtered by the repository, like every user-scope row."""
    await _write_chat_binding(app_engine, seeded_users["member"][0], "555001")

    other = await client_factory("other_member")
    response = await other.get(_SECTION_URL, follow_redirects=False)

    assert "555001" not in response.text
    assert "not paired" in response.text


@pytest.mark.parametrize("url_name", ["pair", "unpair"])
async def test_the_pairing_endpoints_require_csrf(
    client_factory: Any,
    vault_key: str,
    url_name: str,
) -> None:
    client = await client_factory("member")
    url = _PAIR_URL if url_name == "pair" else _UNPAIR_URL

    response = await client.post(url, data={}, follow_redirects=False)

    assert response.status_code == 403
    assert telegram_pairing.pending_code_count() == 0


@pytest.mark.parametrize("url_name", ["pair", "unpair"])
async def test_the_pairing_endpoints_require_a_session(
    client_factory: Any,
    vault_key: str,
    url_name: str,
) -> None:
    client = await client_factory()  # not logged in
    url = _PAIR_URL if url_name == "pair" else _UNPAIR_URL

    response = await client.post(url, data={"csrf_token": "whatever"}, follow_redirects=False)

    assert response.status_code in (302, 303, 401, 403), response.text
    assert telegram_pairing.pending_code_count() == 0


# ---------------------------------------------------------------------------
# OpenRouter model catalog — the datalist behind the two model fields
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog_stub(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the catalog client with a recorder, and record what it got.

    The route imports :func:`fetch_models` by name, so patching the name in
    the route module is what a caller actually reaches. Nothing here talks
    to a network — the client's own wire behaviour is pinned in
    ``tests/services/test_openrouter_catalog.py``.

    Returns:
        A dict with ``calls`` (list of ``(base_url, api_key)``) and a
        mutable ``result``: a list of models to return, or an exception
        instance to raise.
    """
    state: dict[str, Any] = {
        "calls": [],
        "result": [
            CatalogModel(id="anthropic/claude-opus-4-8", name="Claude Opus 4.8"),
            CatalogModel(id="openai/gpt-5", name="GPT-5"),
        ],
    }

    async def _fake(base_url: str, api_key: str | None, **_kwargs: Any) -> list[CatalogModel]:
        state["calls"].append((base_url, api_key))
        result = state["result"]
        if isinstance(result, Exception):
            raise result
        return list(result)

    monkeypatch.setattr(provider_credentials, "fetch_models", _fake)
    return state


def _models_url(scope: str, key: str) -> str:
    return f"{_MODELS_URL}?scope={scope}&key={key}"


async def test_model_list_renders_as_a_datalist_for_the_field_that_asked(
    client_factory: Any,
    vault_key: str,
    catalog_stub: dict[str, Any],
) -> None:
    client = await client_factory("owner")

    response = await client.get(_models_url("tenant", "irene_model"), follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    # The id the *asking* field's `list=` points at — not the other model row's.
    assert 'id="tenant-openrouter-irene_model-models"' in body
    assert "tenant-openrouter-model-models" not in body
    assert '<option value="anthropic/claude-opus-4-8">Claude Opus 4.8</option>' in body
    assert '<option value="openai/gpt-5">GPT-5</option>' in body
    assert "2 models loaded" in _visible_text(body)
    # The button comes back, so a reload is one click away.
    assert "Load models" in body
    assert 'hx-target="#tenant-openrouter-irene_model-models-slot"' in body


async def test_model_list_serves_the_scraper_model_field(
    client_factory: Any,
    vault_key: str,
    catalog_stub: dict[str, Any],
) -> None:
    """ADR-0123's third model field is served like the other two.

    Same catalog, same slot ids derived from ``(scope, key)`` — the field
    needed no endpoint of its own, only a place in ``_MODEL_FIELD_KEYS``.
    """
    client = await client_factory("owner")

    response = await client.get(_models_url("tenant", "scraper_model"), follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    assert 'id="tenant-openrouter-scraper_model-models"' in body
    assert "tenant-openrouter-irene_model-models" not in body
    assert '<option value="anthropic/claude-opus-4-8">Claude Opus 4.8</option>' in body
    assert 'hx-target="#tenant-openrouter-scraper_model-models-slot"' in body


async def test_user_scope_model_list_serves_a_non_owner_their_own_panel(
    client_factory: Any,
    vault_key: str,
    catalog_stub: dict[str, Any],
) -> None:
    client = await client_factory("member")

    response = await client.get(_models_url("user", "model"), follow_redirects=False)

    assert response.status_code == 200
    assert 'id="user-openrouter-model-models"' in response.text


async def test_model_list_fetch_failure_renders_inline_and_keeps_free_text(
    client_factory: Any,
    vault_key: str,
    catalog_stub: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed fetch says so; it never swaps in an empty datalist."""
    catalog_stub["result"] = CatalogFetchError("the model endpoint answered HTTP 502.")
    client = await client_factory("owner")

    with caplog.at_level(logging.WARNING, logger="web.routes.provider_credentials"):
        response = await client.get(_models_url("tenant", "model"), follow_redirects=False)

    # HTTP 200 deliberately: the body is a fragment swap, and an error
    # status would leave the button standing where the message belongs.
    assert response.status_code == 200
    body = response.text
    assert "<datalist" not in body
    text = _visible_text(body)
    assert "Could not load the model list: the model endpoint answered HTTP 502." in text
    assert "You can still type a model id." in text
    # One operator line, naming the scope — and no credential.
    assert any(
        "openrouter model catalog: fetch failed scope=tenant" in record.message
        for record in caplog.records
    )


async def test_tenant_scope_model_list_is_refused_for_a_non_owner(
    client_factory: Any,
    vault_key: str,
    catalog_stub: dict[str, Any],
) -> None:
    """The same gate the tenant write endpoint carries — nothing leaves the box."""
    client = await client_factory("member")

    response = await client.get(_models_url("tenant", "model"), follow_redirects=False)

    assert response.status_code == 200
    assert "<datalist" not in response.text
    assert "owner-managed" in _visible_text(response.text)
    assert catalog_stub["calls"] == []


@pytest.mark.parametrize(
    ("scope", "key"),
    [
        ("application", "model"),  # not a scope this panel offers
        ("tenant", "api_key"),  # a secret, never an autocomplete
        ("tenant", "nonsense"),  # undeclared
        ("user", "irene_model"),  # declared, but tenant-only (taxonomy gate)
        ("user", "scraper_model"),  # likewise tenant-only (ADR-0123)
    ],
)
async def test_model_list_refuses_a_scope_or_field_it_does_not_offer(
    client_factory: Any,
    vault_key: str,
    catalog_stub: dict[str, Any],
    scope: str,
    key: str,
) -> None:
    client = await client_factory("owner")

    response = await client.get(_models_url(scope, key), follow_redirects=False)

    assert response.status_code == 200
    assert "<datalist" not in response.text
    assert "not offered for" in _visible_text(response.text)
    assert catalog_stub["calls"] == []


async def test_the_resolved_key_is_used_for_the_fetch_and_never_rendered(
    client_factory: Any,
    vault_key: str,
    catalog_stub: dict[str, Any],
) -> None:
    """The fetch is server-side: the key reaches httpx, never the browser.

    Also pins the endpoint half of the resolution — the catalog is read
    from the base URL this tenant configured, not from a hardcoded one.
    """
    client = await client_factory("owner")
    csrf = await _section_csrf(client)
    for key, value in (("api_key", _SECRET_VALUE), ("base_url", "https://llm.example.test/v1")):
        save = await client.post(
            _TENANT_URL,
            data={
                "csrf_token": csrf,
                "provider": "openrouter",
                "key": key,
                "value": value,
                "action": "save",
            },
            follow_redirects=False,
        )
        assert save.status_code == 200, save.text

    response = await client.get(_models_url("tenant", "model"), follow_redirects=False)

    assert response.status_code == 200
    assert catalog_stub["calls"] == [("https://llm.example.test/v1", _SECRET_VALUE)]
    assert _SECRET_VALUE not in response.text


async def test_a_tenant_without_a_key_still_gets_a_list(
    client_factory: Any,
    vault_key: str,
    catalog_stub: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/models`` is public, so a missing credential fetches keyless.

    An unconfigured tenant is precisely the one about to type a model id
    for the first time; refusing it the list would be backwards.
    """
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = await client_factory("owner")

    response = await client.get(_models_url("tenant", "model"), follow_redirects=False)

    assert response.status_code == 200
    assert len(catalog_stub["calls"]) == 1
    assert catalog_stub["calls"][0][1] is None
    assert "<datalist" in response.text


async def test_the_model_list_endpoint_requires_a_session(
    client_factory: Any,
    catalog_stub: dict[str, Any],
) -> None:
    client = await client_factory()  # not logged in

    response = await client.get(_models_url("tenant", "model"), follow_redirects=False)

    assert response.status_code in (302, 303, 401, 403), response.text
    assert catalog_stub["calls"] == []


async def test_the_section_offers_the_list_on_the_model_fields_and_nowhere_else(
    client_factory: Any,
    vault_key: str,
) -> None:
    """Four model inputs across the two panels; no other field gets a list."""
    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)

    assert response.status_code == 200
    body = response.text

    for expected in (
        'list="tenant-openrouter-model-models"',
        'list="tenant-openrouter-scraper_model-models"',
        'list="tenant-openrouter-irene_model-models"',
        'list="user-openrouter-model-models"',
    ):
        assert expected in body

    # Exactly those four — the OpenFIGI key, the base URL and the bot
    # token are not model ids and get no autocomplete.
    assert body.count('list="') == 4
    assert body.count("Load models") == 4
    assert "openrouter/models?scope=tenant&amp;key=model" in body
    assert "openrouter/models?scope=user&amp;key=model" in body
