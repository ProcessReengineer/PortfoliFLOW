# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for ``core.decimal_input`` — R10's server-side number reading.

A pure table test. The module has no I/O, no locale dependency and no
project import of its own, so these run without a database, an app or a
browser, and every row of the module's own docstring table appears here as
a case.

What is pinned:

* the four notation rules of P-UX-A1d (``ui-standards.md`` §2.5 R10) — both
  separators present, one separator once, one separator repeated, and the
  grouping glyphs that carry no value;
* that **a lone separator is the decimal separator**. This is the one rule
  a reader might expect the other way round, and it is deliberate: guessing
  by digit count would be a coin toss the operator cannot see;
* that the result is a :class:`~decimal.Decimal` and exactly equal to the
  literal it should read as — never a ``float``, and never rounded on the
  way in;
* the permissive contract: absent, blank and unreadable all read as
  ``None``, so the recalculation endpoint refuses nothing (D-2);
* :func:`~core.decimal_input.was_interpreted`, which decides whether the
  surface has anything to echo at all.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.decimal_input import parse_decimal, was_interpreted

#: Every example the module docstring states, plus the edges around them.
_CASES: tuple[tuple[str | None, Decimal | None], ...] = (
    # -- both separators present: the last one is the decimal separator ----
    ("1.234,56", Decimal("1234.56")),
    ("1,234.56", Decimal("1234.56")),
    ("1.234.567,89", Decimal("1234567.89")),
    ("1,234,567.89", Decimal("1234567.89")),
    # Mixed order: the decimal separator may appear once and nothing groups
    # behind it, so this is not a number rather than a guess.
    ("1,234.56,7", None),
    # -- one separator, once: it is the decimal separator ------------------
    ("1,234", Decimal("1.234")),
    ("1.234", Decimal("1.234")),
    ("-12,5", Decimal("-12.5")),
    ("+7", Decimal("7")),
    (".5", Decimal("0.5")),
    (",5", Decimal("0.5")),
    ("12,", Decimal("12")),
    # -- one separator, repeated: grouping, in runs of three ---------------
    ("1.234.567", Decimal("1234567")),
    ("1,234,567", Decimal("1234567")),
    ("12,34,56", None),
    # -- space, NBSP, thin space, narrow space and the apostrophe group ----
    ("1 234 567,89", Decimal("1234567.89")),
    ("1'234.56", Decimal("1234.56")),
    ("1 234,5", Decimal("1234.5")),
    ("1 234,5", Decimal("1234.5")),
    ("1 234,5", Decimal("1234.5")),
    # -- the permissive contract (D-2) -------------------------------------
    (None, None),
    ("", None),
    ("   ", None),
    ("abc", None),
    ("1e3", None),
    ("-", None),
    (".", None),
    # -- and the ordinary case, which no rule touches -----------------------
    ("1200", Decimal("1200")),
)


@pytest.mark.parametrize(("raw", "expected"), _CASES)
def test_parse_decimal_reads_the_stated_value(raw: str | None, expected: Decimal | None) -> None:
    """Every rule in the module's table, stated as a case."""
    assert parse_decimal(raw) == expected


def test_the_result_is_a_decimal_and_never_a_float() -> None:
    """R10 is a *money* rule: binary floating point may not touch the value."""
    value = parse_decimal("1.234,56")
    assert isinstance(value, Decimal)
    assert not isinstance(value, float)
    assert value == Decimal("1234.56")
    # Exactly, not merely numerically: a reading that arrived through a float
    # would carry a different exponent even where the comparison holds.
    assert value.as_tuple() == Decimal("1234.56").as_tuple()


def test_both_notations_read_to_the_same_value() -> None:
    """The whole point of R10: the notation is the operator's, not the book's."""
    assert parse_decimal("1.234,56") == parse_decimal("1,234.56") == Decimal("1234.56")


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("1200", False),
        ("", False),
        (None, False),
        ("   ", False),
        ("abc", False),
        ("1,200", True),
        ("1.200", True),
        ("1 200", True),
        ("1'200", True),
        ("-7", True),
        ("+7", True),
    ),
)
def test_was_interpreted(raw: str | None, expected: bool) -> None:
    """A plain integer stays quiet; anything a rule touched is echoed back."""
    assert was_interpreted(raw) is expected
