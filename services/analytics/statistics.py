# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Risk and distribution statistics — Implemented in sub-stream 5c.

Pure-Python migration of the QT calculation logic embedded in
``gui/widgets/statistics_widgets.py`` and
``gui/widgets/_statistics_helpers.py``. The QT widgets read from the
in-memory ``DataStore`` and call matplotlib in the same function;
this module is the calculation half — DB-free, Qt-free,
matplotlib-free — that both the web side (sub-stream 5c) and the
Phase-6 GUI-on-Postgres reorientation consume.

Conventions copied bit-for-bit from the QT side so the QT-consistency
tests pass to within ``1e-12``:

- **Annualisation of the mean.** Arithmetic, not geometric:
  ``mean_daily * 252``. The QT module uses ``np.nanmean(r) * 252``;
  changing this to a geometric annualisation would break the
  Sharpe-ratio numerics on the existing GUI screens.
- **Standard deviation.** Sample std (``ddof=1``). Annualisation
  scales by ``sqrt(252)``.
- **Variance.** Sample variance (``ddof=1``).
- **Skewness / kurtosis.** ``scipy.stats.skew`` /
  ``scipy.stats.kurtosis`` with default flags. ``kurtosis`` returns
  Fisher's *excess* kurtosis (normal distribution = 0). Both use
  ``nan_policy="omit"`` to match the QT call sites.
- **Sharpe ratio.** Annualised:
  ``(mean_annualised - risk_free) / std_annualised``. NaN when the
  annualised std is zero or undefined.
- **Maximum drawdown.** Computed on a NAV series — exactly as in the
  QT helper ``_max_drawdown`` and the QT widget table — by
  cumulating ``1 + r`` where ``r`` is the periodic return implied by
  the NAV ``pct_change`` and taking the minimum of
  ``(cumulative - cummax) / cummax``. This module exposes a thin
  wrapper that accepts either the NAV series directly or a return
  series, so callers can pass whichever they hold.
- **Lag-1 autocorrelation.** ``series.autocorr(lag=1)`` — pandas
  returns NaN for empty / one-element series, which we surface
  unchanged.

The functions take pandas Series as input and return floats so they
compose naturally with :mod:`services.analytics.investment_returns`
upstream.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import scipy.stats

from services.analytics._dtos import DistributionStats, RiskMetrics

PERIODS_PER_YEAR_DAILY: int = 252
PERIODS_PER_YEAR_MONTHLY: int = 12


# ---------------------------------------------------------------------------
# Distribution statistics
# ---------------------------------------------------------------------------


def compute_mean_return(return_series: pd.Series) -> float:
    """Arithmetic mean of a return series, ignoring NaN.

    Args:
        return_series: Pandas Series of periodic returns.

    Returns:
        ``np.nanmean(return_series)``. NaN when the series is empty
        or contains only NaN values.
    """
    if return_series.empty:
        return float("nan")
    return float(np.nanmean(return_series.to_numpy(dtype="float64")))


def annualise_mean_return(
    daily_mean: float, periods_per_year: int = PERIODS_PER_YEAR_DAILY
) -> float:
    """Arithmetic annualisation of a periodic mean return.

    Convention copied from the QT helper ``_annualised_mean``:
    ``mean * periods_per_year``. The web statistics surface and the
    QT statistics surface must agree to within ``1e-12``.

    Args:
        daily_mean: Periodic mean return (decimal).
        periods_per_year: Number of return periods per year. Daily
            returns: 252.

    Returns:
        ``daily_mean * periods_per_year``. NaN propagates.
    """
    return float(daily_mean) * float(periods_per_year)


def compute_std_dev(return_series: pd.Series) -> float:
    """Sample standard deviation (``ddof=1``) of a return series.

    Args:
        return_series: Pandas Series of periodic returns.

    Returns:
        ``np.nanstd(values, ddof=1)``. NaN when the series has fewer
        than two non-NaN values.
    """
    if return_series.empty:
        return float("nan")
    values = return_series.to_numpy(dtype="float64")
    valid = values[~np.isnan(values)]
    if valid.size < 2:
        return float("nan")
    return float(np.nanstd(values, ddof=1))


