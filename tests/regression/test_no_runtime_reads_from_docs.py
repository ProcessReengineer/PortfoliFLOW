# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: no runtime code builds a path into ``docs/``.

``docs/`` is documentation for people. Files the running platform reads live
beside it: configuration in ``config/``, the system prompts in ``prompts/``.
PortfoliFLOW runs from a source checkout (``README.md``), so both are read
relative to the checkout, and a documentation edit can never change runtime
behaviour by accident.

The scan walks the syntax tree of every runtime package and rejects a string
constant that is exactly ``"docs"`` (the ``_REPO_ROOT / "docs"`` shape).
Messages that *mention* a document, such as ``"docs/deploy/…"`` in an error
text, are not path segments and pass.

If this guard goes red, move the file the code reads into ``config/`` or
``prompts/`` and load it from there.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_RUNTIME_ROOTS: tuple[str, ...] = ("core", "services", "modules", "web", "bot", "cli")


def _docs_segments(source: str, label: str) -> list[str]:
    """Return one ``label:line`` entry per ``"docs"`` string constant in ``source``."""
    return [
        f"{label}:{node.lineno}"
        for node in ast.walk(ast.parse(source, filename=label))
        if isinstance(node, ast.Constant) and node.value == "docs"
    ]


def test_runtime_code_builds_no_path_into_docs() -> None:
    """No ``.py`` file in a runtime package uses ``"docs"`` as a path segment."""
    offenders: list[str] = []
    for root in _RUNTIME_ROOTS:
        assert (_REPO_ROOT / root).is_dir(), f"expected directory missing: {root}/"
        for path in sorted((_REPO_ROOT / root).rglob("*.py")):
            label = str(path.relative_to(_REPO_ROOT))
            offenders.extend(_docs_segments(path.read_text(encoding="utf-8"), label))
    assert not offenders, (
        "Runtime code must not read from docs/; use config/ or prompts/. "
        f"Offending constants: {offenders}"
    )


def test_scan_detects_a_docs_segment_and_ignores_messages() -> None:
    """The scan catches the path shape and lets a mention in prose pass."""
    source = (
        'root = _REPO_ROOT / "docs" / "Soul_Shirley.md"\n'
        'hint = "see docs/deploy/credential-vault.md."\n'
        '"""A docstring that names docs/ in passing."""\n'
    )
    assert _docs_segments(source, "sample.py") == ["sample.py:1"]
