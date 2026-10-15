# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ElevenLabs implementation of the call port (ADR-0137 §2, §8).

Implements :class:`~services.voice_call.port.CallProvider` against the
vendor's Tools and Agents API with plain ``httpx``, **no vendor SDK**. Every
request authenticates with the ``xi-api-key`` header.

- **Provisioning** (:meth:`ElevenLabsCallProvider.ensure_agent`): without an
  agent id the ``ask_shirley`` client tool is created, then the agent with
  that tool's id. With an id the agent is read back and the definition hash
  in its ``name`` compared with the current definition's: equal means
  nothing to do; different means the tool and the agent are PATCHed. An
  agent the vendor no longer knows (HTTP 404) is created afresh, and the
  caller persists the new id.
- **Session credential** (:meth:`ElevenLabsCallProvider.session_credentials`):
  a conversation token for a private agent (``enable_auth``), minted
  server-side for the browser's WebRTC session.

The agent body carries no ``turn`` block, so the vendor's turn defaults
apply, and no ``overrides`` block, so overrides stay at the vendor's default
(off).

The conventions of the voice adapter are mirrored, not imported: a fresh
``httpx.AsyncClient`` per call (a client is bound to the loop it was created
on), a 60-second timeout, and a non-2xx rendered as ``HTTP <status>`` plus at
most 200 body characters. The key is sent as a header only and never appears
in an error message: a body or a transport failure that quotes it has every
occurrence replaced by ``<key>``.

The module imports only the standard library, ``httpx`` and its own package.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from services.voice_call.errors import CallProviderError
from services.voice_call.port import AgentDefinition, SessionCredentials

# One call; the Agents API answers within seconds.
_TIMEOUT = httpx.Timeout(60.0)
_TOOLS_PATH = "/v1/convai/tools"
_TOOL_PATH = "/v1/convai/tools/{tool_id}"
_AGENTS_CREATE_PATH = "/v1/convai/agents/create"
_AGENT_PATH = "/v1/convai/agents/{agent_id}"
_TOKEN_PATH = "/v1/convai/conversation/token"
# How much of an error body goes into the raised message.
_BODY_EXCERPT_CHARS = 200
# What an error message shows in place of the request's key.
_KEY_PLACEHOLDER = "<key>"
# The definition hash at the end of the agent name (``… · def 1a2b3c4d``).
_NAME_HASH = re.compile(r"def ([0-9a-f]{8})$")


def _url(base_url: str, path: str) -> str:
    """Join ``base_url`` (host without ``/v1``) and an endpoint ``path``."""
    return base_url.rstrip("/") + path


def _status_detail(response: httpx.Response, *, secret: str = "") -> str:
    """Render a non-2xx response as ``HTTP <status>`` plus a body excerpt.

    A non-empty ``secret`` is replaced by ``<key>`` before the body is cut, so
    a key straddling the cut cannot leave a prefix of itself in the excerpt.
    """
    body = response.text.replace(secret, _KEY_PLACEHOLDER) if secret else response.text
    excerpt = body[:_BODY_EXCERPT_CHARS].strip()
    return f"HTTP {response.status_code}: {excerpt}" if excerpt else f"HTTP {response.status_code}"


def _hash_from_name(name: str) -> str | None:
    """Return the definition hash an agent ``name`` carries, or ``None``."""
    match = _NAME_HASH.search(name)
    return match.group(1) if match else None


def tool_body(definition: AgentDefinition) -> dict[str, Any]:
    """Build the vendor envelope for the ``ask_shirley`` client tool.

    Args:
        definition: The agent definition carrying the tool spec.

    Returns:
        The JSON body of the tool create and update requests.
    """
    tool = definition.tool
    return {
        "tool_config": {
            "type": "client",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
            "expects_response": tool["expects_response"],
            "response_timeout_secs": tool["response_timeout_secs"],
            "execution_mode": tool["execution_mode"],
            "interruption_mode": tool["interruption_mode"],
            "pre_tool_speech": tool["pre_tool_speech"],
        }
    }


def agent_body(definition: AgentDefinition, *, tool_id: str) -> dict[str, Any]:
    """Build the vendor envelope for the agent.

    Args:
        definition: The agent definition.
        tool_id: The vendor id of the ``ask_shirley`` tool.

    Returns:
        The JSON body of the agent create and update requests — a private
        agent (``enable_auth``), no ``turn`` block, no ``overrides`` block.
    """
    return {
        "name": definition.name,
        "conversation_config": {
            "agent": {
                "prompt": {
                    "prompt": definition.system_prompt,
                    "llm": definition.conversation_model,
                    "tool_ids": [tool_id],
                },
                "first_message": definition.first_message,
                "language": definition.language,
            },
            "tts": {
                "voice_id": definition.voice_id,
                "model_id": definition.tts_model_id,
            },
            "conversation": {
                "max_duration_seconds": definition.max_duration_seconds,
                "client_events": list(definition.client_events),
            },
        },
        "platform_settings": {"auth": {"enable_auth": True}},
    }


