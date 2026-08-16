# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: the analytics layer must stay DB-, FastAPI-, and Qt-free.

ADR-0013 establishes the analytics-layer purity contract: every public
function takes pandas / numpy inputs and returns plain Python data
structures, with no database, FastAPI, or PyQt6 coupling. The contract
applies to **both** analytics roots:

1. ``services/analytics/`` — the DB-aware foundation introduced in
   ADR-0045 §3 (five submodules: ``investment_returns``,
   ``statistics``, ``correlation``, ``efficient_frontier``,
   ``portfolio_aggregation``). Its files may import DTO dataclasses
   from ``core/repositories/*_repository.py`` modules; those
   repository files co-locate the DTO with the Repository class —
   which imports SQLAlchemy at top level. The fresh-subprocess check
   below deliberately permits ``sqlalchemy`` to land in
   ``sys.modules`` for this root, while the source scan still pins
   direct SQL-session usage in the analytics files themselves.
2. ``analytics/`` (top-level) — the algorithmic engines that are
   intentionally **stricter** per ADR-0013. They must not pull in
   SQLAlchemy, DTOs, FastAPI, or Qt at all. Kickoff #2's limit-
   coverage engine briefly lived here before Kickoff #3a relocated
   it to :mod:`services.analytics.limit_coverage` (the engine
   consumes repository DTOs, which belong under the more permissive
   ``services/analytics/`` root); the split-guard layout remains so
   future stricter engines (``portfolio_optimizer``, ``sample_window``)
   stay pinned.

Three complementary checks:

* **Source scan.** Walk every ``.py`` file under the root and reject
  any line whose stripped form starts with a forbidden import pattern
  (``import sqlalchemy``, ``from sqlalchemy``, ``import fastapi``,
  ``from fastapi``, ``from PyQt6``, ``import PyQt6``, ``from gui.``,
  ``import gui``) or contains an SQL-session sentinel
  (``async_session``, ``AsyncSession``, ``get_db_session``). Mirrors
  the anchored-start pattern used by
  ``test_no_matplotlib_in_web.py`` — comments are skipped; docstring
  mentions never start with these tokens.
* **Fresh-subprocess import.** Import the analytics root in a clean
  subprocess and assert that no ``fastapi`` or ``PyQt6`` module ends
  up in :data:`sys.modules`. The subprocess form isolates the
  assertion from the parent pytest process, which has typically
  already imported these via other test modules. The check is split
  into two dedicated tests — one per root — so that a failure
  points at exactly one namespace.
* **Type-blindness scan (ADR-0103 §8).** Walk the AST of every file
  under the root and reject any ``investment_type`` identifier or any
  ``'cash'`` string constant. The cash exclusion from the efficient-
  frontier universe lives at the data-assembly seam; pure analytics
  must never learn what an investment type is. See
  :func:`test_analytics_layer_is_investment_type_blind`.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_ANALYTICS_ROOTS: tuple[Path, ...] = (_REPO_ROOT / "services" / "analytics",)

# Anchored-start patterns: any stripped line beginning with one of
# these is a violation.
_FORBIDDEN_START_PATTERNS: tuple[str, ...] = (
    "import sqlalchemy",
    "from sqlalchemy",
    "import fastapi",
    "from fastapi",
    "from PyQt6",
    "import PyQt6",
    "from gui.",
    "import gui",
)

# Case-sensitive substrings that must not appear in non-comment
# source lines, regardless of position.
_FORBIDDEN_CONTAINS: tuple[str, ...] = (
    "async_session",
    "AsyncSession",
    "get_db_session",
)


def test_analytics_layer_has_no_db_imports() -> None:
    """Source-level scan for forbidden DB / FastAPI / Qt imports.

    Covers both ``services/analytics/`` and the top-level
    ``analytics/`` package (ADR-0013).
    """
    offenders: list[tuple[Path, int, str]] = []
    for analytics_root in _ANALYTICS_ROOTS:
        assert analytics_root.exists(), f"expected directory missing: {analytics_root}"
        for python_file in analytics_root.rglob("*.py"):
            for lineno, raw_line in enumerate(
                python_file.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                stripped = raw_line.strip()
                if stripped.startswith("#"):
                    continue
                if any(stripped.startswith(pattern) for pattern in _FORBIDDEN_START_PATTERNS):
                    offenders.append((python_file, lineno, stripped))
                    continue
                if any(symbol in stripped for symbol in _FORBIDDEN_CONTAINS):
                    offenders.append((python_file, lineno, stripped))
    assert not offenders, (
        "ADR-0013 / ADR-0045 §3 forbid DB, FastAPI, or Qt imports under "
        "the analytics layer. Offending lines: "
        f"{offenders}"
    )


def _assert_fresh_import_clean(module_name: str) -> None:
    """Import ``module_name`` in a subprocess and assert no Qt/FastAPI leak.

    Args:
        module_name: Dotted module path to import in the fresh
            subprocess (e.g. ``'services.analytics'`` or
            ``'analytics'``).

    Raises:
        AssertionError: If the subprocess exits non-zero or reports
            FastAPI / PyQt6 modules in :data:`sys.modules` after the
            import.
    """
    code = (
        "import sys\n"
        f"import {module_name}  # noqa: F401\n"
        "leaks = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m == 'fastapi' or m.startswith('fastapi.')\n"
        "    or m == 'PyQt6' or m.startswith('PyQt6.')\n"
        ")\n"
        "assert not leaks, (\n"
        f"    'forbidden modules leaked into {module_name} import graph: '\n"
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
        f"subprocess failed for {module_name}:\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )
    assert "OK" in completed.stdout


def test_services_analytics_imports_without_db_or_qt_or_fastapi() -> None:
    """A fresh ``import services.analytics`` must stay FastAPI- and Qt-free.

    ``sqlalchemy`` is deliberately **not** asserted against here —
    see the module docstring.
    """
    _assert_fresh_import_clean("services.analytics")


def test_analytics_layer_is_investment_type_blind() -> None:
    """The analytics layer must not branch on investment type (ADR-0103 §8).

    ADR-0103 §8 excludes cash from the efficient-frontier universe at
    the *data-assembly* seam
    (:meth:`services.portfolio_analysis.PortfolioAnalysisService._resolve_universe`),
    never inside pure analytics: the optimiser optimises over whatever
    matrix it is handed, and choosing that matrix is assembly's job.
    ADR-0013 / ADR-0045 make the same point structurally — analytics
    takes pandas/numpy in and gives plain data out, so it has no
    business knowing that ``'cash'`` is a discriminator value.

    This is the grep-level form of that sentence, so the exclusion can
    never quietly migrate down into the pure layer. The check is
    AST-based rather than a raw substring scan, because prose in
    docstrings legitimately discusses cash flows, "Cash" asset classes,
    and residual cash (see :mod:`services.analytics.limit_coverage`,
    :mod:`services.analytics.benchmark_comparison`). What is forbidden
    is *code* that is type-aware:

    * any identifier named ``investment_type``, and
    * any string constant equal to ``'cash'`` (a type-discriminator
      literal; a docstring's value is never exactly that word).
    """
    offenders: list[str] = []
    for analytics_root in _ANALYTICS_ROOTS:
        assert analytics_root.exists(), f"expected directory missing: {analytics_root}"
        for python_file in analytics_root.rglob("*.py"):
            tree = ast.parse(
                python_file.read_text(encoding="utf-8"),
                filename=str(python_file),
            )
            rel = python_file.relative_to(_REPO_ROOT)
            for node in ast.walk(tree):
                identifier: str | None = None
                if isinstance(node, ast.Name):
                    identifier = node.id
                elif isinstance(node, ast.Attribute):
                    identifier = node.attr
                elif isinstance(node, (ast.arg, ast.keyword)):
                    identifier = node.arg
                if identifier == "investment_type":
                    offenders.append(f"{rel}:{node.lineno}: identifier 'investment_type'")
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value.strip().lower() == "cash"
                ):
                    offenders.append(f"{rel}:{node.lineno}: string constant 'cash'")
    assert not offenders, (
        "ADR-0013 / ADR-0045 / ADR-0103 §8: the pure analytics layer must "
        "stay blind to investment types — the cash exclusion belongs at the "
        "data-assembly seam (PortfolioAnalysisService._resolve_universe), "
        f"not here. Offending nodes: {offenders}"
    )
