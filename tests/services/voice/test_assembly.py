# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.voice.assembly` (ADR-0136 §4, §5).

Three outcomes per half: ``None`` (inactive — voice off or no provider
chosen), a value object (active, with the adapter's defaults filled in) or
:class:`VoiceConfigurationError` (misconfigured, with a message naming the
field and both scopes). Where a rule is symmetric the two halves are
parametrised; the TTS-only ``voice`` rule has a case of its own. The Admin
card's state line (:func:`describe_voice_state`) closes the module.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from services.voice.assembly import assemble_stt, assemble_tts, describe_voice_state
from services.voice.errors import VoiceConfigurationError
from services.voice.resolved import ResolvedSTT, ResolvedTTS

_ADMIN = "Admin → Providers & Credentials"
_VOCABULARY = "elevenlabs, openai, openai_compatible"

Assemble = Callable[..., ResolvedSTT | ResolvedTTS | None]


def _stt(
    enabled: bool = True,
    provider: str | None = "openai",
    model: str | None = None,
    base_url: str | None = None,
    voice: str | None = None,
    api_key: str | None = "k",
) -> ResolvedSTT | None:
    """Call :func:`assemble_stt` with the TTS signature; ``voice`` is dropped."""
    del voice
    return assemble_stt(enabled, provider, model, base_url, api_key)


def _tts(
    enabled: bool = True,
    provider: str | None = "openai",
    model: str | None = None,
    base_url: str | None = None,
    voice: str | None = None,
    api_key: str | None = "k",
) -> ResolvedTTS | None:
    """Call :func:`assemble_tts` with keyword defaults."""
    return assemble_tts(enabled, provider, model, base_url, voice, api_key)


#: (assemble, env prefix, sentence-initial half name)
_HALVES = [
    pytest.param(_stt, "VOICE_STT", "Speech-to-text", id="stt"),
    pytest.param(_tts, "VOICE_TTS", "Text-to-speech", id="tts"),
]


@pytest.mark.parametrize(("assemble", "prefix", "title"), _HALVES)
def test_disabled_is_inactive(assemble: Assemble, prefix: str, title: str) -> None:
    assert (
        assemble(
            enabled=False,
            provider="openai",
            model="m",
            base_url="https://x/v1",
            voice="v",
            api_key="k",
        )
        is None
    )
    # Inactive is checked before the provider: an unknown key stays quiet too.
    assert assemble(enabled=False, provider="nonsense") is None


@pytest.mark.parametrize("provider", [None, "", "  "])
def test_no_provider_is_inactive(provider: str | None) -> None:
    assert _stt(provider=provider, model="m", api_key="k") is None
    assert _tts(provider=provider, model="m", voice="v", api_key="k") is None


@pytest.mark.parametrize(("assemble", "prefix", "title"), _HALVES)
def test_unknown_provider_is_loud(assemble: Assemble, prefix: str, title: str) -> None:
    with pytest.raises(VoiceConfigurationError) as excinfo:
        assemble(provider="deepgram")
    message = str(excinfo.value)
    assert message.startswith(f"{title} provider 'deepgram' is not supported.")
    assert f"Choose one of {_VOCABULARY}" in message
    assert _ADMIN in message
    assert f"{prefix}_PROVIDER in .env" in message


def test_legacy_openai_fills_todays_defaults() -> None:
    assert _stt(api_key="sk") == ResolvedSTT(
        "openai", "gpt-4o-mini-transcribe", "sk", "https://api.openai.com/v1"
    )
    assert _tts(api_key="sk") == ResolvedTTS(
        "openai", "gpt-4o-mini-tts", "nova", "sk", "https://api.openai.com/v1"
    )


def test_explicit_values_beat_the_defaults() -> None:
    stt = _stt(model="whisper-large-v3", base_url="https://api.groq.com/openai/v1")
    assert stt == ResolvedSTT("openai", "whisper-large-v3", "k", "https://api.groq.com/openai/v1")
    tts = _tts(model="tts-1", base_url="https://proxy/v1", voice="alloy")
    assert tts == ResolvedTTS("openai", "tts-1", "alloy", "k", "https://proxy/v1")


@pytest.mark.parametrize(("assemble", "prefix", "title"), _HALVES)
@pytest.mark.parametrize("api_key", [None, "", "  "])
def test_legacy_openai_without_a_key_is_loud(
    assemble: Assemble, prefix: str, title: str, api_key: str | None
) -> None:
    with pytest.raises(VoiceConfigurationError) as excinfo:
        assemble(api_key=api_key)
    message = str(excinfo.value)
    assert message.startswith(f"{title} is active (openai) but its API key is not configured.")
    assert f"Set API key in {_ADMIN}" in message
    assert f"{prefix}_API_KEY in .env" in message


def test_elevenlabs_fills_the_vendor_defaults_and_requires_a_voice() -> None:
    assert _stt(provider="elevenlabs", api_key="xi") == ResolvedSTT(
        "elevenlabs", "scribe_v2", "xi", "https://api.elevenlabs.io"
    )
    assert _tts(provider="elevenlabs", voice="JBFq", api_key="xi") == ResolvedTTS(
        "elevenlabs", "eleven_multilingual_v2", "JBFq", "xi", "https://api.elevenlabs.io"
    )
    with pytest.raises(VoiceConfigurationError) as excinfo:
        _tts(provider="elevenlabs", api_key="xi")
    assert str(excinfo.value).startswith(
        "Text-to-speech is active (elevenlabs) but the voice is not configured."
    )


@pytest.mark.parametrize(("assemble", "prefix", "title"), _HALVES)
def test_elevenlabs_without_a_key_is_loud(assemble: Assemble, prefix: str, title: str) -> None:
    # The voice is given so that the key is the first empty required value.
    with pytest.raises(VoiceConfigurationError) as excinfo:
        assemble(provider="elevenlabs", voice="JBFq", api_key=None)
    message = str(excinfo.value)
    assert message.startswith(f"{title} is active (elevenlabs) but its API key is not configured.")
    assert f"{prefix}_API_KEY in .env" in message


def test_openai_compatible_keyless_is_active() -> None:
    keyless_stt = assemble_stt(True, "openai_compatible", "m", "http://localhost:8000/v1", None)
    assert keyless_stt == ResolvedSTT("openai_compatible", "m", "", "http://localhost:8000/v1")
    keyless_tts = assemble_tts(
        True, "openai_compatible", "kokoro", "http://localhost:8880/v1", "af_bella", None
    )
    assert keyless_tts == ResolvedTTS(
        "openai_compatible", "kokoro", "af_bella", "", "http://localhost:8880/v1"
    )


@pytest.mark.parametrize(("assemble", "prefix", "title"), _HALVES)
def test_openai_compatible_without_a_model_is_loud(
    assemble: Assemble, prefix: str, title: str
) -> None:
    with pytest.raises(VoiceConfigurationError) as excinfo:
        assemble(provider="openai_compatible", base_url="http://x/v1", voice="v", api_key=None)
    message = str(excinfo.value)
    assert message == (
        f"{title} is active (openai_compatible) but the model is not configured. "
        f"Set Model in {_ADMIN} (it applies on your next voice message) "
        f"or {prefix}_MODEL in .env for the whole application."
    )


@pytest.mark.parametrize(("assemble", "prefix", "title"), _HALVES)
def test_openai_compatible_without_an_endpoint_is_loud(
    assemble: Assemble, prefix: str, title: str
) -> None:
    with pytest.raises(VoiceConfigurationError) as excinfo:
        assemble(provider="openai_compatible", model="m", voice="v", api_key=None)
    message = str(excinfo.value)
    assert f"{title} is active (openai_compatible) but the endpoint is not configured." in message
    assert f"Set Base URL in {_ADMIN}" in message
    assert f"{prefix}_BASE_URL in .env" in message


def test_tts_openai_compatible_without_a_voice_is_loud() -> None:
    with pytest.raises(VoiceConfigurationError) as excinfo:
        _tts(provider="openai_compatible", model="m", base_url="http://x/v1", api_key=None)
    message = str(excinfo.value)
    assert "Text-to-speech is active (openai_compatible) but the voice is not configured." in (
        message
    )
    assert f"Set Voice in {_ADMIN}" in message
    assert "VOICE_TTS_VOICE in .env" in message


@pytest.mark.parametrize(("assemble", "prefix", "title"), _HALVES)
def test_the_first_missing_field_is_the_one_named(
    assemble: Assemble, prefix: str, title: str
) -> None:
    with pytest.raises(VoiceConfigurationError) as excinfo:
        assemble(provider="openai_compatible", voice=None, api_key=None)
    message = str(excinfo.value)
    assert "the model is not configured" in message
    assert f"{prefix}_MODEL" in message
    assert f"{prefix}_BASE_URL" not in message


def test_values_are_stripped() -> None:
    stt = _stt(provider=" openai ", model=" m ", base_url=" https://x/v1 ", api_key=" k ")
    assert stt == ResolvedSTT("openai", "m", "k", "https://x/v1")
    tts = _tts(provider=" openai ", model=" m ", voice=" v ", api_key=" k ")
    assert tts == ResolvedTTS("openai", "m", "v", "k", "https://api.openai.com/v1")


def test_assembly_reads_no_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_STT_MODEL", "from-env")
    monkeypatch.setenv("VOICE_STT_PROVIDER", "openai_compatible")
    resolved = assemble_stt(True, "openai", None, None, "k")
    assert resolved is not None
    assert resolved.model == "gpt-4o-mini-transcribe"


def test_assembly_is_pure_of_imports() -> None:
    import services.voice.assembly as m

    assert not hasattr(m, "os")


# ---------------------------------------------------------------------------
# The Admin card's state line
# ---------------------------------------------------------------------------

_INACTIVE = "inactive, no provider selected"
_UNREACHABLE = " — unreachable without speech-to-text (replies are spoken only to spoken questions)"


def test_state_off() -> None:
    assert describe_voice_state(False, "openai", "openai") == "off"


def test_state_both_inactive() -> None:
    assert describe_voice_state(True, None, "") == (
        f"on — speech-to-text: {_INACTIVE} · text-to-speech: {_INACTIVE}"
    )


def test_state_stt_only() -> None:
    assert describe_voice_state(True, "openai_compatible", None) == (
        f"on — speech-to-text: active (openai_compatible) · text-to-speech: {_INACTIVE}"
    )


def test_state_tts_only_says_unreachable() -> None:
    assert describe_voice_state(True, None, "openai") == (
        f"on — speech-to-text: {_INACTIVE} · text-to-speech: active (openai){_UNREACHABLE}"
    )


def test_state_both_active() -> None:
    assert describe_voice_state(True, "openai", "openai_compatible") == (
        "on — speech-to-text: active (openai) · text-to-speech: active (openai_compatible)"
    )


def test_state_unknown_key_is_misconfigured() -> None:
    assert describe_voice_state(True, "deepgram", "openai") == (
        "on — speech-to-text: misconfigured — provider 'deepgram' is not supported"
        f" · text-to-speech: active (openai){_UNREACHABLE}"
    )


def test_state_strips_whitespace() -> None:
    assert describe_voice_state(True, " openai ", "   ") == (
        f"on — speech-to-text: active (openai) · text-to-speech: {_INACTIVE}"
    )
