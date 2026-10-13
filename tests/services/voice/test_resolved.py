# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.voice.resolved` (ADR-0118 §3, ADR-0136 §6).

Two value objects, one per half: :class:`ResolvedSTT` and :class:`ResolvedTTS`.
Each carries one turn's resolution of its half and must stay **inert in logs**:
the API key is masked by ``repr`` — and therefore by ``str``, by any f-string,
and by any traceback that renders it. These tests pin that masking on every
rendering path the ADR names, the frozen-dataclass immutability, and the
separateness of the two types.
"""

from __future__ import annotations

import dataclasses

import pytest

from services.voice.resolved import ResolvedSTT, ResolvedTTS

_STT_KEY = "sk-stt-supersecret"
_TTS_KEY = "sk-tts-supersecret"


def _stt(*, api_key: str = _STT_KEY) -> ResolvedSTT:
    """Build an STT resolution with explicit fields (no env coupling)."""
    return ResolvedSTT(
        provider="openai",
        model="gpt-4o-mini-transcribe",
        api_key=api_key,
        base_url="https://api.openai.com/v1",
    )


def _tts(*, api_key: str = _TTS_KEY) -> ResolvedTTS:
    """Build a TTS resolution with explicit fields (no env coupling)."""
    return ResolvedTTS(
        provider="openai",
        model="gpt-4o-mini-tts",
        voice="nova",
        api_key=api_key,
        base_url="https://tts.example/v1",
    )


def test_stt_carries_the_four_runtime_fields() -> None:
    """The four STT runtime fields land exactly as given."""
    resolved = _stt()

    assert resolved.provider == "openai"
    assert resolved.model == "gpt-4o-mini-transcribe"
    assert resolved.api_key == _STT_KEY
    assert resolved.base_url == "https://api.openai.com/v1"


def test_tts_carries_the_five_runtime_fields() -> None:
    """The five TTS runtime fields land exactly as given."""
    resolved = _tts()

    assert resolved.provider == "openai"
    assert resolved.model == "gpt-4o-mini-tts"
    assert resolved.voice == "nova"
    assert resolved.api_key == _TTS_KEY
    assert resolved.base_url == "https://tts.example/v1"


def test_stt_is_frozen() -> None:
    """Assigning any STT field raises — the resolution is immutable."""
    resolved = _stt()

    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.model = "whisper-1"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.api_key = "sk-other"  # type: ignore[misc]


def test_tts_is_frozen() -> None:
    """Assigning any TTS field raises — the resolution is immutable."""
    resolved = _tts()

    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.voice = "alloy"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.api_key = "sk-other"  # type: ignore[misc]


def test_repr_masks_the_key_when_set() -> None:
    """``repr``/``str``/f-string each mask the key and leak no value."""
    for resolved, key in ((_stt(), _STT_KEY), (_tts(), _TTS_KEY)):
        for rendered in (repr(resolved), str(resolved), f"{resolved}"):
            assert rendered.count("<set; masked>") == 1
            assert key not in rendered


def test_repr_renders_the_non_secret_fields_verbatim() -> None:
    """Everything that is not a key renders normally."""
    stt = repr(_stt())
    assert stt.startswith("ResolvedSTT(")
    assert "provider='openai'" in stt
    assert "model='gpt-4o-mini-transcribe'" in stt
    assert "base_url='https://api.openai.com/v1'" in stt

    tts = repr(_tts())
    assert tts.startswith("ResolvedTTS(")
    assert "provider='openai'" in tts
    assert "model='gpt-4o-mini-tts'" in tts
    assert "voice='nova'" in tts
    assert "base_url='https://tts.example/v1'" in tts


def test_repr_masks_the_key_when_unset() -> None:
    """An empty key renders as ``<unset; masked>`` rather than an empty string."""
    for resolved in (_stt(api_key=""), _tts(api_key="")):
        for rendered in (repr(resolved), str(resolved), f"{resolved}"):
            assert rendered.count("<unset; masked>") == 1
            assert "<set; masked>" not in rendered


def test_exception_message_leaks_neither_key() -> None:
    """The traceback / error-message path the ADR names stays inert."""
    message = str(ValueError(f"boom: {_stt()} {_tts()}"))

    assert _STT_KEY not in message
    assert _TTS_KEY not in message
    assert message.count("<set; masked>") == 2


def test_the_two_objects_do_not_compare_equal() -> None:
    """Matching fields across the halves still make two distinct types.

    The sentinel argument of ADR-0136 §6 made concrete: a half is identified
    by its type, never by a marker inside a shared object.
    """
    stt = ResolvedSTT(provider="openai", model="m", api_key="k", base_url="https://x/v1")
    tts = ResolvedTTS(provider="openai", model="m", voice="v", api_key="k", base_url="https://x/v1")

    assert type(stt) is not type(tts)
    assert stt != tts
