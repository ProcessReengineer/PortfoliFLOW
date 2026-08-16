# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for Shirley voice messages via the Telegram bot (ADR-0076, ADR-0118).

These drive :func:`bot.telegram_bot._handle_voice_message` directly with faked
aiogram ``Message`` / ``Bot`` objects (the same scaffolding style as
``tests/bot/test_telegram_image_input.py``) plus a fake
:class:`~services.voice.provider.VoiceProvider`.

Since ADR-0118 §4 the handler resolves one
:class:`~services.voice.ResolvedVoice` per inbound message and builds both legs
from it, so the injection seam is the factory rather than a singleton: the fake
provider is installed by monkeypatching ``tb.build_provider`` with a recorder,
and gating runs the **real** ``voice.enabled`` chain over the environment
(``_bot_engine`` is ``None`` here, so there are no vault sources). No real
STT/TTS call is made, and every ``VOICE_*`` variable is cleared by the reset
fixture so a developer's ``.env`` cannot decide an outcome. What the resolution
itself yields is pinned in ``tests/bot/test_bot_voice_resolution.py``; this
module pins the message behaviour around it.

Voice is strictly additive: an inbound voice message is transcribed and enters
the *existing* turn unchanged; the prose goes out as text (so the transcript
stays visible) and as a synthesised voice note. Charts continue to go as PNG
photos. A disabled service, an empty transcript, or an STT/TTS failure
surfaces a clear English message and never silently degrades.

Test catalogue:
    V-01: Happy path — transcript drives the stream (no attachment); a text
          chunk and a single ``send_voice`` go out; ``send_audio`` is not used.
    V-02: The voice note is an OGG/Opus container — the ``BufferedInputFile``
          filename ends ``.ogg`` and ``synthesize`` was called ``fmt="opus"``.
    V-03: ``send_voice`` raises → the ``send_audio`` fallback fires once with
          the same bytes; the turn does not crash.
    V-04: An empty transcript replies clearly and does NOT drive a turn.
    V-05: An STT failure replies clearly and does NOT drive a turn.
    V-06: Voice disabled for the tenant replies clearly; no download, no
          resolution, no turn.
    V-07: A non-whitelisted sender is dropped silently — no reply, no turn.
    V-08: A chart-only turn sends the PNG photo and NO voice note.
    V-09: A TTS failure still sends the text reply; no voice note; no crash.
    V-10: A plain text message still routes through ``_handle_text_message`` and
          never calls ``send_voice`` (additivity regression).
"""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.ai_models import Conversation, Message, MessageRole
from services.ai_service_core import AIServiceCore, StreamEvent
from services.voice import (
    EmptyTranscriptError,
    VoiceSynthesisError,
    VoiceTranscriptionError,
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
    # Every variable the voice chains read (ADR-0118 §1). Cleared per test so
    # the repository ``.env`` — a fully enabled voice deployment on the
    # maintainer's machine — cannot make a case pass for the wrong reason.
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

_CHAT_ID = 999
_USER_ID = 12345
_OGG_BYTES = b"OggS" + b"\x00" * 40  # minimal OGG-looking stand-in
_OPUS_REPLY = b"OggS-reply-bytes"


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
    # No engine → the voice resolution runs the env-only chain (no vault
    # sources), which is what this module's fixtures script.
    bot.telegram_bot._bot_engine = None
    bot.telegram_bot._bot_tenant_id = None
    bot.telegram_bot._bot_database_url = ""
    bot.telegram_bot._chat_histories.clear()

    yield

    bot.config._instance = None
    bot.telegram_bot._bot_thread = None
    bot.telegram_bot._bot_loop = None
    bot.telegram_bot._bot_core = None
    # No engine → the voice resolution runs the env-only chain (no vault
    # sources), which is what this module's fixtures script.
    bot.telegram_bot._bot_engine = None
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
def aiobot_mock() -> MagicMock:
    """Aiogram-Bot-shaped mock; voice/audio sends plus an OGG download."""
    aiobot = MagicMock()
    aiobot.send_chat_action = AsyncMock()
    aiobot.send_message = AsyncMock()
    aiobot.send_photo = AsyncMock()
    aiobot.send_voice = AsyncMock()
    aiobot.send_audio = AsyncMock()
    aiobot.delete_message = AsyncMock()
    aiobot.download = AsyncMock(return_value=io.BytesIO(_OGG_BYTES))
    return aiobot


# ---------------------------------------------------------------------------
# Fakes / factories
# ---------------------------------------------------------------------------


class _FakeVoiceProvider:
    """In-memory :class:`VoiceProvider` stand-in recording its calls."""

    def __init__(
        self,
        transcript: str = "Wie steht mein Portfolio?",
        synth: bytes = _OPUS_REPLY,
        stt_exc: Exception | None = None,
        tts_exc: Exception | None = None,
    ) -> None:
        self._transcript, self._synth = transcript, synth
        self._stt_exc, self._tts_exc = stt_exc, tts_exc
        self.transcribe_calls: list[tuple[bytes, str]] = []
        self.synthesize_calls: list[tuple[str, str]] = []

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        self.transcribe_calls.append((audio, mime_type))
        if self._stt_exc:
            raise self._stt_exc
        return self._transcript

    async def synthesize(self, text: str, *, fmt: str) -> tuple[bytes, str]:
        self.synthesize_calls.append((text, fmt))
        if self._tts_exc:
            raise self._tts_exc
        return self._synth, "audio/ogg"


def _voice_message(
    *, chat_id: int = _CHAT_ID, user_id: int = _USER_ID, mime: str = "audio/ogg"
) -> Any:
    """Telegram voice message (a ``voice`` payload, no text/photo)."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=None,
        caption=None,
        photo=[],
        document=None,
        voice=SimpleNamespace(file_id="voice-id", mime_type=mime, duration=3),
        chat=SimpleNamespace(id=chat_id),
    )


