# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Core-side characterization tests — asyncio path through :class:`AIServiceCore`.

Per ADR-0038's test strategy and the stream A1 implementation prompt's
allocation, these tests live on the Qt-free core: they consume
:class:`StreamEvent` records directly from
:meth:`AIServiceCore.stream_response`, no ``QApplication``, no signal
spy. The Qt-flavoured counterparts (signal-emission tests) live in
``tests/characterization/test_ai_service_qt.py``.

Allocation in this file:

* C-04 — concatenated chunks equal the final assistant message.
* C-07 — tool-call branch leads to one further completion request.
* C-08 — multiple tool calls execute sequentially in declared index
  order.
* C-09 — tool exceptions are swallowed by the registry and forwarded
  as an error string to the next API request, *not* surfaced as an
  ``error`` event.
* C-13 — ``finish_reason="length"`` produces a clean ``stream_finished``.
* C-14 — empty response yields zero ``chunk`` events but still
  finishes.
* C-15 — conversation history (user → assistant → tool) is preserved
  in the next request body.
* C-16 — non-empty ``system_prompt`` is prepended to outgoing messages.
* C-17a — module-level absence-of-lock characterisation (static check).
* C-18-core — :func:`get_ai_service_core` returns the same instance.

C-12 (cancel) is omitted: no cancel mechanism exists in the legacy
``_StreamWorker`` and none was introduced by ADR-0038's "lift the
loop unchanged" rule.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.ai_models import (
    ConnectionStatus,
    Conversation,
    Message,
    MessageRole,
)
from services.ai_service_core import (
    AIServiceCore,
    StreamEvent,
    get_ai_service_core,
)
from services.tool_classes import ToolClass
from services.tool_registry import ToolRegistry
from tests.fixtures.mock_tools import build_fresh_registry
from tests.fixtures.openrouter_responses import (
    EMPTY_RESPONSE,
    LENGTH_LIMITED_RESPONSE,
    MULTI_TOOL_CALL_RESPONSE,
    SIMPLE_TEXT_RESPONSE,
    SINGLE_TOOL_CALL_RESPONSE,
    TOOL_CALL_THEN_TEXT_RESPONSE,
)
from tests.fixtures.sse_helpers import encode_sse_events


_BASE_URL = "https://openrouter.ai/api/v1"
_API_KEY = "sk-test-key"
_TEST_MODEL = "anthropic/claude-sonnet-4.5"
_COMPLETIONS_URL = f"{_BASE_URL}/chat/completions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_stream_response(httpx_mock: Any, events: list[dict[str, Any]]) -> None:
    """Register one streaming chat-completion response with ``pytest-httpx``."""
    httpx_mock.add_response(
        method="POST",
        url=_COMPLETIONS_URL,
        content=encode_sse_events(events),
        headers={"content-type": "text/event-stream"},
    )


def _make_conv(prompt: str) -> Conversation:
    """Build a one-user-message conversation."""
    conv = Conversation()
    conv.add_message(Message(role=MessageRole.USER, content=prompt))
    return conv


async def _drain(generator: Any) -> list[StreamEvent]:
    """Collect every event yielded by an async generator into a list."""
    return [event async for event in generator]


def _register_mock_tool(
    name: str,
    function: Any,
) -> Any:
    """Build a fresh :class:`ToolRegistry` with one mock tool registered.

    Returns the registry itself; callers wrap it with ``patch`` on
    ``services.tool_registry.get_tool_registry``.
    """
    fresh = build_fresh_registry(include_success=False)
    fresh.register_tool(
        name=name,
        function=function,
        description="mock",
        parameters={"type": "object", "properties": {}, "required": []},
        tool_class=ToolClass.READ_INTERNAL,
    )
    return fresh


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def configured_core() -> AIServiceCore:
    """A fresh :class:`AIServiceCore` already in CONNECTED state.

    Bypasses the asynchronous models-list fetch — tests in this module
    care about the streaming surface, not connection bring-up.
    """
    core = AIServiceCore()
    core.configure(_BASE_URL, _API_KEY)
    core.set_status(ConnectionStatus.CONNECTED)
    core.set_model(_TEST_MODEL)
    return core


