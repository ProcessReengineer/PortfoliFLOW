# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Channel-agnostic speech-to-text / text-to-speech service for Shirley (ADR-0076).

Turn-based STT (a pre-processor that turns recorded audio into the text that
enters the existing chat pipeline) and post-completion TTS (a post-processor
that turns streamed prose into audio), behind a :class:`VoiceProvider` Protocol
with one concrete OpenAI adapter. Since ADR-0118 the tenant-facing
configuration is resolved **per request** through the scoped-settings
apparatus, with the ``VOICE_*`` environment variables as the application-scope
links; :class:`VoiceConfig` remains the env-default value object. The
discipline is strict "no silent fallback" — a misconfigured provider, an
unsupported audio format, an empty transcript, or an SDK failure raises a clear
error rather than returning empty data.

Pure Python: this package imports only the standard library and ``openai``. It
must not import from ``web/``, ``bot/``, ``gui/``, ``core/``, ``modules/``, or
PyQt6 (ADR-0038). The web surface and the Telegram bot import **from** this
service; the dependency arrow points one way only.
"""

from __future__ import annotations

from services.voice.config import (
    DEFAULT_STT_BASE_URL,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_VOICE,
    DEFAULT_VOICE_PROVIDER,
    VoiceConfig,
)
from services.voice.errors import (
    EmptyTranscriptError,
    UnsupportedAudioFormatError,
    UnsupportedVoiceProviderError,
    VoiceConfigurationError,
    VoiceError,
    VoiceSynthesisError,
    VoiceTranscriptionError,
)
from services.voice.factory import build_provider
from services.voice.provider import VoiceProvider
from services.voice.resolved import ResolvedVoice

__all__ = [  # noqa: RUF022 — grouped by seam; a flat sort orphans the group comments
    # Provider seam
    "VoiceProvider",
    "build_provider",
    # Resolution
    "ResolvedVoice",
    # Configuration
    "VoiceConfig",
    "DEFAULT_VOICE_PROVIDER",
    "DEFAULT_STT_MODEL",
    "DEFAULT_STT_BASE_URL",
    "DEFAULT_TTS_MODEL",
    "DEFAULT_TTS_VOICE",
    # Errors
    "VoiceError",
    "VoiceConfigurationError",
    "UnsupportedVoiceProviderError",
    "UnsupportedAudioFormatError",
    "VoiceTranscriptionError",
    "EmptyTranscriptError",
    "VoiceSynthesisError",
]
