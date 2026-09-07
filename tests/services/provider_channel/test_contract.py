# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Where the provider channel and the ticket world are allowed to meet.

:mod:`services.provider_channel` re-declares a handful of literals that also
live in :mod:`services.transactions.constants` — the three channel statuses,
the master-data identifier keys, the ISIN scheme, the ticket kinds — because
importing that package would drag the whole ticket-service graph in behind
it. Duplicating a *string* is cheap; duplicating it *silently* is not. This
module is the pin that makes drift a test failure, and it is the only test in
the package permitted to import from either world.

Four groups:

* **C-1** the vocabulary pins;
* **C-2** import isolation — the "channel off" assertion (D-flag). The
  channel is absent rather than disabled, so what stands in for a flag read
  is proof that nothing is wired;
* **C-3** ``executed`` never books, it pre-fills (ADR-0129 §3);
* **C-4** the ADR-0128 S1 guard, re-affirmed at the constants level. Its
  database-level twin is
  ``tests/regression/test_b034_trade_tickets_roundtrip.py``, which pins the
  same three states against the ``ck_trade_tickets_status`` CHECK.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

import services.provider_channel.prefill as prefill_module
from core.models.trade_ticket import TradeTicket
from services.provider_channel.directory import TICKET_KINDS
from services.provider_channel.prefill import (
    IDENTIFIER_SCHEME_ISIN,
    MD_IDENTIFIER_SCHEME,
    MD_IDENTIFIER_VALUE,
    PREFILL_FIELD_CURRENCY,
    PREFILL_FIELD_FEES,
    PREFILL_FIELD_MASTER_DATA,
    PREFILL_FIELD_PRICE_PER_UNIT,
    PREFILL_FIELD_SETTLEMENT_DATE,
    PREFILL_FIELD_TAXES,
    PREFILL_FIELD_TRADE_DATE,
    PREFILL_FIELD_UNITS,
    fill_to_prefill,
)
from services.provider_channel.schemas import (
    ENVELOPE_STATUS_ACKNOWLEDGED,
    ENVELOPE_STATUS_DECLINED,
    ENVELOPE_STATUS_EXECUTED,
    ENVELOPE_STATUS_SENT,
    FILL_SCHEMA_VERSION,
    FillPayload,
)
from services.transactions.constants import (
    KINDS,
    MD_IDENTIFIER_SCHEME as TICKET_MD_IDENTIFIER_SCHEME,
    MD_IDENTIFIER_VALUE as TICKET_MD_IDENTIFIER_VALUE,
    STATUS_ACKNOWLEDGED,
    STATUS_EXECUTED,
    STATUS_SENT,
    STATUSES,
    V1_REACHABLE_STATUSES,
)
from web.routes.transactions import _RESOLVABLE_SCHEMES

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_PACKAGE_ROOT: Final[Path] = _REPO_ROOT / "services" / "provider_channel"


# ---------------------------------------------------------------------------
# C-1 — vocabulary pins
# ---------------------------------------------------------------------------


def test_envelope_statuses_mirror_the_ticket_constants() -> None:
    """C-1: the re-declared channel statuses equal their twins, character for character."""
    assert ENVELOPE_STATUS_SENT == STATUS_SENT
    assert ENVELOPE_STATUS_ACKNOWLEDGED == STATUS_ACKNOWLEDGED
    assert ENVELOPE_STATUS_EXECUTED == STATUS_EXECUTED
    assert {
        ENVELOPE_STATUS_SENT,
        ENVELOPE_STATUS_ACKNOWLEDGED,
        ENVELOPE_STATUS_EXECUTED,
    } <= STATUSES


def test_declined_is_an_envelope_state_and_not_a_ticket_status() -> None:
    """C-1: ``declined`` belongs to the message, never to the ticket.

    Asserted on purpose so nobody helpfully adds it to the status CHECK: a
    declined message leaves the ticket exactly where the operator left it.
    """
    assert ENVELOPE_STATUS_DECLINED not in STATUSES


