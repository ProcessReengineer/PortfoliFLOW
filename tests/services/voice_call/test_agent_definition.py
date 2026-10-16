# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.voice_call.agent_definition` (ADR-0137 §3, §8, §9).

The call prompt is the Soul's first fenced block, the addendum's first fenced
block and the capability block, joined by blank lines — nothing else. The
``ask_shirley`` spec and the client events carry their literal values. The definition hash covers
every field that reaches the vendor and nothing that does not (the key, the
agent id, the base URL), and is carried in the agent's name.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from services.voice_call import (
    ASK_SHIRLEY_TOOL,
    CLIENT_EVENTS,
    DEFAULT_TTS_MODEL_ID,
    ResolvedCall,
    build_agent_definition,
    compose_call_prompt,
    first_message_for,
    load_call_prompt,
    render_call_capabilities,
)
from services.voice_call.agent_definition import (
    ADDENDUM_FILE,
    PROMPTS_DIR,
    SOUL_FILE,
    first_fenced_block,
)

_RESOLVED = ResolvedCall(
    api_key="xi-call-test",
    agent_id=None,
    model="gemini-2.5-flash",
    voice="voice_a",
    language="de",
    base_url="https://api.elevenlabs.io",
)
_PROMPT = "You are Shirley on a call."


def test_first_fenced_block_takes_the_first_fence_and_raises_on_an_unclosed_one() -> None:
    """The first fence pair wins; a fence without its closing line is an error."""
    text = "# Title\n\n```\n  FIRST  \nline two\n```\n\n```\nSECOND\n```\n"
    assert first_fenced_block(text) == "FIRST  \nline two"

    with pytest.raises(ValueError):
        first_fenced_block("# Title\n\n```\nUNCLOSED\n")
    with pytest.raises(ValueError):
        first_fenced_block("no fence at all")


def test_compose_call_prompt_is_soul_addendum_capabilities() -> None:
    """Soul block · addendum block · capability block — no grounding, no inventory."""
    soul = "# Soul\n\n## System Prompt\n\n```\nSOUL BODY\n```\n\n```\nLATER\n```\n"
    addendum = "# Addendum\n\n```\nCALL RULES\n```\n"

    composed = compose_call_prompt(soul, addendum)

    assert composed == "SOUL BODY\n\nCALL RULES\n\n" + render_call_capabilities()
    assert "## Your currently available tools" not in composed
    assert "LATER" not in composed


def test_load_call_prompt_composes_the_real_files() -> None:
    """The loader on the real ``prompts/`` equals the composition of the two files."""
    soul = (PROMPTS_DIR / SOUL_FILE).read_text(encoding="utf-8")
    addendum = (PROMPTS_DIR / ADDENDUM_FILE).read_text(encoding="utf-8")

    assert load_call_prompt() == compose_call_prompt(soul, addendum)
    assert load_call_prompt(PROMPTS_DIR) == load_call_prompt()


def test_ask_shirley_tool_carries_the_adr_values() -> None:
    """The tool contract of ADR-0137 §3, literal for literal."""
    assert ASK_SHIRLEY_TOOL["name"] == "ask_shirley"
    assert ASK_SHIRLEY_TOOL["description"] == (
        "Ask Shirley. She has the portfolio and its data, analyses, limits and charts, and "
        "she can search current news and press coverage. Use it for every question that "
        "needs a fact; relay her answer and never answer such a question yourself."
    )
    assert ASK_SHIRLEY_TOOL["expects_response"] is True
    assert ASK_SHIRLEY_TOOL["response_timeout_secs"] == 90
    assert ASK_SHIRLEY_TOOL["pre_tool_speech"] == "force"
    assert ASK_SHIRLEY_TOOL["interruption_mode"] == "allow"
    assert ASK_SHIRLEY_TOOL["execution_mode"] == "immediate"
    parameters = ASK_SHIRLEY_TOOL["parameters"]
    assert parameters["required"] == ["question"]
    assert parameters["properties"]["question"]["type"] == "string"


def test_client_events_are_exactly_the_eight_adr_events() -> None:
    """The panel's events of ADR-0137 §8, no more, no fewer."""
    assert CLIENT_EVENTS == (
        "user_transcript",
        "agent_response",
        "agent_response_correction",
        "client_tool_call",
        "interruption",
        "audio",
        "ping",
        "conversation_initiation_metadata",
    )


def test_first_message_follows_the_language() -> None:
    """German (formal address) for ``de`` and ``de-*``, English otherwise."""
    german = "Hallo, hier ist Shirley. Was möchten Sie wissen?"
    english = "Hi, this is Shirley. What would you like to know?"

    for language, expected in (
        ("de", german),
        ("DE", german),
        ("de-DE", german),
        ("en", english),
        ("", english),
    ):
        assert first_message_for(language) == expected, language


def test_build_agent_definition_from_a_literal_resolution() -> None:
    """The name carries an 8-hex hash; the session cap and speech model are the defaults."""
    definition = build_agent_definition(_RESOLVED, prompt=_PROMPT)

    assert re.fullmatch(r"PortfoliFLOW Shirley call · def [0-9a-f]{8}", definition.name)
    assert definition.name.endswith(definition.definition_hash)
    assert definition.max_duration_seconds == 1200
    assert definition.tts_model_id == DEFAULT_TTS_MODEL_ID
    assert definition.system_prompt == _PROMPT
    assert definition.voice_id == "voice_a"
    assert definition.conversation_model == "gemini-2.5-flash"
    assert definition.first_message == first_message_for("de")


def test_hash_is_stable_and_follows_every_vendor_field() -> None:
    """Two builds agree; the prompt, the voice and the model each change the hash."""
    base = build_agent_definition(_RESOLVED, prompt=_PROMPT).definition_hash

    assert build_agent_definition(_RESOLVED, prompt=_PROMPT).definition_hash == base
    changed = [
        build_agent_definition(_RESOLVED, prompt=_PROMPT + " ").definition_hash,
        build_agent_definition(
            dataclasses.replace(_RESOLVED, voice="voice_b"), prompt=_PROMPT
        ).definition_hash,
        build_agent_definition(
            dataclasses.replace(_RESOLVED, model="other-model"), prompt=_PROMPT
        ).definition_hash,
    ]
    assert all(h != base for h in changed)


def test_hash_ignores_the_key_the_agent_id_and_the_base_url() -> None:
    """Nothing that never reaches the agent definition moves the hash."""
    base = build_agent_definition(_RESOLVED, prompt=_PROMPT).definition_hash
    other = dataclasses.replace(
        _RESOLVED, api_key="xi-other", agent_id="agent_x", base_url="https://eu.invalid"
    )

    assert build_agent_definition(other, prompt=_PROMPT).definition_hash == base
