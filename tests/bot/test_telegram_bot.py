# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the :mod:`bot` package.

The tests cover three concerns: configuration validation
(:class:`bot.config.BotSettings`), the public lifecycle entry points
(:func:`bot.telegram_bot.start_bot` / :func:`bot.telegram_bot.stop_bot`),
and the long-message splitting helper. Live aiogram polling, the
typing-indicator scheduling, and the executor-based dispatch of
``run_turn`` are explicitly out of scope — covering them would require
a real Telegram test environment or invasive mocking of aiogram
internals, both of which have a poor effort-to-value ratio for a
single-developer project.
"""

from __future__ import annotations

import logging
import subprocess
import sys

import pytest

from core.exceptions import ConfigurationError


_BOT_ENV_VARS = (
    "TELEGRAM_BOT_ENABLED",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_IDS",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_API_KEY",
    "SHIRLEY_MODEL",
    "DATABASE_URL",
    "SHIRLEY_BOT_TENANT_SUBDOMAIN",
)


@pytest.fixture(autouse=True)
def reset_bot_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset bot-related environment and module-level singletons.

    Each test gets a clean slate: any pre-existing bot env vars are
    cleared (the test sets only what it needs via ``monkeypatch.setenv``)
    and the bot.config / bot.telegram_bot module-level state is reset so
    one test's singleton does not leak into the next.
    """
    for var in _BOT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    import bot.config
    import bot.telegram_bot

    def _reset() -> None:
        bot.config._instance = None
        bot.telegram_bot._bot_thread = None
        bot.telegram_bot._bot_loop = None
        bot.telegram_bot._bot_tenant_id = None
        bot.telegram_bot._bot_database_url = ""
        bot.telegram_bot._bot_superuser_url = ""
        bot.telegram_bot._whitelist_deprecation_warned = False
        bot.telegram_bot._bot_tasks.clear()
        bot.telegram_bot._chat_histories.clear()

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# Lifecycle: start_bot / stop_bot
# ---------------------------------------------------------------------------


def test_bot_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the bot disabled, ``start_bot()`` returns without spawning a thread."""
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "false")

    import bot.telegram_bot

    bot.telegram_bot.start_bot()

    assert bot.telegram_bot._bot_thread is None


def test_stop_bot_when_not_started_is_noop() -> None:
    """``stop_bot()`` is a no-op when the bot was never started."""
    import bot.telegram_bot

    # Must not raise.
    bot.telegram_bot.stop_bot()

    assert bot.telegram_bot._bot_thread is None
    assert bot.telegram_bot._bot_loop is None


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


def test_config_accepts_enabled_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """An enabled bot without an env token is an ordinary configuration.

    Since ADR-0112 §5 the token is discovered per tenant from
    ``scoped_settings``, so ``TELEGRAM_BOT_TOKEN`` is the *transition*
    token, not the bot's only one. Raising here would make the
    vault-configured deployment — the one the ADR steers towards —
    impossible to express. A deployment with no token anywhere gets its
    INFO line from ``start_bot``, which is the only place that knows.
    """
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("SHIRLEY_MODEL", "model")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x/y")

    from bot.config import BotSettings

    config = BotSettings()

    assert config.enabled is True
    assert config.telegram_token == ""


def test_config_accepts_enabled_without_a_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty whitelist is the *target* state, not a misconfiguration.

    Authorisation is the pairing binding since ADR-0112 §5; the whitelist
    is a deprecated fallback on the environment-token dispatcher alone.
    """
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("SHIRLEY_MODEL", "model")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x/y")

    from bot.config import BotSettings

    config = BotSettings()

    assert config.enabled is True
    assert config.allowed_user_ids == frozenset()