# ---------------------------------------------------------------------------
# C-04
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C_04_concatenated_chunks_match_complete_message(
    configured_core: AIServiceCore, httpx_mock: Any
) -> None:
    """Concatenated ``chunk`` payloads equal the final ``Message.content``."""
    _add_stream_response(httpx_mock, SIMPLE_TEXT_RESPONSE)

    events = await _drain(configured_core.stream_response(_make_conv("hi")))

    chunks = [e.payload["text"] for e in events if e.event_type == "chunk"]
    finished = next(e for e in events if e.event_type == "stream_finished")
    assert finished.payload["message"].content == "".join(chunks)
    assert finished.payload["message"].content == "Hello from Shirley."


# ---------------------------------------------------------------------------
# C-07
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C_07_tool_loop_runs_second_request_after_execution(
    configured_core: AIServiceCore, httpx_mock: Any
) -> None:
    """``tool_calls`` finish reason triggers exactly one further API call."""
    _add_stream_response(httpx_mock, SINGLE_TOOL_CALL_RESPONSE)
    _add_stream_response(httpx_mock, TOOL_CALL_THEN_TEXT_RESPONSE)

    fresh = _register_mock_tool("list_datasets", lambda **_: "ok")
    with patch("services.tool_registry.get_tool_registry", return_value=fresh):
        events = await _drain(configured_core.stream_response(_make_conv("trigger")))

    assert any(e.event_type == "stream_finished" for e in events)
    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert len(posts) == 2


# ---------------------------------------------------------------------------
# C-08
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C_08_multi_tool_calls_executed_sequentially_in_index_order(
    configured_core: AIServiceCore, httpx_mock: Any
) -> None:
    """Multiple tool calls in one response execute in declared index order.

    Mirrors the legacy ``_StreamWorker.run`` behaviour: the
    streaming worker accumulates ``tool_calls_raw[idx]`` during the
    stream and then runs a plain sequential ``for`` loop. There is no
    parallelism. Both ``tool_called`` events arrive in declared
    order, and the second outgoing request carries two ``role: tool``
    messages in the same order.
    """
    _add_stream_response(httpx_mock, MULTI_TOOL_CALL_RESPONSE)
    _add_stream_response(httpx_mock, TOOL_CALL_THEN_TEXT_RESPONSE)

    execution_order: list[str] = []

    def make_fn(name: str) -> Any:
        def fn(**_kw: Any) -> str:
            execution_order.append(name)
            return f"result_{name}"

        return fn

    fresh = build_fresh_registry(include_success=False)
    for name in ("tool_a", "tool_b"):
        fresh.register_tool(
            name=name,
            function=make_fn(name),
            description="mock",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_INTERNAL,
        )

    with patch("services.tool_registry.get_tool_registry", return_value=fresh):
        events = await _drain(configured_core.stream_response(_make_conv("two tools")))

    tool_called = [e for e in events if e.event_type == "tool_called"]
    assert [e.payload["name"] for e in tool_called] == ["tool_a", "tool_b"]
    assert execution_order == ["tool_a", "tool_b"]

    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    body = json.loads(posts[1].content)
    tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_a", "call_b"]
    assert [m["content"] for m in tool_msgs] == [
        "result_tool_a",
        "result_tool_b",
    ]


# ---------------------------------------------------------------------------
# C-09
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C_09_tool_failure_returned_as_error_string_not_event(
    configured_core: AIServiceCore, httpx_mock: Any
) -> None:
    """A tool exception is swallowed by ``ToolRegistry.execute_tool``.

    The error becomes the ``role: tool`` message body in the next
    request — *not* an ``error`` :class:`StreamEvent`. This characterises
    the deliberately accommodating design of the registry's error
    path; reviewing this assertion matters because the future
    refactor's choice (introduce a ``tool_call_failed`` event vs. keep
    the absorbing behaviour) lives here.
    """
    _add_stream_response(httpx_mock, SINGLE_TOOL_CALL_RESPONSE)
    _add_stream_response(httpx_mock, TOOL_CALL_THEN_TEXT_RESPONSE)

    def boom(**_kwargs: Any) -> str:
        raise RuntimeError("kaboom")

    fresh = _register_mock_tool("list_datasets", boom)
    with patch("services.tool_registry.get_tool_registry", return_value=fresh):
        events = await _drain(configured_core.stream_response(_make_conv("trigger failing tool")))

    assert [e for e in events if e.event_type == "error"] == []

    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    body = json.loads(posts[1].content)
    tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert "kaboom" in tool_msgs[0]["content"]
    assert "RuntimeError" in tool_msgs[0]["content"]


