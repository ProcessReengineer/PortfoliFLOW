# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Voice-service configuration loaded from the environment / ``.env`` (ADR-0076).

Since ADR-0118 the tenant-facing fields are resolved **per request** through the
credential façade, not from a process-wide cache: :class:`VoiceConfig` remains
as the env-default value object, a ``@dataclass`` whose fields default to
``os.getenv`` lookups via ``field(default_factory=...)``. Its ``DEFAULT_*``
constants double as the code-default tails of the per-tenant config chains the
surfaces resolve, so there is exactly one source for a default, and
:meth:`__post_init__` continues to validate explicit construction.

STT and TTS are configured **independently** so providers can be mixed (e.g.
Groq-STT via a ``VOICE_STT_BASE_URL`` swap + OpenAI-TTS). When
``VOICE_ENABLED=false`` (or a required key is empty while enabled) the surfaces
hide the voice affordances and behave exactly as text-only today.

This module imports only the standard library and ``python-dotenv``; it must not
import from ``web/``, ``bot/``, ``gui/``, ``core/``, ``modules/``, or PyQt6
(ADR-0038). The local :mod:`services.voice.errors` exceptions keep
:class:`VoiceConfigurationError` catchable without a ``core/`` dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from services.voice.errors import VoiceConfigurationError

# Load .env from the repo root. ``services/voice/config.py`` is two directories
# below the root, so ``parents[2]`` is the project root — the same .env both
# ``bot/config.py`` and ``core/config.py`` read.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_ENV_PATH)

#: Code defaults for the voice configuration (ADR-0118 §4). These are the
#: final links of the per-tenant config chains the surfaces resolve in
#: V3/V4 — importable so no resolution site ever instantiates
#: ``VoiceConfig()`` to learn a default (its default factories read the
#: environment, which would double the env precedence, and its
#: ``__post_init__`` validates against the *environment's* keys, not the
#: tenant's). ``VoiceConfig`` reads the same constants, so there is exactly
#: one source.
DEFAULT_VOICE_PROVIDER = "openai"
DEFAULT_STT_MODEL = "gpt-4o-mini-transcribe"
DEFAULT_STT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_TTS_VOICE = "nova"


@dataclass
class VoiceConfig:
    """Configuration for the optional voice service.

    All fields default to environment variables. Validation happens in
    :meth:`__post_init__` and is gated on :attr:`enabled` — a disabled voice
    service accepts any combination of empty fields, while an enabled one
    demands the API keys so an accidentally-misconfigured deployment fails
    loudly rather than silently. Models and the voice always carry defaults, so
    they need no validation.

    Attributes:
        enabled: Master switch. ``True`` only when ``VOICE_ENABLED`` equals
            ``"true"`` (case-insensitive).
        stt_provider: Speech-to-text provider key. Round 1 supports
            ``"openai"`` (which also covers OpenAI-wire-compatible STT such as
            Groq via :attr:`stt_base_url`).
        stt_model: STT model ID (default ``"gpt-4o-mini-transcribe"``).
        stt_api_key: STT API key. Required when enabled.
        stt_base_url: STT endpoint base URL. Point this at Groq for an
            OpenAI-compatible STT swap without a new adapter.
        tts_provider: Text-to-speech provider key. Round 1 supports
            ``"openai"`` (TTS is OpenAI-specific; no ``base_url`` knob).
        tts_model: TTS model ID (default ``"gpt-4o-mini-tts"``).
        tts_voice: TTS voice name — Shirley's persona voice (default
            ``"nova"``).
        tts_api_key: TTS API key. Required when enabled. May equal
            :attr:`stt_api_key` for an all-OpenAI deployment.
    """

    enabled: bool = field(
        default_factory=lambda: os.getenv("VOICE_ENABLED", "false").lower() == "true"
    )
    stt_provider: str = field(
        default_factory=lambda: os.getenv("VOICE_STT_PROVIDER", DEFAULT_VOICE_PROVIDER)
    )
    stt_model: str = field(default_factory=lambda: os.getenv("VOICE_STT_MODEL", DEFAULT_STT_MODEL))
    stt_api_key: str = field(default_factory=lambda: os.getenv("VOICE_STT_API_KEY", ""))
    stt_base_url: str = field(
        default_factory=lambda: os.getenv("VOICE_STT_BASE_URL", DEFAULT_STT_BASE_URL)
    )
    tts_provider: str = field(
        default_factory=lambda: os.getenv("VOICE_TTS_PROVIDER", DEFAULT_VOICE_PROVIDER)
    )
    tts_model: str = field(default_factory=lambda: os.getenv("VOICE_TTS_MODEL", DEFAULT_TTS_MODEL))
    tts_voice: str = field(default_factory=lambda: os.getenv("VOICE_TTS_VOICE", DEFAULT_TTS_VOICE))
    tts_api_key: str = field(default_factory=lambda: os.getenv("VOICE_TTS_API_KEY", ""))

    def __post_init__(self) -> None:
        """Validate the configuration, gated on :attr:`enabled`.

        A disabled voice service is a valid, quiet state and never raises.
        When enabled, both API keys are required so a misconfigured deployment
        fails loudly at startup.

        Raises:
            VoiceConfigurationError: If the service is enabled with either
                required API key empty.
        """
        if not self.enabled:
            return

        if not self.stt_api_key:
            raise VoiceConfigurationError("VOICE_ENABLED=true but VOICE_STT_API_KEY is empty")
        if not self.tts_api_key:
            raise VoiceConfigurationError("VOICE_ENABLED=true but VOICE_TTS_API_KEY is empty")
