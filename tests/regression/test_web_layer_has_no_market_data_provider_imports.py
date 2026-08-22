# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: the web layer holds no market-data provider dependency.

ADR-0093 draws a hard line at the request boundary: "Refresh now" and every
other market-data affordance in ``web/`` may only move a *schedule row*
(``next_due_at := now``) through :class:`MarketDataScheduleRepository`. The
fetching itself belongs to the tick — the built-in scheduler or the external
timer (ADR-0117) — because a provider call inside a request would block the
async web layer on third-party network I/O it does not control.

ADR-0125 §6 gave that gate a second route to cover. The Front-Office Overview
now reads the same schedule row for its freshness line, and its owner-gated
"Refresh" posts to the same enqueue endpoint; the ADR states plainly that the
Overview route "still imports neither the refresh core nor any adapter
(ADR-0093 verification gate extends to this route)". Both routers document
that constraint in prose. This guard is what makes it a *fact*: a future edit
that reaches for ``YahooAdapter`` or ``refresh_tenant_live_data`` to make a
refresh feel synchronous fails here rather than in production, under load.

What is forbidden is the **provider machinery**, not the package:

- ``services.market_data.adapters`` — the concrete provider clients;
- ``services.market_data.factory`` — the routing that hands one out;
- ``services.investments.live_refresh`` — the per-tenant refresh core.

``services.market_data.provider`` — the port itself: protocols, DTO-adjacent
types and the declared exceptions — stays allowed, and is imported today by
``web/routes/provider_credentials.py`` for
:class:`UnsupportedCapabilityError`. Naming an exception the credential
surface must catch is not holding a provider dependency; it is the port doing
its job (ADR-0091).

The scanner is imported from ``test_market_data_layer_pure.py`` rather than
copied — one mechanism, two pattern sets — and a self-test feeds it a
synthetic offending module to prove it would actually catch a sneaked-in
import, exactly as the sibling guard does.
"""

from __future__ import annotations

from pathlib import Path

from tests.regression.test_market_data_layer_pure import _scan_source

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_WEB_ROOT: Path = _REPO_ROOT / "web"

# Anchored-start patterns: any stripped, non-comment line beginning with one of
# these is a violation. Both import spellings of each forbidden module, plus
# the bare-package form of the refresh core.
_FORBIDDEN_START_PATTERNS: tuple[str, ...] = (
    "from services.market_data.adapters",
    "import services.market_data.adapters",
    "from services.market_data.factory",
    "import services.market_data.factory",
    "from services.investments.live_refresh",
    "import services.investments.live_refresh",
)

# No substring rule: the routers legitimately *name* the refresh core and the
# adapters in their docstrings to say they do not import them, and a substring
# check would flag exactly that prose. The anchored import patterns above are
# the whole guard, which is what makes it safe to state the constraint in
# words directly above the code that honours it.
_FORBIDDEN_CONTAINS: tuple[str, ...] = ()


def _scan_web_layer() -> list[tuple[Path, int, str]]:
    """Return every offending ``(file, lineno, line)`` under ``web/``."""
    offenders: list[tuple[Path, int, str]] = []
    for python_file in _WEB_ROOT.rglob("*.py"):
        found = _scan_source(
            python_file.read_text(encoding="utf-8"),
            start_patterns=_FORBIDDEN_START_PATTERNS,
            contains=_FORBIDDEN_CONTAINS,
        )
        for lineno, line in found:
            offenders.append((python_file, lineno, line))
    return offenders


def test_web_layer_imports_no_provider_adapter_or_refresh_core() -> None:
    """Source-level scan of every module under ``web/`` (ADR-0093, ADR-0125 §6)."""
    assert _WEB_ROOT.exists(), f"expected directory missing: {_WEB_ROOT}"
    offenders = _scan_web_layer()
    assert not offenders, (
        "ADR-0093 forbids provider adapters, the provider factory and the "
        "refresh core in the web layer — a refresh is enqueued, never run in "
        f"the request (ADR-0125 §6). Offending lines: {offenders}"
    )


def test_overview_and_market_data_routers_are_covered_by_the_scan() -> None:
    """The two routes the ADRs name are actually inside the scanned set.

    A guard that silently stops covering its subject is worse than none: if
    either router were renamed or moved out of ``web/``, the scan above would
    keep passing while guarding nothing. Pin the membership.
    """
    scanned = {path.relative_to(_REPO_ROOT).as_posix() for path in _WEB_ROOT.rglob("*.py")}
    assert "web/routes/market_data.py" in scanned
    assert "web/routes/overview.py" in scanned


def test_scanner_catches_a_sneaked_provider_import() -> None:
    """The scanner must flag a sneaked-in adapter / refresh-core import.

    The verification-gate self-check, run in-memory so the tree stays clean:
    it demonstrates the guard would fail if someone made "Refresh now" fetch
    synchronously, and — the other half — that the allowed port import and
    the routers' own prose about the forbidden modules are *not* flagged.
    """
    sneaky = (
        '"""This module imports no services.investments.live_refresh."""\n'
        "from services.market_data.provider import UnsupportedCapabilityError  # allowed\n"
        "from services.market_data.adapters.yahoo import YahooAdapter  # forbidden\n"
        "from services.investments.live_refresh import refresh_tenant_live_data  # forbidden\n"
        "from services.market_data.factory import build_provider  # forbidden\n"
    )
    offenders = _scan_source(
        sneaky,
        start_patterns=_FORBIDDEN_START_PATTERNS,
        contains=_FORBIDDEN_CONTAINS,
    )
    flagged = {line for _, line in offenders}
    assert "from services.market_data.adapters.yahoo import YahooAdapter  # forbidden" in flagged
    assert (
        "from services.investments.live_refresh import refresh_tenant_live_data  # forbidden"
        in flagged
    )
    assert "from services.market_data.factory import build_provider  # forbidden" in flagged
    # The port import stays legal, and prose naming a forbidden module is not
    # an import — otherwise the routers could not document their own gate.
    assert not any("UnsupportedCapabilityError" in line for line in flagged)
    assert not any("docstring" in line or '"""' in line for line in flagged)
