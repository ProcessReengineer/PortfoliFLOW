# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-message voice resolution and gating in the Telegram handler (ADR-0118).

The voice twin of ``test_bot_llm_resolution.py``, and built the same way: the
handler is driven directly with a **resolver double** installed over
``tb.CredentialResolver``, so the assertions are on the chains the handler
walks and on what reaches the provider factory — no database, no network.

Where the LLM twin resolves once per *turn*, this resolves once per inbound
*voice message*: the STT leg and the TTS reply leg build from the same
:class:`~services.voice.ResolvedVoice`, so a key rotated between two messages
applies to the next one whole, and never half-and-half within one.

What is pinned here:

* **The tenant's rows drive the message.** Both halves' keys and their model /
  base-URL / persona-voice rows reach the recorded resolution; fields no scope
  holds land on the ``DEFAULT_*`` constants (ADR-0118 §4).
* **One resolution per message.** Each half's credential is resolved exactly
  once, and both legs build from the very same object.
* **Enabled-but-keyless is loud.** Either half missing answers with the one
  actionable message naming Providers & Credentials *and* ``.env``, drives no
  turn, and does not crash the dispatcher — the startup validation
  ``VoiceConfig.__post_init__`` used to do, relocated to first use
  (ADR-0118 §2).
* **Gating is a per-tenant answer.** The gate walks ``voice.enabled`` over
  ``tenant → env`` and nothing else: voice off never probes a credential
  (ADR-0118 §5).
* **An undecryptable vault never reads as a missing key.** It answers
  generically and stays out of the dispatcher — the bot's one deliberate
  divergence from the web surface, which propagates it to a 500.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.ai_models import Message, MessageRole