def annualise_std_dev(daily_std: float, periods_per_year: int = PERIODS_PER_YEAR_DAILY) -> float:
    """Annualise a periodic standard deviation by ``sqrt(periods_per_year)``.

    Args:
        daily_std: Periodic sample standard deviation.
        periods_per_year: Number of return periods per year.

    Returns:
        ``daily_std * sqrt(periods_per_year)``. NaN propagates.
    """
    return float(daily_std) * math.sqrt(float(periods_per_year))


def compute_variance(return_series: pd.Series) -> float:
    """Sample variance (``ddof=1``) of a return series.

    Args:
        return_series: Pandas Series of periodic returns.

    Returns:
        ``np.nanvar(values, ddof=1)``. NaN when the series has fewer
        than two non-NaN values.
    """
    if return_series.empty:
        return float("nan")
    values = return_series.to_numpy(dtype="float64")
    valid = values[~np.isnan(values)]
    if valid.size < 2:
        return float("nan")
    return float(np.nanvar(values, ddof=1))


def compute_skewness(return_series: pd.Series) -> float:
    """Sample skewness via :func:`scipy.stats.skew`, ignoring NaN.

    Args:
        return_series: Pandas Series of periodic returns.

    Returns:
        Biased estimator (``bias=True`` default). NaN for empty
        series and series whose variance is zero.
    """
    if return_series.empty:
        return float("nan")
    values = return_series.to_numpy(dtype="float64")
    if np.all(np.isnan(values)):
        return float("nan")
    result = scipy.stats.skew(values, nan_policy="omit")
    if result is None:
        return float("nan")
    try:
        return float(result)
    except (TypeError, ValueError):
        return float("nan")


def compute_kurtosis(return_series: pd.Series) -> float:
    """Excess kurtosis via :func:`scipy.stats.kurtosis`, ignoring NaN.

    Default scipy flags: Fisher's definition (normal distribution =
    0) and biased estimator. The QT widget calls
    ``scipy.stats.kurtosis(r, nan_policy="omit")`` with these
    defaults — this function preserves that convention so the QT
    and web surfaces agree to within ``1e-12``.

    Args:
        return_series: Pandas Series of periodic returns.

    Returns:
        Excess kurtosis (kurtosis - 3 for the Pearson definition).
        NaN for empty series.
    """
    if return_series.empty:
        return float("nan")
    values = return_series.to_numpy(dtype="float64")
    if np.all(np.isnan(values)):
        return float("nan")
    result = scipy.stats.kurtosis(values, nan_policy="omit")
    if result is None:
        return float("nan")
    try:
        return float(result)
    except (TypeError, ValueError):
        return float("nan")


def compute_median_return(return_series: pd.Series) -> float:
    """Median of a return series, ignoring NaN.

    Args:
        return_series: Pandas Series of periodic returns.

    Returns:
        ``np.nanmedian(values)``. NaN when the series is empty.
    """
    if return_series.empty:
        return float("nan")
    return float(np.nanmedian(return_series.to_numpy(dtype="float64")))


def compute_min_return(return_series: pd.Series) -> float:
    """Minimum of a return series, ignoring NaN.

    Args:
        return_series: Pandas Series of periodic returns.

    Returns:
        ``np.nanmin(values)``. NaN when the series is empty / all-NaN.
    """
    if return_series.empty:
        return float("nan")
    values = return_series.to_numpy(dtype="float64")
    if np.all(np.isnan(values)):
        return float("nan")
    return float(np.nanmin(values))


def compute_max_return(return_series: pd.Series) -> float:
    """Maximum of a return series, ignoring NaN.

    Args:
        return_series: Pandas Series of periodic returns.

    Returns:
        ``np.nanmax(values)``. NaN when the series is empty / all-NaN.
    """
    if return_series.empty:
        return float("nan")
    values = return_series.to_numpy(dtype="float64")
    if np.all(np.isnan(values)):
        return float("nan")
    return float(np.nanmax(values))


# ---------------------------------------------------------------------------
# Risk metrics
# ---------------------------------------------------------------------------


