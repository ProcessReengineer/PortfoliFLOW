# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Providers & Credentials surface (ADR-0112 §6, strands F3/F5).

Follows the idiom of the retired ``test_ai_settings.py`` it replaces —
live Postgres, real login flow, ``AsyncClient`` over ``ASGITransport`` —
but asserts against persisted ``scoped_settings`` rows rather than a
mutated in-process singleton.

Eleven groups:

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
* **Telegram pairing** (F5, ADR-0112 §5) — the card states which of the
  four bot states the tenant is in and drops the pairing actions when the
  deployment-wide switch is off (P-TG-H1); the block renders per state for
  every role, a generated code is shown exactly once and never logged,
  re-issuing invalidates the previous code, the code binds the *session's*
  tenant and user, revoking deletes the row and voids pending codes, and
  both endpoints demand a session and a CSRF token.
* **Choices and the Voice card** (ADR-0136 §3, §4) — a field with
  ``choices`` renders as a ``<select>`` and a forged value outside them is
  refused inline; the voice hints read their defaults and sunsets from the
  adapter declarations; the ``Voice`` card states its computed state.
* **Model catalog** — the OpenRouter ``/models`` datalist: it renders for
  the field that asked and no other, a failed fetch says so inline at
  HTTP 200 rather than swapping in an empty list, the tenant scope carries
  the owner gate and an unoffered scope/field is refused before anything
  leaves the machine, the resolved key reaches the fetch but never the
  response body, a keyless tenant still gets a list, and the section wires
  the ``list=`` attribute onto the three model inputs and nothing else.
* **Call catalog** (ADR-0137 §2, §4) — the Voice call card's Load models /
  Load voices: live models and agent-usable voices are offered, deprecated
  models and moderated voices named, a keyless card calls no vendor, a
  refusal renders inline at HTTP 200, and non-owners and unoffered fields
  are refused before the port is reached.
"""

from __future__ import annotations

import html
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

from bot.telegram_bot import BotStatus
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
from services.voice import ADAPTER_DECLARATIONS
from services.voice_call import (
    DEFAULT_BASE_URL,
    CallCatalog,
    CallModel,
    CallProviderError,
    CallVoice,
)
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
_CALL_CATALOG_URL = "/admin/providers-credentials/voice-call/catalog"


@pytest.fixture(autouse=True)
def _clean_pairing_store() -> Any:
    """Empty the in-process pairing store around every test.

    The store is module-level and process-lifetime by design (ADR-0112 §5,
    D4), so one test's code would otherwise still be pending in the next.
    """
    telegram_pairing.reset_store()
    yield
    telegram_pairing.reset_store()


#: The four verbatim status lines the Telegram card renders (P-TG-H1,
#: D-TG-1 … D-TG-4). Asserted as copy, not as a class name: the operator
#: reads these sentences, and a reworded one is a change to the surface.
_BOT_OFF_COPY = (
    "The Telegram bot is switched off for this deployment "
    "(TELEGRAM_BOT_ENABLED). Pairing is not possible until the operator "
    "enables it."
)
_BOT_IDLE_COPY = "The Telegram bot is enabled but not running. Check the web process start-up log."
_BOT_OTHER_TENANT_COPY = (
    "No bot is running for this tenant. An owner stores a bot token under "
    "Tenant credentials; token changes apply after a restart."
)
_BOT_SERVING_COPY = "The bot for this tenant is running."


@pytest.fixture(autouse=True)
def _bot_status_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report an ordinary, healthy bot to every test that does not say otherwise.

    ``tests/web/conftest.py`` forces ``TELEGRAM_BOT_ENABLED=false`` for this
    whole package — a live aiogram bot in a test process would steal the
    production bot's update stream — so the card's *real* state here is
    "switched off for this deployment", which hides the pairing actions
    (P-TG-H1). That is a statement about the test process, not about the
    pairing flow the tests below are for, so the seam is patched to the
    healthy state and the four state tests patch it themselves.
    """
    monkeypatch.setattr(
        "bot.telegram_bot.bot_status",
        lambda tenant_id: BotStatus(env_enabled=True, running=True, tenant_served=True),
    )


