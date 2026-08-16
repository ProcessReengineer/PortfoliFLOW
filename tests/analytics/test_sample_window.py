# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for services.analytics.sample_window.restrict_to_common_window."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from services.analytics.sample_window import WindowReport, restrict_to_common_window


def _daily_index(start: str, end: str) -> pd.DatetimeIndex:
    """Business-day-frequency convenience for compact test setup."""
    return pd.date_range(start=start, end=end, freq="B")


def test_full_overlap_returns_input_unchanged() -> None:
    idx = _daily_index("2020-01-01", "2020-12-31")
    df = pd.DataFrame(
        {
            "A": np.linspace(0.001, 0.002, len(idx)),
            "B": np.linspace(0.0005, 0.0015, len(idx)),
        },
        index=idx,
    )

    df_out, report = restrict_to_common_window(df)

    assert report.n_rows_input == len(idx)
    assert report.n_rows_complete == len(idx)
    assert report.window_start == idx[0]
    assert report.window_end == idx[-1]
    pd.testing.assert_frame_equal(df_out, df)


def test_staggered_start_window_starts_at_late_columns_first_valid_date() -> None:
    idx = _daily_index("2017-01-01", "2026-12-31")
    df = pd.DataFrame(index=idx, columns=["A", "B"], dtype=float)
    # A is observed across the full range
    df["A"] = 0.001
    # B only starts one year after A
    b_start = pd.Timestamp("2018-01-01")
    df.loc[df.index >= b_start, "B"] = 0.0008

    df_out, report = restrict_to_common_window(df)

    expected_first = df_out.index[0]
    assert expected_first >= b_start
    assert report.binding_start_columns == ["B"]
    # Common window's start equals B's first valid index
    assert report.window_start == df["B"].first_valid_index()


def test_staggered_end_window_ends_at_early_columns_last_valid_date() -> None:
    idx = _daily_index("2017-01-01", "2026-12-31")
    df = pd.DataFrame(index=idx, columns=["A", "B"], dtype=float)
    df["B"] = 0.001
    # A ends one year earlier than B
    a_end = pd.Timestamp("2025-12-31")
    df.loc[df.index <= a_end, "A"] = 0.0009

    df_out, report = restrict_to_common_window(df)

    assert report.binding_end_columns == ["A"]
    assert report.window_end == df["A"].last_valid_index()
    assert df_out.index[-1] == report.window_end


def test_three_columns_overlap_window() -> None:
    idx = _daily_index("2017-01-01", "2030-12-31")
    df = pd.DataFrame(index=idx, columns=["A", "B", "C"], dtype=float)
    df.loc[(idx >= "2017-01-01") & (idx <= "2026-12-31"), "A"] = 0.001
    df.loc[(idx >= "2019-01-01") & (idx <= "2028-12-31"), "B"] = 0.0008
    df.loc[(idx >= "2021-01-01") & (idx <= "2030-12-31"), "C"] = 0.0006

    _, report = restrict_to_common_window(df)

    # Latest start is C (2021), earliest end is A (2026)
    assert report.binding_start_columns == ["C"]
    assert report.binding_end_columns == ["A"]
    assert report.window_start == df["C"].first_valid_index()
    assert report.window_end == df["A"].last_valid_index()


def test_disjoint_lifetimes_yield_empty_window() -> None:
    idx = _daily_index("2017-01-01", "2025-12-31")
    df = pd.DataFrame(index=idx, columns=["A", "B"], dtype=float)
    df.loc[(idx >= "2017-01-01") & (idx <= "2019-12-31"), "A"] = 0.001
    df.loc[(idx >= "2021-01-01") & (idx <= "2023-12-31"), "B"] = 0.0008

    df_out, report = restrict_to_common_window(df)

    assert df_out.empty
    assert report.n_rows_complete == 0
    assert report.window_start is None
    assert report.window_end is None
    assert report.binding_start_columns == []
    assert report.binding_end_columns == []


def test_single_column_raises_value_error() -> None:
    idx = _daily_index("2020-01-01", "2020-12-31")
    df = pd.DataFrame({"A": np.zeros(len(idx))}, index=idx)
    with pytest.raises(ValueError, match=">= 2 columns"):
        restrict_to_common_window(df)


def test_non_datetime_index_raises_value_error() -> None:
    df = pd.DataFrame({"A": [0.0, 0.1], "B": [0.05, 0.15]})
    with pytest.raises(ValueError, match="DatetimeIndex"):
        restrict_to_common_window(df)


def test_column_order_is_preserved() -> None:
    idx = _daily_index("2020-01-01", "2020-06-30")
    df = pd.DataFrame(
        {
            "Zeta": np.full(len(idx), 0.001),
            "Alpha": np.full(len(idx), 0.002),
            "Mu": np.full(len(idx), 0.0015),
        },
        index=idx,
    )

    df_out, _ = restrict_to_common_window(df)

    assert list(df_out.columns) == ["Zeta", "Alpha", "Mu"]


def test_restricted_cov_is_psd_for_non_overlapping_lifetimes() -> None:
    """Synthetic 4-asset case with non-overlapping lifetimes — restricted
    covariance must be positive semi-definite (min eig >= -1e-12)."""
    rng = np.random.default_rng(seed=42)
    full_idx = _daily_index("2015-01-01", "2026-12-31")

    # Each asset is observed only over a sub-window. Lifetimes overlap
    # in a common window 2019-01-01 .. 2024-12-31 (~6 years of business days).
    spans = {
        "Vintage_2015": ("2015-01-01", "2024-06-30"),
        "Vintage_2017": ("2017-03-01", "2025-12-31"),
        "Vintage_2019": ("2019-01-01", "2026-12-31"),
        "Vintage_2020": ("2020-06-30", "2026-12-31"),
    }
    df = pd.DataFrame(index=full_idx, columns=list(spans), dtype=float)
    for col, (start, end) in spans.items():
        mask = (full_idx >= start) & (full_idx <= end)
        # i.i.d. normal returns with realistic daily scale
        df.loc[mask, col] = rng.normal(loc=0.0005, scale=0.01, size=int(mask.sum()))

    # Pairwise cov on the raw frame: not guaranteed PSD
    df_window, report = restrict_to_common_window(df)
    assert report.n_rows_complete > 50  # sanity — common window non-trivial

    cov = df_window.cov().to_numpy()
    eig_min = float(np.linalg.eigvalsh(cov).min())
    assert eig_min >= -1e-12, f"restricted cov not PSD: min eig = {eig_min:.2e}"


def test_window_report_shape_for_full_overlap() -> None:
    """Spot-check the WindowReport dataclass field types end-to-end."""
    idx = _daily_index("2020-01-01", "2020-03-31")
    df = pd.DataFrame({"A": np.zeros(len(idx)), "B": np.zeros(len(idx))}, index=idx)
    _, report = restrict_to_common_window(df)
    assert isinstance(report, WindowReport)
    assert isinstance(report.window_start, pd.Timestamp)
    assert isinstance(report.window_end, pd.Timestamp)
