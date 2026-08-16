# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Local exception hierarchy for the channel-agnostic voice service (ADR-0076).

These are plain :class:`Exception` subclasses, mirroring the local-error
discipline of :mod:`services.scraper.capabilities` (``UnsupportedModelError``).
They are defined locally — rather than anchored on a project-wide
``PortfoliFlowError`` — to honour the "stdlib + ``openai`` only" layering rule
for ``services/voice/`` (ADR-0038): nothing under this package imports from
``core/``.

The hierarchy exists to make "no silent fallback" enforceable: a
misconfigured provider, an unsupported audio format, an empty transcript, or an
SDK failure each surfaces a distinct, catchable error rather than an empty
string, ``None``, or empty bytes.
"""

from __future__ import annotations


class VoiceError(Exception):
    """Base class for all voice-service errors."""


class VoiceConfigurationError(VoiceError):
    """Required ``VOICE_*`` configuration is missing or invalid.

    Raised only when :attr:`VoiceConfig.enabled` is true — a disabled voice
    service is a valid, quiet state and never raises.
    """


class UnsupportedVoiceProviderError(VoiceError):
    """The configured STT/TTS provider has no adapter."""


class UnsupportedAudioFormatError(VoiceError):
    """The input audio MIME type or requested output ``fmt`` is not supported."""


class VoiceTranscriptionError(VoiceError):
    """STT failed (SDK error, non-2xx, or transport failure)."""


class EmptyTranscriptError(VoiceTranscriptionError):
    """STT returned an empty/whitespace-only transcript."""


class VoiceSynthesisError(VoiceError):
    """TTS failed, or was asked to synthesise empty text."""
