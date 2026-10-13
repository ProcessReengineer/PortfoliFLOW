# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The per-request voice resolution value objects, one per half (ADR-0136 §6).

Mirror :class:`~services.ai_service_core.ResolvedLLM`: frozen dataclasses
carrying one turn's resolved endpoint, credential and model for one half, with
a masked ``repr``/``str`` so the key can never leak into a log line or a
traceback.

There are two objects rather than one because the two halves are independent
(ADR-0136 §1): one object for both halves would need a sentinel to express
"this half is inactive", and a sentinel inside a value object is exactly the
quiet state ADR-0136 §4 forbids.

This module imports only the standard library — the layering rule of the
package (ADR-0136 §8: the standard library, ``openai`` and ``httpx``) applies
here as everywhere in ``services/voice/``.
"""

from __future__ import annotations

from dataclasses import dataclass


def _masked(api_key: str) -> str:
    """Render ``api_key`` as its set/unset state, never its value."""
    return f"<{'set' if api_key else 'unset'}; masked>"


@dataclass(frozen=True)
class ResolvedSTT:
    """One turn's speech-to-text resolution for one tenant.

    The surfaces resolve one per turn through
    :class:`~services.investments.credential_resolver.CredentialResolver`,
    inside that tenant's context (ADR-0118 §3), and hand it to
    :func:`~services.voice.factory.build_stt`. Nothing is cached and nothing
    is stashed: the object lives for exactly one call, so a tenant's key can
    never be held where another tenant's turn could reach it.

    Deliberately **inert in logs**: :func:`repr` (hence :func:`str`, hence any
    f-string, log line or traceback that renders it) masks the key.

    Attributes:
        provider: Speech-to-text adapter key for this turn.
        model: STT model ID.
        api_key: The plain STT API key. Never logged, never stashed. May be
            the empty string for a keyless adapter (ADR-0136 §6).
        base_url: STT endpoint base URL.
    """

    provider: str
    model: str
    api_key: str
    base_url: str

    def __repr__(self) -> str:
        # Never leak the key. Applies to str() too.
        return (
            f"ResolvedSTT(provider={self.provider!r}, "
            f"model={self.model!r}, "
            f"api_key={_masked(self.api_key)}, "
            f"base_url={self.base_url!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class ResolvedTTS:
    """One turn's text-to-speech resolution for one tenant.

    The twin of :class:`ResolvedSTT`, handed to
    :func:`~services.voice.factory.build_tts`; the same one-call lifetime and
    the same masking apply.

    Attributes:
        provider: Text-to-speech adapter key for this turn.
        model: TTS model ID.
        voice: TTS voice name — Shirley's persona voice.
        api_key: The plain TTS API key. Never logged, never stashed. May be
            the empty string for a keyless adapter (ADR-0136 §6).
        base_url: TTS endpoint base URL.
    """

    provider: str
    model: str
    voice: str
    api_key: str
    base_url: str

    def __repr__(self) -> str:
        # Never leak the key. Applies to str() too.
        return (
            f"ResolvedTTS(provider={self.provider!r}, "
            f"model={self.model!r}, "
            f"voice={self.voice!r}, "
            f"api_key={_masked(self.api_key)}, "
            f"base_url={self.base_url!r})"
        )

    __str__ = __repr__