# ---------------------------------------------------------------------------
# C-13
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C_13_finish_reason_length_completes_cleanly(
    configured_core: AIServiceCore, httpx_mock: Any
) -> None:
    """``finish_reason='length'`` still yields a clean ``stream_finished``."""
    _add_stream_response(httpx_mock, LENGTH_LIMITED_RESPONSE)

    events = await _drain(configured_core.stream_response(_make_conv("give me too much")))

    finished = next(e for e in events if e.event_type == "stream_finished")
    assert finished.payload["message"].content == "Truncated"
    assert [e for e in events if e.event_type == "error"] == []


# ---------------------------------------------------------------------------
# C-14
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C_14_empty_response_completes_with_zero_chunks(
    configured_core: AIServiceCore, httpx_mock: Any
) -> None:
    """A response with no content deltas still produces ``stream_finished``."""
    _add_stream_response(httpx_mock, EMPTY_RESPONSE)

    events = await _drain(configured_core.stream_response(_make_conv("respond with nothing")))

    chunks = [e for e in events if e.event_type == "chunk"]
    finished = next(e for e in events if e.event_type == "stream_finished")
    assert chunks == []
    assert finished.payload["message"].content == ""


# ---------------------------------------------------------------------------
# C-15
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C_15_conversation_history_preserved_across_turns(
    configured_core: AIServiceCore, httpx_mock: Any
) -> None:
    """Second request carries user → assistant → tool messages in order."""
    _add_stream_response(httpx_mock, SINGLE_TOOL_CALL_RESPONSE)
    _add_stream_response(httpx_mock, TOOL_CALL_THEN_TEXT_RESPONSE)

    fresh = _register_mock_tool("list_datasets", lambda **_: "tool_result")
    with patch("services.tool_registry.get_tool_registry", return_value=fresh):
        await _drain(configured_core.stream_response(_make_conv("the original prompt")))

    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    msgs = json.loads(posts[1].content)["messages"]
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "tool"]
    assert msgs[0]["content"] == "the original prompt"
    assert msgs[1]["tool_calls"][0]["id"] == "call_1"
    assert msgs[2]["tool_call_id"] == "call_1"
    assert msgs[2]["content"] == "tool_result"


# ---------------------------------------------------------------------------
# C-16
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_C_16_system_prompt_prepended_when_provided(
    configured_core: AIServiceCore, httpx_mock: Any
) -> None:
    """Non-empty ``system_prompt`` becomes the first outgoing message."""
    _add_stream_response(httpx_mock, SIMPLE_TEXT_RESPONSE)

    soul = configured_core.get_system_prompt("shirley")
    await _drain(configured_core.stream_response(_make_conv("hi"), system_prompt=soul))

    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    body = json.loads(posts[0].content)
    first = body["messages"][0]
    assert first["role"] == "system"
    assert "PortfoliFLOW" in first["content"]
    assert "Shirley" in first["content"]


# ---------------------------------------------------------------------------
# Tool-execution context lifecycle (ADR-0063)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_context_set_during_turn_and_cleared_after(
    configured_core: AIServiceCore, httpx_mock: Any
) -> None:
    """A passed ``tool_context`` is visible to tools mid-turn, gone after.

    The core owns the tool-execution context lifecycle (ADR-0063):
    :meth:`stream_response` sets the passed context under ``_TURN_LOCK``
    before the turn runs and clears it — together with the turn-scoped
    data cache — in a ``finally`` when the turn ends. This drives a real
    turn whose tool records what :func:`get_tool_context` returns at
    execution time and asserts (a) the tool observed exactly the passed
    context, and (b) the context is ``None`` once the generator is
    exhausted.
    """
    from uuid import uuid4

    from services.tools._tool_context import (
        ToolExecutionContext,
        get_tool_context,
    )

    _add_stream_response(httpx_mock, SINGLE_TOOL_CALL_RESPONSE)
    _add_stream_response(httpx_mock, TOOL_CALL_THEN_TEXT_RESPONSE)

    ctx = ToolExecutionContext(
        tenant_id=uuid4(),
        database_url="postgresql+asyncpg://user@localhost/db",
    )
    observed: list[ToolExecutionContext | None] = []

    def record_ctx(**_kwargs: Any) -> str:
        observed.append(get_tool_context())
        return "ok"

    fresh = _register_mock_tool("list_datasets", record_ctx)
    with patch("services.tool_registry.get_tool_registry", return_value=fresh):
        events = await _drain(
            configured_core.stream_response(_make_conv("trigger"), tool_context=ctx)
        )

    # (a) The tool ran and saw exactly the context that was passed in.
    assert any(e.event_type == "stream_finished" for e in events)
    assert observed == [ctx]
    # (b) The context is cleared once the generator is exhausted.
    assert get_tool_context() is None


