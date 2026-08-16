# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The per-turn ``llm=`` seam on :class:`AIServiceCore` (ADR-0112 §4b).

F4 ends the process-global "configure once, serve everyone" posture for the
LLM turn consumers. The mechanism is one keyword argument on the two turn
entry points — :meth:`AIServiceCore.stream_response` and
:meth:`AIServiceCore.run_synthesis` — carrying a :class:`ResolvedLLM`.

These tests pin the two properties the whole strand rests on:

1. **With** ``llm``, the singleton is not consulted at all — an entirely
   unconfigured, ``DISCONNECTED`` core still drives a turn, and the client it
   builds carries the resolution's own ``base_url`` / ``api_key`` / ``model``.
   That is what lets one process serve many tenants.
2. **Without** ``llm``, behaviour is exactly what it always was — the Qt/GUI
   compatibility path, where ``configure`` / ``set_model`` / ``set_status``
   remain the seam. A regression here would silently break the desktop flow.

Plus the security property: :class:`ResolvedLLM` never renders its key, so a
log line, an f-string or a traceback that touches one cannot leak it.

The client is captured by monkeypatching ``openai.AsyncOpenAI`` — the single
construction site both paths funnel through — so the assertions are on what
the SDK would actually have been handed.
"""

from __future__ import annotations

from typing import Any, ClassVar

import openai
import pytest

from services.ai_models import ConnectionStatus, Conversation, Message, MessageRole
from services.ai_service_core import AIServiceCore, ResolvedLLM

_RESOLVED = ResolvedLLM(
    base_url="https://tenant.example/api/v1",
    api_key="sk-tenant-secret",
    model="tenant/model",
)


class _FakeStream:
    """Minimal async-iterable stand-in for a streaming completion."""

    def __init__(self) -> None:
        self._chunks = [
            type(
                "Chunk",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "delta": type("Delta", (), {"content": "hi", "tool_calls": None})(),
                                "finish_reason": "stop",
                            },
                        )()
                    ]
                },
            )()
        ]

    def __aiter__(self) -> Any:
        async def _gen() -> Any:
            for chunk in self._chunks:
                yield chunk

        return _gen()


class _FakeCompletions:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self._recorder = recorder

    async def create(self, **kwargs: Any) -> Any:
        self._recorder["kwargs"] = kwargs
        if kwargs.get("stream"):
            return _FakeStream()
        # Non-streaming (run_synthesis) shape.
        message = type("Msg", (), {"content": "", "tool_calls": []})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class _FakeClient:
    def __init__(self, recorder: dict[str, Any], **kwargs: Any) -> None:
        recorder["client_kwargs"] = kwargs
        self.chat = type("Chat", (), {"completions": _FakeCompletions(recorder)})()

    async def close(self) -> None:
        return None


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the kwargs of every ``openai.AsyncOpenAI`` construction."""
    recorder: dict[str, Any] = {}
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: _FakeClient(recorder, **kwargs))
    return recorder


def _conversation() -> Conversation:
    conv = Conversation()
    conv.add_message(Message(role=MessageRole.USER, content="hello"))
    return conv


# ---------------------------------------------------------------------------
# ResolvedLLM masks its key
# ---------------------------------------------------------------------------


class TestMasking:
    def test_repr_never_shows_the_key(self) -> None:
        assert "sk-tenant-secret" not in repr(_RESOLVED)
        assert "sk-tenant-secret" not in str(_RESOLVED)
        assert "sk-tenant-secret" not in f"{_RESOLVED}"

    def test_repr_still_identifies_the_endpoint_and_model(self) -> None:
        # Masked, not useless: an operator must be able to tell *which*
        # resolution a log line is about.
        text = repr(_RESOLVED)
        assert "tenant.example" in text
        assert "tenant/model" in text
        assert "masked" in text

    def test_an_absent_key_is_distinguishable_from_a_present_one(self) -> None:
        assert "unset" in repr(ResolvedLLM(base_url="u", api_key="", model="m"))
        assert "set;" in repr(_RESOLVED)


# ---------------------------------------------------------------------------
# stream_response
# ---------------------------------------------------------------------------


