# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The :class:`VoiceProvider` protocol — the seam for STT/TTS adapters (ADR-0076).

A provider turns audio into text (STT) and text into audio (TTS). The Protocol
is the single abstraction that keeps the system "ElevenLabs-ready" without
building a second adapter now: a new provider is a new class satisfying this
interface, not a change to any caller.

The Protocol is :func:`~typing.runtime_checkable` so callers and tests can
assert ``isinstance(adapter, VoiceProvider)``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class VoiceProvider(Protocol):
    """Channel-agnostic speech-to-text and text-to-speech operations.

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
