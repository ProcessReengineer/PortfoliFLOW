# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: holdings derivation is pure (ADR-0097 §4).

ADR-0097 §4 places holdings derivation in a pure, DB-free module
(``services/investments/holdings.py``) that mirrors the pure-predicate
precedent of ``services/investments/market_linked.py`` but is stricter:
it imports only the standard library and operates on a structural
protocol, so it takes no dependency on the repository, the ORM, a DB
session, a network client, or FastAPI. The natural companion to
``tests/regression/test_cashflow_dedup_key_pure.py``.

This test machine-enforces two properties:

1. **Purity of the source.** No import line in the module names a DB /
   ORM / network / web dependency. A derivation that read from a DB row
   or a session would be neither pure nor reproducible.
2. **Determinism.** The same ledger always derives the same holdings,
   regardless of input iteration order (the total order
   ``(trade_date, created_at, id)`` makes it reproducible).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from services.investments.holdings import derive_holdings

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_HOLDINGS_MODULE: Path = _REPO_ROOT / "services" / "investments" / "holdings.py"

# Tokens that would betray a DB / ORM / network / web dependency inside the
# pure derivation. None may appear as an import in its source.
_FORBIDDEN_IMPORT_TOKENS: tuple[str, ...] = (
    "sqlalchemy",
    "AsyncSession",
    "core.repositories",
    "core.models",
    "fastapi",
    "httpx",
    "requests",
    "PyQt6",
    "matplotlib",
    "pandas",
)


@dataclass(frozen=True)
class _Txn:
    txn_type: str
    trade_date: date
    units: Decimal
    created_at: datetime
    id: UUID


def test_holdings_module_imports_only_stdlib() -> None:
    """No import line in the holdings module names a DB / web / network dep."""
    offenders: list[str] = []
    for raw in _HOLDINGS_MODULE.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not (stripped.startswith("import ") or stripped.startswith("from ")):
            continue
        for tok in _FORBIDDEN_IMPORT_TOKENS:
            if tok in stripped:
                offenders.append(stripped)
    assert not offenders, (
        "ADR-0097 §4: the holdings derivation must import only the standard "
        f"library; found forbidden import(s): {offenders}."
    )


def test_derivation_is_input_order_independent() -> None:
    """Same ledger → same holdings, regardless of input iteration order."""
    a = _Txn(
        "opening",
        date(2025, 1, 1),
        Decimal("100"),
        datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
        UUID(int=1),
    )
    b = _Txn(
        "buy",
        date(2025, 3, 1),
        Decimal("50"),
        datetime(2026, 1, 1, 9, tzinfo=timezone.utc),
        UUID(int=2),
    )
    c = _Txn(
        "sell",
        date(2025, 6, 1),
        Decimal("-30"),
        datetime(2026, 1, 1, 10, tzinfo=timezone.utc),
        UUID(int=3),
    )
    assert derive_holdings([c, b, a]) == derive_holdings([a, b, c])
    assert derive_holdings([a, b, c])[-1].units == Decimal("120")
