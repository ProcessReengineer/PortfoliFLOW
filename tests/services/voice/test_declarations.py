# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.voice.declarations` (ADR-0136 §2).

The adapter declarations are the single place a voice default lives and the
owner of the provider vocabulary. A wire-compatible adapter defaults nothing
and needs no key; the legacy ``openai`` declaration carries exactly the
defaults written under ADR-0076 and OpenAI's announced shutdown dates.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from services.voice.declarations import (
    ADAPTER_DECLARATIONS,
    PROVIDER_KEYS,
    AdapterDeclaration,
)


def test_vocabulary_is_openai_compatible_and_openai_for_now() -> None:
    # The ElevenLabs adapter's prompt adds ``elevenlabs`` here and in the
    # taxonomy's vocabulary literal in one commit, together with its factory
    # entry — so the Admin card never offers a key no adapter serves.
    assert frozenset({"openai_compatible", "openai"}) == PROVIDER_KEYS


def test_openai_compatible_is_keyless_and_defaults_nothing() -> None:
    declaration = ADAPTER_DECLARATIONS["openai_compatible"]

    assert declaration.requires_api_key is False
    assert declaration.stt_model is None
    assert declaration.tts_model is None
    assert declaration.tts_voice is None
    assert declaration.base_url is None
    assert declaration.stt_sunset is None
    assert declaration.tts_sunset is None


def test_openai_legacy_declaration_carries_todays_defaults_and_sunsets() -> None:
    declaration = ADAPTER_DECLARATIONS["openai"]

    assert declaration.requires_api_key is True
    assert declaration.stt_model == "gpt-4o-mini-transcribe"
    assert declaration.tts_model == "gpt-4o-mini-tts"
    assert declaration.tts_voice == "nova"
    assert declaration.base_url == "https://api.openai.com/v1"
    assert declaration.stt_sunset == date(2027, 2, 26)
    assert declaration.tts_sunset == date(2027, 1, 6)


def test_declaration_key_matches_its_mapping_key() -> None:
    for key, declaration in ADAPTER_DECLARATIONS.items():
        assert declaration.key == key


def test_a_keyless_adapter_may_not_announce_a_sunset() -> None:
    with pytest.raises(ValueError, match="sunset"):
        AdapterDeclaration(
            key="local",
            requires_api_key=False,
            stt_model=None,
            tts_model=None,
            tts_voice=None,
            base_url=None,
            stt_sunset=date(2030, 1, 1),
        )


def test_declarations_are_frozen() -> None:
    declaration = ADAPTER_DECLARATIONS["openai"]

    with pytest.raises(dataclasses.FrozenInstanceError):
        declaration.stt_model = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        ADAPTER_DECLARATIONS["new"] = declaration  # type: ignore[index]
