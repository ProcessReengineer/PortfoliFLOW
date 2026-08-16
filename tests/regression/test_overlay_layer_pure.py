# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: the overlay layer is ephemeral — no DB, no web, no Qt.

ADR-0104 §1 splits the Planning Desk in two layers: the **book** (persisted)
and the **overlay** (ephemeral). Nothing in ``services/overlay/`` may read or
write the book. That is not a coding-style preference — it is the load-bearing
claim of the architecture: *if nothing persists, nothing can leak into the
book*, so annex §B.2/§B.3 (no scenario row ever reaches
``position_transactions``, ``investment_navs``, or ``instrument_prices``)
become properties of the structure rather than disciplines of the
implementer. This guard is what makes that claim checkable, and it is meant
to outlive every strand of the Planning Desk.

The guard mirrors ``tests/regression/test_analytics_layer_pure.py`` and
``tests/regression/test_market_data_layer_pure.py``:

* **Source scan.** Every ``.py`` under ``services/overlay/`` is scanned for
  forbidden import prefixes and SQL-session sentinels. The forbidden set is
  the analytics guard's, plus ``core.repositories`` /
  ``core.persistent_data_store`` — the analytics layer is *permitted* to
  import repository modules for their DTO dataclasses (ADR-0045 §3), the
  overlay layer is not: it takes frames, not rows.
* **Fresh-subprocess import.** ``import services.overlay`` in a clean
  interpreter must leave no SQLAlchemy, no ``core.repositories``, no FastAPI
  and no PyQt6 module in :data:`sys.modules`. This is the *machine proof* of
  ADR-0104 §1: not merely that no overlay module names the book, but that the
  overlay cannot even reach code that could.
* **Mechanism proof.** A synthetic offending source is fed to the scanner, so
  a guard that silently stopped catching anything fails instead.

**The subprocess check is strict again (S2.1c).** ADR-0104 §2 *requires* the
executors to import :func:`services.investments.archetype.resolve_archetype`
and
:data:`services.investments.flow_type_invariants.OVERLAY_EXEMPT_FLOW_TYPES` —
the single formulations of archetype dispatch and of the ADR-0103 §5 exemption
invariant, which must never be restated locally. Both modules are themselves
import-pure (stdlib only; ``tests/regression/test_flow_type_invariants_pure.py``
pins it), but importing either used to execute an eager
``services/investments/__init__.py``, which re-exported the DB-coupled service
modules and so pulled ``core.repositories`` — and with it SQLAlchemy — into
:data:`sys.modules`. That leak was the *package façade's*, not the overlay's,
and it forced a temporary relaxation of the leak set in S2.1b. Since S2.1c the
façade is lazy (PEP 562 ``__getattr__``), the mandated seams import without
their DB-coupled neighbours, and the strict leak set is restored here.
``tests/regression/test_investments_facade_lazy.py`` pins that property at its
source, so a future eager import in the façade fails there as well as here.

**Why a separate guard rather than a third analytics root.** The overlay
package deliberately lives *outside* ``services/analytics/``: its executors
dispatch on :func:`services.investments.archetype.resolve_archetype`, and the
ADR-0103 §8 type-blindness scan in the analytics guard forbids
``investment_type`` semantics inside that layer. Extending the analytics roots
to cover the overlay would therefore make the two guards contradict each
other. This guard consequently carries **no** type-blindness scan — archetype
dispatch is the overlay's job, by ADR-0104 §2 — while keeping the purity
scan strictly stricter than analytics' own.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_OVERLAY_ROOT: Path = _REPO_ROOT / "services" / "overlay"

# Anchored-start patterns: any stripped, non-comment line beginning with one
# of these is a violation. The analytics guard's set, plus the repository and
# persistent-data-store namespaces (ADR-0104 §1: the overlay consumes frames,
# never rows).
_FORBIDDEN_START_PATTERNS: tuple[str, ...] = (
    "import sqlalchemy",
    "from sqlalchemy",
    "import fastapi",
    "from fastapi",
    "from PyQt6",
    "import PyQt6",
    "from gui.",
    "import gui",
    "from core.repositories",
    "import core.repositories",
    "from core.persistent_data_store",
    "import core.persistent_data_store",
)

