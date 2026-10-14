# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The agent definition and the call prompt composition (ADR-0137 §3, §8, §9).

The definition lives in the repository; the vendor agent is brought in line
with it. Its system prompt is composed in code from two files under
``prompts/``: the first fenced block of ``Soul_Shirley.md`` — Shirley's own
System Prompt, which stays byte-identical — and the first fenced block of
``Soul_Shirley_call.md``, the call addendum. Nothing else: no temporal
grounding block (it changes daily and would change the definition hash every
day), no tool inventory, no context files. The agent gets dates from Shirley
through ``ask_shirley``.

The definition hash covers every field that reaches the vendor and nothing
that does not (the key, the agent id, the base URL); it is carried in the
agent's name, so a changed definition is detected by reading the agent back.

This module imports only the standard library and its own package — not the
AI core, whose fence rule is reimplemented here so the LLM client is not
dragged into the package.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from services.voice_call.defaults import DEFAULT_TTS_MODEL_ID, MAX_DURATION_SECONDS
from services.voice_call.port import AgentDefinition
from services.voice_call.resolved import ResolvedCall

#: The repository's runtime prompt directory.
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
#: Shirley's Soul; its first fenced block opens the call prompt.
SOUL_FILE = "Soul_Shirley.md"
#: The call addendum; its first fenced block closes the call prompt (ADR-0137 §9).
ADDENDUM_FILE = "Soul_Shirley_call.md"

#: The one client tool of the call agent, vendor-neutral keys (ADR-0137 §3).
ASK_SHIRLEY_TOOL: Mapping[str, Any] = MappingProxyType(
    {
        "name": "ask_shirley",
        "description": (
            "Ask Shirley, who has the portfolio data. Use it for every question about "
            "numbers, names, dates, positions, limits or charts. Relay her answer; never "
            "answer such a question yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The user's question, in their own words.",
                }
            },
            "required": ["question"],
        },
        "expects_response": True,
        "response_timeout_secs": 90,
        "pre_tool_speech": "force",
        "interruption_mode": "allow",
        "execution_mode": "immediate",
    }
)

#: The vendor events the browser panel needs (ADR-0137 §8).
CLIENT_EVENTS: tuple[str, ...] = (
    "user_transcript",
    "agent_response",
    "agent_response_correction",
    "client_tool_call",
    "interruption",
    "audio",
    "ping",
    "conversation_initiation_metadata",
)

_FIRST_MESSAGE_DE = "Hallo, hier ist Shirley. Was möchten Sie wissen?"
_FIRST_MESSAGE_EN = "Hi, this is Shirley. What would you like to know?"


def first_fenced_block(text: str) -> str:
    """Return the content between the first pair of triple-backtick fence lines, stripped.

    The rule ``AIServiceCore.get_system_prompt`` applies to a Soul file — "the
    content between the first triple-backtick fence pair" — read by line: a
    fence is a line starting with three backticks.

    Args:
        text: The Markdown file's text.

    Returns:
        The stripped content of the first fenced block.

    Raises:
        ValueError: If ``text`` holds no complete fence pair.
    """
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("```"):
            if current is None:
                current = []
            else:
                return "\n".join(current).strip()
        elif current is not None:
            current.append(line)
    raise ValueError("No complete triple-backtick fence pair found.")


def compose_call_prompt(soul_text: str, addendum_text: str) -> str:
    """Compose the call agent's system prompt: Soul block · blank line · addendum block.

    Args:
        soul_text: The text of ``Soul_Shirley.md``.
        addendum_text: The text of ``Soul_Shirley_call.md``.

    Returns:
        The composed prompt — no grounding block, no tool inventory, no
        context files.

    Raises:
        ValueError: If either text holds no complete fence pair.
    """
    return first_fenced_block(soul_text) + "\n\n" + first_fenced_block(addendum_text)


def load_call_prompt(prompts_dir: Path | None = None) -> str:
    """Read the Soul and the call addendum and compose the call prompt.

    Args:
        prompts_dir: The directory holding both files; defaults to
            :data:`PROMPTS_DIR`.

    Returns:
        The composed call prompt.

    Raises:
        OSError: If either file cannot be read.
        ValueError: If either file holds no complete fence pair.
    """
    directory = prompts_dir if prompts_dir is not None else PROMPTS_DIR
    soul = (directory / SOUL_FILE).read_text(encoding="utf-8")
    addendum = (directory / ADDENDUM_FILE).read_text(encoding="utf-8")
    return compose_call_prompt(soul, addendum)


def first_message_for(language: str) -> str:
    """Return the agent's greeting for the call ``language``.

    Args:
        language: The call language code; ``de`` or any ``de-*`` variant
            (case-insensitive) greets in German with the formal address,
            anything else in English.

    Returns:
        The first message the agent speaks.
    """
    code = language.strip().lower()
    if code == "de" or code.startswith("de-"):
        return _FIRST_MESSAGE_DE
    return _FIRST_MESSAGE_EN


def definition_hash(canonical: Mapping[str, Any]) -> str:
    """Hash the canonical definition: the first 8 hex characters of its sha256.

    Args:
        canonical: The JSON-serialisable mapping of every field that reaches
            the vendor.

    Returns:
        Eight lowercase hex characters.
    """
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:8]


def build_agent_definition(resolved: ResolvedCall, *, prompt: str | None = None) -> AgentDefinition:
    """Build the agent definition for one tenant's resolved call.

    Args:
        resolved: The tenant's call resolution; its key, agent id and base
            URL do not enter the definition.
        prompt: The system prompt; defaults to :func:`load_call_prompt`.

    Returns:
        The :class:`~services.voice_call.port.AgentDefinition`, hashed over
        every field that reaches the vendor.
    """
    system_prompt = prompt if prompt is not None else load_call_prompt()
    canonical: dict[str, Any] = {
        "system_prompt": system_prompt,
        "first_message": first_message_for(resolved.language),
        "language": resolved.language,
        "voice_id": resolved.voice,
        "tts_model_id": DEFAULT_TTS_MODEL_ID,
        "conversation_model": resolved.model,
        "max_duration_seconds": MAX_DURATION_SECONDS,
        "tool": dict(ASK_SHIRLEY_TOOL),
        "client_events": list(CLIENT_EVENTS),
    }
    return AgentDefinition(
        system_prompt=system_prompt,
        first_message=canonical["first_message"],
        language=resolved.language,
        voice_id=resolved.voice,
        tts_model_id=DEFAULT_TTS_MODEL_ID,
        conversation_model=resolved.model,
        max_duration_seconds=MAX_DURATION_SECONDS,
        tool=ASK_SHIRLEY_TOOL,
        client_events=CLIENT_EVENTS,
        definition_hash=definition_hash(canonical),
    )
