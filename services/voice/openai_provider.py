# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""OpenAI adapter for the voice service (ADR-0076).

Implements :class:`~services.voice.provider.VoiceProvider` against the OpenAI
``/v1/audio/transcriptions`` (STT) and ``/v1/audio/speech`` (TTS) endpoints
using the ``openai`` SDK already present in the project — **no new dependency**.

Two layering / lifecycle disciplines mirror :mod:`services.ai_service_core`:

- **No env reads here.** The adapter receives a :class:`VoiceConfig` and never
  touches ``os.environ``, which keeps it unit-testable.
- **Per-call client lifecycle.** A fresh ``openai.AsyncOpenAI`` is built inside
  each method and closed in a ``finally`` — an ``httpx.AsyncClient`` is bound to
  the loop it was created on, so it must not be cached across calls/threads.
  ``pytest-httpx`` intercepts at the transport layer, so this mocks
  transparently (see ``AIServiceCore._make_async_client``).

"No silent fallback": an unsupported MIME / ``fmt``, an empty transcript, an
empty synthesis input, or an SDK error each raises a clear
:mod:`services.voice.errors` exception instead of returning empty data.
"""

from __future__ import annotations

import logging

import openai

from services.voice.config import VoiceConfig
from services.voice.errors import (
    EmptyTranscriptError,
    UnsupportedAudioFormatError,
    VoiceSynthesisError,
    VoiceTranscriptionError,
)
from services.voice.resolved import ResolvedVoice

logger = logging.getLogger(__name__)

# Input MIME (normalised, parameters stripped) -> filename extension the
# /v1/audio/transcriptions endpoint recognises. The endpoint infers the
# decoder from the filename extension, so an in-memory upload needs a
# correct extension.
_MIME_TO_EXT: dict[str, str] = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mp4": "mp4",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
}

# Output fmt -> (OpenAI response_format, output MIME type).
_FMT_TO_RESPONSE: dict[str, tuple[str, str]] = {
    "mp3": ("mp3", "audio/mpeg"),
    "opus": ("opus", "audio/ogg"),
}


class OpenAIVoiceProvider:
    """OpenAI-backed STT/TTS adapter satisfying :class:`VoiceProvider`.

    Args:
        config: The resolved configuration — an env-backed
            :class:`VoiceConfig` or a per-tenant
            :class:`~services.voice.resolved.ResolvedVoice` (ADR-0118 §3).
            STT and TTS credentials, models, the STT base URL, and the TTS
            voice are read from it; the adapter never reads the environment
            directly, and reads attributes only, so both shapes fit.
    """

    def __init__(self, config: VoiceConfig | ResolvedVoice) -> None:
        self._config = config

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        """Transcribe ``audio`` via ``/v1/audio/transcriptions``.

        Args:
            audio: The raw audio bytes for one recorded turn.
            mime_type: The audio MIME type; a ``;codecs=...`` suffix (or any
                other parameter) is stripped before lookup, so
                ``"audio/webm;codecs=opus"`` resolves to ``"audio/webm"``.

        Returns:
            The non-empty, stripped transcript.

        Raises:
            UnsupportedAudioFormatError: If the (normalised) MIME type has no
                known extension. No HTTP call is made.
            VoiceTranscriptionError: If the SDK call fails (auth, non-2xx, or
                transport).
            EmptyTranscriptError: If the API returns an empty/whitespace-only
                transcript.
        """
        base = mime_type.split(";", 1)[0].strip().lower()
        ext = _MIME_TO_EXT.get(base)
        if ext is None:
            supported = ", ".join(sorted(_MIME_TO_EXT))
            raise UnsupportedAudioFormatError(
                f"Unsupported audio MIME type {mime_type!r} for transcription. "
                f"Supported types: {supported}."
            )

        client = openai.AsyncOpenAI(
            base_url=self._config.stt_base_url, api_key=self._config.stt_api_key
        )
        try:
            result = await client.audio.transcriptions.create(
                model=self._config.stt_model,
                file=(f"audio.{ext}", audio, base),
            )
        except openai.OpenAIError as exc:
            raise VoiceTranscriptionError(f"Transcription failed: {exc}") from exc
        finally:
            await client.close()

        text = (result.text or "").strip()
        if not text:
            raise EmptyTranscriptError("Transcription returned no speech.")
        logger.debug("OpenAIVoiceProvider.transcribe: %d chars.", len(text))
        return text

    async def synthesize(self, text: str, *, fmt: str) -> tuple[bytes, str]:
        """Synthesise ``text`` via ``/v1/audio/speech``.

        Uses the SDK's streaming-response form because the non-streaming
        ``.create(...)`` body read is deprecated; the bytes are read fully
        into memory (post-completion TTS, not streamed playback).

        Args:
            text: The prose to speak. Empty/whitespace-only input raises.
            fmt: The output container — ``"mp3"`` (web ``<audio>``) or
                ``"opus"`` (Telegram voice note).

        Returns:
            A ``(audio_bytes, output_mime_type)`` tuple.

        Raises:
            UnsupportedAudioFormatError: If ``fmt`` is unknown. No HTTP call is
                made.
            VoiceSynthesisError: If ``text`` is empty (no HTTP call) or the SDK
                call fails.
        """
        mapping = _FMT_TO_RESPONSE.get(fmt)
        if mapping is None:
            supported = ", ".join(sorted(_FMT_TO_RESPONSE))
            raise UnsupportedAudioFormatError(
                f"Unsupported synthesis format {fmt!r}. Supported formats: {supported}."
            )

        clean = (text or "").strip()
        if not clean:
            raise VoiceSynthesisError("Cannot synthesise empty text.")

        response_format, out_mime = mapping

        # TTS is OpenAI-specific; use the SDK default base URL — there is
        # intentionally no VOICE_TTS_BASE_URL knob.
        client = openai.AsyncOpenAI(api_key=self._config.tts_api_key)
        try:
            async with client.audio.speech.with_streaming_response.create(
                model=self._config.tts_model,
                voice=self._config.tts_voice,
                input=clean,
                response_format=response_format,
            ) as response:
                audio_bytes = await response.read()
        except openai.OpenAIError as exc:
            raise VoiceSynthesisError(f"Synthesis failed: {exc}") from exc
        finally:
            await client.close()

        logger.debug(
            "OpenAIVoiceProvider.synthesize: %d bytes (%s).",
            len(audio_bytes),
            out_mime,
        )
        return audio_bytes, out_mime
