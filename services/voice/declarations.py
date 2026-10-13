# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Adapter declarations — what each voice adapter tells the rest (ADR-0136 §2).

Every adapter carries one declaration that the factory, the assembly
(:mod:`services.voice.assembly`) and the Admin card read: whether it needs an
API key, which defaults it fills, and when its vendor shuts the defaults
down. The declaration is the single place a default lives; there is no
provider default and no second env-reading path.

Two kinds of adapter sit side by side. A **wire-compatible** adapter
(``openai_compatible``) speaks a protocol and knows nothing about the server
behind the URL, so it defaults nothing — model, endpoint and voice are the
operator's to set, and it needs no key because local servers usually run
unauthenticated. A **vendor** adapter knows its vendor's current models and
endpoint and requires that vendor's key; ``elevenlabs`` is the native one,
speaking ElevenLabs' own API (ADR-0136 §9).

The ``openai`` key is a declaration, not a code branch: the adapter class is
the OpenAI-compatible one, the defaults are exactly OpenAI's as written under
ADR-0076, and the sunset dates are data the Admin card and the operator
handbook can show. It is kept until OpenAI's shutdown so that every ``.env``
and every tenant row written under ADR-0076/ADR-0118 keeps working unchanged;
removing it then is the deletion of one declaration and one vocabulary entry.

The keys of :data:`ADAPTER_DECLARATIONS` are the provider vocabulary. The
taxonomy mirrors them as a literal it may not import from here, and a contract
test pins the two sets equal.

This module imports only the standard library (ADR-0136 §8).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType


@dataclass(frozen=True)
class AdapterDeclaration:
    """What one adapter tells the factory, the assembly and the Admin card (ADR-0136 §2).

    Attributes:
        key: The vocabulary entry — the value of ``voice_stt.provider`` /
            ``voice_tts.provider`` that selects this adapter.
        requires_api_key: Whether an active half on this adapter needs a
            non-empty API key; each adapter declares it in code.
        stt_model: Default speech-to-text model, or ``None`` when the
            operator must set it.
        tts_model: Default text-to-speech model, or ``None``.
        tts_voice: Default text-to-speech voice, or ``None``.
        base_url: Default endpoint for both halves, or ``None``.
        stt_sunset: The vendor's announced shutdown of the default STT
            model, if any.
        tts_sunset: The vendor's announced shutdown of the default TTS
            model, if any.

    Raises:
        ValueError: If ``key`` is not a non-empty lowercase identifier, or a
            sunset date is set on an adapter that requires no key — a local
            server has no vendor to shut it down.
    """

    key: str
    requires_api_key: bool
    stt_model: str | None
    tts_model: str | None
    tts_voice: str | None
    base_url: str | None
    stt_sunset: date | None = None
    tts_sunset: date | None = None

    def __post_init__(self) -> None:
        key = self.key
        if not (key.isascii() and key.isidentifier() and key == key.lower()):
            raise ValueError(f"Adapter key {self.key!r} must be a non-empty lowercase identifier.")
        has_sunset = self.stt_sunset is not None or self.tts_sunset is not None
        if has_sunset and not self.requires_api_key:
            raise ValueError(
                f"Adapter {self.key!r} requires no API key, so it has no vendor to "
                "announce a sunset; a sunset date belongs to a vendor adapter."
            )


#: Provider key → declaration (ADR-0136 §2). Read-only.
ADAPTER_DECLARATIONS: Mapping[str, AdapterDeclaration] = MappingProxyType(
    {
        "openai_compatible": AdapterDeclaration(
            key="openai_compatible",
            requires_api_key=False,
            stt_model=None,
            tts_model=None,
            tts_voice=None,
            base_url=None,
        ),
        "elevenlabs": AdapterDeclaration(
            key="elevenlabs",
            requires_api_key=True,
            stt_model="scribe_v2",
            tts_model="eleven_multilingual_v2",
            tts_voice=None,  # the ElevenLabs voice ID — a value in code would be a guess (ADR-0136 §9)
            base_url="https://api.elevenlabs.io",
        ),
        "openai": AdapterDeclaration(
            key="openai",
            requires_api_key=True,
            stt_model="gpt-4o-mini-transcribe",
            tts_model="gpt-4o-mini-tts",
            tts_voice="nova",
            base_url="https://api.openai.com/v1",
            stt_sunset=date(2027, 2, 26),
            tts_sunset=date(2027, 1, 6),
        ),
    }
)

#: The provider vocabulary — the keys an adapter serves.
PROVIDER_KEYS: frozenset[str] = frozenset(ADAPTER_DECLARATIONS)


__all__ = ["ADAPTER_DECLARATIONS", "PROVIDER_KEYS", "AdapterDeclaration"]