def _url(value: str | None) -> str:
    """Narrow a configured URL to ``str``; ``_require_db`` already skipped if unset."""
    assert value is not None
    return value


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
    engine = create_async_engine(_url(DATABASE_URL_SUPERUSER), future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """An RLS-subject engine for reading rows back the way the app writes them."""
    _require_db()
    engine = create_async_engine(_url(DATABASE_URL), future=True, poolclass=NullPool)
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


def _card_copy(html: str) -> str:
    """Return the rendered copy as one whitespace-normalised line.

    Builds on :func:`_visible_text`, which turns every tag boundary into a
    space: an inline ``<code>`` mid-sentence would otherwise pad the
    parentheses around it, and the status lines name an environment
    variable that way.
    """
    return " ".join(_visible_text(html).split()).replace("( ", "(").replace(" )", ")")


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
        "The LLM provider behind Shirley, the Report Scraper, web research and the "
        "Watch Desk monitoring notes." in body
    )
    assert "The model Shirley uses." in body
    assert (
        "The model that extracts figures from uploaded GP reports. "
        "Must be an Anthropic model (PDF input)." in body
    )
    assert (
        "The model that filters the RSS candidates and extracts the articles "
        "Shirley&#39;s web research reads. Leave empty to use the Shirley model; "
        "applies on the next query." in body
    )
    # The key hint names every surface the key is spent on.
    assert (
        "Used for every Shirley turn, Report Scraper run, web research query and "
        "Watch Desk beat in this tenant." in body
    )

    # The four OpenRouter model rows no longer all read "Model"...
    assert "Watch Desk model" in body
    assert "Report Scraper model" in body
    assert 'value="scraper_model"' in body
    assert "Web research model" in body
    assert 'value="research_model"' in body
    # ...and the pill says a save is live, not merely stored.
    assert "live — saves apply instantly" in body

    # The three voice cards state their purpose as well (ADR-0118 §7).
    assert "Voice input and spoken replies for Shirley — the on/off switch for this tenant." in body
    assert "Transcribes recorded questions, on the web chat and in Telegram voice messages." in body
    assert "The voice Shirley answers with, as browser audio and Telegram voice notes." in body

    # The TTS card's persona-voice row reads "Voice" — the same word as the
    # card family, so the hint carries the distinction.
    assert '<span class="pf-credentials__label">Voice</span>' in body
    assert 'id="tenant-voice_tts-voice-hint">The voice Shirley speaks with.' in body

    # The wire value keeps the taxonomy key; nothing an operator reads does.
    assert 'value="irene_model"' in body
    assert "irene" not in _visible_text(body).lower()


async def test_provider_channel_card_is_dormant_and_says_what_leaves_the_instance(
    client_factory: Any,
    vault_key: str,
) -> None:
    """ADR-0131 §4 / ADR-0129 §6: the card exists, is marked dormant, and its hint is the data-minimisation statement."""
    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)

    assert response.status_code == 200
    body = response.text

    # The card appears because the declaration exists (ADR-0118 §7), and its
    # pill says nothing reads the switch yet — a truth, not a promise.
    dormant = "dormant — nothing reads this switch yet; the provider channel lands in Stage B"
    assert "Provider channel" in body
    assert dormant in body
    assert re.search(
        rf">Provider channel</h4>\s*<span[^>]*>{re.escape(dormant)}</span>",
        body,
    )
    assert (
        "The provider channel — signed provider directory and encrypted ticket exports. "
        "Off by default; this is the on/off switch for this tenant." in body
    )

    # The hint is the ADR-0129 §6 statement of what leaves the instance, tied
    # to the row by its id, and it reaches the page once.
    first_sentence = (
        "When on, this instance fetches the signed provider directory from portfoliflow.com — "
        "the list version only, never a query — and offers encrypted exports of individual "
        "tickets."
    )
    assert f'id="tenant-provider_channel-enabled-hint">{first_sentence}' in body
    assert body.count(first_sentence) == 1

    # The wire value is the taxonomy key, as a hidden ``provider`` input.
    assert body.count('<input type="hidden" name="provider" value="provider_channel">') >= 1

    # The three boolean ``enabled`` fields render as a switch: a checkbox
    # inside a ``pf-switch`` label, never a free-text input and never the
    # write-only password control a secret gets.
    for row_id in (
        "tenant-telegram-enabled",
        "tenant-voice-enabled",
        "tenant-provider_channel-enabled",
    ):
        assert re.search(
            rf'<label class="pf-switch"><input type="checkbox"\s+id="{row_id}"', body
        ), row_id
        assert not re.search(rf'<input type="text"\s+id="{row_id}"', body), row_id
        assert not re.search(rf'<input type="password"\s+id="{row_id}"', body), row_id


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
        # Likewise tenant-only (ADR-0132): web research is a tenant
        # capability, and one field serves both of its LLMs.
        ("openrouter", "research_model", "may not be written at user scope"),
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
# Choices and the Voice card (ADR-0136 §3, §4)
# ---------------------------------------------------------------------------

