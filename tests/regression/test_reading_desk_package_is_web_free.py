# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: ``services/reading_desk/`` imports no web framework.

ADR-0139 §5: the route layer calls the Reading Desk writer when a scraper run
returns, but the writer must stay callable without that layer — from a job,
a CLI command or a test. The check walks the syntax tree of every module in
the package and inspects each import at any depth: at module level, inside a
function or under ``if TYPE_CHECKING``. ``from web import x`` counts as
``web.x``, and a relative import is resolved against the package.

If this guard goes red, move the web-facing code into the route layer; do
not relax the list.
"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_PACKAGE = "services.reading_desk"
_PACKAGE_DIR: Path = _REPO_ROOT.joinpath(*_PACKAGE.split("."))

_FORBIDDEN: tuple[str, ...] = (
    "fastapi",
    "starlette",
    "web",
)


def _imported_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = resolve_name("." * node.level + (node.module or ""), _PACKAGE)
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _is_forbidden(name: str) -> bool:
    # Whole components: ``web`` is not ``webbrowser``.
    return any(name == banned or name.startswith(f"{banned}.") for banned in _FORBIDDEN)


def test_reading_desk_package_imports_no_web_framework() -> None:
    paths = sorted(_PACKAGE_DIR.glob("*.py"))
    assert paths, f"no modules found in {_PACKAGE_DIR}"

    offenders = [
        f"{path.relative_to(_REPO_ROOT)}:{node.lineno} imports {name}"
        for path in paths
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in _imported_names(node)
        if _is_forbidden(name)
    ]
    assert not offenders, "services/reading_desk/ must stay web-free:\n  " + "\n  ".join(offenders)