def test_directory_ticket_kinds_mirror_the_ticket_kinds() -> None:
    """C-1: what a provider may accept is the same vocabulary a ticket may be."""
    assert TICKET_KINDS == KINDS


def test_master_data_keys_mirror_the_ticket_constants() -> None:
    """C-1: a typo in a JSONB key is invisible to every schema guard there is."""
    assert MD_IDENTIFIER_SCHEME == TICKET_MD_IDENTIFIER_SCHEME
    assert MD_IDENTIFIER_VALUE == TICKET_MD_IDENTIFIER_VALUE


def test_isin_scheme_is_one_the_web_surface_resolves() -> None:
    """C-1: the scheme the pre-fill writes is one the composer can resolve."""
    assert IDENTIFIER_SCHEME_ISIN in _RESOLVABLE_SCHEMES


@pytest.mark.parametrize(
    "field",
    [
        PREFILL_FIELD_UNITS,
        PREFILL_FIELD_PRICE_PER_UNIT,
        PREFILL_FIELD_FEES,
        PREFILL_FIELD_TAXES,
        PREFILL_FIELD_CURRENCY,
        PREFILL_FIELD_TRADE_DATE,
        PREFILL_FIELD_SETTLEMENT_DATE,
        PREFILL_FIELD_MASTER_DATA,
    ],
)
def test_every_prefill_field_is_a_ticket_column(field: str) -> None:
    """C-1: the pre-fill speaks the ticket's own field names, so no table translates."""
    assert hasattr(TradeTicket, field)


# ---------------------------------------------------------------------------
# C-2 — import isolation (the "channel off" assertion, D-flag)
#
# The subprocess idiom is copied from
# tests/regression/test_analytics_layer_pure.py rather than imported: a
# private helper reaching across test packages is exactly the coupling these
# tests exist to forbid.
# ---------------------------------------------------------------------------

_FORBIDDEN_MODULE_PREFIXES: Final[tuple[str, ...]] = (
    "services.transactions",
    "core.repositories",
    "sqlalchemy",
    "fastapi",
    "httpx",
    "pydantic",
)

_FORBIDDEN_START_PATTERNS: Final[tuple[str, ...]] = (
    "from services.transactions",
    "import services.transactions",
    "from core.",
    "import core",
    "from sqlalchemy",
    "import sqlalchemy",
    "from fastapi",
    "import fastapi",
    "import httpx",
    "from httpx",
    "import socket",
    "import urllib",
    "from urllib",
    "import smtplib",
)

_FORBIDDEN_CONTAINS: Final[tuple[str, ...]] = (
    "async_session",
    "AsyncSession",
    "get_db_session",
    "set_status",
    "TicketService",
    ".book(",
    "datetime.now",
    "date.today",
)


def test_provider_channel_imports_nothing_from_the_ticket_world() -> None:
    """C-2: a fresh import pulls in no ticket service, ORM, web or HTTP module.

    This is what "the channel is off" means in Stage A: not a flag read, but
    the absence of any wiring at all.
    """
    code = (
        "import sys\n"
        "import services.provider_channel  # noqa: F401\n"
        f"prefixes = {_FORBIDDEN_MODULE_PREFIXES!r}\n"
        "leaks = sorted(\n"
        "    m for m in sys.modules\n"
        "    if any(m == p or m.startswith(p + '.') for p in prefixes)\n"
        ")\n"
        "assert not leaks, f'forbidden modules leaked into the import graph: {leaks}'\n"
        "print('OK')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=_REPO_ROOT,
    )
    assert completed.returncode == 0, (
        f"subprocess failed:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    assert "OK" in completed.stdout


def test_provider_channel_source_has_no_forbidden_imports() -> None:
    """C-2: the anchored-start source scan, mirroring the analytics purity guard."""
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
        "ADR-0129 Stage A keeps services/provider_channel pure — no ticket "
        f"service, ORM, web, network or clock. Offending lines: {offenders}"
    )


# ---------------------------------------------------------------------------
# C-3 — `executed` never books, it pre-fills
# ---------------------------------------------------------------------------