#: Every environment variable the voice chains read; pinned by the state-line
#: test so the repository ``.env`` this module loads cannot decide its outcome.
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


def _select_options(html: str, select_id: str) -> list[tuple[str, bool, bool]]:
    """Return ``(value, disabled, selected)`` for each option of one ``<select>``."""
    match = re.search(rf'<select id="{select_id}"[^>]*>(.*?)</select>', html, re.DOTALL)
    assert match is not None, f"no <select id={select_id!r}>"
    return [
        (value, "disabled" in attrs, "selected" in attrs)
        for value, attrs in re.findall(r'<option value="([^"]*)"([^>]*)>', match.group(1))
    ]


def _card_html(html: str, title: str) -> str:
    """Return one tenant card's markup, from its title to the next card."""
    start = html.index(f'<h4 class="pf-credentials__card-title">{title}</h4>')
    end = html.find("<article", start)
    return html[start:] if end == -1 else html[start:end]


async def _write_tenant_config(engine: AsyncEngine, provider: str, key: str, value: str) -> None:
    """Write one tenant-scope config row exactly as the surface would."""
    async with tenant_context(engine, SENTINEL_TENANT_ID) as session:
        await ScopedSettingRepository(session).upsert(
            scope="tenant",
            provider=provider,
            key=key,
            is_secret=False,
            value_plain=value,
        )


async def test_a_choices_field_renders_as_a_select_with_the_vocabulary(
    client_factory: Any,
    vault_key: str,
) -> None:
    """Both provider fields are a ``<select>``; an ordinary config field stays a text input."""
    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    for select_id in ("tenant-voice_stt-provider", "tenant-voice_tts-provider"):
        assert f'<select id="{select_id}"' in body
        assert _select_options(body, select_id) == [
            ("", True, True),
            ("elevenlabs", False, False),
            ("openai", False, False),
            ("openai_compatible", False, False),
        ]
    assert re.search(r'<input type="text"\s+id="tenant-voice_stt-model"', body)
    assert "Not set</option>" in body


