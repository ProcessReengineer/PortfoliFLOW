# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""OpenAI-wire adapter for the voice service (ADR-0076, ADR-0136 §1, §6).

Implements both ports — :class:`~services.voice.provider.SpeechToText` against
``/v1/audio/transcriptions`` and :class:`~services.voice.provider.TextToSpeech`
against ``/v1/audio/speech`` — using the ``openai`` SDK already present in the
project, **no new dependency**. The adapter is built **per half**: the factory
hands it a :class:`~services.voice.resolved.ResolvedSTT` or a
:class:`~services.voice.resolved.ResolvedTTS`, and each half talks to its own
``base_url`` with its own key.

Two layering / lifecycle disciplines mirror :mod:`services.ai_service_core`:

- **No env reads here.** The adapter receives its resolved half (or halves)
  and never touches ``os.environ``, which keeps it unit-testable.
- **Per-call client lifecycle.** A fresh ``openai.AsyncOpenAI`` is built inside
  each method and closed in a ``finally`` — an ``httpx.AsyncClient`` is bound to
  the loop it was created on, so it must not be cached across calls/threads.
  ``pytest-httpx`` intercepts at the transport layer, so this mocks
  transparently (see ``AIServiceCore._make_async_client``).

"No silent fallback": an unsupported MIME / ``fmt``, an empty transcript, an
empty synthesis input, or an SDK error each raises a clear
:mod:`services.voice.errors` exception instead of returning empty data.

The module imports only the standard library and ``openai`` — within the
package formula of ADR-0136 §8 (the standard library, ``openai`` and
``httpx``).
"""

from __future__ import annotations

import logging

import openai

from services.voice.errors import (
    EmptyTranscriptError,
    UnsupportedAudioFormatError,
    VoiceConfigurationError,
    VoiceSynthesisError,
    VoiceTranscriptionError,
)
from services.voice.formats import extension_for
from services.voice.resolved import ResolvedSTT, ResolvedTTS

logger = logging.getLogger(__name__)

# Output fmt -> (OpenAI response_format, output MIME type).
_FMT_TO_RESPONSE: dict[str, tuple[str, str]] = {
    "mp3": ("mp3", "audio/mpeg"),
    "opus": ("opus", "audio/ogg"),
}


class OpenAIVoiceProvider:
    """OpenAI-wire adapter satisfying :class:`SpeechToText` and :class:`TextToSpeech`.

    One class, two halves (ADR-0136 §1): the factory builds it with exactly
    the half it was asked for, so a tenant's STT and TTS never share an
    adapter instance. The adapter never reads the environment; endpoint, key
    and model of each half come from its resolution (ADR-0136 §6).

    Args:
        stt: The speech-to-text resolution, or ``None`` when this instance
            serves only text-to-speech.
        tts: The text-to-speech resolution, or ``None`` when this instance
            serves only speech-to-text.

    Raises:
        ValueError: If neither half is given.
    """

    def __init__(self, *, stt: ResolvedSTT | None = None, tts: ResolvedTTS | None = None) -> None:
        if stt is None and tts is None:
            raise ValueError("OpenAIVoiceProvider needs at least one half: stt= or tts=.")
        self._stt = stt
        self._tts = tts

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
            VoiceConfigurationError: If this instance was built without a
                speech-to-text resolution. A defensive guard — the factory
                never builds a half it was not asked for. No HTTP call is made.
            UnsupportedAudioFormatError: If the (normalised) MIME type has no
                known extension. No HTTP call is made.
            VoiceTranscriptionError: If the SDK call fails (auth, non-2xx, or
                transport).
            EmptyTranscriptError: If the API returns an empty/whitespace-only
                transcript.
        """
        if self._stt is None:
            raise VoiceConfigurationError(
                "OpenAIVoiceProvider was built without a speech-to-text resolution."
            )

        ext, base = extension_for(mime_type)

        client = openai.AsyncOpenAI(base_url=self._stt.base_url, api_key=self._stt.api_key)
        try:
            result = await client.audio.transcriptions.create(
                model=self._stt.model,
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
            VoiceConfigurationError: If this instance was built without a
                text-to-speech resolution. A defensive guard — the factory
                never builds a half it was not asked for. No HTTP call is made.
            UnsupportedAudioFormatError: If ``fmt`` is unknown. No HTTP call is
                made.
            VoiceSynthesisError: If ``text`` is empty (no HTTP call) or the SDK
                call fails.
        """
        if self._tts is None:
            raise VoiceConfigurationError(
                "OpenAIVoiceProvider was built without a text-to-speech resolution."
            )

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

        client = openai.AsyncOpenAI(base_url=self._tts.base_url, api_key=self._tts.api_key)
        try:
            async with client.audio.speech.with_streaming_response.create(
                model=self._tts.model,
                voice=self._tts.voice,
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