from services.ai_service_core import AIServiceCore, StreamEvent
from services.credential_vault import VaultDecryptError
from services.investments.credential_resolver import (
    CredentialUnavailableError,
    ProviderCredential,
)
from services.voice import (
    DEFAULT_STT_BASE_URL,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_VOICE,
    DEFAULT_VOICE_PROVIDER,
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
    # The voice chains' application scope. Cleared per test so the repository
    # ``.env`` cannot serve a value the double was meant to serve.
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
_OGG_BYTES = b"OggS" + b"\x00" * 40
_OPUS_REPLY = b"OggS-reply-bytes"

#: A fully scripted tenant, every value distinguishable from the code default
#: so a test reading it back cannot confuse "resolved from a row" with "fell
#: through to the constant".
_TENANT_VOICE_CONFIG: dict[tuple[str, str], str] = {
    ("voice", "enabled"): "true",
    ("voice_stt", "model"): "tenant/stt-model",
    ("voice_stt", "base_url"): "https://tenant.example/stt/v1",
    ("voice_tts", "model"): "tenant/tts-model",
    ("voice_tts", "voice"): "tenant-voice",
    ("openrouter", "model"): "tenant/llm-model",
}

_TENANT_KEYS: dict[str, Any] = {
    "voice_stt": "sk-tenant-stt",
    "voice_tts": "sk-tenant-tts",
    "openrouter": "sk-tenant-llm",
}


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
    """A valid enabled BotSettings with no credential of any kind in ``.env``."""
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
    aiobot.send_voice = AsyncMock()
    aiobot.send_audio = AsyncMock()
    aiobot.delete_message = AsyncMock()
    aiobot.download = AsyncMock(return_value=io.BytesIO(_OGG_BYTES))
    return aiobot


def _voice_message(*, chat_id: int = _CHAT_ID, user_id: int = _USER_ID) -> Any:
    """Telegram voice message (a ``voice`` payload, no text/photo)."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=None,
        caption=None,
        photo=[],
        document=None,
        voice=SimpleNamespace(file_id="voice-id", mime_type="audio/ogg", duration=3),
        chat=SimpleNamespace(id=chat_id),
    )


class _FakeResolver:
    """Records both chains and answers from a per-provider script.

    Voice-aware where the LLM suite's double is not: a credential is scripted
    **per provider** (the two halves chain independently, ADR-0118 §1) and a
    config field **per (provider, key)** — both halves declare a ``model``, so
    a key-only mapping could not tell them apart. It serves ``openrouter``
    too, because the turn behind a voice message still has to resolve.

    A scripted credential value may be an exception instance, which is raised
    instead of returned — the vault-refuses case.
    """

    def __init__(
        self,
        *,
        keys: dict[str, Any] | None = None,
        config: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._keys = keys if keys is not None else dict(_TENANT_KEYS)
        self._config = config if config is not None else dict(_TENANT_VOICE_CONFIG)
        self.config_calls: list[tuple[str, str, Any]] = []
        self.resolve_calls: list[dict[str, Any]] = []

    async def resolve(self, provider: str, **kwargs: Any) -> Any:
        self.resolve_calls.append({"provider": provider, **kwargs})
        value = self._keys.get(provider)
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise CredentialUnavailableError(f"no credential for {provider!r} (test)")
        return ProviderCredential(provider=provider, payload={"api_key": value})

    async def resolve_config(
        self,
        provider: str,
        key: str,
        *,
        user_id: Any = None,
        scopes: Any = None,
    ) -> str | None:
        self.config_calls.append((provider, key, scopes))
        return self._config.get((provider, key))


class _FakeVoiceProvider:
    """In-memory :class:`VoiceProvider` stand-in recording its calls."""

    def __init__(self, transcript: str = "How is my portfolio doing?") -> None:
        self._transcript = transcript
        self.transcribe_calls: list[tuple[bytes, str]] = []
        self.synthesize_calls: list[tuple[str, str]] = []

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        self.transcribe_calls.append((audio, mime_type))
        return self._transcript

    async def synthesize(self, text: str, *, fmt: str) -> tuple[bytes, str]:
        self.synthesize_calls.append((text, fmt))
        return _OPUS_REPLY, "audio/ogg"


class _ProviderRecorder:
    """Stands in for ``build_provider``: records the resolution, serves the fake."""

    def __init__(self, fake: _FakeVoiceProvider) -> None:
        self._fake = fake
        self.calls: list[Any] = []

    def __call__(self, voice: Any) -> _FakeVoiceProvider:
        self.calls.append(voice)
        return self._fake


def _install(
    monkeypatch: pytest.MonkeyPatch, resolver: _FakeResolver
) -> tuple[Any, _ProviderRecorder]:
    """Install the resolver double and the provider recorder; return both handles."""
    import bot.telegram_bot as tb

    monkeypatch.setattr(tb, "CredentialResolver", lambda **kwargs: resolver)
    recorder = _ProviderRecorder(_FakeVoiceProvider())
    monkeypatch.setattr(tb, "build_provider", recorder)
    return tb, recorder


def _recording_stream() -> Any:
    """Patched ``stream_response`` recording the conversations it was handed."""
    calls: list[Any] = []

    async def fake_stream(
        self: AIServiceCore,
        conversation: Any,
        system_prompt: str = "",
        temperature: float = 0.7,
        tool_context: Any = None,
        llm: Any = None,
    ):
        calls.append(conversation)
        final = Message(role=MessageRole.ASSISTANT, content="reply")
        yield StreamEvent("chunk", {"text": "reply"})
        yield StreamEvent("stream_finished", {"message": final, "iterations": 0})

    fake_stream.calls = calls  # type: ignore[attr-defined]
    return fake_stream


# ---------------------------------------------------------------------------
# The tenant's rows drive the message
# ---------------------------------------------------------------------------


async def test_tenant_rows_drive_the_message(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both halves' scripted rows reach the resolution the legs build from."""
    resolver = _FakeResolver()
    tb, recorder = _install(monkeypatch, resolver)
    tb._bot_tenant_id = uuid4()

    with patch.object(AIServiceCore, "stream_response", _recording_stream()):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), bot_config)

    assert recorder.calls, "the STT leg never reached the provider factory"
    voice = recorder.calls[0]
    assert voice.stt_api_key == "sk-tenant-stt"
    assert voice.tts_api_key == "sk-tenant-tts"
    assert voice.stt_model == "tenant/stt-model"
    assert voice.stt_base_url == "https://tenant.example/stt/v1"
    assert voice.tts_model == "tenant/tts-model"
    assert voice.tts_voice == "tenant-voice"
    # Voice carries no user axis: every field is tenant-scope only.
    voice_resolves = [c for c in resolver.resolve_calls if c["provider"].startswith("voice")]
    assert all("user_id" not in call for call in voice_resolves), voice_resolves
    assert {call["tenant_id"] for call in voice_resolves} == {tb._bot_tenant_id}


async def test_unscripted_config_fields_fall_to_the_constants(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fields no scope holds land on the ``DEFAULT_*`` tails, not on ``None``.

    The resolver returns ``None`` for "configured nowhere" and the caller owns
    the default (ADR-0112 §4b), so the tails belong here rather than in
    ``services/voice/``. The two provider keys have no chain at all — the
    taxonomy does not declare them (ADR-0118 §1) — so they read the
    environment, which this module clears.
    """
    resolver = _FakeResolver(config={("voice", "enabled"): "true", ("openrouter", "model"): "m/1"})
    tb, recorder = _install(monkeypatch, resolver)
    tb._bot_tenant_id = uuid4()

    with patch.object(AIServiceCore, "stream_response", _recording_stream()):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), bot_config)

    voice = recorder.calls[0]
    assert voice.stt_model == DEFAULT_STT_MODEL
    assert voice.stt_base_url == DEFAULT_STT_BASE_URL
    assert voice.tts_model == DEFAULT_TTS_MODEL
    assert voice.tts_voice == DEFAULT_TTS_VOICE
    assert voice.stt_provider == DEFAULT_VOICE_PROVIDER
    assert voice.tts_provider == DEFAULT_VOICE_PROVIDER


# ---------------------------------------------------------------------------
# One resolution per message
# ---------------------------------------------------------------------------


async def test_one_resolution_serves_both_legs(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each half resolves once, and the STT and TTS legs build from one object."""
    resolver = _FakeResolver()
    tb, recorder = _install(monkeypatch, resolver)
    tb._bot_tenant_id = uuid4()

    with patch.object(AIServiceCore, "stream_response", _recording_stream()):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), bot_config)

    providers = [call["provider"] for call in resolver.resolve_calls]
    assert providers.count("voice_stt") == 1
    assert providers.count("voice_tts") == 1

    # Both legs ran (a text reply and a spoken one went out) …
    aiobot_mock.send_voice.assert_awaited_once()
    # … and each built its provider from the very same resolution.
    assert 1 <= len(recorder.calls) <= 2
    assert all(call is recorder.calls[0] for call in recorder.calls)


