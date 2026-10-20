# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The speakable answer of a voice call's ``ask_shirley`` turn (ADR-0137 §3).

Shirley's written answer is prose for the chat: markdown, lists, figures
written for the eye. The call agent needs something it can say. A second,
short, non-streaming call through ``AIServiceCore.run_synthesis`` rewrites
the answer with one forced tool, ``speak({text})`` — prose only, numbers
written for the ear, about sixty words, in the answer's own language.
Deterministic post-processing cannot read figures aloud well; a model can.

This module builds the pieces of that call — the tool (:data:`SPEAK_TOOL`),
the forced choice (:data:`SPEAK_TOOL_CHOICE`), the instructions
(:data:`SPEAKABLE_SYSTEM_PROMPT`) and the messages
(:func:`speakable_messages`) — and parses the tool calls the core hands back
as plain dicts (:func:`speakable_from_tool_calls`). The core call itself is
made by the job runner. Standard library only: the AI core is not imported.

The text is not clipped here: a hard cut mid-sentence sounds worse than a
long answer, so the length is left to the instructions.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final, Literal, TypedDict

#: The name of the one tool the rewrite call is forced to use.
SPEAK_TOOL_NAME = "speak"

#: The word budget the instructions ask for.
SPEAKABLE_TARGET_WORDS = 60

#: The forced tool, in OpenAI function-tool shape (ADR-0137 §3, step 3).
SPEAK_TOOL: Mapping[str, Any] = MappingProxyType(
    {
        "type": "function",
        "function": {
            "name": SPEAK_TOOL_NAME,
            "description": "Say the spoken version of the answer aloud to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The spoken version of the answer, plain prose.",
                    }
                },
                "required": ["text"],
            },
        },
    }
)


class _SpeakFunctionName(TypedDict):
    """The ``function`` member of a named tool choice: the tool's name."""

    name: str


class SpeakToolChoice(TypedDict):
    """A named tool choice in OpenAI shape, declared without the SDK.

    The call package imports only the standard library and ``httpx``, so the
    shape of ``ChatCompletionNamedToolChoiceParam`` is mirrored here and the
    constant is passed to ``AIServiceCore.run_synthesis`` as it is.
    """

    type: Literal["function"]
    function: _SpeakFunctionName


#: The ``tool_choice`` that forces :data:`SPEAK_TOOL`.
SPEAK_TOOL_CHOICE: Final[SpeakToolChoice] = {
    "type": "function",
    "function": {"name": SPEAK_TOOL_NAME},
}

#: The instructions of the rewrite call.
SPEAKABLE_SYSTEM_PROMPT = (
    "You receive an assistant's written answer to a question asked in a voice call. "
    f"Call the {SPEAK_TOOL_NAME} tool once with a spoken version of it. Use plain prose "
    "only: no lists, headings, markdown or symbols. Use at most about "
    f"{SPEAKABLE_TARGET_WORDS} words. Write numbers for the ear, in the answer's own "
    'language (in German, for example, "drei Komma fünf Milliarden Euro"). Use the same '
    "language as the answer. State no fact that is not in the answer. When the answer is "
    "long, give its substance and say that the details are in the chat. When a chart "
    "accompanies the answer, say so in one short clause."
)

#: The sentence spoken when no speakable answer could be produced, per language.
SPEAKABLE_FALLBACKS: Mapping[str, str] = MappingProxyType(
    {
        "de": "Die Antwort steht jetzt im Chat.",
        "en": "The answer is in the chat now.",
    }
)

_WHITESPACE = re.compile(r"\s+")


def speakable_messages(prose: str, *, chart_count: int = 0) -> list[dict[str, str]]:
    """Build the context messages of the rewrite call.

    Args:
        prose: Shirley's written answer, as appended to the chat history.
        chart_count: How many charts accompany the answer in the chat; when
            positive, a one-line note precedes the prose.

    Returns:
        One ``user`` message in OpenAI message shape carrying the answer.
    """
    content = prose
    if chart_count > 0:
        if chart_count == 1:
            note = "One chart accompanies this answer in the chat."
        else:
            note = f"{chart_count} charts accompany this answer in the chat."
        content = f"{note}\n\n{prose}"
    return [{"role": "user", "content": content}]


def speakable_from_tool_calls(tool_calls: Sequence[Mapping[str, Any]]) -> str | None:
    """Extract the speakable text from the rewrite call's tool calls.

    Args:
        tool_calls: The parsed tool calls the core returns, each a mapping
            ``{"name": str, "arguments": dict, "id": str}``.

    Returns:
        The ``text`` argument of the first :data:`SPEAK_TOOL_NAME` call whose
        ``text`` is a non-blank string, with whitespace collapsed to single
        spaces; ``None`` when there is no such call.
    """
    for call in tool_calls:
        if call.get("name") != SPEAK_TOOL_NAME:
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, Mapping):
            continue
        text = arguments.get("text")
        if isinstance(text, str) and text.strip():
            return _WHITESPACE.sub(" ", text).strip()
    return None


def fallback_for(language: str) -> str:
    """Return the fallback sentence for the call ``language``.

    Args:
        language: The call language code; ``de`` or any ``de-*`` variant
            (case-insensitive) selects German, anything else English.

    Returns:
        The sentence to speak when no speakable answer is available.
    """
    code = language.strip().lower()
    if code == "de" or code.startswith("de-"):
        return SPEAKABLE_FALLBACKS["de"]
    return SPEAKABLE_FALLBACKS["en"]
