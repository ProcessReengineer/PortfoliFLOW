# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.voice.config`, one step from retirement.

The module keeps only the two environment-only provider readers until
ADR-0136 §3 declares the provider keys in the settings taxonomy; these cases
pin that the readers still default to ``"openai"`` and read at call time.
"""

from __future__ import annotations

import pytest

from services.voice.config import env_stt_provider, env_tts_provider

# Every ``VOICE_*`` env var, so a test can scrub the ambient environment to the
# documented defaults before setting its own.
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
    """Remove all ``VOICE_*`` vars so the readers fall back to their default."""
    for name in _VOICE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_provider_readers_default_when_unset(clean_voice_env) -> None:
    """With no provider env set, both readers return the legacy default."""
    assert env_stt_provider() == "openai"
    assert env_tts_provider() == "openai"


def test_provider_readers_read_at_call_time(
    clean_voice_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each call sees the current environment."""
    monkeypatch.setenv("VOICE_STT_PROVIDER", "stt-env")
    monkeypatch.setenv("VOICE_TTS_PROVIDER", "tts-env")

    assert env_stt_provider() == "stt-env"
    assert env_tts_provider() == "tts-env"

    monkeypatch.setenv("VOICE_STT_PROVIDER", "stt-changed")
    assert env_stt_provider() == "stt-changed"
