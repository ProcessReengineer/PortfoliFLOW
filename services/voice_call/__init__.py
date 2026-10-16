# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The voice call — a vendor-hosted conversation layer for Shirley (ADR-0137).

A real-time spoken conversation between the user's browser and a
vendor-hosted conversational agent that carries Shirley's persona and no
program function; every fact comes from Shirley through one client tool,
``ask_shirley`` (:data:`ASK_SHIRLEY_TOOL`).

The vendor sits behind the :class:`CallProvider` port (ADR-0137 §2), with
:class:`ElevenLabsCallProvider` as the v1 implementation. The agent
definition lives in the repository (:func:`build_agent_definition`): the
call prompt composed from Shirley's Soul, the call addendum and the
capability block of :data:`CALL_CAPABILITIES` (:func:`load_call_prompt`), the
tool, the voice, the models and the client
events, hashed into the agent's name so a changed definition is detected at
the vendor (ADR-0137 §8). The tenant-facing configuration is resolved per
session into a :class:`ResolvedCall` by the pure :func:`assemble_call`,
which returns inactive (``None``), active, or raises
:class:`CallConfigurationError` naming the field and the Admin card
(ADR-0137 §7).

The ``ask_shirley`` turn runs under :data:`CALL_TOOL_ALLOWLIST` (ADR-0137
§3.1), and its written answer is rewritten for the ear through the forced
``speak`` tool of :mod:`services.voice_call.speakable` (ADR-0137 §3). Each
turn is a job the browser polls; its record is a :class:`CallJob`, held in a
bounded :class:`CallJobStore` (ADR-0137 §3, §4).

Pure Python: this package imports only the standard library and ``httpx``.
It must not import from ``web/``, ``bot/``, ``core/``, ``modules/``,
``services.voice`` or the AI core; the web surface imports **from** it, and
the dependency arrow points one way only.
"""

from __future__ import annotations

from services.voice_call.agent_definition import (
    ASK_SHIRLEY_TOOL,
    CLIENT_EVENTS,
    build_agent_definition,
    compose_call_prompt,
    first_message_for,
    load_call_prompt,
)
from services.voice_call.allowlist import CALL_TOOL_ALLOWLIST
from services.voice_call.capabilities import (
    CALL_CAPABILITIES,
    CallCapability,
    render_call_capabilities,
)
from services.voice_call.defaults import (
    DEFAULT_BASE_URL,
    DEFAULT_CONVERSATION_MODEL,
    DEFAULT_LANGUAGE,
    DEFAULT_TTS_MODEL_ID,
    MAX_DURATION_SECONDS,
)
from services.voice_call.elevenlabs_provider import ElevenLabsCallProvider
from services.voice_call.errors import (
    CallConfigurationError,
    CallJobStateError,
    CallProviderError,
    VoiceCallError,
)
from services.voice_call.jobs import CallJob, CallJobStore
from services.voice_call.port import AgentDefinition, CallProvider, SessionCredentials
from services.voice_call.resolved import ResolvedCall, assemble_call
from services.voice_call.speakable import (
    SPEAK_TOOL,
    SPEAK_TOOL_CHOICE,
    SPEAK_TOOL_NAME,
    SPEAKABLE_FALLBACKS,
    SPEAKABLE_SYSTEM_PROMPT,
    SPEAKABLE_TARGET_WORDS,
    fallback_for,
    speakable_from_tool_calls,
    speakable_messages,
)

__all__ = [  # noqa: RUF022 — grouped by seam; a flat sort orphans the group comments
    # Port and provider
    "CallProvider",
    "SessionCredentials",
    "AgentDefinition",
    "ElevenLabsCallProvider",
    # Resolution
    "ResolvedCall",
    "assemble_call",
    # Agent definition
    "ASK_SHIRLEY_TOOL",
    "CLIENT_EVENTS",
    "build_agent_definition",
    "compose_call_prompt",
    "load_call_prompt",
    "first_message_for",
    "CALL_CAPABILITIES",
    "CallCapability",
    "render_call_capabilities",
    # The ask_shirley turn
    "CALL_TOOL_ALLOWLIST",
    "SPEAK_TOOL",
    "SPEAK_TOOL_CHOICE",
    "SPEAK_TOOL_NAME",
    "SPEAKABLE_SYSTEM_PROMPT",
    "SPEAKABLE_TARGET_WORDS",
    "SPEAKABLE_FALLBACKS",
    "speakable_messages",
    "speakable_from_tool_calls",
    "fallback_for",
    # The ask_shirley job
    "CallJob",
    "CallJobStore",
    "CallJobStateError",
    # Defaults
    "DEFAULT_CONVERSATION_MODEL",
    "DEFAULT_TTS_MODEL_ID",
    "DEFAULT_LANGUAGE",
    "DEFAULT_BASE_URL",
    "MAX_DURATION_SECONDS",
    # Errors
    "VoiceCallError",
    "CallConfigurationError",
    "CallProviderError",
]