def compute_max_drawdown(nav_series: pd.Series) -> float:
    """Maximum drawdown of a NAV series.

    Mirrors the QT helper ``_max_drawdown`` but works on the NAV
    series directly so callers can pass the chronologically-sorted
    NAV history without first deriving the period returns. The
    function internally applies ``pct_change`` and reproduces the
    QT formula bit-for-bit:

    1. ``r = nav.pct_change().dropna()``
    2. ``cumulative = (1 + r).cumprod()``
    3. ``drawdown = (cumulative - cumulative.cummax()) /
       cumulative.cummax()``
    4. Return ``drawdown.min()``.

    The output is a negative decimal (``-0.272`` for -27.2%). Zero
    when the NAV series is monotone non-decreasing. NaN when the
    series has fewer than two non-NaN datapoints.

    Args:
        nav_series: NAV time series indexed by ``as_of_date``.

    Returns:
        Maximum drawdown as a negative decimal, or NaN when not
        computable.
    """
    cleaned = nav_series.dropna().sort_index()
    if len(cleaned) < 2:
        return float("nan")
    returns = cleaned.pct_change().dropna()
    if returns.empty:
        return float("nan")
    cumulative = (1.0 + returns).cumprod()
    running_max = cumulative.cummax()
    drawdown = (cumulative - running_max) / running_max
    return float(drawdown.min())


def compute_max_drawdown_from_returns(return_series: pd.Series) -> float:
    """Maximum drawdown computed directly from a return series.

    Companion to :func:`compute_max_drawdown` for callers that
    already hold the period returns (so the QT helper's
    ``(1 + r).cumprod()`` short-circuit remains exact). Mirrors
    ``gui/widgets/_statistics_helpers.py::_max_drawdown``.

    Args:
        return_series: Pandas Series of periodic returns.

    Returns:
        Maximum drawdown as a negative decimal, or NaN.
    """
    if return_series.empty:
        return float("nan")
    values = return_series.dropna()
    if values.empty:
        return float("nan")
    cumulative = (1.0 + values).cumprod()
    running_max = cumulative.cummax()
    drawdown = (cumulative - running_max) / running_max
    return float(drawdown.min())


def compute_underwater_series(return_series: pd.Series) -> pd.Series:
    """Drawdown-level ("underwater") profile of a return series.

    The level-series companion to :func:`compute_max_drawdown_from_returns`:
    instead of the scalar minimum, it returns the full path of
    peak-to-current drawdown. The maths mirror the scalar helper
    bit-for-bit so the two stay mutually consistent:

    1. ``cumulative = (1 + r).cumprod()``
    2. ``running_max = cumulative.cummax()``
    3. ``underwater = (cumulative - running_max) / running_max``

    Every value is ``<= 0`` (zero at each new high-water mark, negative
    below it). The output is indexed like the cleaned input, i.e. after
    ``dropna`` and ``sort_index``.

    Invariant (well-formed inputs of two or more datapoints):
    ``compute_underwater_series(r).min() ==
    compute_max_drawdown_from_returns(r)`` — the underwater minimum is the
    maximum drawdown.

    Args:
        return_series: Pandas Series of periodic returns.

    Returns:
        Pandas Series of non-positive drawdown levels (decimals; ``-0.12``
        for -12 %), indexed like the cleaned input. Empty Series when the
        input has fewer than two datapoints after ``dropna``.
    """
    cleaned = return_series.dropna().sort_index()
    if len(cleaned) < 2:
        return pd.Series(dtype="float64")
    cumulative = (1.0 + cleaned).cumprod()
    running_max = cumulative.cummax()
    return (cumulative - running_max) / running_max


