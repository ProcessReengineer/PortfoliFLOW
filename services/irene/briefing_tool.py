# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The ``write_briefing`` function-tool definition for Irene's briefing.

After the findings of a completed beat are persisted, the beat calls
:meth:`services.ai_service_core.AIServiceCore.run_synthesis` a second time
and offers the model exactly one tool, ``write_briefing`` (ADR-0133 §1.3).
Its two parameters *are* the briefing: ``text_markets`` is Part 1,
"Markets and press", and ``text_watch`` is Part 2, "Under observation".

Unlike the finding call, whose ``tool_choice="auto"`` lets silence fall out
natively, the briefing call **forces** its tool
(:data:`WRITE_BRIEFING_TOOL_CHOICE`): every check that has something to
brief gets a briefing. The output is accepted only as one structured call —
free text beside or instead of it is never read as a briefing —
and :func:`parse_write_briefing` is the one validator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# The stable tool name; the parser and the briefing step key off it.
WRITE_BRIEFING_TOOL_NAME = "write_briefing"

WRITE_BRIEFING_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": WRITE_BRIEFING_TOOL_NAME,
        "description": (
            "Write this check's briefing for the top of the Watch Desk. Call this "
            "exactly once, with both texts. Interpret only the figures you were "
            "given, in their given unit and window; never originate a number."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text_markets": {
                    "type": "string",
                    "description": (
                        'Part 1, "Markets and press": about 60 to 120 words reading '
                        "the market and the press against the book."
                    ),
                },
                "text_watch": {
                    "type": "string",
                    "description": (
                        'Part 2, "Under observation": about 60 to 120 words on the '
                        "switched-on watch points and what this check raised. An "
                        "empty string when no watch point is switched on."
                    ),
                },
            },
            "required": ["text_markets", "text_watch"],
            "additionalProperties": False,
        },
    },
}

#: The named-tool choice that forces the call (``run_synthesis(tool_choice=…)``).
WRITE_BRIEFING_TOOL_CHOICE: dict[str, Any] = {
    "type": "function",
    "function": {"name": WRITE_BRIEFING_TOOL_NAME},
}


def parse_write_briefing(tool_calls: Sequence[Mapping[str, Any]]) -> tuple[str, str] | None:
    """Validate the briefing call's output and return its two texts.

    The output is valid only as exactly one ``write_briefing`` call whose
    arguments carry both texts as strings, with a non-blank Part 1. Extra
    argument keys are ignored. An empty Part 2 is valid: the prompt asks
    for one when every watch point is switched off.

    Args:
        tool_calls: The parsed tool calls of one ``run_synthesis`` result,
            each ``{"name": str, "arguments": dict, "id": str}``.

    Returns:
        ``(text_markets, text_watch)``, each stripped, or ``None`` when the
        output is not one valid call.
    """
    if len(tool_calls) != 1:
        return None
    call = tool_calls[0]
    if call.get("name") != WRITE_BRIEFING_TOOL_NAME:
        return None
    arguments = call.get("arguments")
    if not isinstance(arguments, Mapping):
        return None
    text_markets = arguments.get("text_markets")
    text_watch = arguments.get("text_watch")
    if not isinstance(text_markets, str) or not isinstance(text_watch, str):
        return None
    if not text_markets.strip():
        return None
    return text_markets.strip(), text_watch.strip()


__all__ = [
    "WRITE_BRIEFING_TOOL",
    "WRITE_BRIEFING_TOOL_CHOICE",
    "WRITE_BRIEFING_TOOL_NAME",
    "parse_write_briefing",
]
