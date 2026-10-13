# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Channel-agnostic speech-to-text / text-to-speech service for Shirley (ADR-0076).

Turn-based STT (a pre-processor that turns recorded audio into the text that
enters the existing chat pipeline) and post-completion TTS (a post-processor
that turns streamed prose into audio), behind two ports — :class:`SpeechToText`
and :class:`TextToSpeech` — with one builder per half (ADR-0136 §1). The
tenant-facing configuration is resolved **per request** through the
scoped-settings apparatus (ADR-0118), with the ``VOICE_*`` environment
variables as the application-scope links, into one value object per half:
:class:`ResolvedSTT` and :class:`ResolvedTTS` (ADR-0136 §6). The discipline is
strict "no silent fallback" — a misconfigured provider, an unsupported audio
format, an empty transcript, or an SDK failure raises a clear error rather than
returning empty data.

The two environment-only provider readers in :mod:`services.voice.config`
survive until the taxonomy declares the provider keys (ADR-0136 §3).

Pure Python: this package imports only the standard library, ``openai`` and
``httpx`` (ADR-0136 §8). It must not import from ``web/``, ``bot/``, ``core/``,
``modules/``, or PyQt6 (ADR-0038). The web surface and the Telegram bot import
**from** this service; the dependency arrow points one way only.
"""

from __future__ import annotations

from services.voice.config import env_stt_provider, env_tts_provider
from services.voice.errors import (
    EmptyTranscriptError,
    UnsupportedAudioFormatError,
    UnsupportedVoiceProviderError,
    VoiceConfigurationError,
    VoiceError,
    VoiceSynthesisError,
    VoiceTranscriptionError,
)
from services.voice.factory import build_stt, build_tts
from services.voice.provider import SpeechToText, TextToSpeech
from services.voice.resolved import ResolvedSTT, ResolvedTTS

__all__ = [  # noqa: RUF022 — grouped by seam; a flat sort orphans the group comments
    # Ports and builders
    "SpeechToText",
    "TextToSpeech",
    "build_stt",
    "build_tts",
    # Resolution
    "ResolvedSTT",
    "ResolvedTTS",
    # Configuration — retired by the taxonomy chain of ADR-0136 §3
    "env_stt_provider",
    "env_tts_provider",
    # Errors
    "VoiceError",
    "VoiceConfigurationError",
    "UnsupportedVoiceProviderError",
    "UnsupportedAudioFormatError",
    "VoiceTranscriptionError",
    "EmptyTranscriptError",
    "VoiceSynthesisError",
]
