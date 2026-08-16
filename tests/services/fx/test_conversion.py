# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""FxConverter tests — the pure conversion contract of ADR-0099 §3.

Coverage:

* The **identity short-circuit** works against an *empty* rates frame. This
  is the backwards-compatibility guarantee: an EUR-only tenant holds zero
  ``fx_rates`` rows, so if identity conversion needed data, every existing
  single-currency deployment would break.
* Reference-leg conversion in both directions (``USD → EUR``, ``EUR → USD``).
* Triangulation between two non-reference currencies (``JPY → GBP`` via EUR)
  against a hand-computed golden value.
* Carry-forward across a gap (a holiday with no published rate).
* :class:`MissingFxRateError` on an uncovered currency and on a date before
  the currency's first rate — never a silent 1:1 fallback.
* ``convert_series`` prices each observation at its own date's rate, so a
  rate change mid-series shows up in the result.

These tests are pure: no database, no session, no repository. They build
rate frames in the shape
:meth:`core.repositories.fx_rate_repository.FxRateRepository.load_rates_frame`
returns.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from core.exceptions import MissingFxRateError, ValidationError
from core.repositories.fx_rate_repository import RATES_FRAME_COLUMNS
from services.fx import FxConverter

# EUR-based deployment: 1 USD = 0.92 EUR, 1 GBP = 1.17 EUR, 1 JPY = 0.006 EUR.
_REFERENCE = "EUR"


def _frame(rows: list[tuple[date, str, str]]) -> pd.DataFrame:
    """Build a rates frame in the repository's hand-off shape."""
    frame = pd.DataFrame(
        [(as_of, currency, Decimal(rate), _REFERENCE) for as_of, currency, rate in rows],
        columns=list(RATES_FRAME_COLUMNS),
    )
    frame["as_of_date"] = pd.to_datetime(frame["as_of_date"])
    return frame


def _empty_frame() -> pd.DataFrame:
    """The frame an EUR-only tenant produces: zero rows, right dtypes."""
    return pd.DataFrame(
        {
            "as_of_date": pd.Series([], dtype="datetime64[ns]"),
            "currency": pd.Series([], dtype="object"),
            "rate_to_reference": pd.Series([], dtype="object"),
            "reference_currency": pd.Series([], dtype="object"),
        }
    )


# ---------------------------------------------------------------------------
# FXC-01: identity short-circuit needs no FX data at all
# ---------------------------------------------------------------------------


def test_fxc01_identity_conversion_requires_no_rates() -> None:
    """``from == to`` returns the amount untouched, from an empty frame.

    The ADR-0099 §3 backwards-compatibility guarantee, stated as a test:
    a pure-EUR portfolio operates with zero FX rows.
    """
    converter = FxConverter(_empty_frame(), _REFERENCE)

    amount = Decimal("1234.5678")
    result = converter.convert(amount, "EUR", "EUR", date(2025, 3, 1))

    assert result == amount
    assert result is amount  # untouched, not re-derived


def test_fxc01b_identity_series_conversion_requires_no_rates() -> None:
    """``convert_series`` short-circuits identically, values byte-identical."""
    converter = FxConverter(_empty_frame(), _REFERENCE)
    series = pd.Series(
        [100.0, 200.5, float("nan")],
        index=pd.to_datetime(["2025-03-01", "2025-03-02", "2025-03-03"]),
        name="nav",
    )

    result = converter.convert_series(series, "EUR", "EUR")

    pd.testing.assert_series_equal(result, series)
    assert result is not series  # a copy: the caller's series stays isolated


def test_fxc01c_reference_currency_rate_is_one_without_lookup() -> None:
    converter = FxConverter(_empty_frame(), _REFERENCE)
    assert converter.rate("EUR", date(2025, 3, 1)) == Decimal(1)
    assert converter.reference_currency == "EUR"


# ---------------------------------------------------------------------------
# FXC-02: conversion against the reference leg, both directions
# ---------------------------------------------------------------------------


