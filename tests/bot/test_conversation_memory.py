# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Telegram bot's per-chat conversation memory.

The bot keeps a bounded in-memory history per Telegram ``chat_id`` so
Shirley remembers the dialogue across messages, matching the web
surface (Prompt 5). These tests drive
:func:`bot.telegram_bot._handle_text_message` directly, with a fake core
whose ``stream_response`` records the :class:`Conversation` it received
and yields a scripted reply.

Test catalogue:
    M-01: A second turn for the same chat carries over the first turn's
          user text AND assistant reply (history persisted).
    M-02: A ``/reset`` message clears the chat's history; the next turn
          sees only the new user message.
    M-03: History is bounded — driving more than
          ``_MAX_HISTORY_MESSAGES`` turns never grows the stored history
          beyond the cap.
    M-04: A crashed turn is NOT remembered, and the user is told so
          through the fallback error reply.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.ai_models import Conversation, Message, MessageRole
from services.ai_service_core import AIServiceCore, StreamEvent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def reset_bot_singletons(monkeypatch: pytest.MonkeyPatch):
    """Clear bot env vars and reset bot module-level state, incl. histories."""
    for var in _BOT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    import bot.config
    import bot.telegram_bot

    bot.config._instance = None
    bot.telegram_bot._bot_thread = None
    bot.telegram_bot._bot_loop = None
    bot.telegram_bot._bot_core = None
    bot.telegram_bot._bot_tenant_id = None
    bot.telegram_bot._bot_database_url = ""
    bot.telegram_bot._chat_histories.clear()

    yield

    bot.config._instance = None
    bot.telegram_bot._bot_thread = None
    bot.telegram_bot._bot_loop = None
    bot.telegram_bot._bot_core = None
    bot.telegram_bot._bot_tenant_id = None
    bot.telegram_bot._bot_database_url = ""
    bot.telegram_bot._chat_histories.clear()


@pytest.fixture
def bot_config(monkeypatch: pytest.MonkeyPatch):
    """A valid enabled BotSettings constructed from a known env."""
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "12345")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SHIRLEY_MODEL", "test-model")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app@localhost/db")

    from bot.config import BotSettings

    return BotSettings()


@pytest.fixture
def aiobot_mock() -> MagicMock:
    """Aiogram-Bot-shaped mock with AsyncMock methods for awaited calls."""
    aiobot = MagicMock()
    aiobot.send_chat_action = AsyncMock()
    aiobot.send_message = AsyncMock()
    aiobot.send_photo = AsyncMock()
    aiobot.delete_message = AsyncMock()
    return aiobot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_CHAT_ID = 999

#: ``_chat_histories`` is keyed by ``(tenant_id, chat_id)`` since
#: ADR-0112 §5 — a private chat id is the same for every tenant's bot,
#: so the tenant is part of the key. These tests inject no tenant.
_HISTORY_KEY = (None, _CHAT_ID)


def _message(text: str, *, chat_id: int = _CHAT_ID, user_id: int = 12345) -> Any:
    """Telegram-Message-shaped mock from a whitelisted sender."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        chat=SimpleNamespace(id=chat_id),
    )


def _make_recording_stream() -> Any:
    """Async-generator stand-in that records each received conversation.

    Records the :class:`Conversation` handed to it on ``.calls`` (one entry
    per turn) and yields a distinct scripted assistant reply per call so a
    later turn's persisted history is unambiguous.
    """
    calls: list[Conversation] = []

    async def fake_stream(
        self: AIServiceCore,
        conversation: Conversation,
        system_prompt: str = "",
        temperature: float = 0.7,
        tool_context: Any = None,
        llm: Any = None,
    ):
        calls.append(conversation)
        reply = f"reply-{len(calls)}"
        final_msg = Message(role=MessageRole.ASSISTANT, content=reply)
        yield StreamEvent("chunk", {"text": reply})
        yield StreamEvent("stream_finished", {"message": final_msg, "iterations": 0})

    fake_stream.calls = calls  # type: ignore[attr-defined]
    return fake_stream


def _make_crashing_stream() -> Any:
    """Async-generator stand-in that raises on first iteration."""

    async def crashing_stream(
        self: AIServiceCore,
        conversation: Conversation,
        system_prompt: str = "",
        temperature: float = 0.7,
        tool_context: Any = None,
        llm: Any = None,
    ):
        raise RuntimeError("simulated upstream failure")
        yield  # unreachable; makes the function an async generator

    return crashing_stream


# ---------------------------------------------------------------------------
# M-01: history carries across turns for the same chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m01_history_carries_across_turns(bot_config, aiobot_mock) -> None:
    """M-01: A second turn sees the first turn's user text and assistant reply."""
    import bot.telegram_bot as tb

    fake_stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", fake_stream):
        await tb._handle_text_message(
            aiobot_mock, _message("My favourite fund is Alpha."), bot_config
        )
        await tb._handle_text_message(
            aiobot_mock, _message("What did I just tell you?"), bot_config
        )

    assert len(fake_stream.calls) == 2

    # The first turn's conversation contained only the new user message.
    first_conv = fake_stream.calls[0]
    assert [m.content for m in first_conv.messages] == ["My favourite fund is Alpha."]

    # The second turn's conversation replays the first exchange (user +
    # assistant reply) before the new user message.
    second = fake_stream.calls[1].messages
    roles = [m.role for m in second]
    contents = [m.content for m in second]

    assert roles == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.USER,
    ]
    assert contents == [
        "My favourite fund is Alpha.",  # first turn's user text
        "reply-1",  # first turn's assistant reply
        "What did I just tell you?",  # new user message
    ]


