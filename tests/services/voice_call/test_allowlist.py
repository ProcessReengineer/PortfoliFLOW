# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The voice-call tool allowlist and its tripwire (ADR-0137 §3.1).

The allowlist equals the whole registry (D-CS-M-17): every read, and the one
write tool, ``case_add`` — additive and announced on the call. The tripwire
pins that equality, so a new tool fails here until someone decides whether
a voice call may use it.
"""

from __future__ import annotations

import re

from services.ai_service_core import AIServiceCore
from services.tool_registry import get_tool_registry
from services.voice_call import CALL_TOOL_ALLOWLIST


def test_allowlist_equals_registered_tools() -> None:
    """Tripwire: the allowlist names exactly the registered tools."""
    AIServiceCore()
    registered = frozenset(get_tool_registry().list_tools())
    assert registered == CALL_TOOL_ALLOWLIST, (
        "The tool registry and CALL_TOOL_ALLOWLIST differ "
        f"(registered only: {sorted(registered - CALL_TOOL_ALLOWLIST)}; "
        f"allowlisted only: {sorted(CALL_TOOL_ALLOWLIST - registered)}). "
        "For a new or writing tool: decide whether a voice call may use it, then "
        "update the allowlist in services/voice_call/allowlist.py."
    )


def test_allowlist_has_twenty_well_formed_names() -> None:
    """Twenty snake_case tool names, the one write tool among them."""
    assert len(CALL_TOOL_ALLOWLIST) == 20
    assert "case_add" in CALL_TOOL_ALLOWLIST
    assert all(re.fullmatch(r"[a-z_]+", name) for name in CALL_TOOL_ALLOWLIST)
