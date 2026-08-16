# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: the Takahashi–Alexander generator is import-pure (ADR-0105 §1).

ADR-0105 §1 makes import-purity the *binding* constraint on the TA module: it
"provides one entry point … import-pure like the existing ``services/investments``
computation submodules, no DB, no web, no Qt." The generator is a pure engine
that takes values and returns flows; it must be consultable from the plan-world
assembly seam (S34.7) without dragging a repository, a session, FastAPI, or Qt
into that seam's import graph.

**A sibling guard, not an extra analytics root.** ``test_analytics_layer_pure.py``
walks ``services/analytics/`` only, and its ADR-0103 §8 type-blindness scan
forbids any ``investment_type`` identifier — which the TA generator legitimately
takes as a parameter and dispatches its constants on. Folding the TA module into
that guard's roots would make the two checks contradict each other. So this is a
**sibling** mirroring the source-scan + fresh-subprocess pattern of
``test_analytics_layer_pure.py`` and ``test_overlay_layer_pure.py``, scoped to
the two TA modules and carrying **no** type-blindness scan (the generator's
whole job is to key parameters off the capital-account ``investment_type``).

Three complementary checks, over ``services/investments/ta_profile.py`` and
``services/investments/ta_profile_constants.py``:

* **Source scan.** Every line is rejected if it begins with a forbidden import
  prefix (SQLAlchemy, ``core.repositories``, FastAPI, PyQt6, ``gui``) or
  contains an SQL-session sentinel. The ``TYPE_CHECKING``-only import of
  ``Periodisation`` from the (impure) ``cash_flow_timeline`` module is *not* a
  runtime import and does not match any forbidden prefix; the fresh-subprocess
  check below is what proves it never actually loads at runtime.
* **Fresh-subprocess import.** ``import services.investments.ta_profile`` in a
  clean interpreter must leave no SQLAlchemy, no ``core.repositories``, no
  FastAPI and no PyQt6 in :data:`sys.modules`. This is the machine proof that
  the ``TYPE_CHECKING`` type hint and the reuse of the (book-free) overlay
  package's ``PlanFlow`` keep the module genuinely pure.
* **Mechanism proof.** A synthetic offending source is fed to the scanner, so a
  guard that silently stopped catching anything fails instead.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_TA_MODULES: tuple[Path, ...] = (
    _REPO_ROOT / "services" / "investments" / "ta_profile.py",
    _REPO_ROOT / "services" / "investments" / "ta_profile_constants.py",
)

# Anchored-start patterns: any stripped, non-comment line beginning with one of
# these is a violation. The overlay guard's set — the analytics guard's, plus
# the repository namespace the pure module may not reach.
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

# Case-sensitive substrings that must not appear in non-comment source lines.
_FORBIDDEN_CONTAINS: tuple[str, ...] = (
    "async_session",
    "AsyncSession",
    "get_db_session",
)


def _scan_source(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, line)`` for every offending line in ``source``.

    Comment lines are skipped; no docstring in these modules begins with a
    forbidden import token, and none of the forbidden substrings occurs in
    their prose.
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


def test_ta_modules_have_no_forbidden_imports() -> None:
    """Source-level scan of both TA modules for DB / web / Qt coupling."""
    offenders: list[tuple[Path, int, str]] = []
    for module_path in _TA_MODULES:
        assert module_path.exists(), f"expected module missing: {module_path}"
        for lineno, line in _scan_source(module_path.read_text(encoding="utf-8")):
            offenders.append((module_path, lineno, line))
    assert not offenders, (
        "ADR-0105 §1 makes the TA generator import-pure: no DB, repository, "
        "FastAPI, or Qt coupling may enter the ta_profile modules. Offending "
        f"lines: {offenders}"
    )


def test_scanner_catches_a_sneaked_import() -> None:
    """The scanner must flag sneaked-in DB coupling (mechanism proof)."""
    sneaky = (
        '"""docstring is fine."""\n'
        "from decimal import Decimal  # allowed\n"
        "from core.repositories.investment_repository import X  # forbidden\n"
        "from sqlalchemy import select  # forbidden\n"
        "session = AsyncSession()  # forbidden\n"
    )
    flagged = {line for _, line in _scan_source(sneaky)}
    assert any("core.repositories" in line for line in flagged)
    assert any("from sqlalchemy import select" in line for line in flagged)
    assert any("AsyncSession()" in line for line in flagged)
    assert not any("from decimal import" in line for line in flagged)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter rooted at the repository."""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_REPO_ROOT),
    )


#: The leak probe: an ``import services.investments.ta_profile`` must reach
#: neither the book (SQLAlchemy, ``core.repositories``) nor a user surface
#: (FastAPI, PyQt6). The ``Periodisation`` type hint is ``TYPE_CHECKING``-only
#: and never executes; the reused ``PlanFlow`` lives in the book-free overlay
#: package.
_LEAK_PROBE = (
    "import sys\n"
    "import services.investments.ta_profile  # noqa: F401\n"
    "leaks = sorted(\n"
    "    m for m in sys.modules\n"
    "    if m == 'sqlalchemy' or m.startswith('sqlalchemy.')\n"
    "    or m == 'core.repositories' or m.startswith('core.repositories.')\n"
    "    or m == 'fastapi' or m.startswith('fastapi.')\n"
    "    or m == 'PyQt6' or m.startswith('PyQt6.')\n"
    ")\n"
    "assert not leaks, f'forbidden modules leaked: {leaks}'\n"
    "print('OK')\n"
)


def test_ta_import_is_db_web_and_qt_free() -> None:
    """A fresh ``import services.investments.ta_profile`` reaches no DB/web/Qt.

    The machine proof of ADR-0105 §1: importing the generator does not even
    load the code that could reach the book. The subprocess isolates the
    assertion from the parent pytest process, which has typically already
    imported these via other test modules.
    """
    completed = _run(_LEAK_PROBE)
    assert completed.returncode == 0, (
        f"subprocess failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    assert "OK" in completed.stdout
