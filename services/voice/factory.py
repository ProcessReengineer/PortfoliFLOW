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

Every key of :data:`~services.voice.declarations.PROVIDER_KEYS` has a class here
and every class here has a declaration (ADR-0136 §2). ``openai_compatible`` and
the legacy ``openai`` key share the OpenAI-wire adapter class; they differ only
in their declarations; ``elevenlabs`` is the native adapter of ADR-0136 §9. Groq STT, Mistral transcription and local servers are
reached **without** a separate adapter, as ``openai_compatible`` with their
``base_url``. A further adapter is a new declaration, a new entry here and a
vocabulary entry, not a change to any caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from services.voice.elevenlabs_provider import ElevenLabsVoiceProvider
from services.voice.errors import UnsupportedVoiceProviderError
from services.voice.openai_provider import OpenAIVoiceProvider
from services.voice.provider import SpeechToText, TextToSpeech
from services.voice.resolved import ResolvedSTT, ResolvedTTS

# Any adapter class the factory may build; both take ``stt=`` / ``tts=``.
_AdapterClass = type[OpenAIVoiceProvider] | type[ElevenLabsVoiceProvider]

#: Provider key → adapter class (ADR-0136 §2).
_ADAPTER_CLASSES: Mapping[str, _AdapterClass] = MappingProxyType(
    {
        "openai_compatible": OpenAIVoiceProvider,
        # Native adapter (ADR-0136 §9).
        "elevenlabs": ElevenLabsVoiceProvider,
        # Legacy declaration: same wire, OpenAI's defaults (ADR-0136 §2).
        "openai": OpenAIVoiceProvider,
    }
)


def build_stt(resolved: ResolvedSTT) -> SpeechToText:
    """Return the speech-to-text adapter for ``resolved`` (no silent fallback).

    Args:
        resolved: This turn's speech-to-text resolution.

    Returns:
        A :class:`SpeechToText` adapter built with the STT half only.

    Raises:
        UnsupportedVoiceProviderError: If ``resolved.provider`` has no adapter.
    """
    adapter_class = _ADAPTER_CLASSES.get(resolved.provider)
    if adapter_class is None:
        supported = ", ".join(sorted(_ADAPTER_CLASSES))
        raise UnsupportedVoiceProviderError(
            f"Unsupported STT provider {resolved.provider!r}. Supported providers: {supported}."
        )
    return adapter_class(stt=resolved)


def build_tts(resolved: ResolvedTTS) -> TextToSpeech:
    """Return the text-to-speech adapter for ``resolved`` (no silent fallback).

    Args:
        resolved: This turn's text-to-speech resolution.

    Returns:
        A :class:`TextToSpeech` adapter built with the TTS half only.

    Raises:
        UnsupportedVoiceProviderError: If ``resolved.provider`` has no adapter.
    """
    adapter_class = _ADAPTER_CLASSES.get(resolved.provider)
    if adapter_class is None:
        supported = ", ".join(sorted(_ADAPTER_CLASSES))
        raise UnsupportedVoiceProviderError(
            f"Unsupported TTS provider {resolved.provider!r}. Supported providers: {supported}."
        )
    return adapter_class(tts=resolved)
