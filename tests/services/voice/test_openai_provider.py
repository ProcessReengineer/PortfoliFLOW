# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-layer tests for :class:`OpenAIVoiceProvider` (ADR-0076, Block 1).

These mock the HTTP transport (``httpx``) — not the provider methods — exactly
as ``tests/assistants/test_ai_service_extraction.py`` does. The ``openai`` SDK
uses ``httpx`` internally, so ``pytest-httpx`` captures requests before they
leave the process and lets us assert the outgoing request shape.

Async tests need no decorator (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import json

import pytest

from services.voice.config import VoiceConfig
from services.voice.errors import (
    EmptyTranscriptError,
    UnsupportedAudioFormatError,
    VoiceSynthesisError,
    VoiceTranscriptionError,
)
from services.voice.openai_provider import OpenAIVoiceProvider

_STT_URL = "https://api.openai.com/v1/audio/transcriptions"
_TTS_URL = "https://api.openai.com/v1/audio/speech"


def _make_provider() -> OpenAIVoiceProvider:
    """Build a provider over an enabled config with dummy keys (no env reads)."""
    cfg = VoiceConfig(
        enabled=True,
        stt_provider="openai",
        stt_model="gpt-4o-mini-transcribe",
        stt_api_key="sk-stt-test",
        stt_base_url="https://api.openai.com/v1",
        tts_provider="openai",
        tts_model="gpt-4o-mini-tts",
        tts_voice="nova",
        tts_api_key="sk-tts-test",
    )
    return OpenAIVoiceProvider(cfg)


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------


async def test_transcribe_happy_path(httpx_mock) -> None:
    """A 2xx JSON transcript is returned stripped."""
    httpx_mock.add_response(url=_STT_URL, json={"text": "hello world"})

    provider = _make_provider()
    result = await provider.transcribe(b"audio-bytes", "audio/webm")

    assert result == "hello world"


async def test_transcribe_normalises_mime_parameters(httpx_mock) -> None:
    """A ``;codecs=...`` suffix is stripped; the request still fires."""
    httpx_mock.add_response(url=_STT_URL, json={"text": "ok"})

    provider = _make_provider()
    result = await provider.transcribe(b"audio-bytes", "audio/webm;codecs=opus")

    assert result == "ok"
    requests = httpx_mock.get_requests()
    posts = [r for r in requests if r.method == "POST" and str(r.url) == _STT_URL]
    assert len(posts) == 1
    # The normalised extension drives the multipart filename.
    assert b"audio.webm" in posts[0].content


async def test_transcribe_empty_transcript_raises(httpx_mock) -> None:
    """A whitespace-only transcript surfaces as EmptyTranscriptError."""
    httpx_mock.add_response(url=_STT_URL, json={"text": "   "})

    provider = _make_provider()
    with pytest.raises(EmptyTranscriptError):
        await provider.transcribe(b"audio-bytes", "audio/webm")


async def test_transcribe_unsupported_mime_no_http(httpx_mock) -> None:
    """An unsupported MIME raises before any HTTP call is made."""
    provider = _make_provider()

    with pytest.raises(UnsupportedAudioFormatError):
        await provider.transcribe(b"audio-bytes", "application/zip")

    assert httpx_mock.get_requests() == []


async def test_transcribe_api_error_surfaced(httpx_mock) -> None:
    """A 400 surfaces as VoiceTranscriptionError (4xx is not retried)."""
    httpx_mock.add_response(
        url=_STT_URL,
        status_code=400,
        json={"error": {"message": "bad"}},
    )

    provider = _make_provider()
    with pytest.raises(VoiceTranscriptionError):
        await provider.transcribe(b"audio-bytes", "audio/webm")


# ---------------------------------------------------------------------------
# synthesize
# ---------------------------------------------------------------------------


async def test_synthesize_happy_path_mp3(httpx_mock) -> None:
    """mp3 returns the audio bytes and the audio/mpeg MIME type."""
    httpx_mock.add_response(
        url=_TTS_URL,
        content=b"FAKEAUDIO",
        headers={"content-type": "audio/mpeg"},
    )

    provider = _make_provider()
    audio, mime = await provider.synthesize("hi", fmt="mp3")

    assert audio == b"FAKEAUDIO"
    assert mime == "audio/mpeg"


async def test_synthesize_opus_selects_container(httpx_mock) -> None:
    """opus maps to audio/ogg and sends response_format=opus in the body."""
    httpx_mock.add_response(
        url=_TTS_URL,
        content=b"OGGOPUS",
        headers={"content-type": "audio/ogg"},
    )

    provider = _make_provider()
    audio, mime = await provider.synthesize("hello", fmt="opus")

    assert audio == b"OGGOPUS"
    assert mime == "audio/ogg"
    requests = httpx_mock.get_requests()
    post = next(r for r in requests if r.method == "POST" and str(r.url) == _TTS_URL)
    body = json.loads(post.content)
    assert body["response_format"] == "opus"


async def test_synthesize_empty_text_no_http(httpx_mock) -> None:
    """Empty/whitespace input raises before any HTTP call is made."""
    provider = _make_provider()

    with pytest.raises(VoiceSynthesisError):
        await provider.synthesize("   ", fmt="mp3")

    assert httpx_mock.get_requests() == []


async def test_synthesize_unsupported_fmt_no_http(httpx_mock) -> None:
    """An unsupported fmt raises before any HTTP call is made."""
    provider = _make_provider()

    with pytest.raises(UnsupportedAudioFormatError):
        await provider.synthesize("hi", fmt="aac")

    assert httpx_mock.get_requests() == []


async def test_synthesize_api_error_surfaced(httpx_mock) -> None:
    """A 400 surfaces as VoiceSynthesisError (4xx is not retried)."""
    httpx_mock.add_response(
        url=_TTS_URL,
        status_code=400,
        json={"error": {"message": "bad"}},
    )

    provider = _make_provider()
    with pytest.raises(VoiceSynthesisError):
        await provider.synthesize("hi", fmt="mp3")
