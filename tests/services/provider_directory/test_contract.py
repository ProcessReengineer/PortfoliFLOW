# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""What the arming layer is allowed to reach (SB-3b).

:mod:`services.provider_channel` is pure because trust must not depend on
where bytes came from. This package is the opposite half — it exists to reach
the network and the filesystem — so its contract cannot be "reaches nothing".
It is narrower and more specific: HTTP and files, the pure package it feeds,
and nothing else. No settings, no ORM, no web framework, no ticket world, and
above all no clock, because every date in the channel is an argument and a
function that reads one quietly makes a test suite expire.

Three groups:

* **A-1** the import graph, observed in a fresh interpreter;
* **A-2** the source scan, which catches what an unexercised branch would
  hide from A-1;
* **A-3** the status vocabulary, pinned in order because the CLI (PB-1g) and
  the surfaces (SB-6) both branch on it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

from services.provider_directory.refresh import REFRESH_STATUSES

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_PACKAGE_ROOT: Final[Path] = _REPO_ROOT / "services" / "provider_directory"

#: What the package may never pull in. ``services.market_data`` joins the list
#: because it is the other async HTTP client in the tree: the day someone
#: reaches for its retry helpers, the directory starts depending on a
#: provider-blind market-data package for its trust path.
_FORBIDDEN_MODULE_PREFIXES: Final[tuple[str, ...]] = (
    "core",
    "sqlalchemy",
    "fastapi",
    "pydantic",
    "services.transactions",
    "services.market_data",
)

#: What the package must pull in — the assertion asked the other way round, so
#: a refactor that quietly stops verifying, or that swaps the HTTP client for
#: something undeclared, is a test failure rather than a silent fact.
_REQUIRED_MODULES: Final[tuple[str, ...]] = ("httpx", "services.provider_channel")

_FORBIDDEN_START_PATTERNS: Final[tuple[str, ...]] = (
    "from core.",
    "import core",
    "from sqlalchemy",
    "import sqlalchemy",
    "from fastapi",
    "import fastapi",
    "from services.transactions",
    "import socket",
    "import urllib",
    "from urllib",
)

#: No clock. ``now`` is an argument everywhere in this package, as it is in
#: the pure one (D-clock).
_FORBIDDEN_CONTAINS: Final[tuple[str, ...]] = (
    "datetime.now",
    "date.today",
    "time.time",
)

_DELTA_SCRIPT: Final[str] = (
    "import sys\n"
    "before = set(sys.modules)\n"
    "import services.provider_directory  # noqa: F401\n"
    "print(' '.join(sorted(set(sys.modules) - before)))\n"
)


# ---------------------------------------------------------------------------
# A-1 — the import graph
# ---------------------------------------------------------------------------


def test_the_import_graph_reaches_http_and_the_pure_package_and_nothing_forbidden() -> None:
    """A-1: a fresh import pulls in HTTP and the trust gate, no settings and no ORM.

    Run in a subprocess because the delta is only honest in an interpreter
    that has not already imported half the application. The idiom is copied
    from ``tests/services/provider_channel/test_contract.py`` rather than
    imported: a private helper reaching across test packages is exactly the
    coupling these tests exist to forbid.

    The delta is printed because it is the thing to keep an eye on — a new
    transitive dependency shows up here first.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _DELTA_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=_REPO_ROOT,
    )
    assert completed.returncode == 0, (
        f"subprocess failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )

    delta = completed.stdout.split()
    print(f"sys.modules delta after importing services.provider_directory: {delta}")

    for required in _REQUIRED_MODULES:
        assert required in delta, (
            f"{required} is part of what this package is for; importing it did not "
            f"pull it in. Observed: {delta}"
        )
    leaks = sorted(
        module
        for module in delta
        if any(
            module == prefix or module.startswith(prefix + ".")
            for prefix in _FORBIDDEN_MODULE_PREFIXES
        )
    )
    assert not leaks, (
        "the arming layer reads no setting, opens no database and knows nothing "
        f"about tickets; these leaked into the import graph: {leaks}"
    )


# ---------------------------------------------------------------------------
# A-2 — the source scan
# ---------------------------------------------------------------------------


def test_the_source_reaches_no_settings_no_orm_and_no_clock() -> None:
    """A-2: the anchored-start source scan, mirroring the analytics purity guard.

    A-1 only sees what an import executes. This sees every line, which is what
    catches the lazy import inside the error branch nobody runs.
    """
    assert _PACKAGE_ROOT.exists(), f"expected directory missing: {_PACKAGE_ROOT}"
    offenders: list[tuple[str, int, str]] = []
    for python_file in sorted(_PACKAGE_ROOT.rglob("*.py")):
        for lineno, raw_line in enumerate(
            python_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = raw_line.strip()
            if stripped.startswith("#"):
                continue
            if any(stripped.startswith(pattern) for pattern in _FORBIDDEN_START_PATTERNS):
                offenders.append((python_file.name, lineno, stripped))
                continue
            if any(symbol in stripped for symbol in _FORBIDDEN_CONTAINS):
                offenders.append((python_file.name, lineno, stripped))
    assert not offenders, (
        "services/provider_directory reaches HTTP and the filesystem and nothing "
        f"else — no core.config, no ORM, no clock. Offending lines: {offenders}"
    )


# ---------------------------------------------------------------------------
# A-3 — the status vocabulary
# ---------------------------------------------------------------------------


def test_the_refresh_statuses_are_exactly_these_eight_in_this_order() -> None:
    """A-3: the vocabulary the CLI and the surfaces branch on, pinned.

    Order is part of the contract: these are listed best-to-worst, and a
    renderer that walks them is entitled to that reading.
    """
    assert REFRESH_STATUSES == (
        "updated",
        "unchanged",
        "refused_downgrade",
        "refused_republished",
        "refused_unknown_version",
        "refused_unknown_key",
        "refused_invalid",
        "unavailable",
    )
    assert len(set(REFRESH_STATUSES)) == len(REFRESH_STATUSES)
