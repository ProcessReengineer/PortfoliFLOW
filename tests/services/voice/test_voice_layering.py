# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: ``services.voice`` must stay layering-clean (ADR-0076).

The voice service imports only the standard library, ``openai`` and ``httpx``
(ADR-0038, ADR-0136 §8). The web surface and the Telegram bot import *from* it;
the dependency arrow points one way only. This guard imports ``services.voice``
— runs the pure assembly on both halves and constructs every declared adapter
of both halves via ``build_stt`` / ``build_tts`` from literal ``ResolvedSTT`` /
``ResolvedTTS`` values, so it reads no environment at all, and runs the Ogg
check of ``services.voice.transcode`` without spawning ``ffmpeg`` — in a *fresh
subprocess*, so the assertion is
independent of whatever the parent pytest process happened to import first,
then asserts that no module whose name implicates ``PyQt6``, ``PySide``,
``web.``, ``bot.``, ``gui.``, or ``modules.`` leaked into ``sys.modules``.

Mirrors ``tests/regression/test_ai_service_core_qt_free.py``. If this goes red,
the most likely cause is a new transitive import that drags a forbidden layer
into the voice import graph.
"""

from __future__ import annotations

import subprocess
import sys


def test_voice_import_stays_layering_clean() -> None:
    """A fresh ``import services.voice`` must not pull in forbidden layers."""
    code = (
        "import sys\n"
        "import services.voice  # noqa: F401\n"
        "import services.voice.assembly  # noqa: F401\n"
        "import services.voice.declarations  # noqa: F401\n"
        "import services.voice.transcode  # noqa: F401\n"
        "import services.voice.elevenlabs_provider  # noqa: F401\n"
        "import services.voice.formats  # noqa: F401\n"
        "from services.voice import (\n"
        "    PROVIDER_KEYS, ResolvedSTT, ResolvedTTS, assemble_stt, assemble_tts,\n"
        "    build_stt, build_tts,\n"
        ")\n"
        "# Run the pure assembly on both halves from literal values.\n"
        "_ = assemble_stt(True, 'openai_compatible', 'm', 'https://example.invalid/v1', None)\n"
        "_ = assemble_tts(True, 'openai', None, None, None, 'k')\n"
        "# Construct every adapter of both halves from literal values — exercise\n"
        "# the factory path without touching the network or reading the environment.\n"
        "for key in sorted(PROVIDER_KEYS):\n"
        "    _ = build_stt(ResolvedSTT(provider=key, model='m', api_key='k',\n"
        "                              base_url='https://example.invalid/v1'))\n"
        "    _ = build_tts(ResolvedTTS(provider=key, model='m', voice='v', api_key='k',\n"
        "                              base_url='https://example.invalid/v1'))\n"
        "# The Ogg check, without spawning ffmpeg.\n"
        "assert services.voice.transcode.is_ogg(b'OggS')\n"
        "forbidden = ('PyQt6', 'PySide', 'web.', 'bot.', 'gui.', 'modules.')\n"
        "leaked = [\n"
        "    m for m in sys.modules\n"
        "    if any(token in m for token in forbidden)\n"
        "]\n"
        "assert not leaked, (\n"
        "    'Forbidden layers leaked into services.voice import graph: '\n"
        "    f'{leaked}'\n"
        ")\n"
        "print('OK')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, (
        f"subprocess failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    assert "OK" in completed.stdout