# ---------------------------------------------------------------------------
# Enabled but keyless → the one actionable reply
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["voice_stt", "voice_tts"])
async def test_a_missing_half_answers_politely_and_never_streams(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """One key of two is a misconfiguration, whichever half it is.

    Voice enabled requires both halves (ADR-0118 §2), and which leg would have
    noticed first must not decide whether the operator hears about it.
    """
    keys = {key: value for key, value in _TENANT_KEYS.items() if key != missing}
    resolver = _FakeResolver(keys=keys)
    tb, recorder = _install(monkeypatch, resolver)
    tb._bot_tenant_id = uuid4()

    stream = _recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), bot_config)

    assert stream.calls == []
    assert recorder.calls == []
    aiobot_mock.send_voice.assert_not_awaited()

    sent = [c.kwargs["text"] for c in aiobot_mock.send_message.call_args_list]
    assert len(sent) == 1, sent
    assert "Providers & Credentials" in sent[0]
    assert "VOICE_STT_API_KEY" in sent[0]
    # The one warning sign is the send site's, prefixed exactly once.
    assert sent[0].startswith("⚠️ ")
    assert sent[0].count("⚠️") == 1


# ---------------------------------------------------------------------------
# Gating is a per-tenant answer
# ---------------------------------------------------------------------------


async def test_the_gate_walks_the_tenant_then_env_chain(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate asks ``voice.enabled`` over ``tenant → env`` — never a user row."""
    resolver = _FakeResolver()
    tb, _recorder = _install(monkeypatch, resolver)
    tb._bot_tenant_id = uuid4()

    with patch.object(AIServiceCore, "stream_response", _recording_stream()):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), bot_config)

    assert ("voice", "enabled", ("tenant", "env")) in resolver.config_calls


@pytest.mark.parametrize("answer", [None, "false"])
async def test_voice_off_never_probes_a_credential(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch, answer: str | None
) -> None:
    """A disabled tenant replies "not enabled" and resolves no credential.

    Whether the affordance is offered and whether a key can be found are
    separate questions (ADR-0118 §5); conflating them would hide a
    misconfiguration behind a bland reply.
    """
    config = dict(_TENANT_VOICE_CONFIG)
    if answer is None:
        del config[("voice", "enabled")]
    else:
        config[("voice", "enabled")] = answer
    resolver = _FakeResolver(config=config)
    tb, recorder = _install(monkeypatch, resolver)
    tb._bot_tenant_id = uuid4()

    stream = _recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), bot_config)

    assert stream.calls == []
    assert recorder.calls == []
    assert resolver.resolve_calls == []
    aiobot_mock.download.assert_not_awaited()
    sent = [c.kwargs["text"] for c in aiobot_mock.send_message.call_args_list]
    assert any("not enabled" in text for text in sent), sent


# ---------------------------------------------------------------------------
# An undecryptable vault is not a missing key
# ---------------------------------------------------------------------------


async def test_an_undecryptable_vault_is_not_a_missing_key(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusing vault answers generically, drives no turn, and never raises.

    A wrong or rotated master key is an operator emergency, not a "configure
    me" nudge — and it must not reach the aiogram dispatcher either, which is
    where the bot diverges from the web surface's 500.
    """
    keys = dict(_TENANT_KEYS)
    keys["voice_stt"] = VaultDecryptError("ciphertext will not decrypt")
    resolver = _FakeResolver(keys=keys)
    tb, recorder = _install(monkeypatch, resolver)
    tb._bot_tenant_id = uuid4()

    stream = _recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), bot_config)

    assert stream.calls == []
    assert recorder.calls == []
    sent = [c.kwargs["text"] for c in aiobot_mock.send_message.call_args_list]
    assert len(sent) == 1, sent
    assert "temporarily unavailable" in sent[0]
    assert tb._NO_VOICE_MESSAGE not in sent[0]
