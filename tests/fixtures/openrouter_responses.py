# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Canned OpenAI / OpenRouter chat-completion SSE event sequences.

Each constant is a list of ``chat.completion.chunk``-shaped dicts in the
exact order they would arrive over the wire. Tests pass them through
:func:`tests.fixtures.sse_helpers.sse_stream_from_events` to obtain an
``httpx.SyncByteStream`` for ``pytest-httpx``.

Naming follows the characterization-table IDs (C-NN) where there is a
direct correspondence; otherwise the constant name describes the
scenario.
"""

from __future__ import annotations

from typing import Any

# A constant chunk skeleton; tests override only the ``choices`` field so
# the noise doesn't drown out what the test is actually asserting.
_BASE: dict[str, Any] = {
    "id": "chatcmpl-test",
    "object": "chat.completion.chunk",
    "created": 1700000000,
    "model": "anthropic/claude-sonnet-4.5",
}


def _chunk(delta: dict[str, Any], *, finish_reason: str | None = None) -> dict[str, Any]:
    """Build one ``chat.completion.chunk`` event dict.

    Args:
        delta: The ``choices[0].delta`` payload (e.g. ``{"content": "Hi"}``).
        finish_reason: The terminal finish reason for this chunk, or
            ``None`` for non-terminal chunks.

    Returns:
        A complete chunk dict ready for SSE serialisation.
    """
    return {
        **_BASE,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Plain-text scenarios
# ---------------------------------------------------------------------------


SIMPLE_TEXT_RESPONSE: list[dict[str, Any]] = [
    _chunk({"role": "assistant", "content": ""}),
    _chunk({"content": "Hello"}),
    _chunk({"content": " from"}),
    _chunk({"content": " Shirley."}),
    _chunk({}, finish_reason="stop"),
]
"""Five-chunk reply assembling to ``"Hello from Shirley."``."""


EMPTY_RESPONSE: list[dict[str, Any]] = [
    _chunk({"role": "assistant", "content": ""}),
    _chunk({}, finish_reason="stop"),
]
"""A response that produces no text content but finishes cleanly."""


LENGTH_LIMITED_RESPONSE: list[dict[str, Any]] = [
    _chunk({"role": "assistant", "content": ""}),
    _chunk({"content": "Truncated"}),
    _chunk({}, finish_reason="length"),
]
"""A reply whose stream ends with ``finish_reason='length'``."""


# ---------------------------------------------------------------------------
# Tool-call scenarios
# ---------------------------------------------------------------------------


SINGLE_TOOL_CALL_RESPONSE: list[dict[str, Any]] = [
    _chunk(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "list_datasets", "arguments": ""},
                }
            ],
        }
    ),
    _chunk(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "function": {"arguments": "{}"},
                }
            ],
        }
    ),
    _chunk({}, finish_reason="tool_calls"),
]
"""One ``list_datasets()`` tool call, no text."""


MULTI_TOOL_CALL_RESPONSE: list[dict[str, Any]] = [
    _chunk(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "tool_a", "arguments": ""},
                },
                {
                    "index": 1,
                    "id": "call_b",
                    "type": "function",
                    "function": {"name": "tool_b", "arguments": ""},
                },
            ],
        }
    ),
    _chunk(
        {
            "tool_calls": [
                {"index": 0, "function": {"arguments": "{}"}},
                {"index": 1, "function": {"arguments": "{}"}},
            ],
        }
    ),
    _chunk({}, finish_reason="tool_calls"),
]
"""Two parallel tool calls, ``tool_a`` (idx 0) and ``tool_b`` (idx 1)."""


TOOL_CALL_THEN_TEXT_RESPONSE: list[dict[str, Any]] = [
    _chunk({"role": "assistant", "content": ""}),
    _chunk({"content": "Final answer."}),
    _chunk({}, finish_reason="stop"),
]
"""Plain text reply that follows a prior tool-call round."""


# ---------------------------------------------------------------------------
# Helpers for non-200 paths
# ---------------------------------------------------------------------------


ERROR_403_BODY: dict[str, Any] = {
    "error": {
        "message": "Invalid API key",
        "type": "authentication_error",
        "code": "invalid_api_key",
    }
}
"""Body for a 401/403 ``AuthenticationError`` response. Status code is
set in the ``httpx_mock.add_response`` call site."""


__all__ = [
    "EMPTY_RESPONSE",
    "ERROR_403_BODY",
    "LENGTH_LIMITED_RESPONSE",
    "MULTI_TOOL_CALL_RESPONSE",
    "SIMPLE_TEXT_RESPONSE",
    "SINGLE_TOOL_CALL_RESPONSE",
    "TOOL_CALL_THEN_TEXT_RESPONSE",
]
