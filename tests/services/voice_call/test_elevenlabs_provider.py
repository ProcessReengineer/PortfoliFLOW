# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-layer tests for :class:`ElevenLabsCallProvider` (ADR-0137 §2, §8).

These mock the HTTP transport with ``pytest-httpx``, exactly as the voice
adapter's tests do, and assert the outgoing requests: their order, the
``xi-api-key`` header and the JSON envelopes of the tool and the agent. The
provisioning paths — adopt by name, create, unchanged, update, vanished
agent, agent without a tool — and the session token are each pinned, and so
is the error rendering: ``HTTP <status>`` plus at most 200 body characters,
the request's key replaced by ``<key>``.

Async tests need no decorator (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest

from services.voice_call import (
    AgentDefinition,
    CallProvider,
    CallProviderError,
    ElevenLabsCallProvider,
    ResolvedCall,
    SessionCredentials,
    build_agent_definition,
)

_BASE_URL = "https://vendor.invalid"
_KEY = "xi-call-test"
_TOOLS_URL = f"{_BASE_URL}/v1/convai/tools"
_CREATE_URL = f"{_BASE_URL}/v1/convai/agents/create"
_AGENTS_URL = f"{_BASE_URL}/v1/convai/agents"
_LIST_URL = re.compile(re.escape(_AGENTS_URL) + r"\?.*")
_NO_AGENTS = {"agents": [], "has_more": False}
_AGENT_URL = f"{_BASE_URL}/v1/convai/agents/agent_1"
_TOOL_URL = f"{_BASE_URL}/v1/convai/tools/tool_1"
_TOKEN_URL = f"{_BASE_URL}/v1/convai/conversation/token?agent_id=agent_1"


def _definition() -> AgentDefinition:
    resolved = ResolvedCall(
        api_key=_KEY,
        agent_id=None,
        model="gemini-2.5-flash",
        voice="voice_a",
        language="de",
        base_url=_BASE_URL,
    )
    return build_agent_definition(resolved, prompt="CALL PROMPT")


def _provider(api_key: str = _KEY) -> ElevenLabsCallProvider:
    return ElevenLabsCallProvider(api_key=api_key, base_url=_BASE_URL)


def _agent(name: str, tool_ids: list[str]) -> dict[str, Any]:
    """An agent as the vendor's GET returns it, trimmed to what the adapter reads."""
    return {
        "agent_id": "agent_1",
        "name": name,
        "conversation_config": {"agent": {"prompt": {"tool_ids": tool_ids}}},
    }


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def _trail(requests: list[httpx.Request]) -> list[tuple[str, str]]:
    """Method and URL without its query of each request, in order."""
    return [(r.method, str(r.url).split("?", 1)[0]) for r in requests]


def _assert_list_request(request: httpx.Request, definition: AgentDefinition) -> None:
    assert request.method == "GET"
    assert request.url.params["search"] == definition.name
    assert request.url.params["page_size"] == "100"


def _assert_tool_body(body: dict[str, Any]) -> None:
    config = body["tool_config"]
    assert config["type"] == "client"
    assert config["name"] == "ask_shirley"
    assert config["parameters"]["required"] == ["question"]
    assert config["expects_response"] is True
    assert config["response_timeout_secs"] == 90
    assert config["pre_tool_speech"] == "force"
    assert config["interruption_mode"] == "allow"
    assert config["execution_mode"] == "immediate"


def _assert_agent_body(body: dict[str, Any], definition: AgentDefinition, tool_id: str) -> None:
    assert body["name"] == definition.name
    config = body["conversation_config"]
    assert config["agent"]["prompt"] == {
        "prompt": "CALL PROMPT",
        "llm": "gemini-2.5-flash",
        "tool_ids": [tool_id],
    }
    assert config["agent"]["first_message"] == definition.first_message
    assert config["agent"]["language"] == "de"
    assert config["tts"] == {"voice_id": "voice_a", "model_id": definition.tts_model_id}
    assert config["conversation"]["max_duration_seconds"] == 1200
    assert config["conversation"]["client_events"] == list(definition.client_events)
    assert "turn" not in config
    assert body["platform_settings"] == {"auth": {"enable_auth": True}}


async def test_create_path_posts_the_tool_then_the_agent(httpx_mock) -> None:
    """No agent id and no agent of that name: POST tool, POST agent; the new id is returned."""
    httpx_mock.add_response(method="GET", url=_LIST_URL, json=_NO_AGENTS)
    httpx_mock.add_response(method="POST", url=_TOOLS_URL, json={"id": "tool_new"})
    httpx_mock.add_response(method="POST", url=_CREATE_URL, json={"agent_id": "agent_new"})
    definition = _definition()
    provider = _provider()
    assert isinstance(provider, CallProvider)

    agent_id = await provider.ensure_agent(definition, None)

    assert agent_id == "agent_new"
    requests = httpx_mock.get_requests()
    assert _trail(requests) == [
        ("GET", _AGENTS_URL),
        ("POST", _TOOLS_URL),
        ("POST", _CREATE_URL),
    ]
    assert all(r.headers["xi-api-key"] == _KEY for r in requests)
    _assert_list_request(requests[0], definition)
    _assert_tool_body(_body(requests[1]))
    _assert_agent_body(_body(requests[2]), definition, "tool_new")


async def test_existing_agent_with_the_same_hash_is_left_alone(httpx_mock) -> None:
    """The hash in the agent's name matches: one GET, no PATCH, the same id."""
    definition = _definition()
    httpx_mock.add_response(method="GET", url=_AGENT_URL, json=_agent(definition.name, ["tool_1"]))

    agent_id = await _provider().ensure_agent(definition, "agent_1")

    assert agent_id == "agent_1"
    requests = httpx_mock.get_requests()
    assert [(r.method, str(r.url)) for r in requests] == [("GET", _AGENT_URL)]
    assert requests[0].headers["xi-api-key"] == _KEY


async def test_existing_agent_with_another_hash_is_patched(httpx_mock) -> None:
    """A stale hash: GET, PATCH the agent's tool, PATCH the agent — in that order."""
    definition = _definition()
    httpx_mock.add_response(
        method="GET",
        url=_AGENT_URL,
        json=_agent("PortfoliFLOW Shirley call · def 00000000", ["tool_1"]),
    )
    httpx_mock.add_response(method="PATCH", url=_TOOL_URL, json={"id": "tool_1"})
    httpx_mock.add_response(method="PATCH", url=_AGENT_URL, json={"agent_id": "agent_1"})

    agent_id = await _provider().ensure_agent(definition, "agent_1")

    assert agent_id == "agent_1"
    requests = httpx_mock.get_requests()
    assert [(r.method, str(r.url)) for r in requests] == [
        ("GET", _AGENT_URL),
        ("PATCH", _TOOL_URL),
        ("PATCH", _AGENT_URL),
    ]
    _assert_tool_body(_body(requests[1]))
    _assert_agent_body(_body(requests[2]), definition, "tool_1")


async def test_a_vanished_agent_is_created_afresh(httpx_mock) -> None:
    """GET answers 404: the name search finds nothing, the create path runs, its id is returned."""
    httpx_mock.add_response(method="GET", url=_AGENT_URL, status_code=404)
    httpx_mock.add_response(method="GET", url=_LIST_URL, json=_NO_AGENTS)
    httpx_mock.add_response(method="POST", url=_TOOLS_URL, json={"id": "tool_new"})
    httpx_mock.add_response(method="POST", url=_CREATE_URL, json={"agent_id": "agent_new"})

    agent_id = await _provider().ensure_agent(_definition(), "agent_1")

    assert agent_id == "agent_new"
    assert _trail(httpx_mock.get_requests()) == [
        ("GET", _AGENT_URL),
        ("GET", _AGENTS_URL),
        ("POST", _TOOLS_URL),
        ("POST", _CREATE_URL),
    ]


async def test_an_orphan_of_the_same_name_is_adopted_instead_of_created(httpx_mock) -> None:
    """The name search lists an agent named for this definition: adopted, nothing created."""
    definition = _definition()
    httpx_mock.add_response(
        method="GET",
        url=_LIST_URL,
        json={
            "agents": [
                {"agent_id": "agent_other", "name": "PortfoliFLOW Shirley call · def 00000000"},
                {"name": definition.name},
                {"agent_id": "agent_orphan", "name": definition.name},
            ],
            "has_more": False,
        },
    )

    agent_id = await _provider().ensure_agent(definition, None)

    assert agent_id == "agent_orphan"
    requests = httpx_mock.get_requests()
    assert _trail(requests) == [("GET", _AGENTS_URL)]
    _assert_list_request(requests[0], definition)


async def test_a_list_hit_with_another_name_is_not_adopted(httpx_mock) -> None:
    """Only an exact name counts: a hit with another hash leaves the create path to run."""
    httpx_mock.add_response(
        method="GET",
        url=_LIST_URL,
        json={
            "agents": [
                {"agent_id": "agent_old", "name": "PortfoliFLOW Shirley call · def 00000000"}
            ],
            "has_more": False,
        },
    )
    httpx_mock.add_response(method="POST", url=_TOOLS_URL, json={"id": "tool_new"})
    httpx_mock.add_response(method="POST", url=_CREATE_URL, json={"agent_id": "agent_new"})

    agent_id = await _provider().ensure_agent(_definition(), None)

    assert agent_id == "agent_new"
    assert _trail(httpx_mock.get_requests()) == [
        ("GET", _AGENTS_URL),
        ("POST", _TOOLS_URL),
        ("POST", _CREATE_URL),
    ]


async def test_an_agent_without_a_tool_gets_a_new_one(httpx_mock) -> None:
    """A stale hash and no ``tool_ids``: POST a tool, then PATCH the agent with it."""
    definition = _definition()
    httpx_mock.add_response(method="GET", url=_AGENT_URL, json=_agent("hand-made agent", []))
    httpx_mock.add_response(method="POST", url=_TOOLS_URL, json={"id": "tool_new"})
    httpx_mock.add_response(method="PATCH", url=_AGENT_URL, json={"agent_id": "agent_1"})

    agent_id = await _provider().ensure_agent(definition, "agent_1")

    assert agent_id == "agent_1"
    requests = httpx_mock.get_requests()
    assert [(r.method, str(r.url)) for r in requests] == [
        ("GET", _AGENT_URL),
        ("POST", _TOOLS_URL),
        ("PATCH", _AGENT_URL),
    ]
    _assert_agent_body(_body(requests[2]), definition, "tool_new")


async def test_session_credentials_mint_a_conversation_token(httpx_mock) -> None:
    """GET the token with the agent id; the credential's ``repr`` hides the token."""
    httpx_mock.add_response(
        method="GET",
        url=_TOKEN_URL,
        json={"token": "tok-very-private", "conversation_id": "conv_1"},
    )

    credentials = await _provider().session_credentials("agent_1")

    assert credentials == SessionCredentials(
        vendor="elevenlabs", token="tok-very-private", conversation_id="conv_1"
    )
    request = httpx_mock.get_requests()[0]
    assert request.url.params["agent_id"] == "agent_1"
    assert request.headers["xi-api-key"] == _KEY
    assert "tok-very-private" not in repr(credentials)
    assert "tok-very-private" not in str(credentials)


async def test_a_refusal_carries_the_status_and_the_body_excerpt(httpx_mock) -> None:
    """HTTP 401: the message names the status and quotes the vendor's body."""
    body = '{"detail": {"status": "missing_permissions", "key": "sk-secret"}}'
    httpx_mock.add_response(method="GET", url=_LIST_URL, status_code=401, text=body)

    with pytest.raises(CallProviderError) as excinfo:
        await _provider().ensure_agent(_definition(), None)

    message = str(excinfo.value)
    assert "HTTP 401" in message
    assert body in message


async def test_a_body_quoting_the_key_is_scrubbed(httpx_mock) -> None:
    """HTTP 401 quoting the request's own key: ``<key>`` stands in its place, twice over."""
    body = f'{{"detail": "invalid api key {_KEY}"}}'
    httpx_mock.add_response(method="GET", url=_LIST_URL, status_code=401, text=body)
    httpx_mock.add_response(method="GET", url=_AGENT_URL, status_code=401, text=body)

    with pytest.raises(CallProviderError) as created:
        await _provider().ensure_agent(_definition(), None)
    with pytest.raises(CallProviderError) as looked_up:
        await _provider().ensure_agent(_definition(), "agent_1")

    for excinfo in (created, looked_up):
        message = str(excinfo.value)
        assert "HTTP 401" in message
        assert "<key>" in message
        assert _KEY not in message


async def test_a_long_error_body_is_cut_at_200_characters(httpx_mock) -> None:
    """HTTP 500 with a 2,000-character body: only the first 200 reach the message."""
    httpx_mock.add_response(method="GET", url=_TOKEN_URL, status_code=500, text="x" * 2000)

    with pytest.raises(CallProviderError) as excinfo:
        await _provider().session_credentials("agent_1")

    message = str(excinfo.value)
    assert "HTTP 500" in message
    assert "x" * 200 in message
    assert "x" * 201 not in message


async def test_a_transport_failure_is_a_provider_error(httpx_mock) -> None:
    """``httpx.ConnectError`` surfaces as :class:`CallProviderError`, the key absent."""
    httpx_mock.add_exception(httpx.ConnectError("boom"))

    with pytest.raises(CallProviderError) as excinfo:
        await _provider().ensure_agent(_definition(), "agent_1")

    assert "boom" in str(excinfo.value)
    assert _KEY not in str(excinfo.value)


async def test_a_token_response_without_a_conversation_id_names_the_field(httpx_mock) -> None:
    """A missing field fails naming the field, not echoing the body."""
    httpx_mock.add_response(method="GET", url=_TOKEN_URL, json={"token": "tok-very-private"})

    with pytest.raises(CallProviderError) as excinfo:
        await _provider().session_credentials("agent_1")

    assert "conversation_id" in str(excinfo.value)
    assert "tok-very-private" not in str(excinfo.value)
