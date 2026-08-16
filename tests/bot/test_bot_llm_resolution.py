# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-turn LLM resolution in the Telegram handler (ADR-0112 §4b).

The bot's turn no longer runs on a core configured once at bot start. It
resolves its endpoint, credential and model per message, through the
credential façade, inside the injected tenant's context — so a key written
in Admin → Providers & Credentials answers the very next message, with no
bot restart.

These drive :func:`bot.telegram_bot._handle_text_message` directly (the
harness the conversation-memory tests use) with a **resolver double**, so
the assertions are on the chain the handler walks and on what reaches
``stream_response`` — no database, no network.

Single-tenant transition mode: there is no user axis until F5, so the chain
is ``tenant → env`` for the credential and the model, with
:attr:`BotSettings.openai_base_url` as the ``base_url``'s last fallback.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.ai_models import Message, MessageRole
from services.ai_service_core import AIServiceCore, StreamEvent
from services.investments.credential_resolver import (
    CredentialUnavailableError,
    ProviderCredential,
)

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

_CHAT_ID = 999
_USER_ID = 12345


@pytest.fixture(autouse=True)
def reset_bot_singletons(monkeypatch: pytest.MonkeyPatch):
    """Clear bot env vars and reset every bot module-level handle."""
    for var in _BOT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    import bot.config
    import bot.telegram_bot

    def _reset() -> None:
        bot.config._instance = None
        bot.telegram_bot._bot_thread = None
        bot.telegram_bot._bot_loop = None
        bot.telegram_bot._bot_core = None
        bot.telegram_bot._bot_engine = None
        bot.telegram_bot._bot_tenant_id = None
        bot.telegram_bot._bot_database_url = ""
        bot.telegram_bot._chat_histories.clear()

    _reset()
    yield
    _reset()


@pytest.fixture
def bot_config(monkeypatch: pytest.MonkeyPatch):
    """A valid enabled BotSettings with no Shirley credential in ``.env``.

    The relaxation of ADR-0112 §4b in practice: the bot starts without an
    ``OPENROUTER_API_KEY``, because the tenant's row can serve the turn.
    """
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", str(_USER_ID))
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x/y")

    from bot.config import BotSettings

    return BotSettings()


@pytest.fixture
def aiobot_mock() -> MagicMock:
    aiobot = MagicMock()
    aiobot.send_chat_action = AsyncMock()
    aiobot.send_message = AsyncMock()
    aiobot.send_photo = AsyncMock()
    aiobot.delete_message = AsyncMock()
    return aiobot


def _message(text: str) -> Any:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=_USER_ID),
        chat=SimpleNamespace(id=_CHAT_ID),
        photo=None,
        document=None,
        caption=None,
    )


class _FakeResolver:
    """Records the config chain and answers from a script."""

    def __init__(
        self,
        *,
        key: str | None,
        config: dict[str, str] | None = None,
    ) -> None:
        self._key = key
        self._config = config or {}
        self.config_calls: list[tuple[str, Any]] = []
        self.resolve_calls: list[dict[str, Any]] = []

    async def resolve(self, provider: str, **kwargs: Any) -> Any:
        self.resolve_calls.append({"provider": provider, **kwargs})
        if self._key is None:
            raise CredentialUnavailableError(f"no credential for {provider!r} (test)")
        return ProviderCredential(provider=provider, payload={"api_key": self._key})

    async def resolve_config(
        self,
        provider: str,
        key: str,
        *,
        user_id: Any = None,
        scopes: Any = None,
    ) -> str | None:
        self.config_calls.append((key, scopes))
        return self._config.get(key)


def _install_resolver(monkeypatch: pytest.MonkeyPatch, resolver: _FakeResolver) -> None:
    import bot.telegram_bot as tb

    monkeypatch.setattr(tb, "CredentialResolver", lambda **kwargs: resolver)


def _recording_stream() -> Any:
    """Patched ``stream_response`` recording the resolution it was handed."""
    calls: list[Any] = []

    async def fake_stream(
        self: AIServiceCore,
        conversation: Any,
        system_prompt: str = "",
        temperature: float = 0.7,
        tool_context: Any = None,
        llm: Any = None,
    ):
        calls.append(llm)
        final = Message(role=MessageRole.ASSISTANT, content="reply")
        yield StreamEvent("chunk", {"text": "reply"})
        yield StreamEvent("stream_finished", {"message": final, "iterations": 0})

    fake_stream.calls = calls  # type: ignore[attr-defined]
    return fake_stream


# ---------------------------------------------------------------------------
# The tenant's rows serve the turn
# ---------------------------------------------------------------------------