class ElevenLabsCallProvider:
    """ElevenLabs Agents adapter satisfying :class:`~services.voice_call.port.CallProvider`.

    The adapter never reads the environment; the key and the host come from
    the tenant's resolution.

    Args:
        api_key: The vendor key with the Agents write permission.
        base_url: The vendor API host, without ``/v1``.
    """

    def __init__(self, *, api_key: str, base_url: str) -> None:
        self._api_key = api_key
        self._base_url = base_url

    async def _request(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send one request; raise on a transport failure, return the response otherwise."""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                return await client.request(
                    method,
                    _url(self._base_url, path),
                    headers={"xi-api-key": self._api_key},
                    json=json,
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise CallProviderError(f"{operation} failed: {self._scrub(str(exc))}") from exc

    def _scrub(self, text: str) -> str:
        """Replace every occurrence of the key in ``text`` with ``<key>``."""
        return text.replace(self._api_key, _KEY_PLACEHOLDER) if self._api_key else text

    def _detail(self, response: httpx.Response) -> str:
        """Render a non-2xx response as :func:`_status_detail` does, the key scrubbed.

        A vendor that quotes the request's key in its error body must not put
        it into the raised message, and from there into a log line or a 502
        body; every occurrence becomes ``<key>``.
        """
        return _status_detail(response, secret=self._api_key)

    async def _call(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send one request and return its JSON object; raise on any failure."""
        response = await self._request(operation, method, path, json=json, params=params)
        if not response.is_success:
            raise CallProviderError(f"{operation} failed: {self._detail(response)}")
        return _json_object(operation, response)

    async def _create_tool(self, definition: AgentDefinition) -> str:
        body = await self._call("Tool creation", "POST", _TOOLS_PATH, json=tool_body(definition))
        return _field(body, "id", "Tool creation")

    async def _create(self, definition: AgentDefinition) -> str:
        """Create the tool, then the agent with it; return the new agent id."""
        tool_id = await self._create_tool(definition)
        body = await self._call(
            "Agent creation",
            "POST",
            _AGENTS_CREATE_PATH,
            json=agent_body(definition, tool_id=tool_id),
        )
        return _field(body, "agent_id", "Agent creation")

    async def ensure_agent(self, definition: AgentDefinition, agent_id: str | None) -> str:
        """Bring the vendor agent in line with ``definition``.

        Args:
            definition: The definition the agent must match.
            agent_id: The persisted agent id, or ``None`` before provisioning.

        Returns:
            The agent id the caller must persist: the new one after a create
            (no id, or the vendor answered 404 for it), else ``agent_id``.

        Raises:
            CallProviderError: On a transport failure, a non-2xx status other
                than the 404 of a vanished agent, or a response lacking an id.
        """
        if agent_id is None:
            return await self._create(definition)

        agent_path = _AGENT_PATH.format(agent_id=quote(agent_id, safe=""))
        response = await self._request("Agent lookup", "GET", agent_path)
        if response.status_code == httpx.codes.NOT_FOUND:
            return await self._create(definition)
        if not response.is_success:
            raise CallProviderError(f"Agent lookup failed: {self._detail(response)}")
        current = _json_object("Agent lookup", response)

        if _hash_from_name(str(current.get("name") or "")) == definition.definition_hash:
            return agent_id

        tool_ids = (
            current.get("conversation_config", {})
            .get("agent", {})
            .get("prompt", {})
            .get("tool_ids")
        ) or []
        if tool_ids:
            tool_id = str(tool_ids[0])
            await self._call(
                "Tool update",
                "PATCH",
                _TOOL_PATH.format(tool_id=quote(tool_id, safe="")),
                json=tool_body(definition),
            )
        else:
            tool_id = await self._create_tool(definition)
        await self._call(
            "Agent update", "PATCH", agent_path, json=agent_body(definition, tool_id=tool_id)
        )
        return agent_id

    async def session_credentials(self, agent_id: str) -> SessionCredentials:
        """Mint a conversation token for ``agent_id``, server-side.

        Args:
            agent_id: The provisioned agent's id.

        Returns:
            The :class:`~services.voice_call.port.SessionCredentials` for one
            browser session.

        Raises:
            CallProviderError: On a transport failure, a non-2xx status, or a
                response lacking ``token`` or ``conversation_id``.
        """
        body = await self._call("Session token", "GET", _TOKEN_PATH, params={"agent_id": agent_id})
        return SessionCredentials(
            vendor="elevenlabs",
            token=_field(body, "token", "Session token"),
            conversation_id=_field(body, "conversation_id", "Session token"),
        )


def _json_object(operation: str, response: httpx.Response) -> dict[str, Any]:
    """Return the response's JSON object, or raise naming ``operation``."""
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise CallProviderError(f"{operation} returned no JSON object.") from exc
    if not isinstance(payload, dict):
        raise CallProviderError(f"{operation} returned no JSON object.")
    return payload


def _field(body: dict[str, Any], name: str, operation: str) -> str:
    """Return the non-empty string field ``name`` of ``body``, or raise naming the field."""
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise CallProviderError(f"{operation} response has no {name!r}.")
    return value