# ---------------------------------------------------------------------------
# C-17a — module-level turn lock characterisation for the core
# ---------------------------------------------------------------------------


def test_C_17a_module_level_turn_lock_in_core() -> None:
    """``services.ai_service_core`` exposes a module-level ``_TURN_LOCK``.

    Stream A2 of ADR-0038 consolidated the bot-side ``_TURN_LOCK``
    (ADR-0031) into the core. Every consumer (Qt adapter, Telegram
    bot, future FastAPI handler) routes through
    :meth:`AIServiceCore.stream_response`, so a lock at this seam
    closes both the bot-vs-bot race ADR-0031 originally addressed and
    the bot-vs-GUI race that ADR-0031 named as a known limitation.

    The lock is :class:`threading.Lock` (not :class:`asyncio.Lock`) so
    it serialises across threads regardless of which event loop holds
    it — see the module-level "Concurrency" docstring in
    :mod:`services.ai_service_core` for the full rationale.
    """
    import services.ai_service_core as core_mod

    assert hasattr(core_mod, "_TURN_LOCK"), (
        "services.ai_service_core must expose a module-level _TURN_LOCK after ADR-0038 stream A2."
    )
    assert isinstance(core_mod._TURN_LOCK, type(threading.Lock())), (
        f"_TURN_LOCK must be a threading.Lock, got {type(core_mod._TURN_LOCK)!r}"
    )


def test_C_17a_lock_serialises_concurrent_turns() -> None:
    """Two threads driving ``stream_response`` concurrently are serialised.

    Characterises serialisation by measuring the gap between when each
    thread reaches the mocked ``client.chat.completions.create`` call.
    With the lock active and a 0.3 s sleep inside each call, the second
    thread cannot enter the body until the first thread's call has
    completed and released the lock — so the start-time gap is ≥ 0.3 s.
    Without the lock the two calls would overlap and the gap would be
    near zero. Direct measurement of call-start times sidesteps the
    asyncio-loop and thread-startup overhead that makes pure wall-clock
    bounds flaky on CI.

    Ported from the deleted
    ``tests/services/test_headless_shirley.py::test_lock_serialises_concurrent_turns``
    when ADR-0038 stream A2 relocated the lock into the core.
    """
    import asyncio
    import time

    from services.ai_models import ConnectionStatus, Conversation, Message, MessageRole
    from services.ai_service_core import AIServiceCore

    sleep_seconds = 0.3
    call_start_times: list[float] = []
    call_lock = threading.Lock()

    async def slow_streaming_create(**_kw: object):
        with call_lock:
            call_start_times.append(time.monotonic())
        await asyncio.sleep(sleep_seconds)

        async def empty_iter():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="ok", tool_calls=None),
                        finish_reason="stop",
                    )
                ]
            )

        return empty_iter()

    def call() -> None:
        async def _drive() -> None:
            core = AIServiceCore()
            core.configure("https://example.invalid", "k")
            core.set_status(ConnectionStatus.CONNECTED)
            core.set_model("test-model")

            mock_client = MagicMock()
            mock_client.chat.completions.create = MagicMock(side_effect=slow_streaming_create)
            mock_client.close = AsyncMock()
            # `get_tool_registry` is imported lazily inside
            # `stream_response`, so the patch target is the source module.
            with (
                patch.object(core, "_make_async_client", return_value=mock_client),
                patch("services.tool_registry.get_tool_registry") as mock_get_reg,
            ):
                mock_reg = MagicMock()
                mock_reg.get_tool_definitions.return_value = []
                mock_get_reg.return_value = mock_reg

                conv = Conversation()
                conv.add_message(Message(role=MessageRole.USER, content="go"))
                async for _event in core.stream_response(conv, ""):
                    pass

        asyncio.run(_drive())

    t1 = threading.Thread(target=call)
    t2 = threading.Thread(target=call)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert not t1.is_alive() and not t2.is_alive()
    assert len(call_start_times) == 2, (
        f"expected exactly 2 API-call entries, got {call_start_times!r}"
    )
    gap = abs(call_start_times[1] - call_start_times[0])
    # Serialised → gap ≥ sleep_seconds. Allow a small margin for jitter.
    assert gap >= sleep_seconds * 0.85, (
        f"expected serialised gap ≥ {sleep_seconds * 0.85:.2f}s between turns, "
        f"got {gap:.2f}s — turns appear to overlap"
    )


