# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The capability block of the call prompt (ADR-0137 §9).

The call agent knows what Shirley can reach only through this block, so it
must cover every tool Shirley has: each name of the allowlist — itself pinned
to the whole registry by the allowlist tripwire — belongs to exactly one
capability, and none is unknown. A new tool fails here until its line is
written. The block names no tool, because the agent cannot call them.
"""

from __future__ import annotations

from collections import Counter

from services.voice_call import (
    CALL_CAPABILITIES,
    CALL_TOOL_ALLOWLIST,
    load_call_prompt,
    render_call_capabilities,
)
from services.voice_call.capabilities import CAPABILITIES_HEADING


def test_every_allowlisted_tool_is_covered_exactly_once() -> None:
    """Tripwire: the capabilities name each allowlisted tool once, and nothing else."""
    named = Counter(tool for capability in CALL_CAPABILITIES for tool in capability.tools)

    assert set(named) == set(CALL_TOOL_ALLOWLIST), (
        f"uncovered: {sorted(set(CALL_TOOL_ALLOWLIST) - set(named))}; "
        f"unknown: {sorted(set(named) - set(CALL_TOOL_ALLOWLIST))}. "
        "Add or fix the line in services/voice_call/capabilities.py."
    )
    assert [tool for tool, count in named.items() if count > 1] == []


def test_the_block_names_no_tool_and_one_line_per_capability() -> None:
    """Plain language only: no tool name reaches the agent; one bullet per capability."""
    block = render_call_capabilities()

    assert block.startswith(CAPABILITIES_HEADING + "\n")
    assert [name for name in CALL_TOOL_ALLOWLIST if name in block] == []
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert bullets == [f"- {capability.text}" for capability in CALL_CAPABILITIES]
    assert render_call_capabilities() == block


def test_the_press_capability_is_offered_and_the_block_closes_the_prompt() -> None:
    """News and press coverage are on the list; the block is the prompt's last part."""
    block = render_call_capabilities()

    assert "news and press coverage" in block
    assert load_call_prompt().endswith("\n\n" + block)
