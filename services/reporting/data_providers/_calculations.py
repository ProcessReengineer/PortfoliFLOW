# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared low-level calculations for the reporting providers.

These helpers are reused by :class:`MultiplesProvider`, :class:`IRRProvider`
and :class:`KeyFiguresProvider` so that the numbers shown in the key-figures
strip are guaranteed consistent with the per-tile charts.

Defensive against CF Out sign inconsistencies — see CLAUDE.md Excel import schema
for the canonical convention.  Capital calls are always treated as positive
*magnitudes*; whenever a negative-cashflow stream is required for IRR
root-finding, the magnitude is explicitly negated.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.optimize import brentq


def cumulative_calls_magnitude(cf_out: pd.Series, report_date: pd.Timestamp) -> float:
    """Return the absolute cumulative capital called up to (and including) ``report_date``.

    Args:
        cf_out: ``cash_flow_out_actual`` series for one investment (or the
            sum across investments).
        report_date: The as-of date.

    Returns:
        Sum of absolute cashflow-out magnitudes on or before ``report_date``.
        Returns ``0.0`` if the series is empty.
    """
    series = cf_out.fillna(0.0).loc[cf_out.index <= report_date]
    if series.empty:
        return 0.0
    return float(series.abs().cumsum().iloc[-1])


def cumulative_distributions(cf_in: pd.Series, report_date: pd.Timestamp) -> float:
    """Return the cumulative distributions received up to ``report_date``.

    Args:
        cf_in: ``cash_flow_in_actual`` series for one investment.
        report_date: The as-of date.

    Returns:
        Cumulative sum on or before ``report_date``.  ``0.0`` if empty.
    """
    series = cf_in.fillna(0.0).loc[cf_in.index <= report_date]
    if series.empty:
        return 0.0
    return float(series.cumsum().iloc[-1])


def latest_nav(nav: pd.Series, report_date: pd.Timestamp) -> float:
    """Return the most recent non-NaN NAV on or before ``report_date``.

    Args:
        nav: ``navs_actual`` series for one investment (or the sum across
            investments).
        report_date: The as-of date.

    Returns:
        Latest non-NaN NAV value.  Returns ``0.0`` if no value is available.
    """
    series = nav.dropna().loc[nav.dropna().index <= report_date]
    if series.empty:
        return 0.0
    return float(series.iloc[-1])


def compute_tvpi_dpi(
    cum_calls_mag: float,
    cum_distributions: float,
    nav_value: float,
) -> tuple[float, float]:
    """Compute TVPI and DPI from aggregated cashflow components.

    Args:
        cum_calls_mag: Magnitude of cumulative calls (always non-negative).
        cum_distributions: Cumulative distributions (non-negative).
        nav_value: Latest NAV value (non-negative).

    Returns:
        A ``(tvpi, dpi)`` tuple.  Both are :data:`math.nan` if
        ``cum_calls_mag`` is zero.
    """
    if cum_calls_mag <= 0.0:
        return math.nan, math.nan
    tvpi = (cum_distributions + nav_value) / cum_calls_mag
    dpi = cum_distributions / cum_calls_mag
    return tvpi, dpi


def compute_irr(
    cf_in: pd.Series,
    cf_out: pd.Series,
    nav_value: float,
    report_date: pd.Timestamp,
) -> float:
    """Compute the IRR of a single cashflow stream.

    The signed cashflow stream is built as ``cf_in - abs(cf_out)`` (forcing
    capital calls to be negative regardless of input sign), filtered to dates
    on or before ``report_date``, then ``nav_value`` is appended at
    ``report_date`` as a synthetic terminal cashflow.  The IRR is found by
    Brent's method on ``[-0.99, 10.0]``.

    Args:
        cf_in: ``cash_flow_in_actual`` series.
        cf_out: ``cash_flow_out_actual`` series.  Sign is normalised
            internally (``-abs(...)``).
        nav_value: Latest NAV at ``report_date``.  Treated as a positive
            terminal cashflow if greater than zero.
        report_date: The as-of date.

    Returns:
        The IRR as a decimal (e.g. ``0.123`` for 12.3 %), or :data:`numpy.nan`
        if fewer than two cashflows exist or the root finder fails to
        converge.
    """
    in_clean = cf_in.fillna(0.0)
    out_clean = -cf_out.fillna(0.0).abs()

    in_clean = in_clean[in_clean.index <= report_date]
    out_clean = out_clean[out_clean.index <= report_date]

    combined = in_clean.add(out_clean, fill_value=0.0)
    combined = combined[combined != 0.0]
    if combined.empty:
        return float("nan")

    if nav_value > 0.0:
        terminal = pd.Series({pd.Timestamp(report_date): float(nav_value)})
        combined = combined.add(terminal, fill_value=0.0)

    combined = combined[combined != 0.0].sort_index()
    if len(combined) < 2:
        return float("nan")

    t0 = combined.index.min()
    times = np.array([(d - t0).days / 365.25 for d in combined.index], dtype=float)
    flows = combined.to_numpy(dtype=float)

    def npv(rate: float) -> float:
        return float(np.sum(flows / np.power(1.0 + rate, times)))

    lo, hi = -0.99, 10.0
    try:
        f_lo = npv(lo)
        f_hi = npv(hi)
    except (FloatingPointError, OverflowError):
        return float("nan")

    if not np.isfinite(f_lo) or not np.isfinite(f_hi):
        return float("nan")
    if f_lo * f_hi > 0:
        return float("nan")

    try:
        return float(brentq(npv, lo, hi, maxiter=200, xtol=1e-7))
    except (ValueError, RuntimeError):
        return float("nan")
