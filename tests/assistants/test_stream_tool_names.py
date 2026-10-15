# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The ``tool_names`` seam of :meth:`AIServiceCore.stream_response` (ADR-0137 §3.1).

With ``tool_names`` the turn's request offers only those tools, in registry
order; an empty allowlist offers none and the request carries no ``tools``
key; an unknown name raises before the turn lock is taken, so no request
reaches the vendor. Without it the whole registry is offered — pinned by
``test_shirley_stream_offers_full_registry`` in ``test_prompt_composition.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from services.ai_models import ConnectionStatus, Conversation, Message, MessageRole
from services.ai_service_core import AIServiceCore
from services.tool_registry import ToolRegistryError, get_tool_registry
from tests.fixtures.openrouter_responses import SIMPLE_TEXT_RESPONSE
from tests.fixtures.sse_helpers import encode_sse_events

_BASE_URL = "https://openrouter.ai/api/v1"
_COMPLETIONS_URL = f"{_BASE_URL}/chat/completions"


def _connected_core() -> AIServiceCore:
    core = AIServiceCore()
    core.configure(_BASE_URL, "sk-test-key")
    core.set_status(ConnectionStatus.CONNECTED)
    core.set_model("test/model")
    return core


def _conversation() -> Conversation:
    conv = Conversation()
    conv.add_message(Message(role=MessageRole.USER, content="hi"))
    return conv


def _add_text_response(httpx_mock: Any) -> None:
    httpx_mock.add_response(
        method="POST",
        url=_COMPLETIONS_URL,
        content=encode_sse_events(SIMPLE_TEXT_RESPONSE),
        headers={"content-type": "text/event-stream"},
    )


def _request_body(httpx_mock: Any) -> dict[str, Any]:
    return json.loads(next(r for r in httpx_mock.get_requests() if r.method == "POST").content)


async def test_tool_names_narrows_offered_tools(httpx_mock: Any) -> None:
    """Only the named tools are offered, in registry order, and the turn finishes."""
    core = _connected_core()
    _add_text_response(httpx_mock)
    names = frozenset({"list_investments", "get_portfolio_overview"})

    events = [e async for e in core.stream_response(_conversation(), tool_names=names)]

    offered = [t["function"]["name"] for t in _request_body(httpx_mock)["tools"]]
    registry_order = [d["function"]["name"] for d in get_tool_registry().get_tool_definitions()]
    assert offered == [n for n in registry_order if n in names]
    assert sorted(offered) == sorted(names)
    assert any(e.event_type == "stream_finished" for e in events)


async def test_empty_tool_names_offers_no_tools(httpx_mock: Any) -> None:
    """An empty allowlist sends a request without a ``tools`` key."""
    core = _connected_core()
    _add_text_response(httpx_mock)

    events = [e async for e in core.stream_response(_conversation(), tool_names=frozenset())]

    assert "tools" not in _request_body(httpx_mock)
    assert any(e.event_type == "stream_finished" for e in events)


async def test_unknown_tool_name_raises_before_any_request(httpx_mock: Any) -> None:
    """An unregistered name raises ToolRegistryError and nothing is sent."""
    core = _connected_core()

    with pytest.raises(ToolRegistryError, match="no_such_tool"):
        async for _ in core.stream_response(
            _conversation(), tool_names=frozenset({"no_such_tool"})
        ):
            pass

    assert httpx_mock.get_requests() == []
