# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The two speech ports — :class:`SpeechToText` and :class:`TextToSpeech` (ADR-0136 §1).

One port per half: an STT adapter turns audio into text, a TTS adapter turns
text into audio. The factory composes **one adapter per half**, so a tenant may
pair any speech-to-text adapter with any text-to-speech adapter; a single
adapter class may still satisfy both Protocols (the OpenAI-wire one does). A
new provider is a new class satisfying the port of its half, not a change to
any caller.

Both Protocols are :func:`~typing.runtime_checkable` so callers and tests can
assert ``isinstance(adapter, SpeechToText)`` / ``isinstance(adapter,
TextToSpeech)``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SpeechToText(Protocol):
    """Channel-agnostic speech-to-text operation.

    Implementations process audio in memory for the duration of one turn and
    never persist it (ADR-0076). They raise — never return empty results — on
    failure, so callers can surface a clear message instead of silently
    degrading.
    """

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        """Transcribe ``audio`` to text.

        Args:
            audio: The raw audio bytes (a single recorded turn).
            mime_type: The audio MIME type (e.g. ``"audio/webm;codecs=opus"``);
                parameters after ``;`` are ignored.

        Returns:
            The non-empty transcript.

        Raises:
            UnsupportedAudioFormatError: If ``mime_type`` is not supported.
            VoiceTranscriptionError: If the STT call fails.
            EmptyTranscriptError: If the transcript is empty/whitespace-only.
        """
        ...


@runtime_checkable
class TextToSpeech(Protocol):
    """Channel-agnostic text-to-speech operation.

    Implementations process audio in memory for the duration of one turn and
    never persist it (ADR-0076). They raise — never return empty results — on
    failure, so callers can surface a clear message instead of silently
    degrading.
    """

    async def synthesize(self, text: str, *, fmt: str) -> tuple[bytes, str]:
        """Synthesise ``text`` to speech in container ``fmt``.

        Args:
            text: The prose to speak. Must not be empty/whitespace-only.
            fmt: The desired output container (e.g. ``"mp3"`` for the web
                ``<audio>`` element, ``"opus"`` for a Telegram voice note).

        Returns:
            A ``(audio_bytes, output_mime_type)`` tuple.

        Raises:
            UnsupportedAudioFormatError: If ``fmt`` is not supported.
            VoiceSynthesisError: If ``text`` is empty or the TTS call fails.
        """
        ...
