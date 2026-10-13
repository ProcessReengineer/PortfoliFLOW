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

Each adapter declares in code whether it needs an API key and which defaults
it fills (:data:`ADAPTER_DECLARATIONS`, ADR-0136 §2); the keys of those
declarations are the provider vocabulary (:data:`PROVIDER_KEYS`). There are
two adapter classes: the OpenAI-wire one (also serving the legacy ``openai``
key) and the native ElevenLabs one (ADR-0136 §9). The pure
assembly — :func:`assemble_stt` / :func:`assemble_tts` — applies a declaration
to the raw values a surface resolved and returns inactive (``None``), active
(a value object) or raises :class:`VoiceConfigurationError` (ADR-0136 §5).

Telegram renders a voice note only for an Ogg/Opus container, so the reply
path checks the first bytes with :func:`is_ogg` and, when they are not Ogg,
transcodes them with the host's optional ``ffmpeg`` over pipes through
:func:`to_ogg_opus` (ADR-0136 §7). That module uses the standard library only.

Pure Python: this package imports only the standard library, ``openai`` and
``httpx`` (ADR-0136 §8). It must not import from ``web/``, ``bot/``, ``core/``,
``modules/``, or PyQt6 (ADR-0038). The web surface and the Telegram bot import
**from** this service; the dependency arrow points one way only.
"""

from __future__ import annotations

from services.voice.assembly import assemble_stt, assemble_tts, describe_voice_state
from services.voice.declarations import (
    ADAPTER_DECLARATIONS,
    PROVIDER_KEYS,
    AdapterDeclaration,
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
from services.voice.factory import build_stt, build_tts
from services.voice.provider import SpeechToText, TextToSpeech
from services.voice.resolved import ResolvedSTT, ResolvedTTS
from services.voice.transcode import OGG_MAGIC, ffmpeg_available, is_ogg, to_ogg_opus

__all__ = [  # noqa: RUF022 — grouped by seam; a flat sort orphans the group comments
    # Ports and builders
    "SpeechToText",
    "TextToSpeech",
    "build_stt",
    "build_tts",
    # Resolution
    "ResolvedSTT",
    "ResolvedTTS",
    # Declarations and assembly
    "AdapterDeclaration",
    "ADAPTER_DECLARATIONS",
    "PROVIDER_KEYS",
    "assemble_stt",
    "assemble_tts",
    "describe_voice_state",
    # Transcoding
    "OGG_MAGIC",
    "is_ogg",
    "ffmpeg_available",
    "to_ogg_opus",
    # Errors
    "VoiceError",
    "VoiceConfigurationError",
    "UnsupportedVoiceProviderError",
    "UnsupportedAudioFormatError",
    "VoiceTranscriptionError",
    "EmptyTranscriptError",
    "VoiceSynthesisError",
]
