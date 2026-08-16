# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: ``web/`` and ``services/chart_specs/`` must be matplotlib-free.

ADR-0042 §4 designates Plotly.js as the web charting standard. §5
reserves matplotlib for Shirley's planned Phase-5+ dynamic-chart
tool call only. Sub-stream 3c hardens the boundary by forbidding
matplotlib imports in the web Python codebase and in the Plotly
spec builders.

Two complementary checks:

1. **Source scan.** Walk every ``.py`` file under ``web/`` and
   ``services/chart_specs/`` and reject any line whose stripped form
   matches ``import matplotlib`` or ``from matplotlib``.
2. **Fresh-subprocess import.** Import
   :mod:`services.chart_specs` in a clean subprocess and assert no
   ``matplotlib`` module ends up in :data:`sys.modules`. The
   subprocess form mirrors the existing
   ``test_ai_service_core_qt_free.py`` pattern; the parent pytest
   process has typically already imported matplotlib via PyQt6 widget
   tests, so an in-process check would be useless.

If this guard goes red, the fix is to remove the matplotlib import.
The chart_specs path uses :func:`services.chart_specs.base.get_chart_theme`
which reads the theme JSON directly so no matplotlib is required.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "import matplotlib",
    "from matplotlib",
)
_SCAN_ROOTS: tuple[Path, ...] = (
    _REPO_ROOT / "web",
    _REPO_ROOT / "services" / "chart_specs",
)


def test_no_matplotlib_imports_in_web_or_chart_specs() -> None:
    """Source-level scan for forbidden matplotlib imports."""
    offenders: list[tuple[Path, int, str]] = []
    for root in _SCAN_ROOTS:
        assert root.exists(), f"expected directory missing: {root}"
        for python_file in root.rglob("*.py"):
            for lineno, raw_line in enumerate(
                python_file.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                stripped = raw_line.strip()
                # Skip comments and docstring lines that *mention*
                # matplotlib but do not import it. The pattern check
                # is anchored at the start of the stripped line.
                if stripped.startswith("#"):
                    continue
                if any(stripped.startswith(pattern) for pattern in _FORBIDDEN_PATTERNS):
                    offenders.append((python_file, lineno, stripped))
    assert not offenders, (
        "ADR-0042 forbids matplotlib imports in web/ and services/chart_specs/. "
        f"Offending lines: {offenders}"
    )


def test_chart_specs_imports_without_matplotlib() -> None:
    """A fresh ``import services.chart_specs`` must stay matplotlib-free."""
    code = (
        "import sys\n"
        "import services.chart_specs  # noqa: F401\n"
        "leaks = [m for m in sys.modules if m == 'matplotlib' "
        "or m.startswith('matplotlib.')]\n"
        "assert not leaks, (\n"
        "    'matplotlib leaked into services.chart_specs import graph: '\n"
        "    f'{leaks}'\n"
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
