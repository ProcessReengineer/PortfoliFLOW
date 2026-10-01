# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: no Qt binding is imported anywhere in the tree.

ADR-0094 removed the desktop surface, and ``AGENTS.md`` states the rule
without exception: PyQt6 is not imported anywhere. The purity guards of
single packages (analytics, market data, overlay, the TA profile, the AI
service core) each cover their own layer; this guard covers every
first-party Python file, so a stray import in a package without a guard of
its own fails here rather than only on a machine whose environment lacks
the binding.

The check is a source scan and therefore independent of which packages the
local environment has installed. It walks every ``.py`` file under the
first-party roots and rejects any line whose stripped form starts with an
import of a Qt binding or of the pytest-qt plugin. Comment lines are
skipped; string literals and docstring prose do not start with ``import``
or ``from`` and are not matched.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# Every first-party Python root. ``tests`` is included: a test that needs a
# Qt binding would bring the dependency back through the dev extra.
_SCANNED_ROOTS: tuple[str, ...] = (
    "bot",
    "cli",
    "core",
    "db",
    "modules",
    "scripts",
    "services",
    "tests",
    "tools",
    "web",
)

_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "import PyQt",
    "from PyQt",
    "import PySide",
    "from PySide",
    "import qtpy",
    "from qtpy",
    "import pytestqt",
    "from pytestqt",
)


def _qt_import_offenders(roots: list[Path]) -> list[tuple[Path, int, str]]:
    """Return ``(file, line number, stripped line)`` for each Qt import under ``roots``."""
    offenders: list[tuple[Path, int, str]] = []
    for root in roots:
        for python_file in sorted(root.rglob("*.py")):
            lines = python_file.read_text(encoding="utf-8").splitlines()
            for lineno, raw_line in enumerate(lines, start=1):
                stripped = raw_line.strip()
                if stripped.startswith("#"):
                    continue
                if stripped.startswith(_FORBIDDEN_PREFIXES):
                    offenders.append((python_file, lineno, stripped))
    return offenders


def test_no_qt_binding_is_imported_anywhere() -> None:
    """No first-party Python file imports a Qt binding or pytest-qt."""
    roots = [_REPO_ROOT / name for name in _SCANNED_ROOTS]
    missing = [str(root) for root in roots if not root.is_dir()]
    assert not missing, f"expected first-party roots missing: {missing}"

    offenders = _qt_import_offenders(roots)
    assert not offenders, (
        "PyQt6 is not imported anywhere (AGENTS.md, ADR-0094). Offending lines: "
        + "; ".join(f"{path}:{lineno}:{text}" for path, lineno, text in offenders)
    )


def test_scanner_flags_an_import_but_not_prose(tmp_path: Path) -> None:
    """The scanner reports an indented import, never a comment or a string."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        '"""Mentions PyQt6 in prose only."""\n'
        "# import PyQt6 in a comment\n"
        'MARKER = "from PyQt6 import QtCore"\n'
        "def load():\n"
        "    from PyQt6.QtCore import QObject\n",
        encoding="utf-8",
    )

    offenders = _qt_import_offenders([tmp_path])

    assert [(lineno, text) for _, lineno, text in offenders] == [
        (5, "from PyQt6.QtCore import QObject"),
    ]
