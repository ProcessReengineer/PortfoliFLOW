# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-message voice resolution and gating in the Telegram handler (ADR-0118, ADR-0136).

The voice twin of ``test_bot_llm_resolution.py``, and built the same way: the
handler is driven directly with a **resolver double** installed over
``tb.CredentialResolver``, so the assertions are on the chains the handler
walks and on what reaches the two builders — no database, no network.

Where the LLM twin resolves once per *turn*, this resolves each half once per
inbound *voice message*, for the leg that needs it (ADR-0136 §5): the STT leg
builds from a :class:`~services.voice.ResolvedSTT`, the TTS reply leg from a
:class:`~services.voice.ResolvedTTS`.

What is pinned here:

* **The tenant's rows drive the message.** Both halves' providers, keys and
  their model / base-URL / persona-voice rows reach the recorded resolution;
  fields no scope holds land on the ``openai`` declaration's defaults
  (ADR-0136 §2).
* **Each half resolves once per message.** Each half's credential is resolved
  exactly once, and each leg builds exactly once from its half.
* **Incomplete is loud, per half.** A missing STT key answers with the
  assembly's message naming Providers & Credentials *and* ``.env`` and drives
  no turn; a missing TTS key warns once and the answer still goes out in
  text. Neither crashes the dispatcher (ADR-0136 §4).
* **Gating is a per-tenant answer.** The gate walks ``voice.enabled`` over
  ``tenant → env`` first; voice off reads nothing else and never probes a
  credential (ADR-0118 §5).
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
from services.investments.credential_resolver import NoCredential, ProviderCredential
from services.voice import ADAPTER_DECLARATIONS, ResolvedSTT, ResolvedTTS

#: The ``openai`` declaration — today's defaults (ADR-0136 §2), compared
#: against rather than restated as literals.
_OPENAI = ADAPTER_DECLARATIONS["openai"]

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
    "VOICE_TTS_BASE_URL",
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
    ("voice_stt", "provider"): "openai",
    ("voice_tts", "provider"): "openai",
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
    instead of returned — the vault-refuses case. An unscripted credential is
    a :class:`NoCredential`, as the real resolver answers for the two
    optional voice halves (ADR-0136 §3).
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
            return NoCredential(provider)
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
    """In-memory :class:`SpeechToText` / :class:`TextToSpeech` stand-in recording its calls."""

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
    """Stands in for ``build_stt`` / ``build_tts``: records each half, serves the fake."""

    def __init__(self, fake: _FakeVoiceProvider) -> None:
        self._fake = fake
        self.stt: ResolvedSTT | None = None
        self.tts: ResolvedTTS | None = None
        self.stt_calls = 0
        self.tts_calls = 0

    def build_stt(self, resolved: ResolvedSTT) -> _FakeVoiceProvider:
        self.stt = resolved
        self.stt_calls += 1
        return self._fake

    def build_tts(self, resolved: ResolvedTTS) -> _FakeVoiceProvider:
        self.tts = resolved
        self.tts_calls += 1
        return self._fake


def _install(
    monkeypatch: pytest.MonkeyPatch, resolver: _FakeResolver
) -> tuple[Any, _ProviderRecorder]:
    """Install the resolver double and the provider recorder; return both handles."""
    import bot.telegram_bot as tb

    monkeypatch.setattr(tb, "CredentialResolver", lambda **kwargs: resolver)
    recorder = _ProviderRecorder(_FakeVoiceProvider())
    monkeypatch.setattr(tb, "build_stt", recorder.build_stt)
    monkeypatch.setattr(tb, "build_tts", recorder.build_tts)
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

    stt, tts = recorder.stt, recorder.tts
    assert stt is not None, "the STT leg never reached its builder"
    assert tts is not None, "the TTS leg never reached its builder"
    assert stt.api_key == "sk-tenant-stt"
    assert tts.api_key == "sk-tenant-tts"
    assert stt.model == "tenant/stt-model"
    assert stt.base_url == "https://tenant.example/stt/v1"
    assert tts.model == "tenant/tts-model"
    assert tts.voice == "tenant-voice"
    # Voice carries no user axis: every field is tenant-scope only.
    voice_resolves = [c for c in resolver.resolve_calls if c["provider"].startswith("voice")]
    assert all("user_id" not in call for call in voice_resolves), voice_resolves
    assert {call["tenant_id"] for call in voice_resolves} == {tb._bot_tenant_id}