def compute_sharpe_ratio(
    return_series: pd.Series,
    risk_free_rate_annual: float = 0.0,
    periods_per_year: int = PERIODS_PER_YEAR_DAILY,
) -> float:
    """Annualised Sharpe ratio with a configurable risk-free rate.

    Mirrors the QT helper ``_sharpe`` modulo the risk-free rate
    parameter (the QT helper hardcodes ``rf = 0.0``):

        mean_ann = mean(r) * periods_per_year
        std_ann  = std(r, ddof=1) * sqrt(periods_per_year)
        sharpe   = (mean_ann - rf) / std_ann

    NaN when the annualised standard deviation is zero or undefined
    so the QT GUI surface and the web surface render identical "N/A"
    cells for degenerate inputs.

    Args:
        return_series: Pandas Series of periodic returns.
        risk_free_rate_annual: Annualised risk-free rate (decimal).
            ``0.0`` matches the QT screens.
        periods_per_year: Number of return periods per year.

    Returns:
        Annualised Sharpe ratio, or NaN.
    """
    mean_ann = annualise_mean_return(compute_mean_return(return_series), periods_per_year)
    std_ann = annualise_std_dev(compute_std_dev(return_series), periods_per_year)
    if not math.isfinite(std_ann) or std_ann <= 0.0:
        return float("nan")
    return (mean_ann - risk_free_rate_annual) / std_ann


def compute_lag_1_autocorrelation(return_series: pd.Series) -> float:
    """Lag-1 autocorrelation of a return series.

    Defers to ``pandas.Series.autocorr(lag=1)`` so the result
    matches the QT helper ``_lag1_autocorr`` exactly. Returns NaN
    for empty / single-element series.

    Args:
        return_series: Pandas Series of periodic returns.

    Returns:
        ``series.autocorr(lag=1)``, or NaN.
    """
    cleaned = return_series.dropna()
    if cleaned.size < 2:
        return float("nan")
    val = cleaned.autocorr(lag=1)
    if val is None:
        return float("nan")
    return float(val)


def compute_value_at_risk(
    return_series: pd.Series,
    level: float = 0.95,
) -> float:
    """Historical Value-at-Risk at the given confidence level.

    Mirrors the QT helper used in
    ``gui/widgets/statistics_widgets.py::RiskTableWidget``:

        var = np.nanpercentile(r, (1 - level) * 100)

    ``level=0.95`` → 5th percentile of the return distribution.
    Returns the percentile value directly (a negative decimal for
    losses), matching the QT convention. NaN for empty series.

    Args:
        return_series: Pandas Series of periodic returns.
        level: Confidence level in (0, 1). Typical values 0.90,
            0.95, 0.99.

    Returns:
        Historical VaR as a decimal. Negative for losses. NaN when
        the series is empty.
    """
    if return_series.empty:
        return float("nan")
    values = return_series.to_numpy(dtype="float64")
    if np.all(np.isnan(values)):
        return float("nan")
    percentile = (1.0 - float(level)) * 100.0
    return float(np.nanpercentile(values, percentile))


def compute_conditional_value_at_risk(
    return_series: pd.Series,
    level: float = 0.95,
) -> float:
    """Historical Conditional VaR (Expected Shortfall) at the given level.

    Mirrors the QT helper:

        var = nanpercentile(r, (1-level)*100)
        tail = r[r <= var]
        cvar = mean(tail) if tail.size > 0 else NaN

    Returns the mean of the tail observations at or below the VaR
    threshold. Negative decimal for typical loss profiles.

    Args:
        return_series: Pandas Series of periodic returns.
        level: Confidence level in (0, 1).

    Returns:
        Historical CVaR as a decimal. NaN when the tail is empty
        or the input series is empty / all-NaN.
    """
    var = compute_value_at_risk(return_series, level=level)
    if not math.isfinite(var):
        return float("nan")
    values = return_series.to_numpy(dtype="float64")
    tail = values[~np.isnan(values)]
    tail = tail[tail <= var]
    if tail.size == 0:
        return float("nan")
    return float(np.mean(tail))


def compute_ulcer_index(return_series: pd.Series) -> float:
    """Ulcer Index — root mean square of percentage drawdowns.

    Mirrors the QT helper bit-for-bit:

        cumulative = (1 + r).cumprod()
        running_max = cumulative.cummax()
        drawdown_pct = ((cumulative - running_max) / running_max) * 100
        ulcer = sqrt(mean(drawdown_pct ** 2))

    The output is dimensionless (note the ``* 100`` inside the
    RMS — the Qt convention surfaces the index in percentage-point
    units, e.g. ``11.2906``). NaN for empty / all-NaN series.

    Args:
        return_series: Pandas Series of periodic returns.

    Returns:
        Ulcer Index in percentage-point units. NaN for empty input.
    """
    if return_series.empty:
        return float("nan")
    values = return_series.dropna()
    if values.empty:
        return float("nan")
    cumulative = (1.0 + values).cumprod()
    running_max = cumulative.cummax()
    drawdown_pct = ((cumulative - running_max) / running_max) * 100.0
    return float(np.sqrt(np.mean(drawdown_pct.to_numpy() ** 2)))


