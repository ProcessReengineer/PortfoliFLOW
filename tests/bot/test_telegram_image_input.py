# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for Shirley image input via the Telegram bot (ADR-0075).

These drive :func:`bot.telegram_bot._handle_text_message` directly with
faked aiogram ``Message`` / ``Bot`` objects (the same scaffolding style as
``tests/bot/test_conversation_memory.py`` and the T-02 smoke harness) and a
fake core whose ``stream_response`` records the :class:`Conversation` it
received.

Test catalogue:
    I-01: A photo from a whitelisted user drives ``stream_response`` with a
          user message carrying one ``image/jpeg`` attachment; the caption
          is used as the content.
    I-02: A photo with no caption uses the default German instruction.
    I-03: An image-typed ``document`` builds an attachment with that MIME.
    I-04: An oversize image sends the German size reply and does NOT drive
          the stream.
    I-05: A non-vision model sends the German vision reply and does NOT
          drive the stream.
    I-06: A non-whitelisted sender with a photo is dropped silently — no
          reply, no stream.
    I-07: After a successful image turn the stored history holds a
          text-only user message (no attachment bytes).
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.ai_models import Conversation, Message, MessageRole
from services.ai_service_core import AIServiceCore, StreamEvent


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

#: ``_chat_histories`` is keyed by ``(tenant_id, chat_id)`` since
#: ADR-0112 §5 — a private chat id is the same for every tenant's bot,
#: so the tenant is part of the key. These tests inject no tenant.
_HISTORY_KEY = (None, _CHAT_ID)
_USER_ID = 12345
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32


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


def _bot_config(monkeypatch: pytest.MonkeyPatch, model: str):
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", str(_USER_ID))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("SHIRLEY_MODEL", model)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app@localhost/db")

    from bot.config import BotSettings

    return BotSettings()


@pytest.fixture
def vision_bot_config(monkeypatch: pytest.MonkeyPatch):
    """A valid enabled config whose model is vision-capable."""
    return _bot_config(monkeypatch, "anthropic/claude-sonnet-4.5")


@pytest.fixture
def nonvision_bot_config(monkeypatch: pytest.MonkeyPatch):
    """A valid enabled config whose model cannot read images."""
    return _bot_config(monkeypatch, "mistralai/mistral-7b-instruct")


@pytest.fixture
def aiobot_mock() -> MagicMock:
    """Aiogram-Bot-shaped mock with AsyncMock methods, including download."""
    aiobot = MagicMock()
    aiobot.send_chat_action = AsyncMock()
    aiobot.send_message = AsyncMock()
    aiobot.send_photo = AsyncMock()
    aiobot.delete_message = AsyncMock()
    aiobot.download = AsyncMock(return_value=io.BytesIO(_JPEG_BYTES))
    return aiobot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _photo_message(
    caption: str | None = None,
    *,
    chat_id: int = _CHAT_ID,
    user_id: int = _USER_ID,
) -> Any:
    """Telegram photo message (a list of PhotoSize, largest last)."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=None,
        caption=caption,
        photo=[
            SimpleNamespace(file_id="small-id"),
            SimpleNamespace(file_id="largest-id"),
        ],
        document=None,
        chat=SimpleNamespace(id=chat_id),
    )


def _document_message(
    mime: str,
    *,
    caption: str | None = None,
    filename: str = "sheet.png",
    chat_id: int = _CHAT_ID,
    user_id: int = _USER_ID,
) -> Any:
    """Telegram document message with a given MIME type."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=None,
        caption=caption,
        photo=[],
        document=SimpleNamespace(file_id="doc-id", mime_type=mime, file_name=filename),
        chat=SimpleNamespace(id=chat_id),
    )


def _make_recording_stream() -> Any:
    """Async-generator stand-in recording each received conversation."""
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


# ---------------------------------------------------------------------------
# I-01: a photo builds an image/jpeg attachment, caption as content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i01_photo_builds_jpeg_attachment_with_caption(
    vision_bot_config, aiobot_mock
) -> None:
    """A photo drives the stream with a single ``image/jpeg`` attachment."""
    import bot.telegram_bot as tb

    fake_stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", fake_stream):
        await tb._handle_text_message(
            aiobot_mock,
            _photo_message(caption="How does this fund fit?"),
            vision_bot_config,
        )

    assert len(fake_stream.calls) == 1
    conv = fake_stream.calls[0]
    user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "How does this fund fit?"
    assert len(user_msgs[0].attachments) == 1
    assert user_msgs[0].attachments[0].mime_type == "image/jpeg"
    assert user_msgs[0].attachments[0].data == _JPEG_BYTES
    # The largest PhotoSize was downloaded.
    aiobot_mock.download.assert_awaited_once_with("largest-id")