async def test_a_tenant_credential_and_model_drive_the_turn(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot.telegram_bot as tb

    tb._bot_tenant_id = uuid4()
    _install_resolver(
        monkeypatch,
        _FakeResolver(key="sk-tenant", config={"model": "tenant/model"}),
    )
    stream = _recording_stream()

    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_text_message(aiobot_mock, _message("hallo"), bot_config)

    assert len(stream.calls) == 1
    resolved = stream.calls[0]
    assert resolved.api_key == "sk-tenant"
    assert resolved.model == "tenant/model"
    # The reply went out; no error path was taken.
    sent = [c.kwargs["text"] for c in aiobot_mock.send_message.call_args_list]
    assert sent == ["reply"]


async def test_base_url_falls_back_to_the_bot_settings_default(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot.telegram_bot as tb

    tb._bot_tenant_id = uuid4()
    _install_resolver(monkeypatch, _FakeResolver(key="sk-tenant", config={"model": "tenant/model"}))
    stream = _recording_stream()

    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_text_message(aiobot_mock, _message("hallo"), bot_config)

    assert stream.calls[0].base_url == bot_config.openai_base_url


async def test_a_tenant_base_url_outranks_the_default(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot.telegram_bot as tb

    tb._bot_tenant_id = uuid4()
    _install_resolver(
        monkeypatch,
        _FakeResolver(
            key="sk-tenant",
            config={"model": "tenant/model", "base_url": "https://tenant.example/api/v1"},
        ),
    )
    stream = _recording_stream()

    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_text_message(aiobot_mock, _message("hallo"), bot_config)

    assert stream.calls[0].base_url == "https://tenant.example/api/v1"


async def test_an_unpaired_turn_resolves_without_a_user_axis(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whitelist-admitted turn carries no identity, so no user-scope row
    may be consulted for it.

    ADR-0112 §5 gives the chain a user axis, but only pairing can fill it:
    the deprecated whitelist admits a Telegram *account*, not a
    PortfoliFLOW user. This turn is unpaired, so the user link stays empty
    and the chain is tenant → env exactly as F4 left it. The paired
    counterpart is asserted in ``tests/bot/test_pairing.py``.
    """
    import bot.telegram_bot as tb

    tenant_id = uuid4()
    tb._bot_tenant_id = tenant_id
    resolver = _FakeResolver(key="sk-tenant", config={"model": "tenant/model"})
    _install_resolver(monkeypatch, resolver)

    with patch.object(AIServiceCore, "stream_response", _recording_stream()):
        await tb._handle_text_message(aiobot_mock, _message("hallo"), bot_config)

    assert resolver.resolve_calls == [
        {"provider": "openrouter", "tenant_id": tenant_id, "user_id": None}
    ]
    # And every config lookup went out unscoped-by-user as well.
    assert all(call[1] is None for call in resolver.config_calls)


async def test_each_turn_resolves_afresh(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is cached between turns — a rotated key applies immediately."""
    import bot.telegram_bot as tb

    tb._bot_tenant_id = uuid4()
    resolver = _FakeResolver(key="sk-first", config={"model": "tenant/model"})
    _install_resolver(monkeypatch, resolver)
    stream = _recording_stream()

    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_text_message(aiobot_mock, _message("erste"), bot_config)
        resolver._key = "sk-rotated"
        await tb._handle_text_message(aiobot_mock, _message("zweite"), bot_config)

    assert [call.api_key for call in stream.calls] == ["sk-first", "sk-rotated"]


# ---------------------------------------------------------------------------
# Nothing resolvable → the polite error path
# ---------------------------------------------------------------------------


async def test_no_credential_answers_politely_and_never_streams(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot.telegram_bot as tb

    tb._bot_tenant_id = uuid4()
    _install_resolver(monkeypatch, _FakeResolver(key=None))
    stream = _recording_stream()

    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_text_message(aiobot_mock, _message("hallo"), bot_config)

    assert stream.calls == []
    sent = [c.kwargs["text"] for c in aiobot_mock.send_message.call_args_list]
    assert len(sent) == 1
    assert "Providers & Credentials" in sent[0]
    assert "OPENROUTER_API_KEY" in sent[0]
    # The existing error path prefixes the warning sign exactly once.
    assert sent[0].startswith("⚠️ ")
    assert sent[0].count("⚠️") == 1


async def test_no_model_answers_politely_too(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key with no model cannot drive a turn either."""
    import bot.telegram_bot as tb

    tb._bot_tenant_id = uuid4()
    _install_resolver(monkeypatch, _FakeResolver(key="sk-tenant", config={}))
    stream = _recording_stream()

    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_text_message(aiobot_mock, _message("hallo"), bot_config)

    assert stream.calls == []
    sent = [c.kwargs["text"] for c in aiobot_mock.send_message.call_args_list]
    assert len(sent) == 1
    assert "Credentials" in sent[0]


async def test_a_failed_turn_is_not_remembered(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unresolved turn takes the error path, so it never enters history."""
    import bot.telegram_bot as tb

    tb._bot_tenant_id = uuid4()
    _install_resolver(monkeypatch, _FakeResolver(key=None))

    with patch.object(AIServiceCore, "stream_response", _recording_stream()):
        await tb._handle_text_message(aiobot_mock, _message("hallo"), bot_config)

    # Keyed by (tenant_id, chat_id) since ADR-0112 §5 (D6).
    assert tb._chat_histories.get((tb._bot_tenant_id, _CHAT_ID)) is None


# ---------------------------------------------------------------------------
# The core is no longer configured
# ---------------------------------------------------------------------------


def test_the_bot_core_is_built_unconfigured(bot_config: Any) -> None:
    """The core carries the turn machinery and no endpoint state at all."""
    import bot.telegram_bot as tb

    core = tb._bot_ai_core()

    assert isinstance(core, AIServiceCore)
    assert core.get_model() == ""
    assert core._base_url is None
    assert core._api_key is None
    # And it is cached per worker, not rebuilt each turn.
    assert tb._bot_ai_core() is core


def test_the_retired_configure_helpers_are_gone() -> None:
    """``_build_bot_core`` / ``_get_bot_core`` configured the core once at
    bot start; per-turn resolution replaces both, and leaving either in
    place would be a second, silently-divergent source of endpoint state."""
    import bot.telegram_bot as tb

    assert not hasattr(tb, "_build_bot_core")
    assert not hasattr(tb, "_get_bot_core")
