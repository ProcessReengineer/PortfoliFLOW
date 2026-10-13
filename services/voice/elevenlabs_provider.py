# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Native ElevenLabs adapter for the voice service (ADR-0136 §9).

Implements both ports — :class:`~services.voice.provider.SpeechToText` against
``/v1/speech-to-text`` and :class:`~services.voice.provider.TextToSpeech`
against ``/v1/text-to-speech/{voice_id}`` — with plain ``httpx``, **no
``elevenlabs`` SDK**. Both endpoints authenticate with the ``xi-api-key``
header. Speech-to-text uploads the recorded turn as multipart ``file`` with
``model_id``, switches audio-event tagging off (the documented default would
put "(laughter)"-style tags into the transcript) and sends no language hint,
so the language is detected; the transcript is the response's top-level
``text``.
Text-to-speech posts JSON ``text`` and ``model_id`` with the query
``output_format``; ``voice`` is the ElevenLabs voice ID, not a name. The
adapter is built **per half**, exactly as the OpenAI-wire one: the factory
hands it a :class:`~services.voice.resolved.ResolvedSTT` or a
:class:`~services.voice.resolved.ResolvedTTS`, and each half talks to its own
``base_url`` with its own key — an EU-residency host is just another
``base_url``.

- **No env reads here.** The adapter receives its resolved half (or halves)
  and never touches ``os.environ``; the key is sent as a header and never
  logged or put into an error message.
- **Per-call client lifecycle.** A fresh ``httpx.AsyncClient`` is opened in
  each method and closed when the call ends — a client is bound to the loop
  it was created on, so it must not be cached across calls/threads.
  ``pytest-httpx`` intercepts at the transport layer.

"No silent fallback": an unsupported MIME / ``fmt``, an empty transcript, an
empty synthesis input, a non-2xx status or a transport failure each raises a
clear :mod:`services.voice.errors` exception instead of returning empty data.

The module imports only the standard library and ``httpx`` — within the
package formula of ADR-0136 §8; it does not import ``openai``.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from services.voice.errors import (
    EmptyTranscriptError,
    UnsupportedAudioFormatError,
    VoiceConfigurationError,
    VoiceSynthesisError,
    VoiceTranscriptionError,
)
from services.voice.formats import extension_for
from services.voice.resolved import ResolvedSTT, ResolvedTTS
from services.voice.transcode import is_ogg

logger = logging.getLogger(__name__)

# One call; ElevenLabs answers within seconds, Scribe on long audio within tens.
_TIMEOUT = httpx.Timeout(60.0)
_STT_PATH = "/v1/speech-to-text"
_TTS_PATH = "/v1/text-to-speech/{voice_id}"
# Output fmt -> (ElevenLabs output_format, declared MIME). The opus MIME is
# settled per response (see synthesize).
_FMT_TO_OUTPUT: dict[str, tuple[str, str]] = {
    "mp3": ("mp3_44100_128", "audio/mpeg"),
    "opus": ("opus_48000_64", "audio/opus"),
}
# How much of an error body goes into the raised message.
_BODY_EXCERPT_CHARS = 200


def _url(base_url: str, path: str) -> str:
    """Join ``base_url`` (host without ``/v1``) and an endpoint ``path``."""
    return base_url.rstrip("/") + path


def _status_detail(response: httpx.Response) -> str:
    """Render a non-2xx response as ``HTTP <status>`` plus a body excerpt."""
    excerpt = response.text[:_BODY_EXCERPT_CHARS].strip()
    return f"HTTP {response.status_code}: {excerpt}" if excerpt else f"HTTP {response.status_code}"


