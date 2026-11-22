# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: ``services/scraper/`` has no database and no web framework.

ADR-0139 §5: the route layer writes the readings and the scraper package gains
no database access. The check walks the syntax tree of every module in the
package and inspects each import at any depth: at module level, inside a
function or under ``if TYPE_CHECKING``. ``from core import models`` counts as
``core.models``, and a relative import is resolved against the package.

If this guard goes red, move the code that needs the database into
``services/reading_desk/`` or the route layer; do not relax the list.
"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_PACKAGE = "services.scraper"
_PACKAGE_DIR: Path = _REPO_ROOT.joinpath(*_PACKAGE.split("."))

_FORBIDDEN: tuple[str, ...] = (
    "sqlalchemy",
    "fastapi",
    "starlette",
    "web",
    "db",
    "core.repositories",
    "core.models",
)


def _imported_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        base = resolve_name("." * node.level + (node.module or ""), _PACKAGE)
        return [base, *(f"{base}.{alias.name}" for alias in node.names)]
    return []


def _is_forbidden(name: str) -> bool:
    # Whole components: ``web`` is not ``webbrowser``, ``db`` is not ``dbm``.
    return any(name == banned or name.startswith(f"{banned}.") for banned in _FORBIDDEN)


def test_scraper_package_imports_no_database_and_no_web() -> None:
    paths = sorted(_PACKAGE_DIR.glob("*.py"))
    assert paths, f"no modules found in {_PACKAGE_DIR}"

    offenders = [
        f"{path.relative_to(_REPO_ROOT)}:{node.lineno} imports {name}"
        for path in paths
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for name in _imported_names(node)
        if _is_forbidden(name)
    ]
    assert not offenders, "services/scraper/ must stay pure:\n  " + "\n  ".join(offenders)
