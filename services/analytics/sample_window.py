# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Common-observation-window helper for time-series analytics.

When fund-of-funds investments have non-overlapping lifetimes (vintages
starting and ending in different years), pandas.DataFrame.cov() default
behaviour computes each cell on its own pairwise-complete sample. The
resulting matrix is not guaranteed to be positive semi-definite, which
breaks any downstream mean-variance optimisation.

This module provides a single function: restrict a returns DataFrame
to the rows where ALL columns are observed. No interpolation, no
extrapolation — daily return inputs are treated as fixed and exogenous.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class WindowReport:
    """Diagnostic detail for a common-observation-window restriction.

    Attributes:
        n_rows_input: Row count of the input DataFrame.
        n_rows_complete: Row count after restricting to complete-case rows.
        window_start: First date in the restricted window (None if empty).
        window_end:   Last  date in the restricted window (None if empty).
        binding_start_columns: Columns whose first valid index defines
            window_start (i.e. the late-starting investments).
        binding_end_columns:   Columns whose last  valid index defines
            window_end   (i.e. the early-ending  investments).
    """

    n_rows_input: int
    n_rows_complete: int
    window_start: pd.Timestamp | None
    window_end: pd.Timestamp | None
    binding_start_columns: list[str]
    binding_end_columns: list[str]


def restrict_to_common_window(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, WindowReport]:
    """Restrict a returns DataFrame to rows where every column is observed.

    Daily return data is exogenous — this function performs no imputation,
    interpolation, or fill. It returns the largest contiguous-or-not subset
    of input rows for which every column has a non-NaN value.

    Use this before computing a sample covariance matrix when the columns
    represent investments with potentially non-overlapping lifetimes.

    Args:
        df: DataFrame with a DatetimeIndex and one column per investment.
            Cells may be NaN to indicate "no observation on this date".

    Returns:
        Tuple of (restricted DataFrame, WindowReport). The DataFrame
        retains the original index dtype and column order.

    Raises:
        ValueError: If df has fewer than 2 columns or no DatetimeIndex.
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("restrict_to_common_window requires a DatetimeIndex")
    if df.shape[1] < 2:
        raise ValueError(f"restrict_to_common_window requires >= 2 columns, got {df.shape[1]}")

    n_input = len(df)
    df_complete = df.dropna(how="any")
    n_complete = len(df_complete)

    if n_complete == 0:
        return df_complete, WindowReport(
            n_rows_input=n_input,
            n_rows_complete=0,
            window_start=None,
            window_end=None,
            binding_start_columns=[],
            binding_end_columns=[],
        )

    # Diagnostics: identify which columns drive the window edges.
    firsts = {c: df[c].first_valid_index() for c in df.columns}
    lasts = {c: df[c].last_valid_index() for c in df.columns}
    # Drop columns that never have any data — they cannot bind the window.
    firsts = {c: v for c, v in firsts.items() if v is not None}
    lasts = {c: v for c, v in lasts.items() if v is not None}

    latest_start = max(firsts.values())
    earliest_end = min(lasts.values())

    binding_start = sorted(c for c, v in firsts.items() if v == latest_start)
    binding_end = sorted(c for c, v in lasts.items() if v == earliest_end)

    return df_complete, WindowReport(
        n_rows_input=n_input,
        n_rows_complete=n_complete,
        window_start=df_complete.index[0],
        window_end=df_complete.index[-1],
        binding_start_columns=binding_start,
        binding_end_columns=binding_end,
    )