def compute_downside_deviation(
    return_series: pd.Series,
    *,
    annualise: bool = False,
    periods_per_year: int = PERIODS_PER_YEAR_DAILY,
) -> float:
    """Downside deviation — RMS of negative returns (zero-MAR).

    Mirrors the QT convention:

        negative = np.minimum(r, 0.0)
        dd = sqrt(mean(negative ** 2))

    The threshold is zero (MAR = 0.0); QT does not surface a
    configurable MAR. Annualisation is by ``sqrt(periods_per_year)``
    — controlled by the ``annualise`` flag because QT uses both
    variants:

    - The **Risk** table shows the un-annualised value
      (``annualise=False``).
    - The **Sortino ratio** in the Risk/Return table uses the
      annualised denominator (``annualise=True``).

    Args:
        return_series: Pandas Series of periodic returns.
        annualise: When ``True``, scale by ``sqrt(periods_per_year)``.
        periods_per_year: Annualisation factor when ``annualise``
            is set. Daily returns: 252.

    Returns:
        Downside deviation as a non-negative decimal. NaN for empty
        / all-NaN series.
    """
    if return_series.empty:
        return float("nan")
    values = return_series.dropna().to_numpy(dtype="float64")
    if values.size == 0:
        return float("nan")
    negative = np.minimum(values, 0.0)
    dd = float(np.sqrt(np.mean(negative**2)))
    if annualise:
        dd *= math.sqrt(float(periods_per_year))
    return dd


def compute_sortino_ratio(
    return_series: pd.Series,
    risk_free_rate_annual: float = 0.0,
    periods_per_year: int = PERIODS_PER_YEAR_DAILY,
) -> float:
    """Annualised Sortino ratio.

    Mirrors the QT helper:

        mean_ann = mean(r) * periods_per_year
        dd_ann = sqrt(mean(min(r, 0)**2)) * sqrt(periods_per_year)
        sortino = (mean_ann - rf) / dd_ann

    NaN when the annualised downside deviation is zero or
    undefined.

    Args:
        return_series: Pandas Series of periodic returns.
        risk_free_rate_annual: Annualised risk-free rate (decimal).
            ``0.0`` matches the QT screens.
        periods_per_year: Annualisation factor.

    Returns:
        Annualised Sortino ratio, or NaN.
    """
    mean_ann = annualise_mean_return(compute_mean_return(return_series), periods_per_year)
    dd_ann = compute_downside_deviation(
        return_series,
        annualise=True,
        periods_per_year=periods_per_year,
    )
    if not math.isfinite(dd_ann) or dd_ann <= 0.0:
        return float("nan")
    return (mean_ann - risk_free_rate_annual) / dd_ann


def compute_autocorrelation(
    return_series: pd.Series,
    lag: int,
) -> float:
    """Autocorrelation of a return series at an arbitrary lag.

    Generalisation of :func:`compute_lag_1_autocorrelation` to any
    positive integer lag. Defers to ``pandas.Series.autocorr(lag)``
    so the result matches the QT helper exactly.

    Args:
        return_series: Pandas Series of periodic returns.
        lag: Positive integer lag.

    Returns:
        ``series.autocorr(lag=lag)``, or NaN for series shorter
        than ``lag + 1`` non-NaN observations.

    Raises:
        ValueError: If ``lag`` is not a positive integer.
    """
    if not isinstance(lag, int) or lag < 1:
        raise ValueError(f"lag must be a positive integer, got {lag!r}")
    cleaned = return_series.dropna()
    if cleaned.size <= lag:
        return float("nan")
    val = cleaned.autocorr(lag=lag)
    if val is None:
        return float("nan")
    return float(val)