# ---------------------------------------------------------------------------
# C-18-core
# ---------------------------------------------------------------------------


def test_C_18_core_singleton_returns_same_instance() -> None:
    """:func:`get_ai_service_core` returns the same instance on every call."""
    a = get_ai_service_core()
    b = get_ai_service_core()
    assert a is b


# ---------------------------------------------------------------------------
# get_system_prompt — context-file ordering and graceful-degradation
# ---------------------------------------------------------------------------

_SOUL_FENCE = "# Soul_Test.md\n## System Prompt\n```\nSOUL_BODY\n```\n"


def _seed_fake_repo(root: Any) -> None:
    """Create ``docs/Soul_Shirley.md`` with a minimal fenced soul prompt."""
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "Soul_Shirley.md").write_text(_SOUL_FENCE, encoding="utf-8")


def test_context_files_appended_in_declared_order(tmp_path: Any, monkeypatch: Any) -> None:
    """Soul, analysis-results, and tool-orchestration concatenate in list order.

    The loader joins each present context file to the prompt with the
    literal ``"\\n\\n"`` separator. Order is derived from the
    ``context_files`` list inside ``get_system_prompt`` — analysis
    results first, tool orchestration second.
    """
    import services.ai_service_core as core_mod

    _seed_fake_repo(tmp_path)
    (tmp_path / "docs" / "Shirley_AnalysisResults_Context.md").write_text(
        "ANALYSIS_BODY", encoding="utf-8"
    )
    (tmp_path / "docs" / "Shirley_ToolOrchestration_Context.md").write_text(
        "TOOLORCH_BODY", encoding="utf-8"
    )

    monkeypatch.setattr(core_mod, "_REPO_ROOT", tmp_path)
    # B8 injects a registry-derived tool inventory between the soul and the
    # context files. This test pins context-file assembly only, so empty the
    # registry to keep the generated block out and the assertion deterministic.
    monkeypatch.setattr("services.tool_registry.get_tool_registry", ToolRegistry)
    result = AIServiceCore().get_system_prompt("shirley")

    assert result == "SOUL_BODY" + "\n\n" + "ANALYSIS_BODY" + "\n\n" + "TOOLORCH_BODY"
    soul_idx = result.index("SOUL_BODY")
    analysis_idx = result.index("ANALYSIS_BODY")
    toolorch_idx = result.index("TOOLORCH_BODY")
    assert soul_idx < analysis_idx < toolorch_idx