def _text_message(text: str, *, chat_id: int = _CHAT_ID, user_id: int = _USER_ID) -> Any:
    """Plain Telegram text message (no voice/photo)."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        caption=None,
        photo=[],
        document=None,
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


def _make_chart_only_stream() -> Any:
    """Async-generator stand-in yielding a chart artefact and empty prose."""
    calls: list[Conversation] = []
    image_b64 = base64.b64encode(b"\x89PNG-bytes").decode()

    async def fake_stream(
        self: AIServiceCore,
        conversation: Conversation,
        system_prompt: str = "",
        temperature: float = 0.7,
        tool_context: Any = None,
        llm: Any = None,
    ):
        calls.append(conversation)
        final_msg = Message(role=MessageRole.ASSISTANT, content="")
        yield StreamEvent("chart_artifact", {"image_base64": image_b64, "caption": "x"})
        yield StreamEvent("stream_finished", {"message": final_msg, "iterations": 0})

    fake_stream.calls = calls  # type: ignore[attr-defined]
    return fake_stream


class _ProviderRecorder:
    """Stands in for ``build_provider``: records the resolution, serves the fake.

    The seam ADR-0118 §4 left behind — both legs of a voice message build
    their provider from the message's :class:`ResolvedVoice`, so recording the
    factory records the resolution too.
    """

    def __init__(self, fake: _FakeVoiceProvider) -> None:
        self._fake = fake
        self.calls: list[Any] = []

    def __call__(self, voice: Any) -> _FakeVoiceProvider:
        self.calls.append(voice)
        return self._fake


def _install_voice(
    monkeypatch: pytest.MonkeyPatch, tb, fake, *, enabled: bool = True
) -> _ProviderRecorder:
    """Set the environment the resolution reads and patch the provider factory.

    Gating and resolution are the real ones (env-only, since ``_bot_engine``
    is ``None``): ``VOICE_ENABLED`` decides the gate and the two API keys are
    what an enabled tenant needs. The models, base URL and persona voice stay
    unset on purpose — the ``DEFAULT_*`` constants serve as the chains' tails.

    Returns:
        The recorder installed over ``tb.build_provider``, so a caller can
        assert on *whether* and *with what* the factory was reached.
    """
    if enabled:
        monkeypatch.setenv("VOICE_ENABLED", "true")
    monkeypatch.setenv("VOICE_STT_API_KEY", "sk-env-stt")
    monkeypatch.setenv("VOICE_TTS_API_KEY", "sk-env-tts")

    recorder = _ProviderRecorder(fake)
    monkeypatch.setattr(tb, "build_provider", recorder)
    return recorder


# ---------------------------------------------------------------------------
# V-01: happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v01_happy_path_transcript_drives_turn(
    vision_bot_config, aiobot_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A voice message is transcribed, drives the turn, and speaks the reply."""
    import bot.telegram_bot as tb

    fake = _FakeVoiceProvider(transcript="Wie steht mein Portfolio?")
    _install_voice(monkeypatch, tb, fake)

    stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), vision_bot_config)

    # STT was called once with the downloaded OGG bytes and its MIME.
    assert fake.transcribe_calls == [(_OGG_BYTES, "audio/ogg")]
    aiobot_mock.download.assert_awaited_once_with("voice-id")

    # The transcript drove exactly one turn, with no attachment.
    assert len(stream.calls) == 1
    user_msgs = [m for m in stream.calls[0].messages if m.role == MessageRole.USER]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "Wie steht mein Portfolio?"
    assert user_msgs[0].attachments == []

    # The prose went out as text AND as exactly one voice note.
    aiobot_mock.send_message.assert_awaited()
    aiobot_mock.send_voice.assert_awaited_once()
    aiobot_mock.send_audio.assert_not_awaited()


