# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The speakable-answer pieces of a voice call's ``ask_shirley`` turn (ADR-0137 §3)."""

from __future__ import annotations

from typing import Any

from services.voice_call import (
    SPEAK_TOOL,
    SPEAK_TOOL_CHOICE,
    SPEAKABLE_FALLBACKS,
    fallback_for,
    speakable_from_tool_calls,
    speakable_messages,
)


def test_speak_tool_shape() -> None:
    """A function tool named ``speak`` with one required string ``text``."""
    assert SPEAK_TOOL["type"] == "function"
    function = SPEAK_TOOL["function"]
    assert function["name"] == "speak"
    parameters = function["parameters"]
    assert parameters["type"] == "object"
    assert list(parameters["properties"]) == ["text"]
    assert parameters["properties"]["text"]["type"] == "string"
    assert parameters["required"] == ["text"]


def test_speak_tool_choice_forces_speak() -> None:
    """The tool choice names ``speak``, which forces the call."""
    assert dict(SPEAK_TOOL_CHOICE) == {"type": "function", "function": {"name": "speak"}}


def test_speakable_messages_with_and_without_charts() -> None:
    """One user message with the prose; the chart note only when charts accompany it."""
    prose = "Das Portfolio ist **3,5 Mrd. EUR** schwer."

    plain = speakable_messages(prose)
    assert len(plain) == 1
    assert plain[0]["role"] == "user"
    assert plain[0]["content"] == prose
    assert "chart" not in plain[0]["content"]

    one = speakable_messages(prose, chart_count=1)
    assert one[0]["content"].startswith("One chart accompanies this answer in the chat.")
    assert prose in one[0]["content"]

    two = speakable_messages(prose, chart_count=2)
    assert two[0]["content"].startswith("2 charts accompany this answer in the chat.")
    assert prose in two[0]["content"]


def test_speakable_from_tool_calls_collapses_whitespace() -> None:
    """The ``text`` of the ``speak`` call, whitespace collapsed to single spaces."""
    calls = [
        {
            "name": "speak",
            "arguments": {"text": "  Drei Komma fünf\n\nMilliarden\tEuro.  "},
            "id": "c1",
        }
    ]
    assert speakable_from_tool_calls(calls) == "Drei Komma fünf Milliarden Euro."


def test_speakable_from_tool_calls_returns_none() -> None:
    """No usable ``speak`` call yields ``None``."""
    unusable: list[list[dict[str, Any]]] = [
        [],
        [{"name": "other", "arguments": {"text": "hello"}, "id": "c1"}],
        [{"name": "speak", "arguments": {}, "id": "c1"}],
        [{"name": "speak", "arguments": {"text": "   "}, "id": "c1"}],
        [{"name": "speak", "arguments": {"text": 42}, "id": "c1"}],
    ]
    for calls in unusable:
        assert speakable_from_tool_calls(calls) is None, calls


def test_fallback_for_language() -> None:
    """German for ``de`` and its variants, English otherwise."""
    assert fallback_for("de") == SPEAKABLE_FALLBACKS["de"]
    assert fallback_for("DE-de") == SPEAKABLE_FALLBACKS["de"]
    assert fallback_for("en") == SPEAKABLE_FALLBACKS["en"]
    assert fallback_for("") == SPEAKABLE_FALLBACKS["en"]
