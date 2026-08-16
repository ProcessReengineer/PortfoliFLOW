# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.analytics.correlation``.

Pure-function tests against ``compute_correlation_matrix``. The QT
widget uses ``pandas.DataFrame.corr(method="pearson")`` directly on
the wide DataFrame; this module wraps that call so the result must
reproduce the QT numbers exactly.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from services.analytics.correlation import compute_correlation_matrix


def _series(values: list[float], dates: list[date]) -> pd.Series:
    return pd.Series(values, index=dates, dtype="float64")


def test_empty_dict_returns_empty_dataframe() -> None:
    result = compute_correlation_matrix({})
    assert result.empty
    assert isinstance(result, pd.DataFrame)


def test_single_investment_diagonal_is_one() -> None:
    series = _series(
        [0.01, -0.02, 0.03, -0.01],
        [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 4)],
    )
    result = compute_correlation_matrix({"A": series})
    assert result.shape == (1, 1)
    assert result.loc["A", "A"] == pytest.approx(1.0, abs=1e-15)


def test_pairwise_complete_handles_unaligned_indexes() -> None:
    """A and B share three dates; the date where only one has data is dropped."""
    a = _series(
        [0.01, -0.02, 0.03, 0.0],
        [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 4)],
    )
    b = _series(
        [0.005, -0.01, 0.015],  # no entry for 2025-01-04
        [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)],
    )
    result = compute_correlation_matrix({"A": a, "B": b})
    # Pandas pairs A and B on the three common dates; the result is
    # the Pearson correlation of the two 3-element vectors.
    expected = pd.Series([0.01, -0.02, 0.03], index=a.index[:3]).corr(
        pd.Series([0.005, -0.01, 0.015], index=b.index)
    )
    assert result.loc["A", "B"] == pytest.approx(expected, abs=1e-12)
    assert result.loc["B", "A"] == pytest.approx(expected, abs=1e-12)


def test_perfectly_correlated_series() -> None:
    a = _series(
        [0.01, 0.02, 0.03, 0.04],
        [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 4)],
    )
    b = a * 2.0
    result = compute_correlation_matrix({"A": a, "B": b})
    assert result.loc["A", "B"] == pytest.approx(1.0, abs=1e-12)


def test_perfectly_anticorrelated_series() -> None:
    a = _series(
        [0.01, 0.02, 0.03, 0.04],
        [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 4)],
    )
    b = -a
    result = compute_correlation_matrix({"A": a, "B": b})
    assert result.loc["A", "B"] == pytest.approx(-1.0, abs=1e-12)


def test_constant_series_yields_nan() -> None:
    """Pandas surfaces NaN when one column has zero variance."""
    a = _series(
        [0.01, 0.02, 0.03, 0.04],
        [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 4)],
    )
    constant = _series(
        [0.01, 0.01, 0.01, 0.01],
        [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 4)],
    )
    result = compute_correlation_matrix({"A": a, "C": constant})
    assert math.isnan(result.loc["A", "C"])


def test_qt_consistency_against_dataframe_corr() -> None:
    """Result must equal ``df.corr(method="pearson")`` from the QT widget."""
    a = _series(
        [0.01, -0.02, 0.03, -0.005, 0.015],
        [
            date(2025, 1, 1),
            date(2025, 1, 2),
            date(2025, 1, 3),
            date(2025, 1, 4),
            date(2025, 1, 5),
        ],
    )
    b = _series(
        [0.005, -0.005, 0.025, 0.0, 0.01],
        [
            date(2025, 1, 1),
            date(2025, 1, 2),
            date(2025, 1, 3),
            date(2025, 1, 4),
            date(2025, 1, 5),
        ],
    )
    c = _series(
        [-0.01, 0.02, -0.02, 0.005, -0.005],
        [
            date(2025, 1, 1),
            date(2025, 1, 2),
            date(2025, 1, 3),
            date(2025, 1, 4),
            date(2025, 1, 5),
        ],
    )

    result = compute_correlation_matrix({"A": a, "B": b, "C": c})

    # QT reference: build a wide DataFrame and call .corr() directly.
    qt_df = pd.DataFrame({"A": a, "B": b, "C": c})
    qt_corr = qt_df.corr(method="pearson")

    for row in qt_corr.index:
        for col in qt_corr.columns:
            qt_value = qt_corr.loc[row, col]
            new_value = result.loc[row, col]
            if pd.isna(qt_value):
                assert pd.isna(new_value)
            else:
                assert abs(float(qt_value) - float(new_value)) < 1e-12


def test_order_preserved() -> None:
    """Output index/column order mirrors dict iteration order."""
    a = _series(
        [0.01, 0.02, 0.03],
        [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)],
    )
    series_dict = {"Z": a.copy(), "A": a.copy(), "M": a.copy()}
    result = compute_correlation_matrix(series_dict)
    assert list(result.index) == ["Z", "A", "M"]
    assert list(result.columns) == ["Z", "A", "M"]


def test_does_not_mutate_inputs() -> None:
    a = _series([0.01, 0.02, 0.03], [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)])
    snapshot = a.copy()
    _ = compute_correlation_matrix({"A": a})
    pd.testing.assert_series_equal(a, snapshot, check_names=False)


def test_two_series_contain_nan_only() -> None:
    a = pd.Series([np.nan, np.nan], index=[date(2025, 1, 1), date(2025, 1, 2)], dtype="float64")
    b = pd.Series([np.nan, np.nan], index=[date(2025, 1, 1), date(2025, 1, 2)], dtype="float64")
    result = compute_correlation_matrix({"A": a, "B": b})
    # No paired observations → NaN on the off-diagonal.
    assert math.isnan(result.loc["A", "B"])
