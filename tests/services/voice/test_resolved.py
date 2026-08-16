# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.voice.resolved` (ADR-0118 §3).

The value object carries one turn's voice resolution and must stay **inert in
logs**: both API keys are masked by ``repr`` — and therefore by ``str``, by any
f-string, and by any traceback that renders it. These tests pin that masking on
every rendering path the ADR names, plus the frozen-dataclass immutability.
"""

from __future__ import annotations

import dataclasses

import pytest

from services.voice.resolved import ResolvedVoice

_STT_KEY = "sk-stt-supersecret"
_TTS_KEY = "sk-tts-supersecret"


def _resolved(*, stt_api_key: str = _STT_KEY, tts_api_key: str = _TTS_KEY) -> ResolvedVoice:
    """Build a resolution with explicit fields (no env coupling)."""
    return ResolvedVoice(
        stt_provider="openai",
        stt_model="gpt-4o-mini-transcribe",
        stt_api_key=stt_api_key,
        stt_base_url="https://api.openai.com/v1",
        tts_provider="openai",
        tts_model="gpt-4o-mini-tts",
        tts_voice="nova",
        tts_api_key=tts_api_key,
    )


def test_carries_the_eight_runtime_fields() -> None:
    """The eight runtime fields land exactly as given."""
    resolved = _resolved()

    assert resolved.stt_provider == "openai"
    assert resolved.stt_model == "gpt-4o-mini-transcribe"
    assert resolved.stt_api_key == _STT_KEY
    assert resolved.stt_base_url == "https://api.openai.com/v1"
    assert resolved.tts_provider == "openai"
    assert resolved.tts_model == "gpt-4o-mini-tts"
    assert resolved.tts_voice == "nova"
    assert resolved.tts_api_key == _TTS_KEY


def test_is_frozen() -> None:
    """Assigning any field raises — the resolution is immutable."""
    resolved = _resolved()

    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.tts_voice = "alloy"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.stt_api_key = "sk-other"  # type: ignore[misc]


def test_repr_masks_both_keys_when_set() -> None:
    """``repr``/``str``/f-string each mask both keys and leak neither value."""
    resolved = _resolved()

    for rendered in (repr(resolved), str(resolved), f"{resolved}"):
        assert rendered.count("<set; masked>") == 2
        assert _STT_KEY not in rendered
        assert _TTS_KEY not in rendered


def test_repr_renders_the_non_secret_fields_verbatim() -> None:
    """Everything that is not a key renders normally."""
    rendered = repr(_resolved())

    assert "stt_provider='openai'" in rendered
    assert "stt_model='gpt-4o-mini-transcribe'" in rendered
    assert "stt_base_url='https://api.openai.com/v1'" in rendered
    assert "tts_provider='openai'" in rendered
    assert "tts_model='gpt-4o-mini-tts'" in rendered
    assert "tts_voice='nova'" in rendered


def test_repr_masks_both_keys_when_unset() -> None:
    """Empty keys render as ``<unset; masked>`` rather than an empty string."""
    resolved = _resolved(stt_api_key="", tts_api_key="")

    for rendered in (repr(resolved), str(resolved), f"{resolved}"):
        assert rendered.count("<unset; masked>") == 2
        assert "<set; masked>" not in rendered


def test_exception_message_leaks_neither_key() -> None:
    """The traceback / error-message path the ADR names stays inert."""
    resolved = _resolved()

    message = str(ValueError(f"boom: {resolved}"))

    assert _STT_KEY not in message
    assert _TTS_KEY not in message
    assert message.count("<set; masked>") == 2
