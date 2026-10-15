# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: ``services.voice_call`` must stay layering-clean (ADR-0137 §2).

The call package imports only the standard library and ``httpx``. This guard
imports the package and each of its nine modules, runs the pure assembly,
builds an agent definition from a literal ``ResolvedCall`` and constructs the
ElevenLabs provider (no request) — in a *fresh subprocess*, so the assertion
is independent of whatever the parent pytest process imported first — then
asserts that no module implicating a forbidden layer leaked into
``sys.modules``: the UI toolkits, ``web``, ``bot``, ``gui``, ``modules``,
``core``, the LLM and database clients, FastAPI, the voice-message package
and the AI core with its tool registry.

The twin of ``tests/services/voice/test_voice_layering.py``. If this goes
red, the most likely cause is a new transitive import that drags a
forbidden layer into the call package's import graph.
"""

from __future__ import annotations

import subprocess
import sys


def test_voice_call_import_stays_layering_clean() -> None:
    """A fresh ``import services.voice_call`` must not pull in forbidden layers."""
    code = (
        "import sys\n"
        "import services.voice_call  # noqa: F401\n"
        "import services.voice_call.agent_definition  # noqa: F401\n"
        "import services.voice_call.allowlist  # noqa: F401\n"
        "import services.voice_call.defaults  # noqa: F401\n"
        "import services.voice_call.elevenlabs_provider  # noqa: F401\n"
        "import services.voice_call.errors  # noqa: F401\n"
        "import services.voice_call.port  # noqa: F401\n"
        "import services.voice_call.resolved  # noqa: F401\n"
        "import services.voice_call.speakable  # noqa: F401\n"
        "from services.voice_call import (\n"
        "    CALL_TOOL_ALLOWLIST, ElevenLabsCallProvider, ResolvedCall, assemble_call,\n"
        "    build_agent_definition,\n"
        ")\n"
        "assert len(CALL_TOOL_ALLOWLIST) == 16\n"
        "_ = assemble_call(True, 'k', None, None, 'v', None, None, None)\n"
        "_ = build_agent_definition(\n"
        "    ResolvedCall(api_key='k', agent_id=None, model='m', voice='v', language='de',\n"
        "                 base_url='https://example.invalid'),\n"
        "    prompt='p',\n"
        ")\n"
        "_ = ElevenLabsCallProvider(api_key='k', base_url='https://example.invalid')\n"
        "forbidden = (\n"
        "    'PyQt6', 'PySide', 'web.', 'bot.', 'gui.', 'modules.', 'openai', 'sqlalchemy',\n"
        "    'fastapi', 'core.', 'services.voice.', 'services.ai_service_core',\n"
        "    'services.tool_registry',\n"
        ")\n"
        "leaked = [\n"
        "    m for m in sys.modules\n"
        "    if any(token in m for token in forbidden)\n"
        "]\n"
        "assert not leaked, (\n"
        "    'Forbidden layers leaked into services.voice_call import graph: '\n"
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
