# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The environment-only reader of the two voice provider keys — one step only.

``VOICE_STT_PROVIDER`` / ``VOICE_TTS_PROVIDER`` have no taxonomy chain yet
(ADR-0118 §1); ADR-0136 §3 declares them, and the credential resolver then
serves them like every other config field, at which point this module goes.
The web surface reads no environment variable itself, so the readers live
here, in the service layer, as they always have. Standard library only.
"""

from __future__ import annotations

import os

_LEGACY_PROVIDER = "openai"


def env_stt_provider() -> str:
    """Return ``VOICE_STT_PROVIDER``, read at call time; ``"openai"`` when unset."""
    return os.getenv("VOICE_STT_PROVIDER", _LEGACY_PROVIDER)


def env_tts_provider() -> str:
    """Return ``VOICE_TTS_PROVIDER``, read at call time; ``"openai"`` when unset."""
    return os.getenv("VOICE_TTS_PROVIDER", _LEGACY_PROVIDER)