async def test_unscripted_config_fields_fall_to_the_legacy_declaration(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fields no scope holds land on the ``openai`` declaration, not on ``None``.

    The resolver returns ``None`` for "configured nowhere"; the adapter
    declaration, applied by the pure assembly, owns the default (ADR-0136 §2).
    """
    resolver = _FakeResolver(
        config={
            ("voice", "enabled"): "true",
            ("voice_stt", "provider"): "openai",
            ("voice_tts", "provider"): "openai",
            ("openrouter", "model"): "m/1",
        }
    )
    tb, recorder = _install(monkeypatch, resolver)
    tb._bot_tenant_id = uuid4()

    with patch.object(AIServiceCore, "stream_response", _recording_stream()):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), bot_config)

    stt, tts = recorder.stt, recorder.tts
    assert stt is not None and tts is not None
    assert stt.model == _OPENAI.stt_model
    assert stt.base_url == _OPENAI.base_url
    assert tts.model == _OPENAI.tts_model
    assert tts.voice == _OPENAI.tts_voice
    assert tts.base_url == _OPENAI.base_url
    assert stt.provider == _OPENAI.key
    assert tts.provider == _OPENAI.key


# ---------------------------------------------------------------------------
# Each half resolves once per message
# ---------------------------------------------------------------------------


async def test_each_half_resolves_once_per_message(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``voice_stt`` resolves once inbound, ``voice_tts`` once outbound; each leg builds once."""
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
    # … and each built its adapter once, from its own half.
    assert recorder.stt_calls == 1 and recorder.tts_calls == 1


# ---------------------------------------------------------------------------
# Incomplete is loud, per half
# ---------------------------------------------------------------------------


async def test_a_missing_stt_key_answers_politely_and_never_streams(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active ``openai`` STT half without a key: the assembly's message, no turn.

    Nothing was heard, so there is nothing to answer — and the operator learns
    where to fix it (ADR-0136 §4).
    """
    keys = {key: value for key, value in _TENANT_KEYS.items() if key != "voice_stt"}
    resolver = _FakeResolver(keys=keys)
    tb, recorder = _install(monkeypatch, resolver)
    tb._bot_tenant_id = uuid4()

    stream = _recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), bot_config)

    assert stream.calls == []
    assert recorder.stt_calls == 0
    assert recorder.tts_calls == 0
    aiobot_mock.send_voice.assert_not_awaited()

    sent = [c.kwargs["text"] for c in aiobot_mock.send_message.call_args_list]
    assert len(sent) == 1, sent
    assert "Providers & Credentials" in sent[0]
    assert "VOICE_STT_API_KEY" in sent[0]
    # The one warning sign is the send site's, prefixed exactly once.
    assert sent[0].startswith("⚠️ ")
    assert sent[0].count("⚠️") == 1


async def test_a_missing_tts_key_warns_and_still_answers_in_text(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active ``openai`` TTS half without a key warns once; the answer goes out in text.

    The question was heard, so it is answered: the misconfiguration is loud,
    but it does not cost the user the reply.
    """
    keys = {key: value for key, value in _TENANT_KEYS.items() if key != "voice_tts"}
    resolver = _FakeResolver(keys=keys)
    tb, recorder = _install(monkeypatch, resolver)
    tb._bot_tenant_id = uuid4()

    stream = _recording_stream()
    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_voice_message(aiobot_mock, _voice_message(), bot_config)

    assert len(stream.calls) == 1
    assert recorder.stt_calls == 1
    assert recorder.tts_calls == 0
    aiobot_mock.send_voice.assert_not_awaited()
    aiobot_mock.send_audio.assert_not_awaited()

    sent = [c.kwargs["text"] for c in aiobot_mock.send_message.call_args_list]
    warnings = [text for text in sent if text.startswith("⚠️ ")]
    assert len(warnings) == 1, sent
    assert "VOICE_TTS_API_KEY" in warnings[0]
    assert any("reply" in text for text in sent if not text.startswith("⚠️")), sent


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
    assert recorder.stt_calls == 0
    assert recorder.tts_calls == 0
    assert resolver.resolve_calls == []
    # The gate reads the switch first and stops there: an off tenant pays one
    # config read, never the providers.
    voice_reads = [call for call in resolver.config_calls if call[0].startswith("voice")]
    assert voice_reads == [("voice", "enabled", ("tenant", "env"))]
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
    assert recorder.stt_calls == 0
    assert recorder.tts_calls == 0
    sent = [c.kwargs["text"] for c in aiobot_mock.send_message.call_args_list]
    assert len(sent) == 1, sent
    assert "temporarily unavailable" in sent[0]
    assert "VOICE_STT_API_KEY" not in sent[0]
