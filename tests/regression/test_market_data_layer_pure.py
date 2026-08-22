# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: the market-data layer stays free of DB / web / LLM coupling.

ADR-0091 places ``services/market_data/`` **parallel** to ``web_research/`` and
``voice/`` — never under ``services/analytics/`` — and makes provider-blindness
the whole point: the layer normalises provider responses into the canonical DTO
and nothing more. This guard, mirroring
``tests/regression/test_analytics_layer_pure.py``, machine-enforces that no
module under ``services/market_data/`` reaches for:

- SQLAlchemy or an async DB session (it must not persist — that is the ingest
  write path's job, a later slice, ADR-0092);
- FastAPI or the Qt surface;
- ``core.repositories`` / ``core.models`` DB machinery;
- an LLM / OpenRouter client (identifier resolution is deterministic OpenFIGI
  mapping, ADR-0090 — never a model).

Importing ``httpx``, the stdlib, ``yaml``, and the package's own ``dto`` is
fine.

Two complementary checks, as in the analytics guard: a source scan (anchored
import patterns + forbidden substrings) and a fresh-subprocess import that
asserts none of SQLAlchemy / FastAPI / PyQt6 land in ``sys.modules``. A third
test feeds a synthetic offending line to the scanner to prove the mechanism
actually catches a sneaked-in import.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_MARKET_DATA_ROOT: Path = _REPO_ROOT / "services" / "market_data"

# Anchored-start patterns: any stripped, non-comment line beginning with one of
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
    "from core.repositories",
    "import core.repositories",
    "from core.models",
    "import core.models",
    "import openai",
    "from openai",
    "import anthropic",
    "from anthropic",
)

# Case-sensitive substrings that must not appear in non-comment source lines.
_FORBIDDEN_CONTAINS: tuple[str, ...] = (
    "async_session",
    "AsyncSession",
    "get_db_session",
    "AsyncOpenAI",
    "OpenRouter",
    "openrouter",
)


def _scan_source(
    source: str,
    *,
    start_patterns: tuple[str, ...] = _FORBIDDEN_START_PATTERNS,
    contains: tuple[str, ...] = _FORBIDDEN_CONTAINS,
) -> list[tuple[int, str]]:
    """Return ``(lineno, line)`` for every offending line in ``source``.

    Comment lines are skipped; docstring text never begins with a forbidden
    import token, and none of the forbidden substrings occur in this package's
    prose.

    The two pattern sets are arguments with this module's own tuples as
    defaults, so a sibling guard over a *different* layer can reuse the
    mechanism rather than copy it — ``tests/regression/
    test_web_layer_has_no_market_data_provider_imports.py`` (ADR-0125) is the
    first such caller. Every call site here passes neither, so this module's
    behaviour is unchanged.

    Args:
        source: The module source to scan.
        start_patterns: Anchored-start tokens; a stripped, non-comment line
            beginning with one of these is a violation.
        contains: Case-sensitive substrings that must not appear in a
            non-comment source line.

    Returns:
        One ``(lineno, stripped_line)`` pair per offending line.
    """
    offenders: list[tuple[int, str]] = []
    for lineno, raw_line in enumerate(source.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue
        if any(stripped.startswith(p) for p in start_patterns):
            offenders.append((lineno, stripped))
            continue
        if any(sym in stripped for sym in contains):
            offenders.append((lineno, stripped))
    return offenders


def test_market_data_layer_has_no_forbidden_imports() -> None:
    """Source-level scan of every module under ``services/market_data/``."""
    assert _MARKET_DATA_ROOT.exists(), f"expected directory missing: {_MARKET_DATA_ROOT}"
    offenders: list[tuple[Path, int, str]] = []
    for python_file in _MARKET_DATA_ROOT.rglob("*.py"):
        for lineno, line in _scan_source(python_file.read_text(encoding="utf-8")):
            offenders.append((python_file, lineno, line))
    assert not offenders, (
        "ADR-0091 forbids DB / FastAPI / Qt / repository / LLM imports under "
        f"services/market_data/. Offending lines: {offenders}"
    )


def test_scanner_catches_a_sneaked_import() -> None:
    """The scanner must flag a sneaked-in SQLAlchemy import (mechanism proof).

    Demonstrates the guard would fail if someone added persistence to the
    market-data layer — the verification-gate self-check, run in-memory so the
    tree stays clean.
    """
    sneaky = (
        '"""docstring is fine."""\n'
        "import httpx  # allowed\n"
        "from sqlalchemy import select  # forbidden\n"
        "session = AsyncSession()  # forbidden\n"
    )
    offenders = _scan_source(sneaky)
    flagged = {line for _, line in offenders}
    assert "from sqlalchemy import select  # forbidden" in flagged
    assert "session = AsyncSession()  # forbidden" in flagged
    # The allowed httpx import and the docstring are not flagged.
    assert not any("import httpx" in line for line in flagged)


def test_market_data_import_is_db_web_and_qt_free() -> None:
    """A fresh ``import services.market_data`` pulls in no SQLAlchemy/FastAPI/Qt.

    The subprocess isolates the assertion from the parent pytest process, which
    has typically already imported these via other test modules.
    """
    code = (
        "import sys\n"
        "import services.market_data  # noqa: F401\n"
        "leaks = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m == 'sqlalchemy' or m.startswith('sqlalchemy.')\n"
        "    or m == 'fastapi' or m.startswith('fastapi.')\n"
        "    or m == 'PyQt6' or m.startswith('PyQt6.')\n"
        ")\n"
        "assert not leaks, f'forbidden modules leaked: {leaks}'\n"
        "print('OK')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_REPO_ROOT),
    )
    assert completed.returncode == 0, (
        f"subprocess failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    assert "OK" in completed.stdout
