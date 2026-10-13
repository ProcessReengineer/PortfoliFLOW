# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Ogg check and the ``ffmpeg`` transcode (ADR-0136 §7).

The transcode cases run against a **fake ``ffmpeg``**: a shell script written
into ``tmp_path`` and passed as ``ffmpeg=``, so they are deterministic and need
no codec. The one real round trip runs only where the host has ``ffmpeg``.

Async tests need no decorator (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import io
import os
import shutil
import stat
import time
import types
import wave
from pathlib import Path

import pytest

import services.voice.transcode as transcode
from services.voice.errors import VoiceSynthesisError
from services.voice.transcode import ffmpeg_available, is_ogg, to_ogg_opus

_EXPECTED_ARGS = [
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
]


def _script(tmp_path: Path, name: str, body: str) -> str:
    """Write an executable ``#!/bin/sh`` script into ``tmp_path`` and return its path."""
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def fake_ffmpeg(tmp_path: Path) -> str:
    """A fake ``ffmpeg`` that consumes stdin and writes a fixed Ogg-looking stream."""
    return _script(tmp_path, "ffmpeg", "cat >/dev/null\nprintf 'OggS%s' fake-opus")


# ---------------------------------------------------------------------------
# is_ogg / ffmpeg_available
# ---------------------------------------------------------------------------


def test_is_ogg_true_on_capture_pattern() -> None:
    assert is_ogg(b"OggS\x00\x02rest-of-page")


def test_is_ogg_false_on_other_bytes() -> None:
    assert not is_ogg(b"\xff\xfb\x90\x64mp3")
    assert not is_ogg(b"RIFF....WAVE")
    assert not is_ogg(b"OpusHead")


def test_is_ogg_false_on_short_input() -> None:
    assert not is_ogg(b"")
    assert not is_ogg(b"Ogg")


def test_ffmpeg_available_reflects_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    assert ffmpeg_available() is True
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert ffmpeg_available() is False


# ---------------------------------------------------------------------------
# to_ogg_opus against a fake ffmpeg
# ---------------------------------------------------------------------------


async def test_to_ogg_opus_pipes_bytes_through_ffmpeg(tmp_path: Path) -> None:
    # Echo the number of stdin bytes consumed, proving the input went through the pipe.
    binary = _script(tmp_path, "ffmpeg", "n=$(wc -c | tr -d ' ')\nprintf 'OggS-len=%s' \"$n\"")
    audio = b"x" * 1234

    result = await to_ogg_opus(audio, ffmpeg=binary)

    assert result.startswith(b"OggS")
    assert result == b"OggS-len=1234"


async def test_to_ogg_opus_passes_the_documented_arguments(tmp_path: Path) -> None:
    args_file = tmp_path / "args.txt"
    binary = _script(
        tmp_path,
        "ffmpeg",
        f"for a in \"$@\"; do printf '%s\\n' \"$a\"; done > '{args_file}'\n"
        "cat >/dev/null\nprintf 'OggS'",
    )

    await to_ogg_opus(b"audio", ffmpeg=binary)

    assert args_file.read_text().splitlines() == _EXPECTED_ARGS


async def test_to_ogg_opus_without_ffmpeg_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(VoiceSynthesisError, match="ffmpeg"):
        await to_ogg_opus(b"audio", ffmpeg=None)


async def test_to_ogg_opus_nonzero_exit_raises(tmp_path: Path) -> None:
    binary = _script(tmp_path, "ffmpeg", "cat >/dev/null\necho 'Invalid data found' >&2\nexit 3")

    with pytest.raises(VoiceSynthesisError) as excinfo:
        await to_ogg_opus(b"audio", ffmpeg=binary)

    assert "exited 3" in str(excinfo.value)
    assert "Invalid data found" in str(excinfo.value)


async def test_to_ogg_opus_empty_output_raises(tmp_path: Path) -> None:
    binary = _script(tmp_path, "ffmpeg", "cat >/dev/null")

    with pytest.raises(VoiceSynthesisError, match="no output"):
        await to_ogg_opus(b"audio", ffmpeg=binary)


async def test_to_ogg_opus_non_ogg_output_raises(tmp_path: Path) -> None:
    binary = _script(tmp_path, "ffmpeg", "cat >/dev/null\nprintf 'RIFF....WAVE'")

    with pytest.raises(VoiceSynthesisError, match="not an Ogg stream"):
        await to_ogg_opus(b"audio", ffmpeg=binary)


async def test_to_ogg_opus_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = tmp_path / "pid"
    # ``exec`` keeps the pid, so the recorded pid is the process that gets killed.
    binary = _script(tmp_path, "ffmpeg", f"echo $$ > '{pid_file}'\nexec sleep 5")
    monkeypatch.setattr(transcode, "TRANSCODE_TIMEOUT_SECONDS", 0.2)

    started = time.monotonic()
    with pytest.raises(VoiceSynthesisError, match="timed out"):
        await to_ogg_opus(b"audio", ffmpeg=binary)

    assert time.monotonic() - started < 2.0
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_transcode_imports_stdlib_only() -> None:
    imported = {v.__name__ for v in vars(transcode).values() if isinstance(v, types.ModuleType)}
    assert not imported & {"openai", "httpx", "av"}, imported
    assert imported <= {"asyncio", "logging", "shutil"}, imported


# ---------------------------------------------------------------------------
# The real binary (host-dependent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg on host")
async def test_real_ffmpeg_round_trip() -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 3200)  # 0.2 s of silence

    result = await to_ogg_opus(buffer.getvalue())

    assert is_ogg(result)
