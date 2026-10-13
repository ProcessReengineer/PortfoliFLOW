# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.voice.formats` — the shared input-audio vocabulary."""

from __future__ import annotations

import pytest

from services.voice.errors import UnsupportedAudioFormatError
from services.voice.formats import extension_for


def test_extension_for_strips_parameters() -> None:
    assert extension_for("audio/webm;codecs=opus") == ("webm", "audio/webm")


def test_extension_for_is_case_insensitive() -> None:
    assert extension_for("AUDIO/OGG") == ("ogg", "audio/ogg")


def test_extension_for_unknown_mime_raises_naming_the_vocabulary() -> None:
    with pytest.raises(UnsupportedAudioFormatError) as excinfo:
        extension_for("application/zip")
    message = str(excinfo.value)
    assert message.startswith("Unsupported audio MIME type 'application/zip' for transcription.")
    assert "audio/webm" in message