def test_tool_orchestration_context_missing_falls_back_cleanly(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Absent tool-orchestration file → soul + analysis-results unchanged.

    Pins the existing graceful-degradation contract for the new context
    file: a missing file is silently skipped, mirroring the behaviour
    already covered for the analysis-results context in
    ``tests/assistants/test_ai_service.py``.
    """
    import services.ai_service_core as core_mod

    _seed_fake_repo(tmp_path)
    (tmp_path / "docs" / "Shirley_AnalysisResults_Context.md").write_text(
        "ANALYSIS_BODY", encoding="utf-8"
    )
    # No Shirley_ToolOrchestration_Context.md written.

    monkeypatch.setattr(core_mod, "_REPO_ROOT", tmp_path)
    # Empty the registry so B8's generated tool inventory is not injected;
    # this test pins context-file degradation only (see the companion test).
    monkeypatch.setattr("services.tool_registry.get_tool_registry", ToolRegistry)
    result = AIServiceCore().get_system_prompt("shirley")

    assert result == "SOUL_BODY" + "\n\n" + "ANALYSIS_BODY"
    assert "TOOLORCH_BODY" not in result


def test_empty_tool_orchestration_context_does_not_append_separator(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Zero-byte (after strip) context file behaves as if absent.

    The loader's ``if ctx_text:`` guard means a file that is empty
    (or only whitespace) must not trigger the ``"\\n\\n"`` separator
    being appended to the prompt.
    """
    import services.ai_service_core as core_mod

    _seed_fake_repo(tmp_path)
    (tmp_path / "docs" / "Shirley_AnalysisResults_Context.md").write_text(
        "ANALYSIS_BODY", encoding="utf-8"
    )
    (tmp_path / "docs" / "Shirley_ToolOrchestration_Context.md").write_text(
        "   \n   \n", encoding="utf-8"
    )

    monkeypatch.setattr(core_mod, "_REPO_ROOT", tmp_path)
    # Empty the registry so B8's generated tool inventory is not injected;
    # this test pins the empty-context-file separator guard only.
    monkeypatch.setattr("services.tool_registry.get_tool_registry", ToolRegistry)
    result = AIServiceCore().get_system_prompt("shirley")

    assert result == "SOUL_BODY" + "\n\n" + "ANALYSIS_BODY"
    assert not result.endswith("\n\n")


# ---------------------------------------------------------------------------
# send_one_shot_extraction — the ``llm | model`` contract (ADR-0112 §4b,
# ADR-0123)
# ---------------------------------------------------------------------------
#
# The one-shot surface takes the same mutual-exclusion contract
# ``run_synthesis`` does. Two consumers, two paths: the Report Scraper hands a
# per-tenant ``ResolvedLLM`` and never touches the singleton, while the
# Fetcher-LLM keeps passing a bare ``model`` against the parked application
# credentials. These tests pin both, and the two ``ValueError`` refusals that
# keep the paths from being mixed.

_LLM_BASE_URL = "https://tenant.example/v1"
_LLM_COMPLETIONS_URL = f"{_LLM_BASE_URL}/chat/completions"


def _one_shot_response(content: str, model: str = _TEST_MODEL) -> dict[str, Any]:
    """Build a minimal non-streaming chat-completion body."""
    return {
        "id": "chatcmpl-one-shot",
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def test_one_shot_llm_bypasses_the_singleton_gate(httpx_mock: Any) -> None:
    """An ``llm=`` call succeeds on a core that was never configured.

    The Report Scraper's whole reason for existing on this seam: the singleton
    triple is empty and the status is DISCONNECTED, and the call still runs —
    on the resolution's endpoint, key and model.
    """
    from services.ai_service_core import ResolvedLLM

    httpx_mock.add_response(
        method="POST",
        url=_LLM_COMPLETIONS_URL,
        json=_one_shot_response("Extracted."),
    )

    core = AIServiceCore()
    assert core.get_status() is ConnectionStatus.DISCONNECTED

    result = core.send_one_shot_extraction(
        messages=[{"role": "user", "content": "extract"}],
        llm=ResolvedLLM(
            base_url=_LLM_BASE_URL,
            api_key="sk-tenant-key",
            model="anthropic/claude-opus-4-7",
        ),
    )

    assert result == "Extracted."
    request = httpx_mock.get_requests()[0]
    assert json.loads(request.content)["model"] == "anthropic/claude-opus-4-7"
    assert request.headers["authorization"] == "Bearer sk-tenant-key"


def test_one_shot_rejects_both_llm_and_model() -> None:
    """Two sources of truth for one call is the drift the contract removes."""
    from services.ai_service_core import ResolvedLLM

    core = AIServiceCore()
    with pytest.raises(ValueError, match="not both"):
        core.send_one_shot_extraction(
            messages=[{"role": "user", "content": "x"}],
            model=_TEST_MODEL,
            llm=ResolvedLLM(base_url=_LLM_BASE_URL, api_key="sk", model=_TEST_MODEL),
        )


def test_one_shot_rejects_neither_llm_nor_model(configured_core: AIServiceCore) -> None:
    """A connected core still needs to be told *which* model to call."""
    with pytest.raises(ValueError, match="`model` is required"):
        configured_core.send_one_shot_extraction(messages=[{"role": "user", "content": "x"}])


def test_one_shot_singleton_path_still_requires_connection() -> None:
    """Without ``llm``, the pre-ADR-0123 gate is verbatim — the Fetcher-LLM's."""
    core = AIServiceCore()
    with pytest.raises(RuntimeError, match="not connected"):
        core.send_one_shot_extraction(
            messages=[{"role": "user", "content": "x"}],
            model=_TEST_MODEL,
        )