async def test_a_forged_value_outside_choices_is_rejected_inline(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    """A value the ``<select>`` never offered is refused before the repository."""
    client = await client_factory("owner")
    csrf = await _section_csrf(client)

    response = await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": "voice_stt",
            "key": "provider",
            "value": "deepgram",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "must be one of: elevenlabs, openai, openai_compatible" in response.text
    assert await _read_row(app_engine, scope="tenant", provider="voice_stt", key="provider") is None


async def test_a_value_inside_choices_saves_and_the_select_shows_it_selected(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    """A legal choice is an ordinary config row, and the re-render selects it."""
    client = await client_factory("owner")
    csrf = await _section_csrf(client)

    response = await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": "voice_tts",
            "key": "provider",
            "value": "openai_compatible",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    row = await _read_row(app_engine, scope="tenant", provider="voice_tts", key="provider")
    assert row is not None
    assert row.value_plain == "openai_compatible"
    assert _select_options(response.text, "tenant-voice_tts-provider") == [
        ("", True, False),
        ("elevenlabs", False, False),
        ("openai", False, False),
        ("openai_compatible", False, True),
    ]


def _switch_input(html: str, input_id: str) -> str:
    """Return the opening tag of one switch's checkbox."""
    match = re.search(rf'<input type="checkbox"\s+id="{input_id}"[^>]*>', html)
    assert match is not None, f"no switch checkbox {input_id!r}"
    return match.group(0)


def _row_html(html: str, input_id: str) -> str:
    """Return one field row's markup, from its row start to the next row."""
    position = html.index(f'id="{input_id}"')
    start = html.rindex('<div class="pf-credentials__row">', 0, position)
    end = html.find('<div class="pf-credentials__row">', position)
    return html[start:] if end == -1 else html[start:end]


async def test_a_switch_field_saves_true_and_false_and_renders_its_state(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    """Each flip stores the boolean pair and the re-render offers the opposite flip."""
    client = await client_factory("owner")
    csrf = await _section_csrf(client)

    for value, other, state in (("true", "false", "on"), ("false", "true", "off")):
        response = await client.post(
            _TENANT_URL,
            data={
                "csrf_token": csrf,
                "provider": "voice",
                "key": "enabled",
                "value": value,
                "action": "save",
            },
            follow_redirects=False,
        )
        assert response.status_code == 200, value
        row = await _read_row(app_engine, scope="tenant", provider="voice", key="enabled")
        assert row is not None
        assert row.value_plain == value

        checkbox = _switch_input(response.text, "tenant-voice-enabled")
        assert (re.search(r"\schecked\b", checkbox) is not None) is (value == "true"), value
        assert f'hx-vals=\'{{"value": "{other}"}}\'' in checkbox, value
        row_html = _row_html(response.text, "tenant-voice-enabled")
        assert f'<span class="pf-credentials__switch-state">{state}</span>' in row_html, value


async def test_a_disabled_switch_row_renders_off_and_offers_true(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    """A stored ``true`` that is disabled is off on the card, as the consumers read it."""
    client = await client_factory("owner")
    csrf = await _section_csrf(client)
    base = {"csrf_token": csrf, "provider": "voice", "key": "enabled"}
    saved = await client.post(
        _TENANT_URL, data={**base, "value": "true", "action": "save"}, follow_redirects=False
    )
    assert saved.status_code == 200

    disabled = await client.post(
        _TENANT_URL, data={**base, "value": "", "action": "disable"}, follow_redirects=False
    )

    assert disabled.status_code == 200
    row = await _read_row(app_engine, scope="tenant", provider="voice", key="enabled")
    assert row is not None
    assert row.value_plain == "true"
    assert row.enabled is False
    checkbox = _switch_input(disabled.text, "tenant-voice-enabled")
    assert re.search(r"\schecked\b", checkbox) is None
    assert 'hx-vals=\'{"value": "true"}\'' in checkbox
    row_html = _row_html(disabled.text, "tenant-voice-enabled")
    assert '<span class="pf-credentials__switch-state">off</span>' in row_html


@pytest.mark.parametrize("value", ["yes", "1", "on"])
async def test_a_switch_field_rejects_anything_but_the_boolean_pair(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
    value: str,
) -> None:
    """A forged POST outside ``true`` / ``false`` is refused before the repository."""
    client = await client_factory("owner")
    csrf = await _section_csrf(client)

    response = await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": "voice",
            "key": "enabled",
            "value": value,
            "action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "voice.enabled must be one of: false, true." in response.text
    assert await _read_row(app_engine, scope="tenant", provider="voice", key="enabled") is None


async def test_a_switch_row_has_no_save_button_but_keeps_remove(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    """The switch saves on change; a stored row is still removable."""
    await _write_tenant_config(app_engine, "voice", "enabled", "true")
    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)
    assert response.status_code == 200

    row_html = _row_html(response.text, "tenant-voice-enabled")
    before_meta = row_html[: row_html.index('<div class="pf-credentials__meta">')]
    assert ">Save</button>" not in before_meta
    assert re.search(
        r'<input type="hidden" name="provider" value="voice">\s*'
        r'<input type="hidden" name="key" value="enabled">\s*'
        r'<input type="hidden" name="action" value="delete">\s*'
        r'<button type="submit" class="btn btn--ghost">Remove</button>',
        row_html,
    )


@pytest.mark.parametrize(
    ("env", "tenant_rows", "expected"),
    [
        ({}, [], "off"),
        (
            {"VOICE_ENABLED": "true"},
            [],
            "on — speech-to-text: inactive, no provider selected · "
            "text-to-speech: inactive, no provider selected",
        ),
        (
            {"VOICE_ENABLED": "true", "VOICE_STT_PROVIDER": "openai"},
            [],
            "speech-to-text: active (openai)",
        ),
        (
            {"VOICE_ENABLED": "true"},
            [("voice_tts", "provider", "openai")],
            "unreachable without speech-to-text",
        ),
    ],
    ids=["env-silent", "enabled-only", "stt-from-env", "tts-only-from-tenant-row"],
)
async def test_the_voice_card_states_its_computed_state(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    tenant_rows: list[tuple[str, str, str]],
    expected: str,
) -> None:
    """The ``Voice`` card carries the state line; the two half cards carry none."""
    for var in _VOICE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    for provider, key, value in tenant_rows:
        await _write_tenant_config(app_engine, provider, key, value)

    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    voice_card = _card_html(body, "Voice")
    notes = re.findall(r'<p class="pf-credentials__note">([^<]*)</p>', voice_card)
    assert len(notes) == 1, notes
    assert expected in notes[0]
    if expected == "off":
        assert notes[0] == "off"
    for half in ("Voice — speech-to-text", "Voice — text-to-speech"):
        assert "pf-credentials__note" not in _card_html(body, half)


async def test_the_voice_call_card_carries_its_copy_and_fields(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    """The ``Voice call`` card: label, compliance description, live pill, seven fields, hints."""
    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)
    assert response.status_code == 200
    card = _card_html(response.text, "Voice call")

    assert "Audio and transcript go to the voice vendor; see the handbook." in card
    assert '<span class="pf-section__pill">live — saves apply instantly</span>' in card
    # Field rows in the declaration's order — judge by the input ids.
    ids = list(dict.fromkeys(re.findall(r'id="tenant-voice_call-([a-z_]+)"', card)))
    assert ids == ["api_key", "enabled", "agent_id", "model", "voice", "language", "base_url"]
    assert "A key with the Agents write permission." in card
    assert "Leave empty to use the text-to-speech voice" in card
    assert "An ElevenLabs voice id without live moderation." in card
    assert "Leave empty for German (de)." in card
    # The switch renders as a switch, off by default.
    assert _switch_input(card, "tenant-voice_call-enabled")
    assert '<span class="pf-credentials__switch-state">off</span>' in card


@pytest.mark.parametrize(
    ("tenant_rows", "store_key", "expected"),
    [
        ([], False, "inactive — switch off"),
        ([("voice_call", "enabled", "true")], False, "inactive — no key"),
        ([("voice_call", "enabled", "true")], True, "active, agent not yet provisioned"),
        (
            [("voice_call", "enabled", "true"), ("voice_call", "agent_id", "agent_abc123")],
            True,
            "active (agent agent_abc123)",
        ),
    ],
    ids=["switch-off", "no-key", "not-provisioned", "provisioned"],
)
async def test_the_voice_call_card_states_its_computed_state(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
    monkeypatch: pytest.MonkeyPatch,
    tenant_rows: list[tuple[str, str, str]],
    store_key: bool,
    expected: str,
) -> None:
    """ADR-0137 §7: four states from the rows; an environment variable changes nothing."""
    # There is no such link; setting it proves the line reads the rows only.
    monkeypatch.setenv("VOICE_CALL_ENABLED", "true")
    monkeypatch.setenv("VOICE_CALL_API_KEY", "sk-env-must-be-ignored")
    for provider, key, value in tenant_rows:
        await _write_tenant_config(app_engine, provider, key, value)

    client = await client_factory("owner")
    if store_key:
        csrf = await _section_csrf(client)
        saved = await client.post(
            _TENANT_URL,
            data={
                "csrf_token": csrf,
                "provider": "voice_call",
                "key": "api_key",
                "value": "sk-test-agents-write",
                "action": "save",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 200

    response = await client.get(_SECTION_URL, follow_redirects=False)
    assert response.status_code == 200
    card = _card_html(response.text, "Voice call")
    notes = re.findall(r'<p class="pf-credentials__note">([^<]*)</p>', card)
    assert notes == [expected]


async def test_voice_hints_carry_the_declared_defaults_and_sunsets(
    client_factory: Any,
    vault_key: str,
) -> None:
    """The defaults and sunset dates on the card are the declaration's, not a copy."""
    declaration = ADAPTER_DECLARATIONS["openai"]
    assert declaration.stt_sunset is not None and declaration.tts_sunset is not None

    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)
    assert response.status_code == 200
    copy = _card_copy(response.text)

    for value in (
        declaration.stt_model,
        declaration.tts_model,
        declaration.tts_voice,
        declaration.stt_sunset.isoformat(),
        declaration.tts_sunset.isoformat(),
    ):
        assert value is not None
        assert value in copy, value


async def test_voice_hints_carry_the_elevenlabs_defaults_and_the_voice_id_note(
    client_factory: Any,
    vault_key: str,
) -> None:
    """The ElevenLabs defaults on the card are the declaration's; the voice is an ID."""
    declaration = ADAPTER_DECLARATIONS["elevenlabs"]

    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)
    assert response.status_code == 200
    copy = _card_copy(response.text)

    for value in (declaration.stt_model, declaration.tts_model, declaration.base_url):
        assert value is not None
        assert value in copy, value
    assert "for elevenlabs the voice ID, not a name" in copy


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


async def test_research_model_saves_and_deletes_at_tenant_scope(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
) -> None:
    """ADR-0132's field is an ordinary tenant config row, end to end.

    Like its ADR-0123 twin: no migration and no code path of its own, which
    is what a taxonomy-driven form buys.
    """
    client = await client_factory("owner")
    csrf = await _section_csrf(client)
    base = {"csrf_token": csrf, "provider": "openrouter", "key": "research_model"}

    saved = await client.post(
        _TENANT_URL,
        data={**base, "value": "anthropic/claude-haiku-4-5", "action": "save"},
        follow_redirects=False,
    )
    assert saved.status_code == 200
    assert "Web research model saved." in saved.text
    row = await _read_row(app_engine, scope="tenant", provider="openrouter", key="research_model")
    assert row is not None
    assert row.value_plain == "anthropic/claude-haiku-4-5"
    assert row.is_secret is False
    assert row.enabled is True

    removed = await client.post(
        _TENANT_URL,
        data={**base, "value": "", "action": "delete"},
        follow_redirects=False,
    )
    assert removed.status_code == 200
    assert (
        await _read_row(app_engine, scope="tenant", provider="openrouter", key="research_model")
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


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (BotStatus(env_enabled=False, running=False, tenant_served=False), _BOT_OFF_COPY),
        (BotStatus(env_enabled=True, running=False, tenant_served=False), _BOT_IDLE_COPY),
        (BotStatus(env_enabled=True, running=True, tenant_served=False), _BOT_OTHER_TENANT_COPY),
        (BotStatus(env_enabled=True, running=True, tenant_served=True), _BOT_SERVING_COPY),
    ],
    ids=["switched-off", "not-running", "other-tenants-only", "serving"],
)
async def test_the_card_states_which_bot_state_the_tenant_is_in(
    client_factory: Any,
    vault_key: str,
    monkeypatch: pytest.MonkeyPatch,
    status: BotStatus,
    expected: str,
) -> None:
    """One line per state, and only that one (P-TG-H1).

    The two-level switch (ADR-0112 §5) used to be invisible here: a tenant
    could store a token, set its Enabled field and mint pairing codes
    against a deployment whose master switch was off, and the card said
    nothing at all.
    """
    monkeypatch.setattr("bot.telegram_bot.bot_status", lambda tenant_id: status)

    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)

    assert response.status_code == 200
    copy = _card_copy(response.text)
    assert expected in copy
    for other in (_BOT_OFF_COPY, _BOT_IDLE_COPY, _BOT_OTHER_TENANT_COPY, _BOT_SERVING_COPY):
        if other != expected:
            assert other not in copy


async def test_a_deployment_wide_off_switch_withdraws_the_pairing_actions(
    client_factory: Any,
    vault_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No code could ever be redeemed, so none is offered — the card explains.

    The paired / not-paired pill stays either way: an existing pairing must
    remain visible, only the actions go.
    """
    monkeypatch.setattr(
        "bot.telegram_bot.bot_status",
        lambda tenant_id: BotStatus(env_enabled=False, running=False, tenant_served=False),
    )

    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)

    assert response.status_code == 200
    assert _PAIR_URL not in response.text
    assert _UNPAIR_URL not in response.text
    assert "Generate pairing code" not in response.text
    assert "not paired" in response.text


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


async def test_model_list_serves_the_research_model_field(
    client_factory: Any,
    vault_key: str,
    catalog_stub: dict[str, Any],
) -> None:
    """ADR-0132's fourth model field is served like the other three.

    Same catalog, same slot ids derived from ``(scope, key)`` — the field
    needed no endpoint of its own, only a place in ``_MODEL_FIELD_KEYS``.
    """
    client = await client_factory("owner")

    response = await client.get(_models_url("tenant", "research_model"), follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    assert 'id="tenant-openrouter-research_model-models"' in body
    assert "tenant-openrouter-scraper_model-models" not in body
    assert '<option value="anthropic/claude-opus-4-8">Claude Opus 4.8</option>' in body
    assert 'hx-target="#tenant-openrouter-research_model-models-slot"' in body


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
        ("user", "research_model"),  # likewise tenant-only (ADR-0132)
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
    """Seven list inputs: five OpenRouter model fields, the call's Model and Voice."""
    client = await client_factory("owner")
    response = await client.get(_SECTION_URL, follow_redirects=False)

    assert response.status_code == 200
    body = response.text

    for expected in (
        'list="tenant-openrouter-model-models"',
        'list="tenant-openrouter-scraper_model-models"',
        'list="tenant-openrouter-research_model-models"',
        'list="tenant-openrouter-irene_model-models"',
        'list="user-openrouter-model-models"',
    ):
        assert expected in body

    # Exactly those five plus the Voice call card's Model and Voice — the
    # OpenFIGI key, the base URL and the bot token are not model ids and get
    # no autocomplete. Six "Load models": five OpenRouter, the call's Model.
    assert body.count('list="') == 7
    assert body.count("Load models") == 6
    assert "openrouter/models?scope=tenant&amp;key=model" in body
    assert "openrouter/models?scope=user&amp;key=model" in body


# ---------------------------------------------------------------------------
# Call catalog — the Voice call card's Load models / Load voices
# ---------------------------------------------------------------------------


@pytest.fixture
def call_catalog_stub(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the route's call-provider factory with a recorder.

    The route builds its provider through ``_call_catalog_provider``; the
    fake's ``catalog()`` returns ``state["result"]`` or raises it. The
    provider's wire behaviour is pinned in
    ``tests/services/voice_call/test_elevenlabs_provider.py``.

    Returns:
        A dict with ``calls`` (list of ``(api_key, base_url)``) and a mutable
        ``result``: a :class:`CallCatalog`, or an exception instance.
    """
    state: dict[str, Any] = {
        "calls": [],
        "result": CallCatalog(
            models=(
                CallModel(llm="gemini-2.5-flash", deprecated=False),
                CallModel(llm="claude-haiku-4-5", deprecated=False),
                CallModel(llm="gpt-4o", deprecated=True),
            ),
            voices=(
                CallVoice(
                    voice_id="v_rachel", name="Rachel", category="premade", live_moderation=False
                ),
                CallVoice(
                    voice_id="v_own", name="Eigene Stimme", category="cloned", live_moderation=False
                ),
                CallVoice(
                    voice_id="v_mod",
                    name="Moderated Lib",
                    category="professional",
                    live_moderation=True,
                ),
            ),
        ),
    }

    class _FakeProvider:
        async def catalog(self) -> CallCatalog:
            result = state["result"]
            if isinstance(result, Exception):
                raise result
            return result

    def _factory(api_key: str, base_url: str) -> _FakeProvider:
        state["calls"].append((api_key, base_url))
        return _FakeProvider()

    monkeypatch.setattr(provider_credentials, "_call_catalog_provider", _factory)
    return state


def _call_catalog_url(key: str) -> str:
    return f"{_CALL_CATALOG_URL}?key={key}"


async def _store_call_key(client: AsyncClient, value: str = "sk-call-catalog-key") -> None:
    """Save the Voice call key through the tenant write endpoint, as an owner does."""
    csrf = await _section_csrf(client)
    saved = await client.post(
        _TENANT_URL,
        data={
            "csrf_token": csrf,
            "provider": "voice_call",
            "key": "api_key",
            "value": value,
            "action": "save",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 200, saved.text


async def test_the_call_model_list_offers_live_models_and_names_the_deprecated(
    client_factory: Any,
    vault_key: str,
    call_catalog_stub: dict[str, Any],
) -> None:
    """``?key=model``: two live models offered, the deprecated one named in the status line."""
    client = await client_factory("owner")
    await _store_call_key(client)

    response = await client.get(_call_catalog_url("model"), follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    assert 'id="tenant-voice_call-model-catalog"' in body
    assert '<option value="gemini-2.5-flash">gemini-2.5-flash</option>' in body
    assert '<option value="claude-haiku-4-5">claude-haiku-4-5</option>' in body
    assert '<option value="gpt-4o">' not in body
    text = _card_copy(body)
    assert "2 of 3 models offered" in text
    assert "Deprecated and not offered: gpt-4o." in text
    assert "Load models" in body
    assert 'hx-target="#tenant-voice_call-model-catalog-slot"' in body
    assert call_catalog_stub["calls"] == [("sk-call-catalog-key", DEFAULT_BASE_URL)]
    assert "sk-call-catalog-key" not in body


async def test_the_call_voice_list_offers_only_voices_an_agent_can_use(
    client_factory: Any,
    app_engine: AsyncEngine,
    vault_key: str,
    call_catalog_stub: dict[str, Any],
) -> None:
    """``?key=voice``: moderated voices are named, not offered; a stored host is used."""
    await _write_tenant_config(app_engine, "voice_call", "base_url", "https://eu.vendor.invalid")
    client = await client_factory("owner")
    await _store_call_key(client)

    response = await client.get(_call_catalog_url("voice"), follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    assert 'id="tenant-voice_call-voice-catalog"' in body
    assert '<option value="v_rachel">Rachel · premade</option>' in body
    assert '<option value="v_own">Eigene Stimme · cloned</option>' in body
    assert '<option value="v_mod">' not in body
    text = _card_copy(body)
    assert "2 of 3 voices offered" in text
    assert "Not usable for the call (live moderation): Moderated Lib." in text
    assert "Load voices" in body
    assert call_catalog_stub["calls"] == [("sk-call-catalog-key", "https://eu.vendor.invalid")]


async def test_the_call_catalog_without_a_key_asks_for_it_and_calls_no_vendor(
    client_factory: Any,
    vault_key: str,
    call_catalog_stub: dict[str, Any],
) -> None:
    """No stored key: the fragment asks for one, and the port is never reached."""
    client = await client_factory("owner")

    response = await client.get(_call_catalog_url("model"), follow_redirects=False)

    assert response.status_code == 200
    assert "<datalist" not in response.text
    assert "store the API key on this card first" in _card_copy(response.text)
    assert call_catalog_stub["calls"] == []


async def test_a_call_catalog_refusal_renders_inline_and_logs_one_scrubbed_line(
    client_factory: Any,
    vault_key: str,
    call_catalog_stub: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A vendor refusal says so at HTTP 200 and leaves the field free text."""
    call_catalog_stub["result"] = CallProviderError("Model list failed: HTTP 401: <key>")
    client = await client_factory("owner")
    await _store_call_key(client)

    with caplog.at_level(logging.WARNING, logger="web.routes.provider_credentials"):
        response = await client.get(_call_catalog_url("model"), follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    assert "<datalist" not in body
    # Autoescaped in the markup; the operator reads the plain placeholder.
    assert "&lt;key&gt;" in body
    text = html.unescape(_card_copy(body))
    assert "Could not load the model list: Model list failed: HTTP 401: <key>" in text
    assert "You can still type a model id." in text
    lines = [
        record
        for record in caplog.records
        if "voice-call catalog: fetch failed tenant=" in record.message
    ]
    assert len(lines) == 1
    assert lines[0].levelno == logging.WARNING
    assert "sk-call-catalog-key" not in lines[0].message


async def test_the_call_catalog_refuses_a_non_owner_and_an_unoffered_key(
    client_factory: Any,
    vault_key: str,
    call_catalog_stub: dict[str, Any],
) -> None:
    """A member is refused; an owner asking for ``language`` is refused; no port call."""
    member = await client_factory("member")
    refused = await member.get(_call_catalog_url("model"), follow_redirects=False)
    assert refused.status_code == 200
    assert "owner-managed" in _card_copy(refused.text)

    owner = await client_factory("owner")
    await _store_call_key(owner)
    unoffered = await owner.get(_call_catalog_url("language"), follow_redirects=False)
    assert unoffered.status_code == 200
    assert "not offered for" in _card_copy(unoffered.text)
    assert "<button" not in unoffered.text

    assert call_catalog_stub["calls"] == []


async def test_the_voice_call_card_offers_load_buttons_on_model_and_voice(
    client_factory: Any,
    vault_key: str,
) -> None:
    """The owner's card wires both fields to the catalog; a member's section has neither."""
    owner = await client_factory("owner")
    response = await owner.get(_SECTION_URL, follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert 'list="tenant-voice_call-model-catalog"' in body
    assert 'list="tenant-voice_call-voice-catalog"' in body
    assert body.count("Load voices") == 1
    assert "voice-call/catalog?key=model" in body
    assert "voice-call/catalog?key=voice" in body

    member = await client_factory("member")
    member_body = (await member.get(_SECTION_URL, follow_redirects=False)).text
    assert "voice_call-model-catalog" not in member_body
    assert "voice_call-voice-catalog" not in member_body
    assert "Load voices" not in member_body
    assert "voice-call/catalog" not in member_body