def test_executed_pre_fills_and_never_books() -> None:
    """C-3: the instance pre-fills the ADR-0128 booking step for user review.

    ADR-0129 §3 — it never books autonomously. The absences below are the
    assertion: no status moves across, and no total is computed (D-amounts;
    net arithmetic stays the ticket layer's single answer).
    """
    fill = FillPayload(
        schema_version=FILL_SCHEMA_VERSION,
        units=Decimal("500"),
        price_per_unit=Decimal("101.25"),
        fees=Decimal("7.50"),
        taxes=Decimal("0.00"),
        currency="EUR",
        trade_date=date(2026, 9, 4),
        settlement_date=date(2026, 9, 8),
        isin="DE0001234567",
        message="Please confirm by close of business.",
    )
    prefill = fill_to_prefill(fill)

    assert set(prefill) == {
        PREFILL_FIELD_UNITS,
        PREFILL_FIELD_PRICE_PER_UNIT,
        PREFILL_FIELD_FEES,
        PREFILL_FIELD_TAXES,
        PREFILL_FIELD_CURRENCY,
        PREFILL_FIELD_TRADE_DATE,
        PREFILL_FIELD_SETTLEMENT_DATE,
        PREFILL_FIELD_MASTER_DATA,
    }
    assert prefill[PREFILL_FIELD_UNITS] == Decimal("500")
    assert prefill[PREFILL_FIELD_MASTER_DATA] == {
        MD_IDENTIFIER_SCHEME: IDENTIFIER_SCHEME_ISIN,
        MD_IDENTIFIER_VALUE: "DE0001234567",
    }

    # No lifecycle, no arithmetic, and the free text stays with the human.
    assert "status" not in prefill
    assert "gross_amount" not in prefill
    assert "net_amount" not in prefill
    assert "message" not in prefill
    assert fill.message not in prefill.values()


@pytest.mark.parametrize("verb", ["book", "set_status", "propose", "approve"])
def test_prefill_module_exposes_no_lifecycle_verb(verb: str) -> None:
    """C-3: nothing in the pre-fill module can move a ticket anywhere."""
    members = {name for name, _ in inspect.getmembers(prefill_module)}
    assert verb not in members


# ---------------------------------------------------------------------------
# C-4 — the S1 guard, re-affirmed at the constants level
# ---------------------------------------------------------------------------


def test_channel_statuses_are_defined_but_unreachable() -> None:
    """C-4: the three channel states exist in the CHECK and in no transition.

    The database-level twin is
    ``tests/regression/test_b034_trade_tickets_roundtrip.py``.
    """
    channel = frozenset({STATUS_SENT, STATUS_ACKNOWLEDGED, STATUS_EXECUTED})
    assert channel <= STATUSES
    assert channel.isdisjoint(V1_REACHABLE_STATUSES)


def test_no_code_path_writes_a_channel_status() -> None:
    """C-4: no source line in the ticket layer assigns a channel status.

    Stage A builds a contract, not a transition. The day ADR-0129 Stage B
    arms these states, this test is the one that must be revisited
    deliberately rather than edited away in passing.
    """
    targets = sorted((_REPO_ROOT / "services" / "transactions").glob("*.py"))
    targets.append(_REPO_ROOT / "core" / "repositories" / "trade_ticket_repository.py")

    forbidden = (
        "status=STATUS_SENT",
        "status=STATUS_ACKNOWLEDGED",
        "status=STATUS_EXECUTED",
        'status="sent"',
        'status="acknowledged"',
        'status="executed"',
        "status='sent'",
        "status='acknowledged'",
        "status='executed'",
    )
    offenders: list[tuple[str, int, str]] = []
    for python_file in targets:
        assert python_file.exists(), f"expected file missing: {python_file}"
        for lineno, raw_line in enumerate(
            python_file.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = raw_line.strip()
            if stripped.startswith("#"):
                continue
            if any(symbol in stripped for symbol in forbidden):
                offenders.append((python_file.name, lineno, stripped))
    assert not offenders, (
        "ADR-0128 §3 keeps sent/acknowledged/executed unreachable in v1; "
        f"ADR-0129 Stage A does not arm them. Offending lines: {offenders}"
    )
