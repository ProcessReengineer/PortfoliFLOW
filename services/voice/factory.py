# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider factory for the voice service (ADR-0076).

Maps a :class:`VoiceConfig` or a per-request :class:`ResolvedVoice` to a
concrete :class:`VoiceProvider`. Round 1 supports only the OpenAI adapter for
both STT and TTS — there is **no silent fallback**: an unknown provider raises
:class:`UnsupportedVoiceProviderError`. Since ADR-0118 §6 :func:`build_provider`
is the whole surface of this module — callers construct per request from their
own resolved configuration rather than from a process-wide cache.

Groq STT is reached **without** a separate adapter: keep
``VOICE_STT_PROVIDER=openai`` and point ``VOICE_STT_BASE_URL`` at Groq, which is
OpenAI-wire-compatible. TTS is OpenAI-specific (no ``base_url`` swap); a future
ElevenLabs/Deepgram adapter is a new branch here, not a change to any caller.
"""

from __future__ import annotations

from services.voice.config import VoiceConfig
from services.voice.errors import UnsupportedVoiceProviderError
from services.voice.openai_provider import OpenAIVoiceProvider
from services.voice.provider import VoiceProvider
from services.voice.resolved import ResolvedVoice

#: Provider keys with an adapter in Round 1.
_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"openai"})


def build_provider(config: VoiceConfig | ResolvedVoice) -> VoiceProvider:
    """Return the adapter for the configured providers (no silent fallback).

    Round 1 supports only ``"openai"`` for both STT and TTS. (Groq STT is
    reached by keeping ``stt_provider="openai"`` and pointing
    ``stt_base_url`` at Groq — it is OpenAI-wire-compatible, so it needs no
    separate adapter.)

    Accepts either the env-backed :class:`VoiceConfig` or a per-tenant
    :class:`ResolvedVoice` (ADR-0118 §3): the adapter is attribute-structural
    over the eight runtime fields both shapes carry.

    Args:
        config: The resolved voice configuration — a :class:`VoiceConfig` or
            a per-request :class:`ResolvedVoice`.

    Returns:
        A :class:`VoiceProvider` for the configured providers.

    Raises:
        UnsupportedVoiceProviderError: If either ``stt_provider`` or
            ``tts_provider`` has no adapter.
    """
    if config.stt_provider not in _SUPPORTED_PROVIDERS:
        supported = ", ".join(sorted(_SUPPORTED_PROVIDERS))
        raise UnsupportedVoiceProviderError(
            f"Unsupported STT provider {config.stt_provider!r}. Supported providers: {supported}."
        )
    if config.tts_provider not in _SUPPORTED_PROVIDERS:
        supported = ", ".join(sorted(_SUPPORTED_PROVIDERS))
        raise UnsupportedVoiceProviderError(
            f"Unsupported TTS provider {config.tts_provider!r}. Supported providers: {supported}."
        )
    return OpenAIVoiceProvider(config)