def test_fxc02_convert_to_and_from_the_reference_currency() -> None:
    converter = FxConverter(_frame([(date(2025, 3, 3), "USD", "0.9200000000")]), _REFERENCE)

    # 100 USD × 0.92 EUR/USD ÷ 1 = 92 EUR.
    assert converter.convert(Decimal("100"), "USD", "EUR", date(2025, 3, 3)) == Decimal("92")
    # 92 EUR × 1 ÷ 0.92 EUR/USD = 100 USD.
    assert converter.convert(Decimal("92"), "EUR", "USD", date(2025, 3, 3)) == Decimal("100")


# ---------------------------------------------------------------------------
# FXC-03: triangulation between two non-reference currencies
# ---------------------------------------------------------------------------


def test_fxc03_triangulation_jpy_to_gbp_via_eur() -> None:
    """``amount × rate(from) / rate(to)`` — golden value computed by hand.

    1,000,000 JPY × 0.006 EUR/JPY = 6,000 EUR.
    6,000 EUR ÷ 1.17 EUR/GBP = 5,128.205128… GBP (a repeating quotient, so
    the assertion also pins the Decimal division rather than an exact hit).
    """
    converter = FxConverter(
        _frame(
            [
                (date(2025, 3, 3), "JPY", "0.0060000000"),
                (date(2025, 3, 3), "GBP", "1.1700000000"),
            ]
        ),
        _REFERENCE,
    )

    result = converter.convert(Decimal("1000000"), "JPY", "GBP", date(2025, 3, 3))

    assert result.quantize(Decimal("0.000001")) == Decimal("5128.205128")
    # An exact-division control: 1.20 EUR/GBP divides 6,000 EUR evenly.
    exact = FxConverter(
        _frame(
            [
                (date(2025, 3, 3), "JPY", "0.0060000000"),
                (date(2025, 3, 3), "GBP", "1.2000000000"),
            ]
        ),
        _REFERENCE,
    )
    assert exact.convert(Decimal("1000000"), "JPY", "GBP", date(2025, 3, 3)) == Decimal("5000")


# ---------------------------------------------------------------------------
# FXC-04: carry-forward across a gap (published-rate holiday)
# ---------------------------------------------------------------------------


def test_fxc04_rate_carries_forward_over_a_gap() -> None:
    """The latest rate at or before the date applies (ADR-0060 idiom).

    ECB publishes no rate on 2025-04-18 (Good Friday) or the weekend; a NAV
    on any of those days converts at Thursday's rate.
    """
    converter = FxConverter(
        _frame(
            [
                (date(2025, 4, 17), "USD", "0.9200000000"),
                (date(2025, 4, 22), "USD", "0.9300000000"),
            ]
        ),
        _REFERENCE,
    )

    assert converter.rate("USD", date(2025, 4, 17)) == Decimal("0.92")
    assert converter.rate("USD", date(2025, 4, 18)) == Decimal("0.92")
    assert converter.rate("USD", date(2025, 4, 21)) == Decimal("0.92")
    assert converter.rate("USD", date(2025, 4, 22)) == Decimal("0.93")
    # Carry-forward runs open-endedly into the future of the dataset.
    assert converter.rate("USD", date(2026, 1, 1)) == Decimal("0.93")


# ---------------------------------------------------------------------------
# FXC-05: typed failure — no silent 1:1 fallback, anywhere
# ---------------------------------------------------------------------------


def test_fxc05a_uncovered_currency_raises() -> None:
    converter = FxConverter(_frame([(date(2025, 3, 3), "USD", "0.9200000000")]), _REFERENCE)

    with pytest.raises(MissingFxRateError) as excinfo:
        converter.rate("CHF", date(2025, 3, 3))

    error = excinfo.value
    assert error.currency == "CHF"
    assert error.as_of_date == date(2025, 3, 3)
    assert error.leg is None
    assert "CHF" in error.message and "2025-03-03" in error.message


def test_fxc05b_date_before_first_rate_raises_and_names_the_leg() -> None:
    """Carry-forward has no anchor before the first stored rate."""
    converter = FxConverter(_frame([(date(2025, 3, 3), "USD", "0.9200000000")]), _REFERENCE)

    with pytest.raises(MissingFxRateError) as excinfo:
        converter.convert(Decimal("100"), "USD", "EUR", date(2025, 3, 2))

    error = excinfo.value
    assert error.currency == "USD"
    assert error.as_of_date == date(2025, 3, 2)
    assert error.leg == "from"


