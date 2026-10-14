# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The call port — the seam a second vendor implements (ADR-0137 §2).

The server side of a voice call needs two operations from a vendor: bring a
hosted conversational agent in line with the definition PortfoliFLOW owns,
and mint one browser session credential for it. :class:`CallProvider` is
that seam; :class:`AgentDefinition` and :class:`SessionCredentials` are the
vendor-neutral values crossing it. Everything above the port — the session
endpoint, the ``ask_shirley`` job, the history logic, the Admin card — knows
only these names.

This module imports only the standard library.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class SessionCredentials:
    """One browser session credential, minted server-side (ADR-0137 §2).

    Deliberately **inert in logs**: :func:`repr` (hence :func:`str`) masks the
    token.

    Attributes:
        vendor: The vendor key the browser picks its adapter by
            (``"elevenlabs"``).
        token: The one-session credential the browser starts the vendor
            session with.
        conversation_id: The vendor's id for the conversation the token opens.
    """

    vendor: str
    token: str
    conversation_id: str

    def __repr__(self) -> str:
        # Never leak the token. Applies to str() too.
        return (
            f"SessionCredentials(vendor={self.vendor!r}, "
            f"token=<{'set' if self.token else 'unset'}; masked>, "
            f"conversation_id={self.conversation_id!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class AgentDefinition:
    """The vendor-neutral agent definition PortfoliFLOW owns (ADR-0137 §8).

    Built by :func:`~services.voice_call.agent_definition.build_agent_definition`;
    every field reaches the vendor and is covered by :attr:`definition_hash`.
    The key, the agent id and the base URL are not part of it.

    Attributes:
        system_prompt: The composed call prompt (Soul block · call addendum).
        first_message: The agent's greeting, in the call's language.
        language: The call language as a vendor language code.
        voice_id: The vendor voice id.
        tts_model_id: The vendor speech model.
        conversation_model: The conversation-layer model id.
        max_duration_seconds: The vendor-side session cap.
        tool: The ``ask_shirley`` client-tool spec, vendor-neutral keys.
        client_events: The vendor events the browser panel needs.
        definition_hash: The first 8 hex characters of the sha256 of the
            canonical form.
    """

    system_prompt: str
    first_message: str
    language: str
    voice_id: str
    tts_model_id: str
    conversation_model: str
    max_duration_seconds: int
    tool: Mapping[str, Any]
    client_events: tuple[str, ...]
    definition_hash: str

    @property
    def name(self) -> str:
        """The vendor agent's name, carrying the definition hash (ADR-0137 §8)."""
        return f"PortfoliFLOW Shirley call · def {self.definition_hash}"


@runtime_checkable
class CallProvider(Protocol):
    """A vendor-hosted conversation layer, seen from the server (ADR-0137 §2).

    Implementations raise — never return empty values — on failure, so the
    caller can surface a clear message instead of silently degrading.
    """

    async def ensure_agent(self, definition: AgentDefinition, agent_id: str | None) -> str:
        """Bring the vendor agent in line with ``definition``.

        Creates the agent (and its tool) from the definition when
        ``agent_id`` is ``None``; otherwise brings the existing agent up to
        date, leaving it untouched when it already carries this definition.

        Args:
            definition: The definition the agent must match.
            agent_id: The persisted agent id, or ``None`` before provisioning.

        Returns:
            The agent id the caller must persist — a new one when the agent
            was created.

        Raises:
            CallProviderError: If the vendor refuses or fails.
        """
        ...

    async def session_credentials(self, agent_id: str) -> SessionCredentials:
        """Mint one browser session credential for ``agent_id``, server-side.

        Args:
            agent_id: The provisioned agent's id.

        Returns:
            The :class:`SessionCredentials` for one session.

        Raises:
            CallProviderError: If the vendor refuses or fails.
        """
        ...
