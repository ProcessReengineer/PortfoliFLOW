# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-layer tests for :class:`ElevenLabsVoiceProvider` (ADR-0136 §9).

These mock the HTTP transport (``httpx``) with ``pytest-httpx``, exactly as
``test_openai_provider.py`` does, and assert the outgoing request shape:
endpoint, ``xi-api-key`` header, multipart fields for speech-to-text and the
JSON body and ``output_format`` query for text-to-speech.

Async tests need no decorator (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import json

import httpx
import pytest

from services.voice.elevenlabs_provider import ElevenLabsVoiceProvider
from services.voice.errors import (
    EmptyTranscriptError,
    UnsupportedAudioFormatError,
    VoiceConfigurationError,
    VoiceSynthesisError,
    VoiceTranscriptionError,
)
from services.voice.provider import SpeechToText, TextToSpeech
from services.voice.resolved import ResolvedSTT, ResolvedTTS

_BASE_URL = "https://api.elevenlabs.io"
_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
_STT_URL = f"{_BASE_URL}/v1/speech-to-text"
_TTS_URL = f"{_BASE_URL}/v1/text-to-speech/{_VOICE_ID}"


def _stt(base_url: str = _BASE_URL) -> ResolvedSTT:
    return ResolvedSTT(
        provider="elevenlabs", model="scribe_v2", api_key="xi-stt-test", base_url=base_url
    )


def _tts(base_url: str = _BASE_URL, voice: str = _VOICE_ID) -> ResolvedTTS:
    return ResolvedTTS(
        provider="elevenlabs",
        model="eleven_multilingual_v2",
        voice=voice,
        api_key="xi-tts-test",
        base_url=base_url,
    )


def _make_provider(base_url: str = _BASE_URL, voice: str = _VOICE_ID) -> ElevenLabsVoiceProvider:
    """Build a provider over both halves with dummy keys (no env reads)."""
    return ElevenLabsVoiceProvider(stt=_stt(base_url), tts=_tts(base_url, voice))


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------


async def test_transcribe_happy_path(httpx_mock) -> None:
    """A 2xx JSON transcript is returned stripped; the request carries the documented fields."""
    httpx_mock.add_response(url=_STT_URL, json={"text": " hallo welt "})

    provider = _make_provider()
    assert isinstance(provider, SpeechToText)
    assert isinstance(provider, TextToSpeech)
    result = await provider.transcribe(b"audio-bytes", "audio/webm")

    assert result == "hallo welt"
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == _STT_URL
    assert request.headers["xi-api-key"] == "xi-stt-test"
    body = request.content
    assert b'name="model_id"\r\n\r\nscribe_v2' in body
    assert b'name="tag_audio_events"\r\n\r\nfalse' in body
    assert b'name="file"; filename="audio.webm"' in body
    assert b"language_code" not in body


async def test_transcribe_normalises_mime_parameters(httpx_mock) -> None:
    """A ``;codecs=...`` suffix is stripped from the filename and the part type."""
    httpx_mock.add_response(url=_STT_URL, json={"text": "ok"})

    provider = _make_provider()
    assert await provider.transcribe(b"audio-bytes", "audio/webm;codecs=opus") == "ok"

    body = httpx_mock.get_requests()[0].content
    assert b'filename="audio.webm"\r\nContent-Type: audio/webm\r\n' in body


async def test_transcribe_empty_transcript_raises(httpx_mock) -> None:
    """A whitespace-only transcript is an error, not an empty string."""
    httpx_mock.add_response(url=_STT_URL, json={"text": "   "})

    with pytest.raises(EmptyTranscriptError):
        await _make_provider().transcribe(b"audio-bytes", "audio/webm")


async def test_transcribe_missing_text_raises(httpx_mock) -> None:
    """A JSON object without ``text`` counts as no speech."""
    httpx_mock.add_response(url=_STT_URL, json={"language_code": "de"})

    with pytest.raises(EmptyTranscriptError):
        await _make_provider().transcribe(b"audio-bytes", "audio/webm")


async def test_transcribe_unsupported_mime_no_http(httpx_mock) -> None:
    """An unsupported MIME type raises before any request is made."""
    with pytest.raises(UnsupportedAudioFormatError):
        await _make_provider().transcribe(b"audio-bytes", "application/zip")

    assert httpx_mock.get_requests() == []


async def test_transcribe_non_2xx_surfaced(httpx_mock) -> None:
    """A non-2xx status names the status and never the key."""
    httpx_mock.add_response(
        url=_STT_URL, status_code=401, json={"detail": {"status": "invalid_api_key"}}
    )

    with pytest.raises(VoiceTranscriptionError) as excinfo:
        await _make_provider().transcribe(b"audio-bytes", "audio/webm")
    message = str(excinfo.value)
    assert "401" in message
    assert "xi-stt-test" not in message


async def test_transcribe_transport_error_surfaced(httpx_mock) -> None:
    """A transport failure becomes a transcription error."""
    httpx_mock.add_exception(httpx.ConnectError("boom"))

    with pytest.raises(VoiceTranscriptionError):
        await _make_provider().transcribe(b"audio-bytes", "audio/webm")


async def test_transcribe_non_json_body_raises(httpx_mock) -> None:
    """A 2xx body that is not JSON is an error."""
    httpx_mock.add_response(url=_STT_URL, content=b"<html>")

    with pytest.raises(VoiceTranscriptionError):
        await _make_provider().transcribe(b"audio-bytes", "audio/webm")


# ---------------------------------------------------------------------------
# synthesize
# ---------------------------------------------------------------------------


async def test_synthesize_happy_path_mp3(httpx_mock) -> None:
    """``"mp3"`` posts the documented JSON body and returns ``audio/mpeg``."""
    url = f"{_TTS_URL}?output_format=mp3_44100_128"
    httpx_mock.add_response(url=url, content=b"MP3")

    result = await _make_provider().synthesize("hello", fmt="mp3")

    assert result == (b"MP3", "audio/mpeg")
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == url
    assert request.headers["xi-api-key"] == "xi-tts-test"
    assert json.loads(request.content) == {"text": "hello", "model_id": "eleven_multilingual_v2"}


async def test_synthesize_opus_ogg_container(httpx_mock) -> None:
    """Opus bytes that start with ``OggS`` are declared ``audio/ogg``."""
    httpx_mock.add_response(
        url=f"{_TTS_URL}?output_format=opus_48000_64", content=b"OggS" + b"\x00" * 8
    )

    _, mime = await _make_provider().synthesize("hello", fmt="opus")

    assert mime == "audio/ogg"


async def test_synthesize_opus_bare_container(httpx_mock) -> None:
    """Opus bytes without the Ogg capture pattern are declared ``audio/opus``."""
    httpx_mock.add_response(url=f"{_TTS_URL}?output_format=opus_48000_64", content=b"\x00\x01opus")

    _, mime = await _make_provider().synthesize("hello", fmt="opus")

    assert mime == "audio/opus"


async def test_synthesize_empty_text_no_http(httpx_mock) -> None:
    """Empty/whitespace text raises before any request is made."""
    with pytest.raises(VoiceSynthesisError):
        await _make_provider().synthesize("   ", fmt="mp3")

    assert httpx_mock.get_requests() == []


async def test_synthesize_unsupported_fmt_no_http(httpx_mock) -> None:
    """An unknown output format raises before any request is made."""
    with pytest.raises(UnsupportedAudioFormatError):
        await _make_provider().synthesize("hello", fmt="wav")

    assert httpx_mock.get_requests() == []


async def test_synthesize_non_2xx_surfaced(httpx_mock) -> None:
    """A non-2xx status names the status and never the key."""
    httpx_mock.add_response(url=f"{_TTS_URL}?output_format=mp3_44100_128", status_code=422)

    with pytest.raises(VoiceSynthesisError) as excinfo:
        await _make_provider().synthesize("hello", fmt="mp3")
    message = str(excinfo.value)
    assert "422" in message
    assert "xi-tts-test" not in message


async def test_synthesize_empty_body_raises(httpx_mock) -> None:
    """A 2xx response without audio is an error, not empty bytes."""
    httpx_mock.add_response(url=f"{_TTS_URL}?output_format=mp3_44100_128", content=b"")

    with pytest.raises(VoiceSynthesisError):
        await _make_provider().synthesize("hello", fmt="mp3")


# ---------------------------------------------------------------------------
# URL building and halves
# ---------------------------------------------------------------------------


async def test_base_url_is_honoured_with_a_trailing_slash(httpx_mock) -> None:
    """An operator's base URL is used as given, without a double slash."""
    httpx_mock.add_response(content=b"MP3")

    await _make_provider(base_url="https://api.eu.residency.elevenlabs.io/").synthesize(
        "hello", fmt="mp3"
    )

    url = str(httpx_mock.get_requests()[0].url)
    assert url.startswith("https://api.eu.residency.elevenlabs.io/v1/text-to-speech/")


async def test_voice_id_is_quoted_in_the_path(httpx_mock) -> None:
    """The voice ID is one path segment, percent-encoded."""
    httpx_mock.add_response(content=b"MP3")

    await _make_provider(voice="a b/c").synthesize("hello", fmt="mp3")

    request = httpx_mock.get_requests()[0]
    assert request.url.raw_path.startswith(b"/v1/text-to-speech/a%20b%2Fc?")


async def test_constructor_requires_a_half_and_a_missing_half_refuses_without_http(
    httpx_mock,
) -> None:
    """No half → ``ValueError``; a half not supplied refuses before any HTTP."""
    with pytest.raises(ValueError):
        ElevenLabsVoiceProvider()

    with pytest.raises(VoiceConfigurationError, match="text-to-speech"):
        await ElevenLabsVoiceProvider(stt=_stt()).synthesize("hello", fmt="mp3")
    with pytest.raises(VoiceConfigurationError, match="speech-to-text"):
        await ElevenLabsVoiceProvider(tts=_tts()).transcribe(b"audio-bytes", "audio/webm")

    assert httpx_mock.get_requests() == []