def test_fxc05c_missing_target_leg_names_the_to_leg() -> None:
    converter = FxConverter(_frame([(date(2025, 3, 3), "USD", "0.9200000000")]), _REFERENCE)

    with pytest.raises(MissingFxRateError) as excinfo:
        converter.convert(Decimal("100"), "USD", "CHF", date(2025, 3, 3))

    assert excinfo.value.currency == "CHF"
    assert excinfo.value.leg == "to"


def test_fxc05d_series_conversion_raises_on_uncovered_date() -> None:
    converter = FxConverter(_frame([(date(2025, 3, 3), "USD", "0.9200000000")]), _REFERENCE)
    series = pd.Series(
        [100.0, 200.0],
        index=pd.to_datetime(["2025-03-01", "2025-03-04"]),
    )

    with pytest.raises(MissingFxRateError) as excinfo:
        converter.convert_series(series, "USD", "EUR")

    # The earliest offending date is reported, not an arbitrary one.
    assert excinfo.value.as_of_date == date(2025, 3, 1)


# ---------------------------------------------------------------------------
# FXC-06: convert_series is point-in-time across a rate change
# ---------------------------------------------------------------------------


def test_fxc06_convert_series_prices_each_point_at_its_own_rate() -> None:
    """A rate change mid-series must move only the observations after it.

    Converting the whole history at one period-end rate would erase the FX
    effect — the property a functional-currency IRR exists to preserve.
    """
    converter = FxConverter(
        _frame(
            [
                (date(2025, 3, 3), "USD", "0.9000000000"),
                (date(2025, 3, 6), "USD", "0.8000000000"),
            ]
        ),
        _REFERENCE,
    )
    series = pd.Series(
        [100.0, 100.0, 100.0, 100.0],
        index=pd.to_datetime(["2025-03-03", "2025-03-05", "2025-03-06", "2025-03-07"]),
        name="nav",
    )

    result = converter.convert_series(series, "USD", "EUR")

    # 2025-03-05 carries 2025-03-03's rate forward; 03-07 carries 03-06's.
    expected = pd.Series([90.0, 90.0, 80.0, 80.0], index=series.index, name="nav")
    pd.testing.assert_series_equal(result, expected)
    # The caller's series is untouched.
    assert series.tolist() == [100.0, 100.0, 100.0, 100.0]


def test_fxc06b_convert_series_triangulates() -> None:
    converter = FxConverter(
        _frame(
            [
                (date(2025, 3, 3), "JPY", "0.0060000000"),
                (date(2025, 3, 3), "GBP", "1.2000000000"),
            ]
        ),
        _REFERENCE,
    )
    series = pd.Series([1_000_000.0], index=pd.to_datetime(["2025-03-03"]))

    result = converter.convert_series(series, "JPY", "GBP")

    assert result.iloc[0] == pytest.approx(5000.0)


def test_fxc06c_convert_series_accepts_a_tz_aware_index() -> None:
    """Cashflow frames carry UTC timestamps; the rate of that day applies."""
    converter = FxConverter(_frame([(date(2025, 3, 3), "USD", "0.9000000000")]), _REFERENCE)
    series = pd.Series([100.0], index=pd.to_datetime(["2025-03-03T14:30:00Z"]), name="flow")

    result = converter.convert_series(series, "USD", "EUR")

    assert result.iloc[0] == pytest.approx(90.0)
    # The caller's original (tz-aware) index survives the round-trip.
    pd.testing.assert_index_equal(result.index, series.index)


# ---------------------------------------------------------------------------
# FXC-07: a frame must be quoted against a single reference currency
# ---------------------------------------------------------------------------


def test_fxc07_mixed_reference_currency_frame_is_rejected() -> None:
    """Triangulation is meaningless across two references; fail at construction."""
    frame = pd.DataFrame(
        [
            (date(2025, 3, 3), "USD", Decimal("0.92"), "EUR"),
            (date(2025, 3, 3), "GBP", Decimal("1.30"), "USD"),
        ],
        columns=list(RATES_FRAME_COLUMNS),
    )
    frame["as_of_date"] = pd.to_datetime(frame["as_of_date"])

    with pytest.raises(ValidationError) as excinfo:
        FxConverter(frame, _REFERENCE)

    assert excinfo.value.field == "reference_currency"
