# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Input-audio vocabulary shared by the adapters (ADR-0136 §1).

Every adapter uploads one recorded turn as a named file; the endpoints infer
the decoder from the filename extension. One table, one normalisation, one
error — so the two adapters cannot drift on what the surfaces may record.

This module imports only the standard library (ADR-0136 §8).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from services.voice.errors import UnsupportedAudioFormatError

#: Input MIME (normalised, parameters stripped) -> filename extension the
#: transcription endpoints recognise. Read-only.
MIME_TO_EXT: Mapping[str, str] = MappingProxyType(
    {
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
)


def normalise_mime(mime_type: str) -> str:
    """Strip parameters and case from a MIME type.

    Args:
        mime_type: A MIME type, possibly with parameters such as
            ``;codecs=opus``.

    Returns:
        The bare, lowercase type — ``"audio/webm;codecs=opus"`` becomes
        ``"audio/webm"``.
    """
    return mime_type.split(";", 1)[0].strip().lower()


def extension_for(mime_type: str) -> tuple[str, str]:
    """Return the upload filename extension for an input MIME type.

    Args:
        mime_type: The recorded audio's MIME type, as the surface reported it.

    Returns:
        A ``(extension, normalised_mime)`` tuple.

    Raises:
        UnsupportedAudioFormatError: If the normalised MIME type has no known
            extension.
    """
    base = normalise_mime(mime_type)
    ext = MIME_TO_EXT.get(base)
    if ext is None:
        supported = ", ".join(sorted(MIME_TO_EXT))
        raise UnsupportedAudioFormatError(
            f"Unsupported audio MIME type {mime_type!r} for transcription. "
            f"Supported types: {supported}."
        )
    return ext, base


__all__ = ["MIME_TO_EXT", "extension_for", "normalise_mime"]