class ElevenLabsVoiceProvider:
    """ElevenLabs adapter satisfying :class:`SpeechToText` and :class:`TextToSpeech`.

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
            raise ValueError("ElevenLabsVoiceProvider needs at least one half: stt= or tts=.")
        self._stt = stt
        self._tts = tts

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        """Transcribe ``audio`` via ``/v1/speech-to-text``.

        Args:
            audio: The raw audio bytes for one recorded turn.
            mime_type: The audio MIME type; a ``;codecs=...`` suffix (or any
                other parameter) is stripped before lookup.

        Returns:
            The non-empty, stripped transcript.

        Raises:
            VoiceConfigurationError: If this instance was built without a
                speech-to-text resolution. No HTTP call is made.
            UnsupportedAudioFormatError: If the (normalised) MIME type has no
                known extension. No HTTP call is made.
            VoiceTranscriptionError: On a transport failure, a non-2xx status,
                or a body that is not a JSON object.
            EmptyTranscriptError: If the response carries no or an
                empty/whitespace-only ``text``.
        """
        if self._stt is None:
            raise VoiceConfigurationError(
                "ElevenLabsVoiceProvider was built without a speech-to-text resolution."
            )

        ext, base = extension_for(mime_type)

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    _url(self._stt.base_url, _STT_PATH),
                    headers={"xi-api-key": self._stt.api_key},
                    files={"file": (f"audio.{ext}", audio, base)},
                    data={"model_id": self._stt.model, "tag_audio_events": "false"},
                )
        except httpx.HTTPError as exc:
            raise VoiceTranscriptionError(f"Transcription failed: {exc}") from exc
        if not response.is_success:
            raise VoiceTranscriptionError(f"Transcription failed: {_status_detail(response)}")

        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise VoiceTranscriptionError("Transcription returned no JSON object.") from exc
        if not isinstance(payload, dict):
            raise VoiceTranscriptionError("Transcription returned no JSON object.")

        text = str(payload.get("text") or "").strip()
        if not text:
            raise EmptyTranscriptError("Transcription returned no speech.")
        logger.debug("ElevenLabsVoiceProvider.transcribe: %d chars.", len(text))
        return text

    async def synthesize(self, text: str, *, fmt: str) -> tuple[bytes, str]:
        """Synthesise ``text`` via ``/v1/text-to-speech/{voice_id}``.

        Args:
            text: The prose to speak. Empty/whitespace-only input raises.
            fmt: The output container — ``"mp3"`` (web ``<audio>``) or
                ``"opus"`` (Telegram voice note).

        Returns:
            A ``(audio_bytes, output_mime_type)`` tuple.

        Raises:
            VoiceConfigurationError: If this instance was built without a
                text-to-speech resolution. No HTTP call is made.
            UnsupportedAudioFormatError: If ``fmt`` is unknown. No HTTP call is
                made.
            VoiceSynthesisError: If ``text`` is empty (no HTTP call), on a
                transport failure or a non-2xx status, or if the response
                carries no audio.
        """
        if self._tts is None:
            raise VoiceConfigurationError(
                "ElevenLabsVoiceProvider was built without a text-to-speech resolution."
            )

        mapping = _FMT_TO_OUTPUT.get(fmt)
        if mapping is None:
            supported = ", ".join(sorted(_FMT_TO_OUTPUT))
            raise UnsupportedAudioFormatError(
                f"Unsupported synthesis format {fmt!r}. Supported formats: {supported}."
            )

        clean = (text or "").strip()
        if not clean:
            raise VoiceSynthesisError("Cannot synthesise empty text.")

        output_format, out_mime = mapping
        path = _TTS_PATH.format(voice_id=quote(self._tts.voice, safe=""))

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    _url(self._tts.base_url, path),
                    params={"output_format": output_format},
                    headers={"xi-api-key": self._tts.api_key},
                    json={"text": clean, "model_id": self._tts.model},
                )
        except httpx.HTTPError as exc:
            raise VoiceSynthesisError(f"Synthesis failed: {exc}") from exc
        if not response.is_success:
            raise VoiceSynthesisError(f"Synthesis failed: {_status_detail(response)}")

        audio_bytes = response.content
        if not audio_bytes:
            raise VoiceSynthesisError("Synthesis returned no audio.")

        # Whether ElevenLabs wraps Opus in Ogg is undocumented, so the container
        # is read from the bytes (ADR-0136 §7). The Telegram reply path runs
        # its own byte check (voice note / transcode / audio file); this MIME
        # only names the fallback file.
        if fmt == "opus" and is_ogg(audio_bytes):
            out_mime = "audio/ogg"

        logger.debug(
            "ElevenLabsVoiceProvider.synthesize: %d bytes (%s).",
            len(audio_bytes),
            out_mime,
        )
        return audio_bytes, out_mime
