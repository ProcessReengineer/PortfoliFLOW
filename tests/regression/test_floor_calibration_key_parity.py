# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Guard: ``floor_calibration``'s columns cover exactly the calibratable keys.

``core/repositories/floor_calibration_repository.py`` maps ``FloorConfig``
keys to columns, and it has to restate the ``TRIGGER_*`` / ``SOURCE_*``
vocabulary as string literals because ``core/`` imports nothing from
within the project (the layering contract in CLAUDE.md). That restatement
is the only place in the codebase where the vocabulary exists twice, so
it gets a guard rather than a comment.

Two failure modes this catches:

1. **A new trigger type or family lands in** ``FloorConfig`` **and gets no
   column.** The Calibration editor would then silently offer no way to
   tune it, and a per-tenant override of it would be impossible.
2. **A column outlives its key**, or ``fund_closure`` acquires one. The
   second is the dangerous direction: ``fund_closure`` is a pinned level
   (floor = cap = 10, ADR-0116 §7 invariant 1) and having nowhere to
   store it is precisely what makes it non-editable.

No database and no migration run: this compares two Python objects.
"""

from __future__ import annotations

from core.repositories.floor_calibration_repository import (
    CAP_COLUMNS,
    DELTA_COLUMNS,
    FLOOR_COLUMNS,
)
from services.analytics.irene_floor import DEFAULT_FLOOR_CONFIG, TRIGGER_FUND_CLOSURE


def test_floor_columns_cover_every_trigger_except_the_pinned_one() -> None:
    expected = set(DEFAULT_FLOOR_CONFIG.floor) - {TRIGGER_FUND_CLOSURE}
    assert set(FLOOR_COLUMNS) == expected, (
        "floor_calibration's floor columns must cover exactly the calibratable "
        f"trigger types. missing={sorted(expected - set(FLOOR_COLUMNS))}, "
        f"extra={sorted(set(FLOOR_COLUMNS) - expected)}"
    )


def test_cap_columns_cover_every_cap_key_except_the_pinned_one() -> None:
    expected = set(DEFAULT_FLOOR_CONFIG.cap) - {TRIGGER_FUND_CLOSURE}
    assert set(CAP_COLUMNS) == expected, (
        "floor_calibration's cap columns must cover exactly the calibratable "
        f"cap keys (both axes: source AND trigger). "
        f"missing={sorted(expected - set(CAP_COLUMNS))}, "
        f"extra={sorted(set(CAP_COLUMNS) - expected)}"
    )


def test_delta_columns_cover_every_family() -> None:
    expected = set(DEFAULT_FLOOR_CONFIG.re_trigger_delta)
    assert set(DELTA_COLUMNS) == expected, (
        "floor_calibration must carry one re-trigger-delta column per subject "
        f"family. missing={sorted(expected - set(DELTA_COLUMNS))}, "
        f"extra={sorted(set(DELTA_COLUMNS) - expected)}"
    )


def test_the_pinned_trigger_has_no_column_in_either_group() -> None:
    assert TRIGGER_FUND_CLOSURE not in FLOOR_COLUMNS
    assert TRIGGER_FUND_CLOSURE not in CAP_COLUMNS
    assert not [
        column
        for column in (*FLOOR_COLUMNS.values(), *CAP_COLUMNS.values())
        if TRIGGER_FUND_CLOSURE in column
    ], (
        "fund_closure is a pinned level, not calibration. Giving it a column "
        "would make it editable, which ADR-0116 §7 forbids under any framing."
    )


def test_column_names_are_unique_across_the_three_groups() -> None:
    """A shared column between groups would silently alias two settings."""
    names = [*FLOOR_COLUMNS.values(), *CAP_COLUMNS.values(), *DELTA_COLUMNS.values()]
    assert len(names) == len(set(names)), "duplicate column name across the key maps"
