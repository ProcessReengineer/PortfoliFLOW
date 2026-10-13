# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Adapter factory for the voice service — one builder per half (ADR-0136 §1).

:func:`build_stt` maps a per-request :class:`~services.voice.resolved.ResolvedSTT`
to a :class:`~services.voice.provider.SpeechToText` adapter, :func:`build_tts` a
:class:`~services.voice.resolved.ResolvedTTS` to a
:class:`~services.voice.provider.TextToSpeech` adapter. The factory composes one
adapter per half, so a tenant may pair any STT adapter with any TTS adapter.
There is **no silent fallback**: an unknown provider key raises
:class:`UnsupportedVoiceProviderError`. Callers construct per request from their
own resolution rather than from a process-wide cache (ADR-0118 §6).

Groq STT is reached **without** a separate adapter: keep the ``openai`` key and
point the STT ``base_url`` at Groq, which is OpenAI-wire-compatible. A future
ElevenLabs/Deepgram adapter is a new branch here, not a change to any caller.
"""

from __future__ import annotations

from services.voice.errors import UnsupportedVoiceProviderError
from services.voice.openai_provider import OpenAIVoiceProvider
from services.voice.provider import SpeechToText, TextToSpeech
from services.voice.resolved import ResolvedSTT, ResolvedTTS

#: Provider keys with an adapter in Round 1.
_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"openai"})


def build_stt(resolved: ResolvedSTT) -> SpeechToText:
    """Return the speech-to-text adapter for ``resolved`` (no silent fallback).

    Args:
        resolved: This turn's speech-to-text resolution.

    Returns:
        A :class:`SpeechToText` adapter built with the STT half only.

    Raises:
        UnsupportedVoiceProviderError: If ``resolved.provider`` has no adapter.
    """
    if resolved.provider not in _SUPPORTED_PROVIDERS:
        supported = ", ".join(sorted(_SUPPORTED_PROVIDERS))
        raise UnsupportedVoiceProviderError(
            f"Unsupported STT provider {resolved.provider!r}. Supported providers: {supported}."
        )
    return OpenAIVoiceProvider(stt=resolved)


def build_tts(resolved: ResolvedTTS) -> TextToSpeech:
    """Return the text-to-speech adapter for ``resolved`` (no silent fallback).

    Args:
        resolved: This turn's text-to-speech resolution.

    Returns:
        A :class:`TextToSpeech` adapter built with the TTS half only.

    Raises:
        UnsupportedVoiceProviderError: If ``resolved.provider`` has no adapter.
    """
    if resolved.provider not in _SUPPORTED_PROVIDERS:
        supported = ", ".join(sorted(_SUPPORTED_PROVIDERS))
        raise UnsupportedVoiceProviderError(
            f"Unsupported TTS provider {resolved.provider!r}. Supported providers: {supported}."
        )
    return OpenAIVoiceProvider(tts=resolved)
