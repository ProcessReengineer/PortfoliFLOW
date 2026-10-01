# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: nothing under ``web/`` may import ``PersistentDataStore``.

ADR-0041 gives the web layer its own persistence entry point: FastAPI
routes go through the repository layer (``UserRepository`` and the
per-domain repositories), never through a ``DataStore``.

The Phase-1 ``PersistentDataStore`` (Postgres-backed ``DataStore``
subclass) has no runtime caller and is reached only from tests; it is
**not** the right abstraction for the web. Routing FastAPI through
``PersistentDataStore`` would force-fit a ``DataStore``-shaped
``store(name, df)`` API onto a surface that should be
repository-flavoured.

This test walks every ``.py`` file under ``web/`` and asserts that no
``import core.persistent_data_store`` or ``from
core.persistent_data_store import ...`` line exists. If it goes red,
the fix is to remove the offending import and route the route handler
through a repository instead — see ``UserRepository`` for the pattern.
"""

from __future__ import annotations

from pathlib import Path

_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "from core.persistent_data_store",
    "import core.persistent_data_store",
)

_WEB_ROOT: Path = Path(__file__).resolve().parents[2] / "web"


def test_web_does_not_import_persistent_data_store() -> None:
    """Walk ``web/`` and reject any ``PersistentDataStore`` import."""
    assert _WEB_ROOT.exists(), f"web/ directory not found at {_WEB_ROOT}"

    offenders: list[tuple[Path, int, str]] = []
    for python_file in _WEB_ROOT.rglob("*.py"):
        for lineno, raw_line in enumerate(
            python_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = raw_line.strip()
            if any(pattern in stripped for pattern in _FORBIDDEN_PATTERNS):
                offenders.append((python_file, lineno, stripped))

    assert not offenders, (
        f"ADR-0041 forbids PersistentDataStore imports under web/. Offending lines: {offenders}"
    )