class TestStreamResponse:
    async def test_llm_drives_the_turn_on_an_unconfigured_core(
        self, captured: dict[str, Any]
    ) -> None:
        # Never configured, never connected — and the turn still runs.
        core = AIServiceCore()
        assert core.get_status() == ConnectionStatus.DISCONNECTED

        events = [e async for e in core.stream_response(_conversation(), llm=_RESOLVED)]

        assert [e.event_type for e in events] == ["chunk", "stream_finished"]
        assert captured["client_kwargs"] == {
            "base_url": "https://tenant.example/api/v1",
            "api_key": "sk-tenant-secret",
        }
        assert captured["kwargs"]["model"] == "tenant/model"

    async def test_llm_outranks_a_configured_singleton(self, captured: dict[str, Any]) -> None:
        # The dangerous case: a core that *is* configured must not leak its
        # own credential into a turn that brought its own.
        core = AIServiceCore()
        core.configure("https://singleton.example/api/v1", "sk-singleton")
        core.set_model("singleton/model")
        core.set_status(ConnectionStatus.CONNECTED)

        [e async for e in core.stream_response(_conversation(), llm=_RESOLVED)]

        assert captured["client_kwargs"]["api_key"] == "sk-tenant-secret"
        assert captured["client_kwargs"]["base_url"] == "https://tenant.example/api/v1"
        assert captured["kwargs"]["model"] == "tenant/model"

    async def test_the_final_message_carries_the_resolved_model(
        self, captured: dict[str, Any]
    ) -> None:
        core = AIServiceCore()
        events = [e async for e in core.stream_response(_conversation(), llm=_RESOLVED)]
        final = events[-1].payload["message"]
        assert final.model == "tenant/model"

    async def test_a_resolution_without_a_model_is_refused(self, captured: dict[str, Any]) -> None:
        core = AIServiceCore()
        empty = ResolvedLLM(base_url="u", api_key="k", model="")

        events = [e async for e in core.stream_response(_conversation(), llm=empty)]

        assert [e.event_type for e in events] == ["error"]
        assert "client_kwargs" not in captured

    async def test_without_llm_an_unconfigured_core_still_refuses(
        self, captured: dict[str, Any]
    ) -> None:
        # The Qt/GUI compatibility path, verbatim: same guard, same message.
        core = AIServiceCore()

        events = [e async for e in core.stream_response(_conversation())]

        assert [e.event_type for e in events] == ["error"]
        assert events[0].payload["message"] == "Not connected. Call configure() first."
        assert "client_kwargs" not in captured

    async def test_without_llm_a_connected_core_without_a_model_refuses(
        self, captured: dict[str, Any]
    ) -> None:
        core = AIServiceCore()
        core.configure("https://singleton.example/api/v1", "sk-singleton")
        core.set_status(ConnectionStatus.CONNECTED)

        events = [e async for e in core.stream_response(_conversation())]

        assert [e.event_type for e in events] == ["error"]
        assert events[0].payload["message"] == "No model selected. Call set_model() first."

    async def test_without_llm_the_singleton_triple_drives_the_turn(
        self, captured: dict[str, Any]
    ) -> None:
        core = AIServiceCore()
        core.configure("https://singleton.example/api/v1", "sk-singleton")
        core.set_model("singleton/model")
        core.set_status(ConnectionStatus.CONNECTED)

        events = [e async for e in core.stream_response(_conversation())]

        assert [e.event_type for e in events] == ["chunk", "stream_finished"]
        assert captured["client_kwargs"] == {
            "base_url": "https://singleton.example/api/v1",
            "api_key": "sk-singleton",
        }
        assert captured["kwargs"]["model"] == "singleton/model"


# ---------------------------------------------------------------------------
# run_synthesis
# ---------------------------------------------------------------------------


class TestRunSynthesis:
    _TOOL: ClassVar[dict[str, Any]] = {
        "type": "function",
        "function": {"name": "surface_finding"},
    }

    async def test_llm_drives_the_call_on_an_unconfigured_core(
        self, captured: dict[str, Any]
    ) -> None:
        core = AIServiceCore()

        await core.run_synthesis(
            system_prompt="You are Irene.",
            context_messages=[{"role": "user", "content": "state"}],
            tool=self._TOOL,
            llm=_RESOLVED,
        )

        assert captured["client_kwargs"] == {
            "base_url": "https://tenant.example/api/v1",
            "api_key": "sk-tenant-secret",
        }
        assert captured["kwargs"]["model"] == "tenant/model"
        assert captured["kwargs"]["stream"] is False

    async def test_passing_both_llm_and_model_is_refused(self, captured: dict[str, Any]) -> None:
        # Two sources of truth for one call is exactly the drift F4 removes,
        # so it is a loud programming error rather than a silent precedence.
        core = AIServiceCore()
        with pytest.raises(ValueError, match="not both"):
            await core.run_synthesis(
                system_prompt="",
                context_messages=[],
                tool=self._TOOL,
                model="some/model",
                llm=_RESOLVED,
            )

    async def test_without_llm_an_unconfigured_core_still_raises(
        self, captured: dict[str, Any]
    ) -> None:
        core = AIServiceCore()
        with pytest.raises(RuntimeError, match="not connected"):
            await core.run_synthesis(
                system_prompt="",
                context_messages=[],
                tool=self._TOOL,
                model="some/model",
            )

    async def test_without_llm_a_missing_model_is_refused(self, captured: dict[str, Any]) -> None:
        core = AIServiceCore()
        core.configure("https://singleton.example/api/v1", "sk-singleton")
        core.set_status(ConnectionStatus.CONNECTED)
        with pytest.raises(ValueError, match="`model` is required"):
            await core.run_synthesis(
                system_prompt="",
                context_messages=[],
                tool=self._TOOL,
            )

    async def test_without_llm_the_singleton_drives_the_call(
        self, captured: dict[str, Any]
    ) -> None:
        core = AIServiceCore()
        core.configure("https://singleton.example/api/v1", "sk-singleton")
        core.set_status(ConnectionStatus.CONNECTED)

        await core.run_synthesis(
            system_prompt="",
            context_messages=[],
            tool=self._TOOL,
            model="explicit/model",
        )

        assert captured["client_kwargs"]["api_key"] == "sk-singleton"
        assert captured["kwargs"]["model"] == "explicit/model"


# ---------------------------------------------------------------------------
# make_client — the factory the per-tenant embedder is handed
# ---------------------------------------------------------------------------


class TestMakeClient:
    def test_make_client_builds_from_the_resolution(self, captured: dict[str, Any]) -> None:
        _RESOLVED.make_client()
        assert captured["client_kwargs"] == {
            "base_url": "https://tenant.example/api/v1",
            "api_key": "sk-tenant-secret",
        }

    def test_two_resolutions_never_share_a_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The Irene isolation property at its source: each resolution's
        # factory builds a client on its own key and no other.
        seen: list[dict[str, Any]] = []
        monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: seen.append(kw))

        a = ResolvedLLM(base_url="https://a.example", api_key="sk-a", model="m")
        b = ResolvedLLM(base_url="https://b.example", api_key="sk-b", model="m")
        a.make_client()
        b.make_client()

        assert [k["api_key"] for k in seen] == ["sk-a", "sk-b"]
