# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: the lower layers never import the layers above them.

``AGENTS.md`` ("Dependency rules") and ``docs/architecture.md`` state the
rules this module pins:

* ``core/`` imports nothing from within the project.
* ``services/`` imports only from ``core/`` and other ``services/``
  packages — never from ``cli/``, ``web/``, ``modules/`` or ``bot/``.
* ``bot/`` imports only from ``core/`` and ``services/`` (ADR-0030).

The check walks the syntax tree of every ``.py`` file under each root and
inspects every ``import`` and ``from … import`` statement, wherever it
sits: at module level, inside a function, or under ``if TYPE_CHECKING``.
A function-local import is still a dependency, and a lazy import that
hides a cycle is the pattern ``AGENTS.md`` forbids. Relative imports stay
inside their own package and are not checked. Prose in comments and
docstrings is not code and does not trip the guard.

If this guard goes red, the fix is not to relax it: move the shared code
down into ``services/`` (or ``core/``) and import it from there, as
``services/tenant_defaults.py`` did for the per-tenant default installers
that ``services/super_admin/operations.py`` once imported from
``cli.bootstrap``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]

_PROJECT_PACKAGES: frozenset[str] = frozenset(
    {"bot", "cli", "core", "db", "modules", "scripts", "services", "tests", "tools", "web"}
)

#: Root directory → the project packages it may import (itself included).
_ALLOWED: dict[str, frozenset[str]] = {
    "core": frozenset({"core"}),
    "services": frozenset({"core", "services"}),
    "bot": frozenset({"bot", "core", "services"}),
}


def _imported_packages(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module.split(".")[0]]
    return []


def _offenders(root: str) -> list[str]:
    allowed = _ALLOWED[root]
    found: list[str] = []
    for path in sorted((_REPO_ROOT / root).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for package in _imported_packages(node):
                if package in _PROJECT_PACKAGES and package not in allowed:
                    relative = path.relative_to(_REPO_ROOT)
                    found.append(f"{relative}:{node.lineno} imports {package}")
    return found


@pytest.mark.parametrize("root", sorted(_ALLOWED))
def test_layer_imports_only_what_it_may(root: str) -> None:
    assert (_REPO_ROOT / root).is_dir(), f"expected directory missing: {root}/"
    offenders = _offenders(root)
    assert not offenders, (
        f"{root}/ may import only {sorted(_ALLOWED[root])} from the project:\n  "
        + "\n  ".join(offenders)
    )
