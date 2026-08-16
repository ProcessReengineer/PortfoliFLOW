# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression test for ADR-0038-era loop-safety of ``send_one_shot_extraction``.

The synchronous one-shot extraction surface is consumed by both
``WebResearchService._pre_filter_feed_items`` (called from the FastAPI
``/chat/stream`` handler) and the report scraper (called from the Qt
``_StreamWorker``). After ADR-0038 migrated the wrapped coroutine onto
``openai.AsyncOpenAI``, the wrapper used :func:`asyncio.run`
unconditionally — which raises ``RuntimeError`` whenever the calling
thread already has a running asyncio loop. This file pins the
loop-aware dispatch path that replaces it: when called from inside a
live loop, the call is dispatched to a fresh daemon thread; when
called from a thread without a loop, the direct path is used.

Both code paths run against the same ``pytest-httpx`` mock so the test
exercises the openai SDK end-to-end without contacting OpenRouter.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.ai_models import ConnectionStatus
from services.ai_service_core import AIServiceCore


_BASE_URL = "https://openrouter.ai/api/v1"
_API_KEY = "sk-test-key"
_MODEL = "anthropic/claude-haiku-4.5"
_COMPLETIONS_URL = f"{_BASE_URL}/chat/completions"


def _completion_response(content: str = "Loop-safe payload.") -> dict[str, Any]:
    """Build a minimal OpenAI chat completion response body."""
    return {
        "id": "chatcmpl-loop-safe",
        "object": "chat.completion",
        "created": 1700000000,
        "model": _MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 4,
            "completion_tokens": 4,
            "total_tokens": 8,
        },
    }


@pytest.fixture
def connected_core() -> AIServiceCore:
    """A fresh :class:`AIServiceCore` already in CONNECTED state."""
    core = AIServiceCore()
    core.configure(_BASE_URL, _API_KEY)
    core.set_status(ConnectionStatus.CONNECTED)
    core.set_model(_MODEL)
    return core


def test_send_one_shot_extraction_works_without_running_loop(
    connected_core: AIServiceCore, httpx_mock: Any
) -> None:
    """Negative control — the no-live-loop path still returns the content."""
    httpx_mock.add_response(
        method="POST",
        url=_COMPLETIONS_URL,
        json=_completion_response("Sync path result."),
    )

    result = connected_core.send_one_shot_extraction(
        messages=[{"role": "user", "content": "ping"}],
        model=_MODEL,
    )

    assert result == "Sync path result."


async def test_send_one_shot_extraction_works_inside_running_loop(
    connected_core: AIServiceCore, httpx_mock: Any
) -> None:
    """The regression fix — calling from inside a live loop must succeed.

    Before the fix this raised ``RuntimeError: asyncio.run() cannot be
    called from a running event loop``. After the fix the call is
    dispatched to a fresh daemon thread and the synchronous contract
    is preserved.
    """
    httpx_mock.add_response(
        method="POST",
        url=_COMPLETIONS_URL,
        json=_completion_response("Async-loop path result."),
    )

    result = connected_core.send_one_shot_extraction(
        messages=[{"role": "user", "content": "ping"}],
        model=_MODEL,
    )

    assert result == "Async-loop path result."


async def test_send_one_shot_extraction_propagates_errors_from_worker_thread(
    connected_core: AIServiceCore, httpx_mock: Any
) -> None:
    """SDK error classes raised on the worker thread reach the caller verbatim.

    ``WebResearchService._pre_filter_feed_items`` and the scraper
    inspect the exception class to decide whether to retry / log; the
    fresh-thread dispatch must not swallow or wrap them.
    """
    import openai

    httpx_mock.add_response(
        method="POST",
        url=_COMPLETIONS_URL,
        status_code=401,
        json={
            "error": {
                "message": "Invalid API key",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
    )

    with pytest.raises(openai.AuthenticationError):
        connected_core.send_one_shot_extraction(
            messages=[{"role": "user", "content": "ping"}],
            model=_MODEL,
        )
