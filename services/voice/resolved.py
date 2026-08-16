# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The per-request voice resolution value object (ADR-0118 §3).

Mirrors :class:`~services.ai_service_core.ResolvedLLM`: a frozen dataclass
carrying one turn's resolved endpoint, credentials and models, with a masked
``repr``/``str`` so neither key can leak into a log line or a traceback.

This module imports only the standard library — the layering rule of the
package (ADR-0038) applies here as everywhere in ``services/voice/``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedVoice:
    """One turn's voice resolution for one tenant.

    The value object that ends the process-global "configure once, serve
    everyone" posture for voice (ADR-0118 §3, applying ADR-0112 §4b). The web
    voice routes resolve one of these per turn and the Telegram voice handler
    one per message — each through
    :class:`~services.investments.credential_resolver.CredentialResolver`,
    inside that tenant's context — and hand it to
    :func:`~services.voice.factory.build_provider`. Nothing is cached and
    nothing is stashed: the object lives for exactly one call, so a tenant's
    key can never be held where another tenant's turn could reach it.

    Carries the **eight runtime fields** only — deliberately not ``enabled``,
    which is gating rather than runtime.
    :class:`~services.voice.config.VoiceConfig` cannot serve as the carrier:
    its default dataclass ``repr`` would leak both keys.

    Deliberately **inert in logs**: :func:`repr` (hence :func:`str`, hence any
    f-string, log line or traceback that renders it) masks both keys,
    mirroring :class:`~services.ai_service_core.ResolvedLLM`.

    Attributes:
        stt_provider: Speech-to-text provider key for this turn.
        stt_model: STT model ID.
        stt_api_key: The plain STT API key. Never logged, never stashed.
        stt_base_url: STT endpoint base URL (the Groq-compatible swap point).
        tts_provider: Text-to-speech provider key for this turn.
        tts_model: TTS model ID.
        tts_voice: TTS voice name — Shirley's persona voice.
        tts_api_key: The plain TTS API key. Never logged, never stashed. May
            equal :attr:`stt_api_key` for an all-OpenAI deployment.
    """

    stt_provider: str
    stt_model: str
    stt_api_key: str
    stt_base_url: str
    tts_provider: str
    tts_model: str
    tts_voice: str
    tts_api_key: str

    def __repr__(self) -> str:
        # Never leak either key. Applies to str() too.
        return (
            f"ResolvedVoice(stt_provider={self.stt_provider!r}, "
            f"stt_model={self.stt_model!r}, "
            f"stt_api_key=<{'set' if self.stt_api_key else 'unset'}; masked>, "
            f"stt_base_url={self.stt_base_url!r}, "
            f"tts_provider={self.tts_provider!r}, "
            f"tts_model={self.tts_model!r}, "
            f"tts_voice={self.tts_voice!r}, "
            f"tts_api_key=<{'set' if self.tts_api_key else 'unset'}; masked>)"
        )

    __str__ = __repr__
