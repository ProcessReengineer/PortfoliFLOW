# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.voice.factory` (ADR-0076; ADR-0136 §1).

One builder per half: :func:`build_stt` takes a :class:`ResolvedSTT` and
returns a :class:`SpeechToText`, :func:`build_tts` takes a :class:`ResolvedTTS`
and returns a :class:`TextToSpeech`. Each refuses an unknown provider key, and
each builds only the half it was asked for. Every declared adapter has a class
and every class a declaration (ADR-0136 §2). The single-port names retire with
no alias.
"""

from __future__ import annotations

import pytest

import services.voice
from services.voice import factory
from services.voice.declarations import PROVIDER_KEYS
from services.voice.errors import UnsupportedVoiceProviderError, VoiceConfigurationError
from services.voice.factory import build_stt, build_tts
from services.voice.openai_provider import OpenAIVoiceProvider
from services.voice.provider import SpeechToText, TextToSpeech
from services.voice.resolved import ResolvedSTT, ResolvedTTS


def _stt(provider: str = "openai") -> ResolvedSTT:
    """Build an STT resolution with explicit fields (no env coupling)."""
    return ResolvedSTT(
        provider=provider,
        model="gpt-4o-mini-transcribe",
        api_key="sk-stt",
        base_url="https://api.openai.com/v1",
    )


def _tts(provider: str = "openai") -> ResolvedTTS:
    """Build a TTS resolution with explicit fields (no env coupling)."""
    return ResolvedTTS(
        provider=provider,
        model="gpt-4o-mini-tts",
        voice="nova",
        api_key="sk-tts",
        base_url="https://api.openai.com/v1",
    )


def test_build_stt_openai_returns_adapter() -> None:
    """``"openai"`` yields an :class:`OpenAIVoiceProvider` satisfying the STT port."""
    adapter = build_stt(_stt())

    assert isinstance(adapter, OpenAIVoiceProvider)
    # The Protocol is runtime-checkable, so the adapter satisfies it.
    assert isinstance(adapter, SpeechToText)


def test_build_tts_openai_returns_adapter() -> None:
    """``"openai"`` yields an :class:`OpenAIVoiceProvider` satisfying the TTS port."""
    adapter = build_tts(_tts())

    assert isinstance(adapter, OpenAIVoiceProvider)
    assert isinstance(adapter, TextToSpeech)


def test_build_stt_openai_compatible_returns_the_wire_adapter() -> None:
    """``"openai_compatible"`` is served by the OpenAI-wire adapter class (ADR-0136 §2)."""
    adapter = build_stt(_stt("openai_compatible"))

    assert isinstance(adapter, OpenAIVoiceProvider)
    assert isinstance(adapter, SpeechToText)


def test_build_tts_openai_compatible_returns_the_wire_adapter() -> None:
    """The TTS twin: ``"openai_compatible"`` yields the OpenAI-wire adapter."""
    adapter = build_tts(_tts("openai_compatible"))

    assert isinstance(adapter, OpenAIVoiceProvider)
    assert isinstance(adapter, TextToSpeech)


def test_every_declared_adapter_has_a_class_and_vice_versa() -> None:
    """The factory's class map and the adapter declarations name the same keys."""
    assert set(factory._ADAPTER_CLASSES) == PROVIDER_KEYS


def test_build_stt_unknown_provider_raises() -> None:
    """An unknown STT provider raises rather than silently falling back."""
    with pytest.raises(UnsupportedVoiceProviderError, match="elevenlabs"):
        build_stt(_stt("elevenlabs"))


def test_build_tts_unknown_provider_raises() -> None:
    """An unknown TTS provider raises rather than silently falling back."""
    with pytest.raises(UnsupportedVoiceProviderError, match="deepgram"):
        build_tts(_tts("deepgram"))


async def test_build_stt_builds_only_the_stt_half(httpx_mock) -> None:
    """The STT adapter has no TTS half: ``synthesize`` refuses before any HTTP."""
    adapter = build_stt(_stt())
    assert isinstance(adapter, OpenAIVoiceProvider)

    with pytest.raises(VoiceConfigurationError, match="text-to-speech"):
        await adapter.synthesize("hello", fmt="mp3")

    assert httpx_mock.get_requests() == []


async def test_build_tts_builds_only_the_tts_half(httpx_mock) -> None:
    """The TTS adapter has no STT half: ``transcribe`` refuses before any HTTP."""
    adapter = build_tts(_tts())
    assert isinstance(adapter, OpenAIVoiceProvider)

    with pytest.raises(VoiceConfigurationError, match="speech-to-text"):
        await adapter.transcribe(b"audio-bytes", "audio/webm")

    assert httpx_mock.get_requests() == []


def test_build_provider_is_gone() -> None:
    """The single-port names retire at once, with no alias (ADR-0136 §1)."""
    assert not hasattr(services.voice, "build_provider")
    assert not hasattr(services.voice, "VoiceProvider")
    assert not hasattr(services.voice, "ResolvedVoice")
    assert not hasattr(services.voice, "VoiceConfig")
