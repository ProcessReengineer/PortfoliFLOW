# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Helpers for emitting OpenAI-compatible SSE byte streams in tests.

The OpenAI Python SDK reads streamed chat completions as
``text/event-stream``: a sequence of ``data: {json}\\n\\n`` frames
terminated by ``data: [DONE]\\n\\n``. ``pytest-httpx`` accepts any
``httpx.SyncByteStream`` for ``add_response(stream=...)``; this module
turns Python event lists into such a stream.

The events themselves are produced by
:mod:`tests.fixtures.openrouter_responses` so individual tests stay
focused on the semantic of the scenario, not on byte-level plumbing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

import httpx


def encode_sse_events(events: Iterable[dict[str, Any]]) -> bytes:
    """Encode a sequence of chat-completion-chunk dicts as SSE bytes.

    Each event becomes one ``data: <json>\\n\\n`` frame. The terminating
    ``data: [DONE]\\n\\n`` frame is appended automatically — callers
    must not include it themselves.

    Args:
        events: The chat-completion-chunk payloads to send. Each must be
            JSON-serialisable.

    Returns:
        The concatenated SSE byte string ready to be served as the
        response body of a ``text/event-stream`` response.
    """
    out = bytearray()
    for event in events:
        out += b"data: "
        out += json.dumps(event).encode("utf-8")
        out += b"\n\n"
    out += b"data: [DONE]\n\n"
    return bytes(out)


class _ByteIterStream(httpx.SyncByteStream):
    """Adapter turning a fixed bytes payload into an ``httpx`` byte stream.

    ``pytest-httpx`` accepts any ``SyncByteStream``; the simplest one is
    a wrapper that yields the entire payload as one chunk. Tests that
    care about how the openai SDK handles fragmentation can replace this
    with their own ``SyncByteStream`` subclass that yields multiple
    chunks.
    """

    def __init__(self, data: bytes) -> None:
        """Store the byte payload.

        Args:
            data: The complete response body in SSE format.
        """
        self._data = data

    def __iter__(self) -> Iterator[bytes]:
        """Yield the payload as a single chunk."""
        yield self._data

    def close(self) -> None:  # pragma: no cover — required by protocol
        """No-op close; the bytes are already in memory."""
        return None


class SlowSseStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A byte stream that yields each SSE frame with a delay.

    Implements both ``httpx.SyncByteStream`` and ``httpx.AsyncByteStream``
    so the same fixture works against ``openai.OpenAI`` (sync httpx
    client) and ``openai.AsyncOpenAI`` (async httpx client). The C-17b
    concurrency test relies on the async path; existing characterization
    tests against the legacy sync core would have used the sync path.

    Each event in the input list is encoded as one
    ``data: <json>\\n\\n`` frame and yielded after
    ``per_frame_delay_s`` seconds; the trailing ``[DONE]`` frame is
    appended automatically.
    """

    def __init__(
        self,
        events: Iterable[dict[str, Any]],
        *,
        per_frame_delay_s: float = 0.1,
    ) -> None:
        """Store the encoded frames and the per-frame delay.

        Args:
            events: Chat-completion-chunk payloads.
            per_frame_delay_s: Sleep duration before yielding each frame
                (default 100 ms).
        """
        self._frames: list[bytes] = []
        for event in events:
            self._frames.append(b"data: " + json.dumps(event).encode("utf-8") + b"\n\n")
        self._frames.append(b"data: [DONE]\n\n")
        self._delay = per_frame_delay_s

    def __iter__(self) -> Iterator[bytes]:
        """Yield each SSE frame after ``self._delay`` seconds (sync path)."""
        import time

        # First frame is sent immediately so the SDK begins reading;
        # subsequent frames are throttled.
        for i, frame in enumerate(self._frames):
            if i > 0:
                time.sleep(self._delay)
            yield frame

    async def __aiter__(self):
        """Yield each SSE frame after ``self._delay`` seconds (async path)."""
        import asyncio

        for i, frame in enumerate(self._frames):
            if i > 0:
                await asyncio.sleep(self._delay)
            yield frame

    def close(self) -> None:  # pragma: no cover — required by protocol
        """No-op close (sync)."""
        return None

    async def aclose(self) -> None:  # pragma: no cover — required by protocol
        """No-op aclose (async)."""
        return None


def sse_stream_from_events(events: Iterable[dict[str, Any]]) -> httpx.SyncByteStream:
    """Build an ``httpx.SyncByteStream`` from a list of SSE events.

    Convenience wrapper around :func:`encode_sse_events` that wraps the
    result in the byte-stream adapter ``pytest-httpx`` expects.

    Args:
        events: Chat-completion-chunk payloads. The terminating ``[DONE]``
            frame is appended automatically.

    Returns:
        A ``SyncByteStream`` ready to be passed to
        ``httpx_mock.add_response(stream=...)``.
    """
    return _ByteIterStream(encode_sse_events(events))


__all__ = ["SlowSseStream", "encode_sse_events", "sse_stream_from_events"]
