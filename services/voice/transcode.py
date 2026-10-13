# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Ogg detection and in-memory Ogg/Opus transcoding for voice replies (ADR-0136 §7).

``synthesize(..., fmt="opus")`` means *Opus audio in whatever container the
adapter delivers*. Telegram renders a voice note only for an Ogg/Opus
container, so the Telegram reply path checks the first bytes with
:func:`is_ogg` and, when they are not Ogg, transcodes them with the host's
optional ``ffmpeg`` through :func:`to_ogg_opus`.

The transcode runs ``ffmpeg`` as a subprocess over pipes: no temporary file is
written and the audio lives in memory for the one call only (ADR-0076's data
contract). Audio bytes are never logged.

This module imports only the standard library — the layering rule of the
package (ADR-0136 §8: the standard library, ``openai`` and ``httpx``) applies
here as everywhere in ``services/voice/``.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

from services.voice.errors import VoiceSynthesisError

logger = logging.getLogger(__name__)

OGG_MAGIC = b"OggS"
"""The Ogg page capture pattern every Ogg stream starts with."""

TRANSCODE_TIMEOUT_SECONDS = 30.0
"""Upper bound on one ``ffmpeg`` run before the process is killed."""


def is_ogg(data: bytes) -> bool:
    """Report whether ``data`` starts with the Ogg page capture pattern.

    Args:
        data: The audio bytes to inspect.

    Returns:
        True iff ``data`` starts with ``OggS``.
    """
    return data.startswith(OGG_MAGIC)


def ffmpeg_available() -> bool:
    """Report whether an ``ffmpeg`` binary is on the host's ``PATH``.

    Read at call time and never cached, so installing ``ffmpeg`` takes effect
    without a restart.

    Returns:
        True iff ``shutil.which("ffmpeg")`` finds a binary.
    """
    return shutil.which("ffmpeg") is not None


async def to_ogg_opus(audio: bytes, *, ffmpeg: str | None = None) -> bytes:
    """Transcode ``audio`` to Opus in an Ogg container, in memory, over pipes.

    Args:
        audio: The audio bytes in any container ``ffmpeg`` can read.
        ffmpeg: Path to the ``ffmpeg`` binary; ``None`` looks it up on
            ``PATH``.

    Returns:
        The Ogg/Opus bytes.

    Raises:
        VoiceSynthesisError: ``ffmpeg`` is not installed, timed out, exited
            non-zero, produced no output, or produced output that is not an
            Ogg stream.
    """
    binary = ffmpeg or shutil.which("ffmpeg")
    if binary is None:
        raise VoiceSynthesisError("ffmpeg is not installed; cannot transcode to Ogg/Opus.")

    proc = await asyncio.create_subprocess_exec(
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        "pipe:0",
        "-vn",
        "-c:a",
        "libopus",
        "-f",
        "ogg",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(audio), timeout=TRANSCODE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise VoiceSynthesisError(
            f"ffmpeg timed out after {TRANSCODE_TIMEOUT_SECONDS:g} s."
        ) from None

    rc = proc.returncode
    if rc != 0:
        raise VoiceSynthesisError(f"ffmpeg exited {rc}: {stderr[:200]!r}")
    if not stdout:
        raise VoiceSynthesisError("ffmpeg produced no output.")
    if not is_ogg(stdout):
        raise VoiceSynthesisError("ffmpeg output is not an Ogg stream.")

    logger.debug("Transcoded %d bytes of audio to %d bytes of Ogg/Opus.", len(audio), len(stdout))
    return stdout
