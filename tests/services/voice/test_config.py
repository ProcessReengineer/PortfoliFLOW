# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.voice.config` (ADR-0076, Block 1).

The config reads the environment in ``field(default_factory=...)`` at
construction time, so each test sets ``VOICE_*`` via ``monkeypatch`` *before*
constructing :class:`VoiceConfig`. Direct construction is simply how the class
is used — since ADR-0118 §6 there is no module-level cache to work around.
"""

from __future__ import annotations

import pytest

from services.voice.config import (
    DEFAULT_STT_BASE_URL,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_VOICE,
    DEFAULT_VOICE_PROVIDER,
    VoiceConfig,
)
from services.voice.errors import VoiceConfigurationError

# Every ``VOICE_*`` env var the dataclass reads, so a test can scrub the
# ambient environment to the documented defaults before setting its own.
_VOICE_ENV_VARS = (
    "VOICE_ENABLED",
    "VOICE_STT_PROVIDER",
    "VOICE_STT_MODEL",
    "VOICE_STT_API_KEY",
    "VOICE_STT_BASE_URL",
    "VOICE_TTS_PROVIDER",
    "VOICE_TTS_MODEL",
    "VOICE_TTS_VOICE",
    "VOICE_TTS_API_KEY",
)


@pytest.fixture
def clean_voice_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all ``VOICE_*`` vars so the dataclass falls back to defaults."""
    for name in _VOICE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_disabled_config_constructs_with_empty_keys(clean_voice_env) -> None:
    """A disabled config never raises, even with both API keys empty."""
    cfg = VoiceConfig()

    assert cfg.enabled is False
    assert cfg.stt_api_key == ""
    assert cfg.tts_api_key == ""


def test_disabled_config_explicit_false(clean_voice_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """``VOICE_ENABLED=false`` is the same valid, quiet state as unset."""
    monkeypatch.setenv("VOICE_ENABLED", "false")

    cfg = VoiceConfig()

    assert cfg.enabled is False


def test_enabled_missing_stt_key_raises(clean_voice_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabled with an empty STT key fails loudly."""
    monkeypatch.setenv("VOICE_ENABLED", "true")
    monkeypatch.setenv("VOICE_TTS_API_KEY", "sk-tts")

    with pytest.raises(VoiceConfigurationError, match="VOICE_STT_API_KEY"):
        VoiceConfig()


def test_enabled_missing_tts_key_raises(clean_voice_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabled with an empty TTS key fails loudly (symmetric)."""
    monkeypatch.setenv("VOICE_ENABLED", "true")
    monkeypatch.setenv("VOICE_STT_API_KEY", "sk-stt")

    with pytest.raises(VoiceConfigurationError, match="VOICE_TTS_API_KEY"):
        VoiceConfig()


def test_enabled_both_keys_present_constructs_with_defaults(
    clean_voice_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled with both keys constructs, and field defaults match §4.3."""
    monkeypatch.setenv("VOICE_ENABLED", "true")
    monkeypatch.setenv("VOICE_STT_API_KEY", "sk-stt")
    monkeypatch.setenv("VOICE_TTS_API_KEY", "sk-tts")

    cfg = VoiceConfig()

    assert cfg.enabled is True
    assert cfg.stt_provider == "openai"
    assert cfg.stt_model == "gpt-4o-mini-transcribe"
    assert cfg.stt_base_url == "https://api.openai.com/v1"
    assert cfg.tts_provider == "openai"
    assert cfg.tts_model == "gpt-4o-mini-tts"
    assert cfg.tts_voice == "nova"
    assert cfg.stt_api_key == "sk-stt"
    assert cfg.tts_api_key == "sk-tts"


def test_defaults_are_the_code_default_constants(clean_voice_env) -> None:
    """With no env set, the fields carry the exported constants (ADR-0118 §4).

    The constants are the tails of the per-tenant config chains the surfaces
    resolve, so :class:`VoiceConfig` must read the same source rather than
    restate the values.
    """
    cfg = VoiceConfig()

    assert cfg.stt_provider == DEFAULT_VOICE_PROVIDER
    assert cfg.stt_model == DEFAULT_STT_MODEL
    assert cfg.stt_base_url == DEFAULT_STT_BASE_URL
    assert cfg.tts_provider == DEFAULT_VOICE_PROVIDER
    assert cfg.tts_model == DEFAULT_TTS_MODEL
    assert cfg.tts_voice == DEFAULT_TTS_VOICE


def test_env_override_beats_the_code_default(
    clean_voice_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An env var still wins over the constant it defaults to."""
    monkeypatch.setenv("VOICE_TTS_VOICE", "alloy")

    cfg = VoiceConfig()

    assert cfg.tts_voice == "alloy"
    assert cfg.tts_model == DEFAULT_TTS_MODEL