# ---------------------------------------------------------------------------
# V-02: the voice note is an OGG/Opus container
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v02_voice_note_is_ogg_opus_container(
    vision_bot_config, aiobot_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``send_voice`` carries an ``.ogg`` BufferedInputFile; synth used opus."""
    import bot.telegram_bot as tb

    fake = _FakeVoiceProvider()
    _install_voice(monkeypatch, tb, fake)

    stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), vision_bot_config)

    # Synthesis requested the Telegram voice-note container.
    assert fake.synthesize_calls == [("reply-1", "opus")]

    # The voice arg is an OGG-named BufferedInputFile carrying the synth bytes.
    voice_arg = aiobot_mock.send_voice.await_args.kwargs["voice"]
    assert voice_arg.filename.endswith(".ogg")
    assert voice_arg.data == _OPUS_REPLY


# ---------------------------------------------------------------------------
# V-03: send_voice fallback to send_audio
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v03_send_voice_falls_back_to_send_audio(
    vision_bot_config, aiobot_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container Telegram won't render → send_audio fires with the same bytes."""
    import bot.telegram_bot as tb

    fake = _FakeVoiceProvider()
    _install_voice(monkeypatch, tb, fake)
    aiobot_mock.send_voice = AsyncMock(side_effect=RuntimeError("bad container"))

    stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), vision_bot_config)

    aiobot_mock.send_voice.assert_awaited_once()
    aiobot_mock.send_audio.assert_awaited_once()
    audio_arg = aiobot_mock.send_audio.await_args.kwargs["audio"]
    assert audio_arg.data == _OPUS_REPLY
    assert audio_arg.filename.endswith(".ogg")
    # The text reply still went out — the turn did not crash.
    aiobot_mock.send_message.assert_awaited()


# ---------------------------------------------------------------------------
# V-04: empty transcript
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v04_empty_transcript_replies_and_skips_turn(
    vision_bot_config, aiobot_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty transcript replies to the user in English and never drives a turn."""
    import bot.telegram_bot as tb

    fake = _FakeVoiceProvider(stt_exc=EmptyTranscriptError("no speech"))
    _install_voice(monkeypatch, tb, fake)

    stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), vision_bot_config)

    assert len(stream.calls) == 0
    aiobot_mock.send_voice.assert_not_awaited()
    sent = [c.kwargs.get("text", "") for c in aiobot_mock.send_message.await_args_list]
    assert any("could not detect any speech" in t for t in sent), sent


# ---------------------------------------------------------------------------
# V-05: STT failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v05_stt_failure_replies_and_skips_turn(
    vision_bot_config, aiobot_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcription failure replies to the user in English and never drives a turn."""
    import bot.telegram_bot as tb

    fake = _FakeVoiceProvider(stt_exc=VoiceTranscriptionError("stt boom"))
    _install_voice(monkeypatch, tb, fake)

    stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), vision_bot_config)

    assert len(stream.calls) == 0
    aiobot_mock.send_voice.assert_not_awaited()
    sent = [c.kwargs.get("text", "") for c in aiobot_mock.send_message.await_args_list]
    assert any("Transcription failed" in t for t in sent), sent