# ---------------------------------------------------------------------------
# Rolling-window metrics
# ---------------------------------------------------------------------------


def compute_rolling_volatility(
    return_series: pd.Series,
    *,
    window: int,
    periods_per_year: int = PERIODS_PER_YEAR_MONTHLY,
    annualise: bool = True,
) -> pd.Series:
    """Rolling sample standard deviation of a return series.

    Computes the trailing ``window``-period sample standard deviation
    (``ddof=1``) at each position, optionally annualised by
    ``sqrt(periods_per_year)``. Backs the rolling-12-month volatility
    figure of the mark-to-market KPI caption (ADR-0082 §5); with monthly
    returns the canonical call is ``window=12`` at the default
    :data:`PERIODS_PER_YEAR_MONTHLY`.

    ``min_periods`` equals ``window``, so positions with fewer than a full
    window of observations are ``NaN`` (no partial-window volatility). The
    output index matches the input index exactly — the series is **not**
    sorted or NaN-dropped first, so the caller is responsible for passing a
    chronologically ordered return series.

    Args:
        return_series: Pandas Series of periodic returns.
        window: Number of periods in the rolling window. Must be a
            positive integer.
        periods_per_year: Annualisation factor used when ``annualise`` is
            set. Monthly returns: 12.
        annualise: When ``True`` (default), scale each window's standard
            deviation by ``sqrt(periods_per_year)``.

    Returns:
        Pandas Series of rolling volatilities indexed like the input, with
        leading ``NaN`` for the first ``window - 1`` positions.

    Raises:
        ValueError: If ``window`` is not a positive integer.
    """
    if not isinstance(window, int) or window < 1:
        raise ValueError(f"window must be a positive integer, got {window!r}")
    rolling_std = return_series.rolling(window=window, min_periods=window).std(ddof=1)
    if annualise:
        rolling_std = rolling_std * math.sqrt(float(periods_per_year))
    return rolling_std


def compute_rolling_sharpe(
    return_series: pd.Series,
    *,
    window: int,
    risk_free_rate_annual: float = 0.0,
    periods_per_year: int = PERIODS_PER_YEAR_MONTHLY,
) -> pd.Series:
    """Rolling annualised Sharpe ratio of a return series.

    For each trailing ``window`` of periods the Sharpe ratio is

        mean(excess) / std(excess, ddof=1) * sqrt(periods_per_year)

    where ``excess = r - risk_free_rate_annual / periods_per_year`` is the
    per-period excess return. Backs the rolling-12-month Sharpe figure of
    the mark-to-market KPI caption (ADR-0082 §5); with monthly returns the
    canonical call is ``window=12`` at the default
    :data:`PERIODS_PER_YEAR_MONTHLY`.

    ``min_periods`` equals ``window`` (leading ``NaN`` for incomplete
    windows). A window whose excess-return standard deviation is exactly
    zero yields ``NaN`` at that position rather than ``±inf`` — the
    no-silent-fallback discipline (ADR-0079 §3). The output index matches
    the input index exactly; the caller passes a chronologically ordered
    series.

    Args:
        return_series: Pandas Series of periodic returns.
        window: Number of periods in the rolling window. Must be a
            positive integer.
        risk_free_rate_annual: Annualised risk-free rate (decimal),
            de-annualised per period before subtraction.
        periods_per_year: Annualisation factor. Monthly returns: 12.

    Returns:
        Pandas Series of rolling annualised Sharpe ratios indexed like the
        input, with leading ``NaN`` for the first ``window - 1`` positions
        and ``NaN`` wherever the window's excess-return std is zero.

    Raises:
        ValueError: If ``window`` is not a positive integer.
    """
    if not isinstance(window, int) or window < 1:
        raise ValueError(f"window must be a positive integer, got {window!r}")
    excess = return_series - risk_free_rate_annual / float(periods_per_year)
    rolling = excess.rolling(window=window, min_periods=window)
    rolling_mean = rolling.mean()
    rolling_std = rolling.std(ddof=1)
    sharpe = rolling_mean / rolling_std * math.sqrt(float(periods_per_year))
    # Zero-variance windows divide to ±inf (or 0/0 → NaN); surface an
    # explicit NaN at those positions. Leading NaN std (incomplete window)
    # already propagates a NaN sharpe and is left untouched.
    return sharpe.where(rolling_std != 0.0)


