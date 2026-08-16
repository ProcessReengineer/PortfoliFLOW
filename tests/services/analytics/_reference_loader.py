# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Loader skeleton for the Excel limit-coverage reference workbook.

The companion XLSX
``data/sample/PortfoliFLOW_Limit_Coverage_Reference_v1.xlsx`` is meant
to carry hand-validated expected coverage rows for three stichtage
(2020-12-31, 2023-06-30, 2026-03-31). The actual reference workbook
is **not yet present** in the repository — Kickoff #2 §6.3 explicitly
allows shipping the engine with the parity test ``@pytest.mark.skip``
as long as this loader skeleton (schema dataclasses + raising loader
stub) lands so that activating the test later is a minimal edit.

This module lives next to ``test_limit_coverage.py`` under
``tests/services/analytics/`` (Kickoff #3a relocated it together with
the engine). The leading underscore in the filename prevents pytest
collection — it is a helper, not a test module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class ExpectedClassRow:
    """One expected coverage row for a single class on a single date.

    Attributes:
        class_key: SAA asset-class code, AnlV code, ``'unallocated'``,
            or a NO_LIMIT class key.
        max_pct: Limit ceiling in percentage points; ``None`` for
            UNALLOCATED and NO_LIMIT rows.
        nav_sum_eur: NAV summed over the class at the stichtag.
        coverage_pct: ``nav_sum_eur / aum_total * 100`` in pp.
        headroom_eur: ``(max_pct - coverage_pct) * aum_total / 100``;
            ``None`` for UNALLOCATED and NO_LIMIT rows.
        status: One of ``OK`` / ``WARN`` / ``BREACH`` /
            ``UNALLOCATED`` / ``NO_LIMIT``.
    """

    class_key: str
    max_pct: Decimal | None
    nav_sum_eur: Decimal
    coverage_pct: Decimal
    headroom_eur: Decimal | None
    status: str


@dataclass(frozen=True)
class ExpectedCoverage:
    """Expected engine output for a single stichtag.

    Attributes:
        as_of_date: The stichtag.
        aum_total: AUM denominator used at the stichtag — ``Σ nav_i_t``
            over the book, cash rows included (ADR-0103 §2).
        saa_rows: Expected long-format rows for the SAA family,
            ordered as the engine would emit them.
        anlv_rows: Expected long-format rows for the AnlV family,
            ordered as the engine would emit them.
    """

    as_of_date: date
    aum_total: Decimal
    saa_rows: list[ExpectedClassRow]
    anlv_rows: list[ExpectedClassRow]


# Tolerances for the parity check (Kickoff #2 §2.7).
EUR_TOLERANCE: Decimal = Decimal("0.01")
PCT_TOLERANCE: Decimal = Decimal("0.0001")


def load_reference(path: Path) -> list[ExpectedCoverage]:
    """Load expected coverage rows from the reference XLSX.

    The expected layout carries one sheet per stichtag plus an
    ``Engine_Inputs`` sheet that mirrors the NAV / AUM / limit-set
    values used to drive the engine, so the test can construct
    ``CoverageEngineInputs`` directly without going through the
    production importer.

    Args:
        path: Filesystem path to the reference workbook.

    Returns:
        List of :class:`ExpectedCoverage` ordered by ``as_of_date``
        ascending.

    Raises:
        NotImplementedError: Always, until the reference XLSX is
            committed and this loader is wired up.
    """
    raise NotImplementedError(
        "Reference XLSX loader not yet implemented — the workbook at "
        f"{path} has not been committed. See Kickoff #2 §6.3."
    )


def assert_decimal_close(
    actual: Decimal,
    expected: Decimal,
    tol: Decimal,
    label: str,
) -> None:
    """Assert ``|actual - expected| <= tol`` with a sprechende message.

    Args:
        actual: Engine-produced value.
        expected: Reference-XLSX value.
        tol: Symmetric tolerance (see :data:`EUR_TOLERANCE` /
            :data:`PCT_TOLERANCE`).
        label: Human-readable locator (e.g. ``"2023-06-30 / saa / private_equity / coverage_pct"``)
            included in the failure message.

    Raises:
        AssertionError: If the absolute difference exceeds ``tol``.
    """
    diff = abs(actual - expected)
    if diff > tol:
        raise AssertionError(f"{label}: actual={actual} expected={expected} diff={diff} tol={tol}")
