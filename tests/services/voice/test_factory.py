# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.voice.factory` (ADR-0076, Block 1; ADR-0118 §3).

``build_provider`` accepts both shapes — the env-backed :class:`VoiceConfig`
and the per-tenant :class:`ResolvedVoice` — so every case is pinned twice.
"""

from __future__ import annotations

import pytest

from services.voice.config import VoiceConfig
from services.voice.errors import UnsupportedVoiceProviderError
from services.voice.factory import build_provider
from services.voice.openai_provider import OpenAIVoiceProvider
from services.voice.provider import VoiceProvider
from services.voice.resolved import ResolvedVoice


def _config(*, stt_provider: str = "openai", tts_provider: str = "openai") -> VoiceConfig:
    """Build a disabled config with explicit provider keys (no env coupling)."""
    return VoiceConfig(
        enabled=False,
        stt_provider=stt_provider,
        stt_model="gpt-4o-mini-transcribe",
        stt_api_key="",
        stt_base_url="https://api.openai.com/v1",
        tts_provider=tts_provider,
        tts_model="gpt-4o-mini-tts",
        tts_voice="nova",
        tts_api_key="",
    )


def _resolved(*, stt_provider: str = "openai", tts_provider: str = "openai") -> ResolvedVoice:
    """Mirror :func:`_config` in the per-request shape (no ``enabled``)."""
    return ResolvedVoice(
        stt_provider=stt_provider,
        stt_model="gpt-4o-mini-transcribe",
        stt_api_key="sk-stt",
        stt_base_url="https://api.openai.com/v1",
        tts_provider=tts_provider,
        tts_model="gpt-4o-mini-tts",
        tts_voice="nova",
        tts_api_key="sk-tts",
    )


def test_build_provider_openai_returns_adapter() -> None:
    """Both providers ``"openai"`` yields an :class:`OpenAIVoiceProvider`."""
    provider = build_provider(_config())

    assert isinstance(provider, OpenAIVoiceProvider)
    # The Protocol is runtime-checkable, so the adapter satisfies it.
    assert isinstance(provider, VoiceProvider)


def test_build_provider_unknown_stt_provider_raises() -> None:
    """An unknown STT provider raises rather than silently falling back."""
    with pytest.raises(UnsupportedVoiceProviderError, match="elevenlabs"):
        build_provider(_config(stt_provider="elevenlabs"))


def test_build_provider_unknown_tts_provider_raises() -> None:
    """An unknown TTS provider raises rather than silently falling back."""
    with pytest.raises(UnsupportedVoiceProviderError, match="deepgram"):
        build_provider(_config(tts_provider="deepgram"))


def test_build_provider_accepts_resolved_voice() -> None:
    """A per-tenant :class:`ResolvedVoice` builds the same adapter."""
    provider = build_provider(_resolved())

    assert isinstance(provider, OpenAIVoiceProvider)
    assert isinstance(provider, VoiceProvider)


def test_build_provider_resolved_unknown_stt_provider_raises() -> None:
    """The no-silent-fallback rule holds on the resolved shape too (STT)."""
    with pytest.raises(UnsupportedVoiceProviderError, match="elevenlabs"):
        build_provider(_resolved(stt_provider="elevenlabs"))


def test_build_provider_resolved_unknown_tts_provider_raises() -> None:
    """The no-silent-fallback rule holds on the resolved shape too (TTS)."""
    with pytest.raises(UnsupportedVoiceProviderError, match="deepgram"):
        build_provider(_resolved(tts_provider="deepgram"))
