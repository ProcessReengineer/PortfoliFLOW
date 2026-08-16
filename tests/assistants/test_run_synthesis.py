# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-layer tests for ``AIServiceCore.run_synthesis`` (ADR-0086).

Like the extraction tests, these mock the HTTP layer (httpx via
pytest-httpx), not the method itself — the only way to assert the shape
of the outgoing request body, which is where the Irene synthesis contract
lives: a **non-streaming** call with ``tool_choice="auto"`` offering the
single ``surface_finding`` tool.

They also guard the concurrency invariant: ``run_synthesis`` must not
touch the process-wide ``_TURN_LOCK`` that serialises Shirley's streaming
turns — the beat runs in a separate process, so the lock is irrelevant by
construction and Shirley's path stays unaffected.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from typing import Any

import pytest

from services.ai_models import ConnectionStatus
from services.ai_service_core import AIServiceCore, SynthesisResult
from services.irene.synthesis_tool import SURFACE_FINDING_TOOL_V0

_BASE_URL = "https://openrouter.ai/api/v1"
_API_KEY = "sk-test-key"
_MODEL = "irene-test-model"
_COMPLETIONS_URL = f"{_BASE_URL}/chat/completions"


def _completion(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal non-streaming chat-completion response body."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": _MODEL,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.fixture
def configured_core() -> AIServiceCore:
    """A fresh :class:`AIServiceCore` already in CONNECTED state."""
    core = AIServiceCore()
    core.configure(_BASE_URL, _API_KEY)
    core.set_status(ConnectionStatus.CONNECTED)
    core.set_model(_MODEL)
    return core


@pytest.mark.asyncio
async def test_run_synthesis_sends_tool_choice_auto_non_streaming(
    configured_core: AIServiceCore, httpx_mock: Any
) -> None:
    """The request is non-streaming, tool_choice=auto, single tool offered."""
    httpx_mock.add_response(
        method="POST",
        url=_COMPLETIONS_URL,
        json=_completion(content="Calm book."),
    )

    result = await configured_core.run_synthesis(
        system_prompt="You are Irene.",
        context_messages=[{"role": "user", "content": "beat"}],
        tool=SURFACE_FINDING_TOOL_V0,
        model=_MODEL,
    )

    assert isinstance(result, SynthesisResult)

    req = next(r for r in httpx_mock.get_requests() if r.method == "POST")
    body = json.loads(req.content)

    assert body["tool_choice"] == "auto"
    # Streaming must be off (absent is preferred; explicit False also passes).
    assert body.get("stream", False) is False
    assert len(body["tools"]) == 1
    assert body["tools"][0]["function"]["name"] == "surface_finding"
    assert body["model"] == _MODEL
    # System prompt is prepended, then the context turns, in order.
    assert body["messages"][0] == {"role": "system", "content": "You are Irene."}
    assert body["messages"][1] == {"role": "user", "content": "beat"}


@pytest.mark.asyncio
async def test_run_synthesis_zero_tool_calls_is_silence(
    configured_core: AIServiceCore, httpx_mock: Any
) -> None:
    """No tool calls in the response ⇒ empty tool_calls (silence)."""
    httpx_mock.add_response(
        method="POST",
        url=_COMPLETIONS_URL,
        json=_completion(content="Nothing material this beat."),
    )

    result = await configured_core.run_synthesis(
        system_prompt="You are Irene.",
        context_messages=[{"role": "user", "content": "beat"}],
        tool=SURFACE_FINDING_TOOL_V0,
        model=_MODEL,
    )

    assert result.tool_calls == []
    assert result.raw_text == "Nothing material this beat."


@pytest.mark.asyncio
async def test_run_synthesis_parses_tool_calls_with_decoded_arguments(
    configured_core: AIServiceCore, httpx_mock: Any
) -> None:
    """A surface_finding call is returned with JSON-decoded arguments."""
    tool_calls = [
        {
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "surface_finding",
                "arguments": json.dumps({"subject_key": "limit:pe", "urgency_suggestion": 2}),
            },
        }
    ]
    httpx_mock.add_response(
        method="POST",
        url=_COMPLETIONS_URL,
        json=_completion(content=None, tool_calls=tool_calls),
    )

    result = await configured_core.run_synthesis(
        system_prompt="You are Irene.",
        context_messages=[{"role": "user", "content": "beat"}],
        tool=SURFACE_FINDING_TOOL_V0,
        model=_MODEL,
    )

    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call["name"] == "surface_finding"
    assert call["arguments"] == {"subject_key": "limit:pe", "urgency_suggestion": 2}
    assert call["id"] == "call_1"
    # Null content becomes an empty raw_text.
    assert result.raw_text == ""


@pytest.mark.asyncio
async def test_run_synthesis_raises_when_not_connected(httpx_mock: Any) -> None:
    """An unconfigured core raises RuntimeError, matching the other methods."""
    core = AIServiceCore()  # not configured
    with pytest.raises(RuntimeError, match="connected"):
        await core.run_synthesis(
            system_prompt="You are Irene.",
            context_messages=[{"role": "user", "content": "beat"}],
            tool=SURFACE_FINDING_TOOL_V0,
            model=_MODEL,
        )


def test_run_synthesis_does_not_reference_turn_lock() -> None:
    """Structural guard: run_synthesis never touches ``_TURN_LOCK``.

    The beat runs in a separate process, so the process-wide turn lock is
    irrelevant by construction (ADR-0086). Asserting the method *code*
    (docstring stripped — it legitimately explains the absence) contains no
    reference to it keeps Shirley's streaming path unaffected.
    """
    source = textwrap.dedent(inspect.getsource(AIServiceCore.run_synthesis))
    func = ast.parse(source).body[0]
    # Drop the leading docstring statement so the explanatory prose (which
    # names _TURN_LOCK) does not trip the guard; check only the real code.
    body = func.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    code = "\n".join(ast.unparse(node) for node in body)
    assert "_TURN_LOCK" not in code
