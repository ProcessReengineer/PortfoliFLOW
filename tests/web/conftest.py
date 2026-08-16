# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Test fixtures for the FastAPI variant.

Each fixture yields a configured ``httpx.AsyncClient`` bound to the
FastAPI app via ``ASGITransport`` — no live uvicorn process is
required.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient

from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


@pytest.fixture(autouse=True)
def _telegram_bot_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    """Force the Telegram bot off for every test in this package.

    The app lifespan (``web/main.py``) starts the in-process aiogram bot
    when ``bot.config.get_bot_config().enabled`` is true, and ~45 test
    modules here run that lifespan. A developer ``.env`` with
    ``TELEGRAM_BOT_ENABLED=true`` therefore made the test process spawn a
    *live* bot: it polls Telegram via ``getUpdates``, which allows exactly
    one consumer per token, so a test run stole the production bot's
    update stream and pumped real updates through a truncated test schema.

    Two seams have to be closed, because the config is a lazily-built
    module-level singleton:

    * the environment variable ``BotSettings`` reads on construction, and
    * the ``bot.config._instance`` cache — a settings object already built
      with ``enabled=True`` (by an earlier test in another package, or by
      an import that beat this fixture) would otherwise ignore the env var
      entirely.

    Invalidating the singleton on both setup and teardown mirrors the
    ``reset_bot_state`` fixture in ``tests/bot`` and keeps the two
    packages from leaking a cached ``BotSettings`` into each other.

    Function-scoped (the default) on purpose: every ``create_app`` call in
    this package happens inside a test function or a function-scoped
    client fixture — there is no session- or module-scoped app — so a
    function-scoped fixture is always in force before app construction.
    ``monkeypatch`` is function-scoped in any case.
    """
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "false")

    import bot.config

    bot.config._instance = None
    yield
    bot.config._instance = None


@pytest.fixture(autouse=True)
def _tick_scheduler_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the built-in tick scheduler off for every test in this package.

    Same reasoning as ``_telegram_bot_disabled`` above, one ADR later: the
    lifespan starts the in-process tick scheduler by default (ADR-0117 §1),
    and ~45 modules here run that lifespan. The task sleeps a full interval
    before its first tick, so most tests would never reach one — but a test
    that holds a lifespan open past 60 seconds would fire a *real* Irene
    beat and market-data refresh against the developer's database, LLM
    calls and provider I/O included. Off by default; the modules that
    exercise the scheduler enable it explicitly (they pass
    ``tick_scheduler_enabled=True`` to ``WebSettings``, which as an init
    value outranks this environment variable).

    One seam only, unlike the bot: ``WebSettings`` is constructed per app
    rather than cached in a module-level singleton, so the environment
    variable is the whole story.
    """
    monkeypatch.setenv("TICK_SCHEDULER_ENABLED", "false")


@pytest.fixture(autouse=True)
def _local_dev_tenant_subdomain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set ``LOCAL_DEV_TENANT_SUBDOMAIN`` so ASGI tests resolve.

    Per ADR-0063 §1, single-label hosts (``localhost``, ``testserver``)
    consult this env var. Web tests run against ASGITransport with the
    default ``testserver`` host, so this fixture seats the env var to
    the primary-tenant subdomain unless a test overrides it.
    """
    monkeypatch.setenv("LOCAL_DEV_TENANT_SUBDOMAIN", "minathena-capital")


@pytest.fixture(autouse=True)
def _deterministic_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the LLM environment for every test in this package.

    Since ADR-0112 §4b the chat routes resolve a turn's credential,
    model and base URL *before* they touch the (test-overridden) AI
    core. That made ``OPENROUTER_API_KEY`` and ``SHIRLEY_MODEL``
    load-bearing for every chat-route test, not only the ones that
    assert on resolution — and the five pre-F4 chat modules
    (``test_chat_routes``, ``test_chat_sse``, ``test_chat_history``,
    ``test_chat_voice``, ``test_chat_consultation``) seed neither. They
    passed only because the ``load_dotenv`` call above imported the
    *developer's* ``.env``, which happens to carry both: a hidden
    environment dependency that fails on a clean clone and whenever the
    operator blanks a key. Seating deterministic values here removes it
    without touching those five modules.

    The base URL points at an unroutable host on purpose — nothing in
    this package may reach a live OpenRouter endpoint, and a test that
    somehow tried should fail loudly rather than spend a real token.

    ``CREDENTIAL_VAULT_MASTER_KEY`` is *deleted* rather than set,
    because vault-unconfigured is the package default: a fresh
    deployment has no vault, and the tests that want one mint their own
    Fernet key (``test_chat_llm_resolution.py``,
    ``test_provider_credentials.py``).

    Function-scoped and autouse like its neighbours. Non-autouse
    fixtures are set up *after* the autouse ones at the same scope, so
    any module seating its own values — every test that asserts on
    resolution does — still overrides these defaults.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-conftest")
    monkeypatch.setenv("SHIRLEY_MODEL", "test/model-conftest")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.invalid/api/v1")
    monkeypatch.delenv("CREDENTIAL_VAULT_MASTER_KEY", raising=False)


@pytest.fixture(autouse=True)
def _ai_core_hygiene() -> Generator[None, None, None]:
    """Reset the process-global AIServiceCore after every web test.

    Web-app lifespans configure the singleton
    (``web.main._configure_ai_core``: credentials, model, CONNECTED
    status) and do not restore it on shutdown; some wiring tests also
    pre-pollute it deliberately. Without this teardown, that state leaks
    into any test module that runs after ``tests/web`` in the same
    process (order-dependent flake — fires in ad-hoc combined runs such
    as ``pytest tests/web tests/assistants``).

    Teardown-only by design: resetting *before* the test would be
    redundant (the polluters are in this package) and the wiring tests
    construct their own pre-pollution in-body. ``reset()`` clears
    exactly the polluted fields (credentials, model list, active model,
    status) and leaves the ToolRegistry untouched — the default tools
    register via the import cache and would not come back after a
    registry wipe.
    """
    yield
    from services.ai_service_core import get_ai_service_core

    get_ai_service_core().reset()


def _build_settings(*, database_url: str | None) -> WebSettings:
    return WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        database_url=database_url,
    )


@pytest_asyncio.fixture
async def web_client_with_db() -> AsyncGenerator[AsyncClient, None]:
    """Client whose engine is built from ``DATABASE_URL`` in .env.

    Skips when the URL is unset so the rest of the suite still runs on
    a contributor laptop without Postgres.
    """
    db_url = os.getenv("DATABASE_URL")
    import pytest

    if not db_url:
        pytest.skip("DATABASE_URL not set; skipping live-DB web smoke test.")

    app = create_app(_build_settings(database_url=db_url))
    transport = ASGITransport(app=app)
    # The lifespan context is entered alongside the client so that
    # ``app.state.engine`` and ``schema_revision`` are populated.
    async with (
        AsyncClient(transport=transport, base_url="http://testserver") as client,
        app.router.lifespan_context(app),
    ):
        yield client


@pytest_asyncio.fixture
async def web_client_no_db() -> AsyncGenerator[AsyncClient, None]:
    """Client built without a configured database URL.

    Health endpoint should report ``status: degraded`` in this mode.
    """
    app = create_app(_build_settings(database_url=None))
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://testserver") as client,
        app.router.lifespan_context(app),
    ):
        yield client