# ---------------------------------------------------------------------------
# M-02: /reset clears the chat's history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m02_reset_clears_history(bot_config, aiobot_mock) -> None:
    """M-02: After ``/reset`` the next turn's conversation is just the new msg."""
    import bot.telegram_bot as tb

    fake_stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", fake_stream):
        await tb._handle_text_message(
            aiobot_mock, _message("My favourite fund is Alpha."), bot_config
        )
        # /reset must NOT reach the core and must clear the stored history.
        await tb._handle_text_message(aiobot_mock, _message("/reset"), bot_config)
        await tb._handle_text_message(
            aiobot_mock, _message("What did I just tell you?"), bot_config
        )

    # Only the two non-reset turns reached the core.
    assert len(fake_stream.calls) == 2

    # The reset acknowledgement was sent.
    sent_texts = [call.kwargs.get("text", "") for call in aiobot_mock.send_message.await_args_list]
    assert any("Context cleared" in text for text in sent_texts), (
        f"expected a reset acknowledgement; got: {sent_texts!r}"
    )

    # The post-reset turn carried no history — only the new user message.
    post_reset = fake_stream.calls[1].messages
    assert [m.content for m in post_reset] == ["What did I just tell you?"]
    assert post_reset[0].role == MessageRole.USER


@pytest.mark.asyncio
async def test_m02b_new_is_an_alias_for_reset(bot_config, aiobot_mock) -> None:
    """M-02b: ``/new`` clears history exactly like ``/reset``."""
    import bot.telegram_bot as tb

    fake_stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", fake_stream):
        await tb._handle_text_message(aiobot_mock, _message("Remember this."), bot_config)
        await tb._handle_text_message(aiobot_mock, _message("/new"), bot_config)
        await tb._handle_text_message(aiobot_mock, _message("Anything?"), bot_config)

    assert len(fake_stream.calls) == 2
    post_reset = fake_stream.calls[1].messages
    assert [m.content for m in post_reset] == ["Anything?"]


# ---------------------------------------------------------------------------
# M-03: history is bounded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m03_history_is_bounded(bot_config, aiobot_mock) -> None:
    """M-03: Driving many turns never grows history beyond the cap."""
    import bot.telegram_bot as tb

    cap = tb._MAX_HISTORY_MESSAGES
    # Each turn appends two messages (user + assistant), so more than
    # ``cap`` messages' worth of turns guarantees the trim kicks in.
    turns = cap + 5

    fake_stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", fake_stream):
        for i in range(turns):
            await tb._handle_text_message(aiobot_mock, _message(f"turn {i}"), bot_config)
            stored = tb._chat_histories[_HISTORY_KEY]
            assert len(stored) <= cap, f"history exceeded cap after turn {i}: {len(stored)} > {cap}"

    # After enough turns the history is pinned at exactly the cap.
    assert len(tb._chat_histories[_HISTORY_KEY]) == cap


# ---------------------------------------------------------------------------
# M-04: a crashed turn is not remembered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m04_crashed_turn_not_remembered(bot_config, aiobot_mock) -> None:
    """M-04: A turn whose stream raises does not enter the chat history.

    The crash also has to be *answered*: the handler returning at all is
    the dispatcher-survival assertion, and the fallback reply below is
    what keeps the user from facing silence.
    """
    import bot.telegram_bot as tb

    # A first successful turn seeds one exchange.
    good_stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", good_stream):
        await tb._handle_text_message(aiobot_mock, _message("Seed the history."), bot_config)
    assert len(tb._chat_histories[_HISTORY_KEY]) == 2

    # The crashing turn must NOT extend the history.
    crashing_stream = _make_crashing_stream()
    with patch.object(AIServiceCore, "stream_response", crashing_stream):
        await tb._handle_text_message(aiobot_mock, _message("This turn explodes."), bot_config)

    stored = tb._chat_histories[_HISTORY_KEY]
    assert len(stored) == 2, "crashed turn must not be persisted"
    assert [m.content for m in stored] == ["Seed the history.", "reply-1"]

    # The crashed turn still answered — a warning-prefixed fallback reply,
    # distinct from the first turn's ordinary "reply-1".
    sent_texts = [call.kwargs.get("text", "") for call in aiobot_mock.send_message.await_args_list]
    assert any(text.startswith("⚠️") for text in sent_texts), (
        f"expected a warning-prefixed fallback reply; got: {sent_texts!r}"
    )