# Case-sensitive substrings that must not appear in non-comment source lines,
# regardless of position.
_FORBIDDEN_CONTAINS: tuple[str, ...] = (
    "async_session",
    "AsyncSession",
    "get_db_session",
)


def _scan_source(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, line)`` for every offending line in ``source``.

    Comment lines are skipped; no docstring in this package begins with a
    forbidden import token, and none of the forbidden substrings occurs in
    its prose.
    """
    offenders: list[tuple[int, str]] = []
    for lineno, raw_line in enumerate(source.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue
        if any(stripped.startswith(p) for p in _FORBIDDEN_START_PATTERNS):
            offenders.append((lineno, stripped))
            continue
        if any(symbol in stripped for symbol in _FORBIDDEN_CONTAINS):
            offenders.append((lineno, stripped))
    return offenders


def test_overlay_layer_has_no_forbidden_imports() -> None:
    """Source-level scan of every module under ``services/overlay/``."""
    assert _OVERLAY_ROOT.exists(), f"expected directory missing: {_OVERLAY_ROOT}"
    offenders: list[tuple[Path, int, str]] = []
    for python_file in _OVERLAY_ROOT.rglob("*.py"):
        for lineno, line in _scan_source(python_file.read_text(encoding="utf-8")):
            offenders.append((python_file, lineno, line))
    assert not offenders, (
        "ADR-0104 §1 makes the overlay ephemeral: no DB, repository, "
        "FastAPI, or Qt coupling may enter services/overlay/. Offending "
        f"lines: {offenders}"
    )


def test_scanner_catches_a_sneaked_import() -> None:
    """The scanner must flag sneaked-in DB coupling (mechanism proof)."""
    sneaky = (
        '"""docstring is fine."""\n'
        "import pandas as pd  # allowed\n"
        "from core.repositories.investment_repository import X  # forbidden\n"
        "from sqlalchemy import select  # forbidden\n"
        "session = AsyncSession()  # forbidden\n"
    )
    flagged = {line for _, line in _scan_source(sneaky)}
    assert any("core.repositories" in line for line in flagged)
    assert any("from sqlalchemy import select" in line for line in flagged)
    assert any("AsyncSession()" in line for line in flagged)
    assert not any("import pandas" in line for line in flagged)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter rooted at the repository."""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_REPO_ROOT),
    )


#: The leak probe, parameterised by the module to import. The set is strict
#: again (S2.1c): an overlay import must reach neither the book (SQLAlchemy,
#: ``core.repositories``) nor a user surface (FastAPI, PyQt6). The ADR-0104 §2
#: archetype and exemption seams are importable without their DB-coupled
#: neighbours because ``services/investments/__init__.py`` is a lazy PEP 562
#: façade.
_LEAK_PROBE = (
    "import sys\n"
    "import {module}  # noqa: F401\n"
    "leaks = sorted(\n"
    "    m for m in sys.modules\n"
    "    if m == 'sqlalchemy' or m.startswith('sqlalchemy.')\n"
    "    or m == 'core.repositories' or m.startswith('core.repositories.')\n"
    "    or m == 'fastapi' or m.startswith('fastapi.')\n"
    "    or m == 'PyQt6' or m.startswith('PyQt6.')\n"
    ")\n"
    "assert not leaks, f'forbidden modules leaked: {{leaks}}'\n"
    "print('OK')\n"
)


def test_overlay_import_is_db_web_and_qt_free() -> None:
    """A fresh ``import services.overlay`` reaches no DB, no web and no Qt.

    This is the machine proof of ADR-0104 §1: not merely that no overlay
    module *names* the book, but that importing the overlay does not even load
    the code that could reach it. The subprocess isolates the assertion from
    the parent pytest process, which has typically already imported these via
    other test modules.
    """
    completed = _run(_LEAK_PROBE.format(module="services.overlay"))
    assert completed.returncode == 0, (
        f"subprocess failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    assert "OK" in completed.stdout