# ---------------------------------------------------------------------------
# I-02: a captionless photo uses the default German instruction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i02_photo_without_caption_uses_default_instruction(
    vision_bot_config, aiobot_mock
) -> None:
    """A photo without a caption falls back to the default instruction."""
    import bot.telegram_bot as tb

    fake_stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", fake_stream):
        await tb._handle_text_message(aiobot_mock, _photo_message(caption=None), vision_bot_config)

    conv = fake_stream.calls[0]
    user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
    assert user_msgs[0].content == ("Please analyse this image in the context of my portfolio.")
    assert len(user_msgs[0].attachments) == 1


# ---------------------------------------------------------------------------
# I-03: an image-typed document builds an attachment with that MIME
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i03_image_document_builds_attachment(vision_bot_config, aiobot_mock) -> None:
    """An image ``document`` is accepted with its declared MIME type."""
    import bot.telegram_bot as tb

    fake_stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", fake_stream):
        await tb._handle_text_message(
            aiobot_mock,
            _document_message("image/png", caption="term sheet"),
            vision_bot_config,
        )

    conv = fake_stream.calls[0]
    user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
    assert user_msgs[0].content == "term sheet"
    assert len(user_msgs[0].attachments) == 1
    assert user_msgs[0].attachments[0].mime_type == "image/png"
    assert user_msgs[0].attachments[0].filename == "sheet.png"
    aiobot_mock.download.assert_awaited_once_with("doc-id")


@pytest.mark.asyncio
async def test_i03b_non_image_document_is_ignored(vision_bot_config, aiobot_mock) -> None:
    """A non-image document with no text is silently ignored (no stream)."""
    import bot.telegram_bot as tb

    fake_stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", fake_stream):
        await tb._handle_text_message(
            aiobot_mock,
            _document_message("application/pdf"),
            vision_bot_config,
        )

    assert len(fake_stream.calls) == 0
    aiobot_mock.download.assert_not_awaited()
    aiobot_mock.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# I-04: oversize image → German size reply, no stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i04_oversize_image_rejected(
    vision_bot_config, aiobot_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image above the ceiling sends the size reply and skips the stream."""
    import bot.telegram_bot as tb

    monkeypatch.setattr(tb, "MAX_IMAGE_BYTES", 8)

    fake_stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", fake_stream):
        await tb._handle_text_message(aiobot_mock, _photo_message(caption="big"), vision_bot_config)

    assert len(fake_stream.calls) == 0
    sent_texts = [call.kwargs.get("text", "") for call in aiobot_mock.send_message.await_args_list]
    assert any("too large" in t for t in sent_texts), sent_texts


# ---------------------------------------------------------------------------
# I-05: non-vision model → vision reply, no stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i05_non_vision_model_rejected(nonvision_bot_config, aiobot_mock) -> None:
    """A non-vision model sends the vision reply and never drives the stream."""
    import bot.telegram_bot as tb

    fake_stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", fake_stream):
        await tb._handle_text_message(
            aiobot_mock, _photo_message(caption="read this"), nonvision_bot_config
        )

    assert len(fake_stream.calls) == 0
    aiobot_mock.download.assert_not_awaited()
    sent_texts = [call.kwargs.get("text", "") for call in aiobot_mock.send_message.await_args_list]
    assert any("cannot process images" in t for t in sent_texts), sent_texts


# ---------------------------------------------------------------------------
# I-06: a non-whitelisted sender with a photo is dropped silently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i06_unauthorised_photo_dropped_silently(vision_bot_config, aiobot_mock) -> None:
    """A photo from a non-whitelisted sender produces no reply and no stream."""
    import bot.telegram_bot as tb

    fake_stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", fake_stream):
        await tb._handle_text_message(
            aiobot_mock,
            _photo_message(caption="hi", user_id=999_999),
            vision_bot_config,
        )

    assert len(fake_stream.calls) == 0
    aiobot_mock.download.assert_not_awaited()
    aiobot_mock.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# I-07: history persisted text-only after a successful image turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i07_history_persisted_text_only(vision_bot_config, aiobot_mock) -> None:
    """After an image turn, stored history has a text-only user message."""
    import bot.telegram_bot as tb

    fake_stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", fake_stream):
        await tb._handle_text_message(
            aiobot_mock, _photo_message(caption="What is this?"), vision_bot_config
        )

    stored = tb._chat_histories[_HISTORY_KEY]
    user_msgs = [m for m in stored if m.role == MessageRole.USER]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "What is this?"
    assert user_msgs[0].attachments == []
    # The assistant reply was persisted alongside it.
    assert any(m.role == MessageRole.ASSISTANT and m.content == "reply-1" for m in stored)
