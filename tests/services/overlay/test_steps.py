# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the step primitive both executors settle through (ADR-0104 §2).

:func:`services.overlay.steps.add_step` is where "a balance gains an amount
from a date onward" is written exactly once. Its three properties — carry-
forward on insertion, Decimal-safety, and no mutation of the input — are what
the executors above it inherit, so they are pinned here rather than
re-asserted in every executor test.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
from pandas.testing import assert_series_equal

from services.overlay.steps import add_step, zero_path

_D = Decimal


def _path() -> pd.Series:
    """A Decimal balance path on a quarterly grid."""
    return pd.Series(
        [_D("100"), _D("110"), _D("120")],
        index=pd.to_datetime(["2026-03-31", "2026-06-30", "2026-09-30"]),
    )


def test_a_step_on_an_existing_point_raises_it_and_everything_after() -> None:
    """History is arithmetically untouched; the future carries the step."""
    stepped = add_step(_path(), date(2026, 6, 30), _D("50"))

    assert stepped.loc[pd.Timestamp("2026-03-31")] == _D("100")
    assert stepped.loc[pd.Timestamp("2026-06-30")] == _D("160")
    assert stepped.loc[pd.Timestamp("2026-09-30")] == _D("170")


def test_a_step_off_the_grid_inserts_a_point_carrying_the_balance_forward() -> None:
    """The level in force at the step's date is the latest earlier one."""
    stepped = add_step(_path(), date(2026, 8, 15), _D("50"))

    assert list(stepped.index) == list(
        pd.to_datetime(["2026-03-31", "2026-06-30", "2026-08-15", "2026-09-30"])
    )
    # The balance was 110 at the seam; the new point is 110 + 50.
    assert stepped.loc[pd.Timestamp("2026-08-15")] == _D("160")
    assert stepped.loc[pd.Timestamp("2026-09-30")] == _D("170")
    assert stepped.index.is_monotonic_increasing


def test_a_step_before_the_path_begins_carries_a_pre_history_of_zero() -> None:
    """A balance that has not begun is zero, not undefined."""
    stepped = add_step(_path(), date(2025, 12, 31), _D("5"))

    assert stepped.loc[pd.Timestamp("2025-12-31")] == _D("5")
    assert stepped.loc[pd.Timestamp("2026-03-31")] == _D("105")


def test_the_step_keeps_decimals_decimal() -> None:
    """Money must not round-trip through a float to gain a step."""
    stepped = add_step(_path(), date(2026, 6, 30), _D("0.01"))

    assert stepped.dtype == object
    assert stepped.loc[pd.Timestamp("2026-06-30")] == _D("110.01")
    assert isinstance(stepped.loc[pd.Timestamp("2026-06-30")], Decimal)


def test_a_float_path_takes_the_step_as_a_float() -> None:
    """A numeric path stays numeric — no object dtype creeps in."""
    path = pd.Series([100.0, 110.0], index=pd.to_datetime(["2026-06-30", "2026-12-31"]))
    stepped = add_step(path, date(2026, 9, 30), _D("50"))

    assert stepped.dtype == "float64"
    assert stepped.loc[pd.Timestamp("2026-09-30")] == 150.0
    assert stepped.loc[pd.Timestamp("2026-12-31")] == 160.0


def test_the_input_series_is_never_mutated() -> None:
    """The purity contract, at the primitive both executors share."""
    path = _path()
    before = path.copy(deep=True)

    add_step(path, date(2026, 8, 15), _D("50"))

    assert_series_equal(path, before, check_exact=True)


def test_a_step_on_the_zero_path_creates_a_single_point() -> None:
    """An absent balance path and an empty one mean the same thing."""
    stepped = add_step(zero_path(), date(2026, 9, 30), _D("77"))

    assert list(stepped.index) == [pd.Timestamp("2026-09-30")]
    assert stepped.loc[pd.Timestamp("2026-09-30")] == _D("77")
    assert isinstance(stepped.loc[pd.Timestamp("2026-09-30")], Decimal)


def test_two_steps_compose_into_a_move() -> None:
    """The re-pacing idiom: lift the amount off one date, set it on another."""
    lifted = add_step(_path(), date(2026, 6, 30), _D("-20"))
    moved = add_step(lifted, date(2026, 9, 30), _D("20"))

    assert moved.loc[pd.Timestamp("2026-03-31")] == _D("100")
    assert moved.loc[pd.Timestamp("2026-06-30")] == _D("90")
    # Past the second step the two cancel: the level is the baseline's again.
    assert moved.loc[pd.Timestamp("2026-09-30")] == _D("120")