# ---------------------------------------------------------------------------
# V-06: voice disabled for the tenant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", [None, "false"])
@pytest.mark.asyncio
async def test_v06_disabled_service_replies_not_enabled(
    vision_bot_config, aiobot_mock, monkeypatch: pytest.MonkeyPatch, flag: str | None
) -> None:
    """Voice off replies clearly; no download, no resolution, no turn.

    The gate is the ``voice.enabled`` chain (ADR-0118 §5), so an absent
    ``VOICE_ENABLED`` and an explicit ``"false"`` are the same answer — and
    neither is allowed to reach the credential chain: whether voice is offered
    and whether a key exists are separate questions.
    """
    import bot.telegram_bot as tb

    fake = _FakeVoiceProvider()
    recorder = _install_voice(monkeypatch, tb, fake, enabled=False)
    if flag is not None:
        monkeypatch.setenv("VOICE_ENABLED", flag)

    stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), vision_bot_config)

    assert len(stream.calls) == 0
    aiobot_mock.download.assert_not_awaited()
    aiobot_mock.send_voice.assert_not_awaited()
    # Nothing was resolved and no provider was ever built.
    assert recorder.calls == []
    sent = [c.kwargs.get("text", "") for c in aiobot_mock.send_message.await_args_list]
    assert any("not enabled" in t for t in sent), sent


# ---------------------------------------------------------------------------
# V-07: unauthorised sender
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v07_unauthorised_sender_dropped_silently(
    vision_bot_config, aiobot_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-whitelisted sender produces no reply, no download, no turn."""
    import bot.telegram_bot as tb

    fake = _FakeVoiceProvider()
    _install_voice(monkeypatch, tb, fake)

    stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(
            aiobot_mock, _voice_message(user_id=999_999), vision_bot_config
        )

    assert len(stream.calls) == 0
    aiobot_mock.download.assert_not_awaited()
    aiobot_mock.send_message.assert_not_awaited()
    aiobot_mock.send_voice.assert_not_awaited()


# ---------------------------------------------------------------------------
# V-08: chart-only turn speaks nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v08_chart_only_turn_sends_no_voice_note(
    vision_bot_config, aiobot_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chart-only voice turn sends the PNG photo and no voice note."""
    import bot.telegram_bot as tb

    fake = _FakeVoiceProvider()
    _install_voice(monkeypatch, tb, fake)

    stream = _make_chart_only_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), vision_bot_config)

    aiobot_mock.send_photo.assert_awaited_once()
    aiobot_mock.send_voice.assert_not_awaited()
    aiobot_mock.send_audio.assert_not_awaited()
    # Empty prose was never synthesised.
    assert fake.synthesize_calls == []


# ---------------------------------------------------------------------------
# V-09: TTS failure still sends text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v09_tts_failure_still_sends_text(
    vision_bot_config, aiobot_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TTS failure is logged and skipped while the text reply still goes out."""
    import bot.telegram_bot as tb

    fake = _FakeVoiceProvider(tts_exc=VoiceSynthesisError("tts boom"))
    _install_voice(monkeypatch, tb, fake)

    stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), vision_bot_config)

    # The text chunk was sent; neither send_voice nor send_audio fired.
    sent = [c.kwargs.get("text", "") for c in aiobot_mock.send_message.await_args_list]
    assert any("reply-1" in t for t in sent), sent
    aiobot_mock.send_voice.assert_not_awaited()
    aiobot_mock.send_audio.assert_not_awaited()


# ---------------------------------------------------------------------------
# V-10: text path regression — never speaks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v10_text_message_never_speaks(vision_bot_config, aiobot_mock) -> None:
    """A plain text message routes through the text handler and never speaks."""
    import bot.telegram_bot as tb

    stream = _make_recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_text_message(
            aiobot_mock,
            _text_message("Wie geht es dem Portfolio?"),
            vision_bot_config,
        )

    assert len(stream.calls) == 1
    aiobot_mock.send_message.assert_awaited()
    aiobot_mock.send_voice.assert_not_awaited()
    aiobot_mock.send_audio.assert_not_awaited()
