# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The pure voice assembly — one rule set for both surfaces (ADR-0136 §5).

Each surface keeps its own resolver calls (inside its tenant context, with its
engine-less degradation) and hands the raw, already-resolved values to
:func:`assemble_stt` / :func:`assemble_tts`. The functions apply the adapter
declaration (:mod:`services.voice.declarations`) to them and return one of
three outcomes (ADR-0136 §4):

- ``None`` — **inactive**: voice is off, or no provider is chosen for this
  half. A deliberate, quiet state.
- a :class:`~services.voice.resolved.ResolvedSTT` /
  :class:`~services.voice.resolved.ResolvedTTS` — **active**.
- :class:`~services.voice.errors.VoiceConfigurationError` — **misconfigured**:
  the provider key is unknown, or a required model, endpoint, voice or API
  key is empty. The message names the field and both scopes an operator can
  fix it in.

The functions read no environment, no database and no clock. Every voice
configuration message lives in this module.

This module imports only the standard library and its own package
(ADR-0136 §8).
"""

from __future__ import annotations

from dataclasses import dataclass

from services.voice.declarations import ADAPTER_DECLARATIONS, AdapterDeclaration
from services.voice.errors import VoiceConfigurationError
from services.voice.resolved import ResolvedSTT, ResolvedTTS

_ADMIN_PATH = "Admin → Providers & Credentials"


@dataclass(frozen=True)
class _Half:
    """The wording and variable names of one half."""

    title: str  # sentence-initial name of the half
    card: str  # the Admin card label (``_PROVIDER_LABELS`` in the web route)
    env_prefix: str  # ``VOICE_STT`` / ``VOICE_TTS``


_STT = _Half(title="Speech-to-text", card="Voice — speech-to-text", env_prefix="VOICE_STT")
_TTS = _Half(title="Text-to-speech", card="Voice — text-to-speech", env_prefix="VOICE_TTS")

#: Field → (what is missing, Admin card field label, env-var suffix).
_FIELD_WORDING: dict[str, tuple[str, str, str]] = {
    "model": ("the model", "Model", "MODEL"),
    "base_url": ("the endpoint", "Base URL", "BASE_URL"),
    "voice": ("the voice", "Voice", "VOICE"),
    "api_key": ("its API key", "API key", "API_KEY"),
}


def _norm(value: str | None) -> str:
    """Return ``value`` stripped, with ``None`` as the empty string."""
    return (value or "").strip()


def _declaration(half: _Half, provider: str) -> AdapterDeclaration:
    """Return ``provider``'s declaration, or raise the unknown-provider message."""
    declaration = ADAPTER_DECLARATIONS.get(provider)
    if declaration is None:
        supported = ", ".join(sorted(ADAPTER_DECLARATIONS))
        raise VoiceConfigurationError(
            f"{half.title} provider {provider!r} is not supported. Choose one of "
            f"{supported} in {_ADMIN_PATH} ({half.card} → Provider) or set "
            f"{half.env_prefix}_PROVIDER in .env."
        )
    return declaration


def _require(half: _Half, provider: str, values: dict[str, str]) -> None:
    """Raise the incomplete message for the first empty value, in order."""
    for field, value in values.items():
        if value:
            continue
        what, label, env_suffix = _FIELD_WORDING[field]
        raise VoiceConfigurationError(
            f"{half.title} is active ({provider}) but {what} is not configured. "
            f"Set {label} in {_ADMIN_PATH} (it applies on your next voice message) "
            f"or {half.env_prefix}_{env_suffix} in .env for the whole application."
        )


def assemble_stt(
    enabled: bool,
    provider: str | None,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
) -> ResolvedSTT | None:
    """Assemble the speech-to-text half from already-resolved raw values.

    Args:
        enabled: The resolved ``voice.enabled`` switch.
        provider: The resolved ``voice_stt.provider``.
        model: The resolved ``voice_stt.model``.
        base_url: The resolved ``voice_stt.base_url``.
        api_key: The resolved ``voice_stt.api_key``, or ``None`` when no
            credential resolves.

    Returns:
        ``None`` when the half is inactive (voice off or no provider chosen),
        otherwise the :class:`ResolvedSTT` with the adapter's defaults filled
        in. ``api_key`` is ``""`` for a keyless adapter without a key.

    Raises:
        VoiceConfigurationError: If the provider is unknown, or the model,
            the endpoint or — when the adapter requires one — the API key is
            empty after defaults.
    """
    provider_key = _norm(provider)
    if not enabled or not provider_key:
        return None
    declaration = _declaration(_STT, provider_key)
    resolved_model = _norm(model) or (declaration.stt_model or "")
    resolved_base_url = _norm(base_url) or (declaration.base_url or "")
    key = _norm(api_key)
    required = {"model": resolved_model, "base_url": resolved_base_url}
    if declaration.requires_api_key:
        required["api_key"] = key
    _require(_STT, provider_key, required)
    return ResolvedSTT(
        provider=provider_key,
        model=resolved_model,
        api_key=key,
        base_url=resolved_base_url,
    )


def assemble_tts(
    enabled: bool,
    provider: str | None,
    model: str | None,
    base_url: str | None,
    voice: str | None,
    api_key: str | None,
) -> ResolvedTTS | None:
    """Assemble the text-to-speech half from already-resolved raw values.

    Args:
        enabled: The resolved ``voice.enabled`` switch.
        provider: The resolved ``voice_tts.provider``.
        model: The resolved ``voice_tts.model``.
        base_url: The resolved ``voice_tts.base_url``.
        voice: The resolved ``voice_tts.voice``.
        api_key: The resolved ``voice_tts.api_key``, or ``None`` when no
            credential resolves.

    Returns:
        ``None`` when the half is inactive (voice off or no provider chosen),
        otherwise the :class:`ResolvedTTS` with the adapter's defaults filled
        in. ``api_key`` is ``""`` for a keyless adapter without a key.

    Raises:
        VoiceConfigurationError: If the provider is unknown, or the model,
            the endpoint, the voice or — when the adapter requires one — the
            API key is empty after defaults.
    """
    provider_key = _norm(provider)
    if not enabled or not provider_key:
        return None
    declaration = _declaration(_TTS, provider_key)
    resolved_model = _norm(model) or (declaration.tts_model or "")
    resolved_base_url = _norm(base_url) or (declaration.base_url or "")
    resolved_voice = _norm(voice) or (declaration.tts_voice or "")
    key = _norm(api_key)
    required = {
        "model": resolved_model,
        "base_url": resolved_base_url,
        "voice": resolved_voice,
    }
    if declaration.requires_api_key:
        required["api_key"] = key
    _require(_TTS, provider_key, required)
    return ResolvedTTS(
        provider=provider_key,
        model=resolved_model,
        voice=resolved_voice,
        api_key=key,
        base_url=resolved_base_url,
    )


__all__ = ["assemble_stt", "assemble_tts"]
