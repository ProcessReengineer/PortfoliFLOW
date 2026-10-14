# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The per-session call resolution and its pure assembly (ADR-0137 §7).

The call twin of :mod:`services.voice.resolved` and
:mod:`services.voice.assembly`: the web surface reads the raw ``voice_call``
values (and the persona voice ``voice_tts.voice``) inside the tenant's
context and hands them to :func:`assemble_call`, which returns one of three
outcomes:

- ``None`` — **inactive**: the call is switched off. A deliberate, quiet state.
- a :class:`ResolvedCall` — **active**, every default filled.
- :class:`~services.voice_call.errors.CallConfigurationError` —
  **misconfigured**: the key or the voice is missing. The message names the
  field and the Admin card. The declaration has no environment link
  (ADR-0137 §7), so unlike the voice halves no ``.env`` variable is named.

The function reads no environment, no database and no clock. This module
imports only the standard library and its own package.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.voice_call.defaults import (
    DEFAULT_BASE_URL,
    DEFAULT_CONVERSATION_MODEL,
    DEFAULT_LANGUAGE,
)
from services.voice_call.errors import CallConfigurationError

_CARD = "Admin → Providers & Credentials → Voice call"
_TTS_CARD = "Voice — text-to-speech"


def _masked(api_key: str) -> str:
    """Render ``api_key`` as its set/unset state, never its value."""
    return f"<{'set' if api_key else 'unset'}; masked>"


@dataclass(frozen=True)
class ResolvedCall:
    """One session's voice-call resolution for one tenant.

    The web surface resolves one per call start through the credential
    resolver, inside that tenant's context. Nothing is cached and nothing is
    stashed: the object lives for exactly one request, so a tenant's key can
    never be held where another tenant's request could reach it.

    Deliberately **inert in logs**: :func:`repr` (hence :func:`str`, hence any
    f-string, log line or traceback that renders it) masks the key.

    Attributes:
        api_key: The plain vendor key with the Agents write permission. Never
            logged, never stashed.
        agent_id: The vendor agent's id, or ``None`` until PortfoliFLOW has
            provisioned one.
        model: The conversation-layer model id.
        voice: The vendor voice id the agent speaks with.
        language: The call language as a vendor language code.
        base_url: The vendor API host, without a trailing slash.
    """

    api_key: str
    agent_id: str | None
    model: str
    voice: str
    language: str
    base_url: str

    def __repr__(self) -> str:
        # Never leak the key. Applies to str() too.
        return (
            f"ResolvedCall(api_key={_masked(self.api_key)}, "
            f"agent_id={self.agent_id!r}, "
            f"model={self.model!r}, "
            f"voice={self.voice!r}, "
            f"language={self.language!r}, "
            f"base_url={self.base_url!r})"
        )

    __str__ = __repr__


def _clean(value: str | None) -> str:
    """Normalise one raw value: ``None`` → ``""``, surrounding whitespace stripped."""
    return (value or "").strip()


def assemble_call(
    enabled: bool,
    api_key: str | None,
    agent_id: str | None,
    model: str | None,
    voice: str | None,
    language: str | None,
    base_url: str | None,
    tts_voice: str | None,
) -> ResolvedCall | None:
    """Assemble the call resolution from raw, already-resolved values.

    Args:
        enabled: Whether ``voice_call.enabled`` is on for the tenant.
        api_key: ``voice_call.api_key``, or ``None`` when no row holds one.
        agent_id: ``voice_call.agent_id``; empty means not yet provisioned.
        model: ``voice_call.model``; empty means
            :data:`~services.voice_call.defaults.DEFAULT_CONVERSATION_MODEL`.
        voice: ``voice_call.voice``; empty falls back to ``tts_voice``.
        language: ``voice_call.language``; empty means
            :data:`~services.voice_call.defaults.DEFAULT_LANGUAGE`.
        base_url: ``voice_call.base_url``; empty means
            :data:`~services.voice_call.defaults.DEFAULT_BASE_URL`. A
            trailing ``/`` is stripped.
        tts_voice: ``voice_tts.voice`` — the persona voice, read as the
            call's default voice.

    Returns:
        ``None`` when the call is off, else the :class:`ResolvedCall`.

    Raises:
        CallConfigurationError: If the call is on but no API key or no voice
            resolves.
    """
    if not enabled:
        return None
    key = _clean(api_key)
    if not key:
        raise CallConfigurationError(
            f"Voice call is enabled but no API key is stored — add one under {_CARD}."
        )
    resolved_voice = _clean(voice) or _clean(tts_voice)
    if not resolved_voice:
        raise CallConfigurationError(
            f"Voice call is enabled but no voice is set — set Voice under {_CARD}, "
            f"or the text-to-speech voice under {_TTS_CARD}."
        )
    return ResolvedCall(
        api_key=key,
        agent_id=_clean(agent_id) or None,
        model=_clean(model) or DEFAULT_CONVERSATION_MODEL,
        voice=resolved_voice,
        language=_clean(language) or DEFAULT_LANGUAGE,
        base_url=(_clean(base_url) or DEFAULT_BASE_URL).rstrip("/"),
    )
