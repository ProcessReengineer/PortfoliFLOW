# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: :mod:`services.ai_service_core` must be Qt-free.

ADR-0038 narrows the layering exception originally recorded in
ADR-0011: only :mod:`services.ai_service_qt` is permitted to import
``PyQt6``. The Qt-free counterpart (:mod:`services.ai_service_core`)
is the home of the asyncio-native tool-execution loop and must be
importable from non-GUI consumers (FastAPI handlers, Telegram bot,
future CLI / cron jobs) without dragging Qt into the import graph.

This test imports ``services.ai_service_core`` in a *fresh
subprocess* — so the assertion is independent of whatever the
parent test process happened to import before — and asserts that
no module whose name contains ``PyQt6`` or ``PySide`` ends up in
``sys.modules``. Mirrors the regression guard ADR-0029 introduced
for :mod:`services.headless_shirley`
(``tests/services/test_headless_shirley.py::test_no_qt_import_in_fresh_subprocess``).

If this test goes red the most likely cause is a transitive import:
something the core depends on (a tool module, a registry helper, a
shared config loader) imports from :mod:`services.ai_service` (the
deprecation shim) or directly from PyQt6. The shim re-exports the
adapter, which is Qt-coupled by design — so any non-GUI code path
must import from :mod:`services.ai_service_core` directly. See
``services/scraper/service.py`` and ``services/web_research/service.py``
for the pattern.
"""

from __future__ import annotations

import subprocess
import sys


def test_ai_service_core_imports_without_pyqt6() -> None:
    """A fresh ``import services.ai_service_core`` must not pull in PyQt6.

    Running in a subprocess isolates the assertion from the parent
    pytest process, which has typically already imported PyQt6 via
    other test modules.
    """
    code = (
        "import sys\n"
        "import services.ai_service_core  # noqa: F401\n"
        "qt_modules = [\n"
        "    m for m in sys.modules\n"
        "    if 'PyQt6' in m or 'PySide' in m\n"
        "]\n"
        "assert not qt_modules, (\n"
        "    'Qt modules leaked into services.ai_service_core import '\n"
        "    f'graph: {qt_modules}'\n"
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


def test_ai_service_core_instantiation_does_not_pull_pyqt6() -> None:
    """Instantiating ``AIServiceCore`` must also stay Qt-free.

    The constructor calls ``_register_default_tools`` which imports
    :mod:`services.tools.web_research_tool`; that module transitively
    pulls in :mod:`services.web_research.service`. The split refactor
    repointed this consumer at :mod:`services.ai_service_core`
    (instead of the legacy ``services.ai_service`` shim that imports
    PyQt6), so this stricter test verifies the chain end-to-end. If
    it goes red, the fix is to find the new transitive importer of
    :mod:`services.ai_service` and repoint it at the core.
    """
    code = (
        "import sys\n"
        "from services.ai_service_core import AIServiceCore\n"
        "_ = AIServiceCore()  # triggers _register_default_tools\n"
        "qt_modules = [\n"
        "    m for m in sys.modules\n"
        "    if 'PyQt6' in m or 'PySide' in m\n"
        "]\n"
        "assert not qt_modules, (\n"
        "    'Qt modules leaked while instantiating AIServiceCore: '\n"
        "    f'{qt_modules}'\n"
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