# ---------------------------------------------------------------------------
# Convenience bundles
# ---------------------------------------------------------------------------


def compute_full_distribution_stats(
    return_series: pd.Series,
    *,
    periods_per_year: int = PERIODS_PER_YEAR_DAILY,
) -> DistributionStats:
    """Compute every distribution statistic in one pass.

    Reduces boilerplate in the service layer: a single call returns
    all ten descriptors mirrored on the QT Distribution table. The
    individual ``compute_*`` functions remain the primitive building
    blocks for finer-grained callers (and are what the QT-consistency
    tests target).

    Args:
        return_series: Pandas Series of periodic returns.
        periods_per_year: Used by both
            :func:`annualise_mean_return` and
            :func:`annualise_std_dev`.

    Returns:
        Frozen :class:`DistributionStats` dataclass. Each field is a
        float; degenerate inputs surface as NaN per the per-function
        contracts.
    """
    mean_daily = compute_mean_return(return_series)
    std_daily = compute_std_dev(return_series)
    return DistributionStats(
        mean_daily=mean_daily,
        mean_annualised=annualise_mean_return(mean_daily, periods_per_year),
        std_daily=std_daily,
        std_annualised=annualise_std_dev(std_daily, periods_per_year),
        variance_daily=compute_variance(return_series),
        skewness=compute_skewness(return_series),
        kurtosis_excess=compute_kurtosis(return_series),
        median=compute_median_return(return_series),
        min_return=compute_min_return(return_series),
        max_return=compute_max_return(return_series),
    )


def compute_risk_metrics(
    return_series: pd.Series,
    nav_series: pd.Series | None = None,
    *,
    risk_free_rate_annual: float = 0.0,
    periods_per_year: int = PERIODS_PER_YEAR_DAILY,
) -> RiskMetrics:
    """Compute every risk / risk-return / autocorrelation metric.

    All calculations follow the QT widget conventions. MDD is
    computed from the return series (matches the QT Risk-table
    body — note this is a behaviour change from sub-stream 5c,
    which used the NAV-based variant; the two agree on
    well-formed data but differ on degenerate short series).

    The ``nav_series`` parameter is accepted for backwards
    compatibility but is not used in the calculation. Callers
    that pass it can drop the argument; callers that omit it
    work unchanged.

    Args:
        return_series: Pandas Series of periodic returns.
        nav_series: Deprecated. Ignored. Kept for ABI continuity.
        risk_free_rate_annual: Annualised risk-free rate.
        periods_per_year: Annualisation factor.

    Returns:
        Frozen :class:`RiskMetrics` with all 13 fields populated.
        Empty input → all-NaN bundle.
    """
    del nav_series
    return RiskMetrics(
        var_90_daily=compute_value_at_risk(return_series, level=0.90),
        var_95_daily=compute_value_at_risk(return_series, level=0.95),
        var_99_daily=compute_value_at_risk(return_series, level=0.99),
        cvar_95_daily=compute_conditional_value_at_risk(return_series, level=0.95),
        max_drawdown=compute_max_drawdown_from_returns(return_series),
        ulcer_index=compute_ulcer_index(return_series),
        downside_deviation=compute_downside_deviation(
            return_series, annualise=False, periods_per_year=periods_per_year
        ),
        sharpe_ratio=compute_sharpe_ratio(
            return_series,
            risk_free_rate_annual=risk_free_rate_annual,
            periods_per_year=periods_per_year,
        ),
        sortino_ratio=compute_sortino_ratio(
            return_series,
            risk_free_rate_annual=risk_free_rate_annual,
            periods_per_year=periods_per_year,
        ),
        lag_1_autocorrelation=compute_autocorrelation(return_series, 1),
        lag_2_autocorrelation=compute_autocorrelation(return_series, 2),
        lag_3_autocorrelation=compute_autocorrelation(return_series, 3),
        lag_4_autocorrelation=compute_autocorrelation(return_series, 4),
    )
