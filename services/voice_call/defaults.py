# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The literal defaults of the voice call (ADR-0137 §7, §8).

Each value is typed once, here; the resolution, the agent definition and the
Admin card's hints read it from this module, so a changed default is one
token in one file.

This module imports nothing.
"""

from __future__ import annotations

#: The conversation-layer model offered when ``voice_call.model`` is empty. The
#: vendor documents no default for its ``llm`` field, so the default is a
#: literal here and a change is a conscious one (ADR-0137 §7). Chosen by ear:
#: with Shirley's Soul as the prompt, this model keeps her character on a call
#: more faithfully than the vendor's own first pick did (owner, 2026-10-24).
DEFAULT_CONVERSATION_MODEL = "claude-haiku-4-5"
#: Vendor speech model for the agent — the vendor's documented default for
#: agents on 2026-10-14, set explicitly so the definition hash covers it.
DEFAULT_TTS_MODEL_ID = "eleven_v4_turbo"
#: ``voice_call.language`` when empty (ADR-0137 §7).
DEFAULT_LANGUAGE = "de"
#: ``voice_call.base_url`` when empty (ADR-0137 §7).
DEFAULT_BASE_URL = "https://api.elevenlabs.io"
#: Vendor-side session cap, seconds (ADR-0137 §8).
MAX_DURATION_SECONDS = 1200
