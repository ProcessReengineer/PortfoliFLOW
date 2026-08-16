# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: ``web/`` may import ``modules`` only via ``module_registry``.

``docs/architecture.md`` and ``CLAUDE.md`` state the hard layering rule:
``web/`` imports from ``core/``, ``services/``, ``services/analytics/``, and
``modules.module_registry`` **only**. The web surface must never reach into a
concrete business module under ``modules/<area>/`` — shared logic belongs in
``services/`` so that both ``web/`` and the module shells can consume it
without ``web/`` depending on ``modules/`` internals.

Historically the sole breach was
``web/routes/data_import.py`` importing ``load_excel`` from
``modules.front_office.data_import``; A5 relocated that parser to
``services/data_normalization/excel_workbook_loader.py`` and added this guard
so the boundary cannot silently regress. Unlike the sibling layering rules
(no matplotlib in ``web/``, Qt-free ``bot/``/``core/``), this rule previously
had no regression guard.

The check is a source scan: walk every ``.py`` file under ``web/`` and reject
any line whose stripped form starts with ``from modules`` or ``import
modules``, except imports of ``modules.module_registry`` (the one permitted
seam). Comment lines are skipped so that prose mentioning a module path does
not trip the guard.

If this guard goes red, the fix is not to relax it: relocate the shared code
into ``services/`` and import it from there.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_WEB_ROOT: Path = _REPO_ROOT / "web"

# The two forbidden import-statement prefixes and the single permitted
# ``module_registry`` seam. A line is an offender when it starts with a
# forbidden prefix but not with a permitted one.
_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "from modules",
    "import modules",
)
_PERMITTED_PREFIXES: tuple[str, ...] = (
    "from modules.module_registry",
    "import modules.module_registry",
)


def test_web_imports_modules_only_via_module_registry() -> None:
    """Source-level scan for forbidden ``modules`` imports under ``web/``."""
    assert _WEB_ROOT.exists(), f"expected directory missing: {_WEB_ROOT}"

    offenders: list[tuple[Path, int, str]] = []
    for python_file in _WEB_ROOT.rglob("*.py"):
        for lineno, raw_line in enumerate(
            python_file.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            stripped = raw_line.strip()
            # Skip comments; the pattern check is anchored at the start of
            # the stripped line so docstring prose that merely mentions a
            # module path is not treated as an import statement.
            if stripped.startswith("#"):
                continue
            if not any(stripped.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES):
                continue
            if any(stripped.startswith(prefix) for prefix in _PERMITTED_PREFIXES):
                continue
            offenders.append((python_file, lineno, stripped))

    assert not offenders, (
        "web/ may import from modules only via modules.module_registry "
        "(docs/architecture.md, CLAUDE.md). Relocate shared code into "
        "services/ instead of importing a concrete module. Offending lines: "
        + "; ".join(f"{path}:{lineno}:{text}" for path, lineno, text in offenders)
    )