def test_config_warns_when_the_whitelist_is_set(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-empty whitelist warns once: it is deprecated (ADR-0112 §5)."""
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123,456")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("SHIRLEY_MODEL", "model")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x/y")

    from bot.config import BotSettings

    with caplog.at_level(logging.WARNING, logger="portfoliflow.bot"):
        config = BotSettings()

    assert config.allowed_user_ids == frozenset({123, 456})
    messages = [r.getMessage() for r in caplog.records]
    assert any("deprecated" in m and "TELEGRAM_ALLOWED_USER_IDS" in m for m in messages), messages


def test_config_warns_but_accepts_enabled_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An enabled bot without an API key warns — it no longer raises.

    Since ADR-0112 §4b the credential is resolved per turn, so a tenant's
    own vault row can serve a bot whose ``.env`` carries none. Refusing to
    start would make that configuration impossible; staying silent would
    hide a genuine mistake. It warns.
    """
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("SHIRLEY_MODEL", "model")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x/y")

    from bot.config import BotSettings

    with caplog.at_level(logging.WARNING, logger="portfoliflow.bot"):
        config = BotSettings()

    assert config.enabled is True
    assert config.openai_api_key == ""
    assert any("OPENROUTER_API_KEY is empty" in r.getMessage() for r in caplog.records)


def test_config_warns_but_accepts_enabled_without_model(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An enabled bot without a model warns on the same grounds."""
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("SHIRLEY_MODEL", "")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x/y")

    from bot.config import BotSettings

    with caplog.at_level(logging.WARNING, logger="portfoliflow.bot"):
        config = BotSettings()

    assert config.enabled is True
    assert config.model == ""
    assert any("SHIRLEY_MODEL is empty" in r.getMessage() for r in caplog.records)


def test_config_still_rejects_enabled_without_a_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relaxation is narrow: what nothing else can supply still raises."""
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("SHIRLEY_MODEL", "model")
    monkeypatch.setenv("DATABASE_URL", "")

    from bot.config import BotSettings

    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        BotSettings()


def test_config_parses_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace around commas is tolerated; entries are parsed as integers."""
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123,  456 ,789")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("SHIRLEY_MODEL", "model")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app@localhost/db")

    from bot.config import BotSettings

    settings = BotSettings()
    assert settings.allowed_user_ids == frozenset({123, 456, 789})


def test_config_rejects_non_integer_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer entry in the whitelist raises ``ConfigurationError``."""
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123,abc,456")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("SHIRLEY_MODEL", "model")

    from bot.config import BotSettings

    with pytest.raises(ConfigurationError, match="abc"):
        BotSettings()


def test_config_disabled_skips_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disabled bot tolerates any combination of empty fields."""
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "false")
    # All other vars are unset (the autouse fixture cleared them).

    from bot.config import BotSettings

    settings = BotSettings()
    assert settings.enabled is False
    assert settings.allowed_user_ids == frozenset()
    assert settings.telegram_token == ""


def test_config_reads_database_url_and_tenant_subdomain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``database_url`` and ``tenant_subdomain`` are read from the environment."""
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("SHIRLEY_MODEL", "model")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app@localhost/db")
    monkeypatch.setenv("SHIRLEY_BOT_TENANT_SUBDOMAIN", "vwn")

    from bot.config import BotSettings

    settings = BotSettings()
    assert settings.database_url == "postgresql+asyncpg://app@localhost/db"
    assert settings.tenant_subdomain == "vwn"


def test_config_rejects_enabled_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled bot with an empty ``DATABASE_URL`` raises ``ConfigurationError``."""
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("SHIRLEY_MODEL", "model")
    # DATABASE_URL deliberately left unset (cleared by the autouse fixture).

    from bot.config import BotSettings

    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        BotSettings()


def test_config_default_tenant_subdomain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tenant_subdomain`` defaults to ``minathena-capital`` when unset."""
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("SHIRLEY_MODEL", "model")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app@localhost/db")
    # SHIRLEY_BOT_TENANT_SUBDOMAIN deliberately left unset.

    from bot.config import BotSettings

    settings = BotSettings()
    assert settings.tenant_subdomain == "minathena-capital"


# ---------------------------------------------------------------------------
# Injected tenant identity (ADR-0063)
# ---------------------------------------------------------------------------


def test_start_bot_caches_injected_identity_and_stop_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``start_bot`` records the injected tenant id + URL; ``stop_bot`` clears.

    The thread spawn is patched out so the test exercises only the
    module-level identity caching the handler later reads — no aiogram,
    no real polling.
    """
    from uuid import uuid4

    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("SHIRLEY_MODEL", "model")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app@localhost/db")

    import bot.telegram_bot as tb

    # Replace the worker thread with an inert stand-in so nothing actually
    # starts polling; we only care that start_bot cached the identity.
    class _InertThread:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            pass

    monkeypatch.setattr(tb.threading, "Thread", _InertThread)

    tid = uuid4()
    tb.start_bot(
        tenant_id=tid,
        database_url="postgresql+asyncpg://app@localhost/db",
        superuser_url="postgresql+asyncpg://su@localhost/db",
    )

    assert tb._bot_tenant_id == tid
    assert tb._bot_database_url == "postgresql+asyncpg://app@localhost/db"
    # The discovery URL is injected too since ADR-0112 §5 — the bot never
    # reads DATABASE_URL_SUPERUSER itself.
    assert tb._bot_superuser_url == "postgresql+asyncpg://su@localhost/db"

    # stop_bot early-returns unless both a loop and a thread handle exist;
    # give it minimal stand-ins so it proceeds to the reset path.
    import asyncio

    loop = asyncio.new_event_loop()
    tb._bot_loop = loop
    tb._bot_thread = _InertThread()
    try:
        tb.stop_bot()
    finally:
        loop.close()

    assert tb._bot_tenant_id is None
    assert tb._bot_database_url == ""
    assert tb._bot_superuser_url == ""


def test_start_bot_noops_without_a_token_or_a_discovery_url(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Enabled, but nothing to poll and nowhere to look: no thread (D3).

    The master switch says "run the bot thread"; discovery says *which*
    bots. With neither an environment token nor a superuser URL to scan
    on, the answer is "none" — an INFO line, not a crash and not a thread
    that polls nothing.
    """
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app@localhost/db")

    import bot.telegram_bot as tb

    with caplog.at_level(logging.INFO, logger="bot.telegram_bot"):
        tb.start_bot(database_url="postgresql+asyncpg://app@localhost/db")

    assert tb._bot_thread is None
    assert any("not starting" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Qt-freeness
# ---------------------------------------------------------------------------


def test_no_qt_import() -> None:
    """Importing ``bot.telegram_bot`` must not pull PyQt6 in.

    Run in a fresh subprocess so the assertion is independent of whatever
    the parent test process has already imported.
    """
    code = (
        "import bot.telegram_bot\n"
        "import sys\n"
        "assert 'PyQt6' not in sys.modules, "
        '"PyQt6 was imported transitively"\n'
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, (
        f"subprocess failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )


# ---------------------------------------------------------------------------
# Long-text splitting helper
# ---------------------------------------------------------------------------


def test_split_long_text() -> None:
    """``_split_text_for_telegram`` splits along paragraphs, then whitespace."""
    from bot.telegram_bot import _split_text_for_telegram

    # Short text — single chunk.
    assert _split_text_for_telegram("hello") == ["hello"]
    short = "a" * 100
    assert _split_text_for_telegram(short, limit=4000) == [short]

    # 10 paragraphs of 1000 chars each, separated by blank lines.
    paragraph = "x" * 1000
    multi_para = "\n\n".join([paragraph] * 10)
    chunks = _split_text_for_telegram(multi_para, limit=4000)
    assert chunks, "expected at least one chunk"
    assert all(len(c) <= 4000 for c in chunks), f"chunk lengths: {[len(c) for c in chunks]}"
    # No content lost: total x-count must equal the input.
    total_x = sum(c.count("x") for c in chunks)
    assert total_x == 10 * 1000

    # Single 10000-char paragraph (no blank lines), word-separated.
    long_paragraph = "word " * 2000  # 10000 characters with spaces.
    chunks2 = _split_text_for_telegram(long_paragraph, limit=4000)
    assert chunks2, "expected at least one chunk"
    assert all(len(c) <= 4000 for c in chunks2), f"chunk lengths: {[len(c) for c in chunks2]}"

    # Empty input.
    assert _split_text_for_telegram("") == []
