# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Benchmark comparison and SAA-hypothetical analytics.

Pure-Python calculations for the three Phase-1 blocks of the
"Benchmarks & Attribution" feature (ADR-0061):

    a) per-investment benchmark comparison (twelve metrics across
       five conceptual groups: Excess Return, Alpha+Beta+R²,
       Tracking Error + Information Ratio, Up/Down Capture, and
       Sharpe Differential),
    b) NAV-weighted asset-class composite return series
       (Beginning-of-Period TWR, GIPS-compatible methodology),
    c) SAA-hypothetical return series that combine SAA weights with
       benchmark returns (Variant I) or own-fund composite returns
       (Variant II), accompanied by the actual portfolio return
       series.

The functions take pandas Series / dicts as input and return frozen
dataclasses or lists thereof. Per ADR-0013 and ADR-0045 §3 this
module is DB-free, FastAPI-free, and Qt-free — alignment and DB
access live in the service-layer caller (Phase 1 Prompt 3).

Methodology notes:

- **Monthly grid.** All metrics aggregate to a month-end-indexed
  monthly grid. Daily input series are resampled by compounding
  ``r_m = (1 + r_d).prod() - 1`` over the calendar month.
- **Arithmetic excess return.** ``r_i - r_b`` per month;
  annualised by ``mean * 12``. Total excess return is the
  difference of cumulative returns (geometric). ADR-0061
  §Rationale "Why arithmetic excess return".
- **TWR composites.** NAV-weighted Beginning-of-Period — the
  weight for month ``m`` is the NAV at end of month ``m-1``.
- **Forward-fill on NAVs.** Illiquid asset classes report NAVs
  quarterly. Forward-filling produces an honest series of two
  zero-return months followed by the quarterly spike rather than
  the artificially smooth series that linear interpolation would
  imply. See ADR-0061 §Rationale "Why Forward-Fill on NAVs".
- **No caching, no DB.** Phase 1 computes live. The orchestration
  layer (``BenchmarkComparisonService`` in Prompt 3) is the only
  consumer that touches Postgres.

References:
    - ADR-0013 (Analytics layer pure and stateless)
    - ADR-0045 §3 (Analytics Service Foundation — DTO convention)
    - ADR-0061 (Benchmarks & Attribution architecture)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkComparisonMetrics:
    """All Phase-1 metrics for a single (investment, benchmark) pair.

    Twelve fields covering five conceptual metric groups:
      - Excess Return (total + annualised)
      - Regression-based (Alpha, Beta, R-squared)
      - Risk-adjusted active (Tracking Error, Information Ratio)
      - Up/Down Capture
      - Sharpe Differential

    NaN sentinels indicate that the metric could not be computed
    given the available aligned observations. The ``n_observations``
    diagnostic field documents the aligned-pair count after the
    inner-join of investment, benchmark, and risk-free series.

    All annualised quantities use the monthly-to-annual convention
    (×12 for returns, ×sqrt(12) for volatilities) consistent with
    the rest of the analytics layer.
    """

    excess_return_total: float
    excess_return_annualised: float
    alpha_annualised: float
    beta: float
    r_squared: float
    tracking_error_annualised: float
    information_ratio: float
    up_capture_ratio: float
    down_capture_ratio: float
    sharpe_investment: float
    sharpe_benchmark: float
    sharpe_difference: float
    n_observations: int
    period_start: date
    period_end: date


@dataclass(frozen=True)
class AssetClassCompositeSeries:
    """One NAV-weighted composite return series for a single asset class.

    Produced by NAV-weighted Beginning-of-Period (BoP) aggregation
    of all investments belonging to the asset class. TWR
    methodology, GIPS-compatible. Months where no constituent
    investment had a valid BoP NAV are dropped.

    Attributes:
        asset_class_code: Tenant-scoped asset class identifier
            (e.g. ``"private_equity"``, ``"ig_credit"``).
        monthly_returns: Pandas Series indexed by month-end
            ``pd.Timestamp``; values are decimal returns
            (``0.02`` = 2 %).
        n_investments: How many distinct investments contributed
            at any point in the period.
        period_start: Earliest non-empty month.
        period_end: Latest non-empty month.
    """

    asset_class_code: str
    monthly_returns: pd.Series
    n_investments: int
    period_start: date
    period_end: date


@dataclass(frozen=True)
class SAAHypotheticalSeries:
    """Two hypothetical portfolio return series plus the actual.

    Variant I  (``saa_x_benchmark``)  : SAA weights × benchmark
        returns per asset class. Answers: "What would the SAA
        itself have produced, irrespective of own-fund manager
        selection?"
    Variant II (``saa_x_composite``)  : SAA weights × own-fund
        composite returns per asset class. Answers: "What would
        the SAA have produced if I had rebalanced my own funds to
        the SAA weights?"
    Actual (``actual_portfolio_returns``): NAV-weighted actuals
        across all investments at portfolio level. The reference
        series.

    Brinson decomposition (Selection vs Allocation effect) follows
    from these three series in Phase 2; it is not produced here.

    Attributes:
        saa_label: Operator-meaningful identifier of the SAA weight
            set ("Target weights — Standard 2026", "Tangency —
            Standard 2026", etc.).
        saa_weights: Dict mapping asset_class_code → weight. Sums
            to ``1.0``; asset classes without a benchmark mapping
            are permitted in the dict but contribute zero to the
            hypothetical Variant I (no benchmark to multiply with).
        saa_x_benchmark: Monthly return series (decimal).
        saa_x_composite: Monthly return series (decimal).
        actual_portfolio_returns: Monthly return series (decimal).
        period_start, period_end: Aligned period bounds (union of
            the input indices — see
            :func:`compute_saa_hypothetical_series`).
    """

    saa_label: str
    saa_weights: dict[str, float]
    saa_x_benchmark: pd.Series
    saa_x_composite: pd.Series
    actual_portfolio_returns: pd.Series
    period_start: date
    period_end: date


@dataclass(frozen=True)
class MonthlyReturnSeries:
    """Pair of date-indexed monthly returns and the source identifier.

    Used as a tagged container so functions can accept lists of
    series without needing parallel arrays.
    """

    identifier: str
    monthly_returns: pd.Series


@dataclass(frozen=True)
class MonthlyNAVSeries:
    """Pair of date-indexed monthly NAVs (BoP-ready) and the identifier.

    The NAV value at month-end ``t`` doubles as the Beginning-of-
    Period NAV for month ``t + 1`` in the composite weighting.
    """

    identifier: str
    monthly_navs: pd.Series


@dataclass(frozen=True)
class BenchmarkComparisonBundle:
    """Convenience aggregate of metrics + the underlying aligned series.

    Stage-a callers typically want both the metrics and the
    monthly series themselves (for charting). This bundle bundles
    them so the call site doesn't have to align twice.
    """

    investment_identifier: str
    benchmark_identifier: str
    metrics: BenchmarkComparisonMetrics
    aligned_investment_returns: pd.Series
    aligned_benchmark_returns: pd.Series
    aligned_excess_returns: pd.Series


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def normalise_monthly_index(series: pd.Series) -> pd.Series:
    """Normalise a monthly series to a tz-naive, time-stripped
    ``pd.DatetimeIndex``.

    The aligning ``pd.concat(..., join="inner")`` relies on
    matching index values bit-for-bit; differences in tz-awareness
    or intraday timestamps would silently drop rows that should
    align. Stripping to date-normalised tz-naive timestamps gives a
    consistent alignment key irrespective of the caller's
    convention.

    Public so that service-layer callers building *display* series
    outside the inner-join can key on the same normalisation the
    join uses — a second, hand-rolled tz-normalisation in the
    service layer would be drift waiting to happen (ADR-0113 §5).
    """
    if series.empty:
        return series.copy()
    idx = pd.to_datetime(series.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    idx = idx.normalize()
    return pd.Series(series.to_numpy(dtype="float64"), index=idx)


def _align_monthly_series(*series: pd.Series) -> list[pd.Series]:
    """Inner-join multiple monthly series on a common month-end index.

    Args:
        *series: Two or more pandas Series. Indexes are normalised
            (tz-stripped, time-stripped) before the inner join so
            that callers using different tz conventions still
            align.

    Returns:
        A list of aligned Series in the same order as the inputs,
        each restricted to the common index. Returns a list of
        empty Series if any input is empty or the inner join is
        empty.
    """
    if not series:
        return []
    normalised = [normalise_monthly_index(s) for s in series]
    if any(s.empty for s in normalised):
        return [pd.Series(dtype="float64") for _ in series]
    frame = pd.concat(normalised, axis=1, join="inner")
    if frame.empty:
        return [pd.Series(dtype="float64") for _ in series]
    return [frame.iloc[:, i].dropna().copy() for i in range(frame.shape[1])]


def _resample_daily_to_monthly_return(daily_returns: pd.Series) -> pd.Series:
    """Compound daily returns to month-end-stamped monthly returns.

    Uses the standard period-return compounding identity
    ``r_m = (1 + r_d).prod() - 1`` over the calendar month. Months
    with no daily observations are absent from the result (rather
    than emitted as ``0.0``).

    Args:
        daily_returns: Pandas Series indexed by ``pd.Timestamp``,
            values are decimal daily returns.

    Returns:
        Pandas Series indexed by month-end ``pd.Timestamp``,
        values are decimal monthly returns. Empty when the input
        is empty.
    """
    if daily_returns.empty:
        return pd.Series(dtype="float64")
    sorted_returns = daily_returns.dropna().sort_index()
    if sorted_returns.empty:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime(sorted_returns.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    sorted_returns = pd.Series(sorted_returns.to_numpy(dtype="float64"), index=idx)
    monthly = sorted_returns.resample("ME").apply(
        lambda x: (1.0 + x).prod() - 1.0 if len(x) else float("nan")
    )
    return monthly.dropna()


def _forward_fill_daily_navs(daily_navs: pd.Series) -> pd.Series:
    """Forward-fill a daily NAV series across calendar gaps.

    Reindexes onto a dense calendar-day range
    ``[first_obs, last_obs]`` and forward-fills missing values.
    Methodology choice per ADR-0061 §Rationale "Why Forward-Fill
    on NAVs": illiquid asset classes get an honest series of
    flat-line months followed by a quarterly spike, rather than an
    artificially smooth linear interpolation.

    Args:
        daily_navs: Pandas Series indexed by ``pd.Timestamp``,
            values are NAV amounts in the investment's currency.

    Returns:
        Pandas Series on a daily ``pd.DatetimeIndex`` covering the
        observation range, with forward-filled values. Empty when
        input is empty.
    """
    if daily_navs.empty:
        return pd.Series(dtype="float64")
    sorted_navs = daily_navs.dropna().sort_index()
    if sorted_navs.empty:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime(sorted_navs.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    sorted_navs = pd.Series(sorted_navs.to_numpy(dtype="float64"), index=idx)
    full_range = pd.date_range(sorted_navs.index.min(), sorted_navs.index.max(), freq="D")
    return sorted_navs.reindex(full_range).ffill()


def _month_end_navs(daily_navs: pd.Series) -> pd.Series:
    """Reduce a daily NAV series to month-end observations.

    Forward-fills first to ensure month-ends inside reporting gaps
    inherit the most recently observed NAV.
    """
    ffilled = _forward_fill_daily_navs(daily_navs)
    if ffilled.empty:
        return pd.Series(dtype="float64")
    return ffilled.resample("ME").last().dropna()


def _empty_metrics() -> BenchmarkComparisonMetrics:
    """Construct a metrics dataclass with NaN sentinels for the empty case."""
    return BenchmarkComparisonMetrics(
        excess_return_total=math.nan,
        excess_return_annualised=math.nan,
        alpha_annualised=math.nan,
        beta=math.nan,
        r_squared=math.nan,
        tracking_error_annualised=math.nan,
        information_ratio=math.nan,
        up_capture_ratio=math.nan,
        down_capture_ratio=math.nan,
        sharpe_investment=math.nan,
        sharpe_benchmark=math.nan,
        sharpe_difference=math.nan,
        n_observations=0,
        period_start=date.min,
        period_end=date.min,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_benchmark_comparison(
    investment_returns: pd.Series,
    benchmark_returns: pd.Series,
    risk_free_returns: pd.Series,
    investment_identifier: str,
    benchmark_identifier: str,
) -> BenchmarkComparisonBundle:
    """Compute the five Phase-1 metric groups for one (investment, benchmark) pair.

    The function expects three pre-aligned monthly return series.
    Alignment to a common monthly grid is the responsibility of
    the caller (typically the service layer). Internally an
    inner-join over the three series is performed to defensive-
    drop any unaligned dates that slipped through.

    Methodology references:
      - Excess return: arithmetic ``r_i - r_b`` per month;
        annualised by multiplying the mean by 12. Total excess
        is the geometric difference of cumulative returns:
        ``(1 + r_i).prod() - (1 + r_b).prod()``.
      - Alpha/Beta: regression of ``(r_i - r_f)`` on
        ``(r_b - r_f)``. Slope = beta; intercept = monthly alpha;
        multiply intercept by 12 to annualise. Uses
        :func:`numpy.cov` / :func:`numpy.var` directly rather than
        :func:`scipy.stats.linregress` for clarity.
      - R-squared: square of Pearson correlation between
        ``(r_i - r_f)`` and ``(r_b - r_f)``.
      - Tracking Error: stddev of ``(r_i - r_b)``, ``ddof=1``,
        times ``sqrt(12)``.
      - Information Ratio: ``excess_annualised / TE_annualised``.
        NaN if TE is zero.
      - Up/Down Capture: arithmetic-mean ratios on the up- and
        down-months of the benchmark. NaN if no up- or down-months
        exist in the sample.
      - Sharpe: ``(mean(r - r_f) / stddev(r - r_f)) × sqrt(12)``,
        computed separately for investment and benchmark.

    The function does NOT compound or otherwise transform the
    inputs — it expects monthly returns ready for arithmetic
    treatment.

    Args:
        investment_returns: Monthly return series, decimal,
            indexed by month-end ``pd.Timestamp`` (UTC-aware or
            naive — both accepted, internally normalised).
        benchmark_returns: Same shape as ``investment_returns``.
        risk_free_returns: Monthly risk-free return series, same
            shape. Already converted from annualised to monthly
            by the caller (see methodology in ADR-0061 §Decision
            "Risk-free returns").
        investment_identifier: Operator-facing identifier
            (investment name or code); embedded into the bundle.
        benchmark_identifier: Operator-facing identifier
            (benchmark code or display_name).

    Returns:
        :class:`BenchmarkComparisonBundle` with metrics and
        aligned series.

    Edge cases:
      - Fewer than 12 aligned observations: metrics with NaN
        sentinels for the ratio-based fields
        (``information_ratio``, ``r_squared``) but valid simple
        stats (``excess_return_total``, ``beta`` if benchmark
        variance > 0).
      - Zero benchmark variance (e.g. constant benchmark over
        the period): ``beta`` and ``r_squared`` are NaN;
        ``alpha`` falls back to ``mean(r_i - r_f) × 12``;
        up/down capture are NaN.
      - Empty inputs: all metrics NaN, ``n_observations = 0``,
        ``period_start = period_end = date.min``.
    """
    r_i_aligned, r_b_aligned, r_f_aligned = _align_monthly_series(
        investment_returns, benchmark_returns, risk_free_returns
    )

    if r_i_aligned.empty:
        empty_metrics = _empty_metrics()
        return BenchmarkComparisonBundle(
            investment_identifier=investment_identifier,
            benchmark_identifier=benchmark_identifier,
            metrics=empty_metrics,
            aligned_investment_returns=pd.Series(dtype="float64"),
            aligned_benchmark_returns=pd.Series(dtype="float64"),
            aligned_excess_returns=pd.Series(dtype="float64"),
        )

    r_i = r_i_aligned.to_numpy(dtype="float64")
    r_b = r_b_aligned.to_numpy(dtype="float64")
    r_f = r_f_aligned.to_numpy(dtype="float64")
    n = int(r_i.size)

    excess_monthly = r_i - r_b
    aligned_excess = pd.Series(excess_monthly, index=r_i_aligned.index)

    # Excess Return (group 1).
    excess_return_total = float(np.prod(1.0 + r_i) - np.prod(1.0 + r_b))
    excess_return_annualised = float(np.mean(excess_monthly) * 12.0)

    # Alpha / Beta / R-squared (group 2).
    r_i_excess = r_i - r_f
    r_b_excess = r_b - r_f
    bench_excess_var = float(np.var(r_b_excess, ddof=1)) if n >= 2 else math.nan
    if n >= 2 and bench_excess_var > 0.0 and not math.isnan(bench_excess_var):
        covariance = float(np.cov(r_i_excess, r_b_excess, ddof=1)[0, 1])
        beta = covariance / bench_excess_var
        monthly_alpha = float(np.mean(r_i_excess)) - beta * float(np.mean(r_b_excess))
        alpha_annualised = monthly_alpha * 12.0
        if n >= 12:
            corr_matrix = np.corrcoef(r_i_excess, r_b_excess)
            corr = float(corr_matrix[0, 1])
            r_squared = corr * corr if not math.isnan(corr) else math.nan
        else:
            r_squared = math.nan
    else:
        beta = math.nan
        r_squared = math.nan
        alpha_annualised = float(np.mean(r_i_excess)) * 12.0 if n >= 1 else math.nan

    # Tracking Error + Information Ratio (group 3).
    te_monthly = float(np.std(excess_monthly, ddof=1)) if n >= 2 else math.nan
    tracking_error_annualised = (
        te_monthly * math.sqrt(12.0) if not math.isnan(te_monthly) else math.nan
    )
    if n >= 12 and not math.isnan(tracking_error_annualised) and tracking_error_annualised > 0.0:
        information_ratio = excess_return_annualised / tracking_error_annualised
    else:
        information_ratio = math.nan

    # Up/Down capture (group 4).
    up_mask = r_b > 0.0
    down_mask = r_b < 0.0
    if up_mask.any():
        bench_up_mean = float(np.mean(r_b[up_mask]))
        if bench_up_mean != 0.0:
            up_capture_ratio = float(np.mean(r_i[up_mask])) / bench_up_mean
        else:
            up_capture_ratio = math.nan
    else:
        up_capture_ratio = math.nan
    if down_mask.any():
        bench_down_mean = float(np.mean(r_b[down_mask]))
        if bench_down_mean != 0.0:
            down_capture_ratio = float(np.mean(r_i[down_mask])) / bench_down_mean
        else:
            down_capture_ratio = math.nan
    else:
        down_capture_ratio = math.nan

    # Sharpe Differential (group 5).
    sharpe_investment = _annualised_sharpe(r_i_excess)
    sharpe_benchmark = _annualised_sharpe(r_b_excess)
    if math.isnan(sharpe_investment) or math.isnan(sharpe_benchmark):
        sharpe_difference = math.nan
    else:
        sharpe_difference = sharpe_investment - sharpe_benchmark

    period_start = r_i_aligned.index.min().date()
    period_end = r_i_aligned.index.max().date()

    metrics = BenchmarkComparisonMetrics(
        excess_return_total=excess_return_total,
        excess_return_annualised=excess_return_annualised,
        alpha_annualised=alpha_annualised,
        beta=beta,
        r_squared=r_squared,
        tracking_error_annualised=tracking_error_annualised,
        information_ratio=information_ratio,
        up_capture_ratio=up_capture_ratio,
        down_capture_ratio=down_capture_ratio,
        sharpe_investment=sharpe_investment,
        sharpe_benchmark=sharpe_benchmark,
        sharpe_difference=sharpe_difference,
        n_observations=n,
        period_start=period_start,
        period_end=period_end,
    )
    return BenchmarkComparisonBundle(
        investment_identifier=investment_identifier,
        benchmark_identifier=benchmark_identifier,
        metrics=metrics,
        aligned_investment_returns=r_i_aligned,
        aligned_benchmark_returns=r_b_aligned,
        aligned_excess_returns=aligned_excess,
    )


def _annualised_sharpe(excess_returns: np.ndarray) -> float:
    """Annualised Sharpe ratio for a monthly excess-return array.

    Returns NaN when fewer than two observations are available or
    the sample standard deviation is zero.
    """
    if excess_returns.size < 2:
        return math.nan
    sample_std = float(np.std(excess_returns, ddof=1))
    if sample_std <= 0.0 or math.isnan(sample_std):
        return math.nan
    return (float(np.mean(excess_returns)) / sample_std) * math.sqrt(12.0)


def compute_asset_class_composites(
    investment_returns_daily: dict[str, pd.Series],
    investment_navs_daily: dict[str, pd.Series],
    investment_to_asset_class: dict[str, str],
) -> list[AssetClassCompositeSeries]:
    """NAV-weighted Beginning-of-Period composite returns per asset class.

    Methodology (TWR, GIPS-compatible, per ADR-0061 §Decision
    "Composite methodology"):

      1. For each investment, resample daily returns to monthly
         compounded returns: ``r_m = (1 + r_d).prod() - 1`` over
         the month, with the monthly stamp set to month-end.
      2. For each investment, forward-fill the daily NAV series
         so that the NAV at month-start (which equals NAV at the
         previous month-end) is always available. Forward-fill is
         the deliberate methodology — see ADR-0061 §Rationale
         "Why Forward-Fill on NAVs".
      3. For each month and each asset class, take the
         constituent investments' month-start NAVs as weights.
         Composite return for that month =
         ``sum(w_i × r_i) / sum(w_i)``.
      4. Months where no constituent has a valid month-start NAV
         are dropped from the composite — the asset class did not
         yet exist at portfolio level at that time.

    Asset classes that appear in ``investment_to_asset_class`` but
    have no investment with valid data over any month are
    returned with an empty ``monthly_returns`` and
    ``n_investments = 0``. Callers should filter these out before
    charting.

    Args:
        investment_returns_daily: Dict mapping
            ``investment_identifier`` → daily decimal returns
            indexed by ``pd.Timestamp``. Missing days are simply
            absent from the index.
        investment_navs_daily: Dict mapping
            ``investment_identifier`` → daily NAV indexed by
            ``pd.Timestamp``. Gaps within the investment's
            lifetime are forward-filled internally.
        investment_to_asset_class: Dict mapping
            ``investment_identifier`` → ``asset_class_code``.
            Investments without an entry are silently skipped
            (defensive — should not happen if the service-layer
            caller pre-filtered to mapped investments).

    Returns:
        List of :class:`AssetClassCompositeSeries`, one per
        distinct ``asset_class_code`` that appears in the mapping.
        Sorted alphabetically by ``asset_class_code`` for stable
        output.
    """
    monthly_returns_per_inv: dict[str, pd.Series] = {}
    monthly_navs_per_inv: dict[str, pd.Series] = {}

    for inv_id, daily_ret in investment_returns_daily.items():
        if inv_id not in investment_to_asset_class:
            continue
        monthly_returns_per_inv[inv_id] = _resample_daily_to_monthly_return(daily_ret)

    for inv_id, daily_nav in investment_navs_daily.items():
        if inv_id not in investment_to_asset_class:
            continue
        monthly_navs_per_inv[inv_id] = _month_end_navs(daily_nav)

    asset_class_to_investments: dict[str, list[str]] = {}
    for inv_id, ac_code in investment_to_asset_class.items():
        asset_class_to_investments.setdefault(ac_code, []).append(inv_id)

    results: list[AssetClassCompositeSeries] = []
    for ac_code in sorted(asset_class_to_investments.keys()):
        constituent_ids = asset_class_to_investments[ac_code]

        candidate_months: set[pd.Timestamp] = set()
        for inv_id in constituent_ids:
            ret_series = monthly_returns_per_inv.get(inv_id)
            if ret_series is None or ret_series.empty:
                continue
            candidate_months.update(ret_series.index)
        if not candidate_months:
            results.append(
                AssetClassCompositeSeries(
                    asset_class_code=ac_code,
                    monthly_returns=pd.Series(dtype="float64"),
                    n_investments=0,
                    period_start=date.min,
                    period_end=date.min,
                )
            )
            continue

        sorted_months = sorted(candidate_months)
        composite_records: list[tuple[pd.Timestamp, float]] = []
        contributing_investments: set[str] = set()

        for month_end in sorted_months:
            prev_month_end = month_end - pd.offsets.MonthEnd(1)
            weighted_sum = 0.0
            total_weight = 0.0
            month_contributors: list[str] = []

            for inv_id in constituent_ids:
                ret_series = monthly_returns_per_inv.get(inv_id)
                nav_series = monthly_navs_per_inv.get(inv_id)
                if ret_series is None or nav_series is None:
                    continue
                if month_end not in ret_series.index:
                    continue
                if prev_month_end not in nav_series.index:
                    continue
                weight = float(nav_series.loc[prev_month_end])
                ret = float(ret_series.loc[month_end])
                if math.isnan(weight) or math.isnan(ret) or weight <= 0.0:
                    continue
                weighted_sum += weight * ret
                total_weight += weight
                month_contributors.append(inv_id)

            if total_weight > 0.0:
                composite_records.append((month_end, weighted_sum / total_weight))
                contributing_investments.update(month_contributors)

        if not composite_records:
            results.append(
                AssetClassCompositeSeries(
                    asset_class_code=ac_code,
                    monthly_returns=pd.Series(dtype="float64"),
                    n_investments=0,
                    period_start=date.min,
                    period_end=date.min,
                )
            )
            continue

        record_index = pd.DatetimeIndex([ts for ts, _ in composite_records])
        record_values = [v for _, v in composite_records]
        composite_series = pd.Series(record_values, index=record_index, dtype="float64")
        results.append(
            AssetClassCompositeSeries(
                asset_class_code=ac_code,
                monthly_returns=composite_series,
                n_investments=len(contributing_investments),
                period_start=record_index.min().date(),
                period_end=record_index.max().date(),
            )
        )
    return results


def compute_saa_hypothetical_series(
    saa_weights: dict[str, float],
    benchmark_returns_by_asset_class: dict[str, pd.Series],
    composite_returns_by_asset_class: dict[str, pd.Series],
    actual_portfolio_returns: pd.Series,
    saa_label: str,
) -> SAAHypotheticalSeries:
    """Combine SAA weights with benchmark and composite returns.

    For each month present in *any* of the input series:

      ``saa_x_benchmark[m] = Σ_ac (w_ac × benchmark_return_ac[m])``
      ``saa_x_composite[m] = Σ_ac (w_ac × composite_return_ac[m])``

    Where an asset class has no benchmark series (e.g. Cash with
    no benchmark mapping in Phase 1), its term in
    ``saa_x_benchmark`` is treated as zero. Similarly for
    ``saa_x_composite`` when the asset class has no investments.
    This silent zero-handling is intentional — it lets the
    operator see the impact of the partially-defined SAA.

    The function does NOT renormalise weights when some asset
    classes are missing data — the operator's SAA decision is
    preserved. A 70 % Equity + 30 % Cash SAA where Cash has no
    benchmark produces a hypothetical that is 70 % of the Equity
    benchmark and silently leaves the 30 % unreturned, which
    visually reads as the SAA underperforming pure Equity.

    Args:
        saa_weights: Dict mapping ``asset_class_code`` → weight.
            Weights must sum to ``1.0 ± 1e-6`` (validated; raises
            :class:`ValueError` otherwise — the SAA service is
            meant to guarantee this, this is defensive).
        benchmark_returns_by_asset_class: Dict mapping
            ``asset_class_code`` → monthly benchmark return series
            (decimal, indexed by month-end ``pd.Timestamp``).
        composite_returns_by_asset_class: Dict mapping
            ``asset_class_code`` → monthly composite return series
            (typically the ``monthly_returns`` from
            :func:`compute_asset_class_composites`).
        actual_portfolio_returns: Monthly portfolio-level NAV-
            weighted actual returns, same index convention.
        saa_label: Operator-facing label of the SAA weight set
            ("Target — Standard 2026", "Tangency — Standard
            2026").

    Returns:
        :class:`SAAHypotheticalSeries` with the three series and
        metadata.

    Raises:
        ValueError: SAA weights do not sum to ``1.0`` (tolerance
            ``1e-6``).
    """
    if saa_weights:
        weight_sum = float(sum(saa_weights.values()))
        if abs(weight_sum - 1.0) > 1e-6:
            raise ValueError(f"SAA weights must sum to 1.0 (tolerance 1e-6); got {weight_sum!r}.")
    else:
        raise ValueError("SAA weights must sum to 1.0 (tolerance 1e-6); got empty dict.")

    normalised_benchmark = {
        ac: normalise_monthly_index(s) for ac, s in benchmark_returns_by_asset_class.items()
    }
    normalised_composite = {
        ac: normalise_monthly_index(s) for ac, s in composite_returns_by_asset_class.items()
    }
    normalised_actual = normalise_monthly_index(actual_portfolio_returns)

    candidate_indices: list[pd.DatetimeIndex] = []
    for s in normalised_benchmark.values():
        if not s.empty:
            candidate_indices.append(s.index)
    for s in normalised_composite.values():
        if not s.empty:
            candidate_indices.append(s.index)
    if not normalised_actual.empty:
        candidate_indices.append(normalised_actual.index)

    if not candidate_indices:
        empty = pd.Series(dtype="float64")
        return SAAHypotheticalSeries(
            saa_label=saa_label,
            saa_weights=dict(saa_weights),
            saa_x_benchmark=empty.copy(),
            saa_x_composite=empty.copy(),
            actual_portfolio_returns=empty.copy(),
            period_start=date.min,
            period_end=date.min,
        )

    common_index = candidate_indices[0]
    for idx in candidate_indices[1:]:
        common_index = common_index.union(idx)
    common_index = common_index.sort_values()

    saa_x_benchmark_values = np.zeros(len(common_index), dtype="float64")
    saa_x_composite_values = np.zeros(len(common_index), dtype="float64")

    for ac_code, weight in saa_weights.items():
        bench_series = normalised_benchmark.get(ac_code)
        if bench_series is not None and not bench_series.empty:
            aligned_bench = bench_series.reindex(common_index, fill_value=0.0).to_numpy(
                dtype="float64"
            )
            saa_x_benchmark_values += weight * aligned_bench
        comp_series = normalised_composite.get(ac_code)
        if comp_series is not None and not comp_series.empty:
            aligned_comp = comp_series.reindex(common_index, fill_value=0.0).to_numpy(
                dtype="float64"
            )
            saa_x_composite_values += weight * aligned_comp

    saa_x_benchmark = pd.Series(saa_x_benchmark_values, index=common_index)
    saa_x_composite = pd.Series(saa_x_composite_values, index=common_index)
    aligned_actual = normalised_actual.reindex(common_index)

    return SAAHypotheticalSeries(
        saa_label=saa_label,
        saa_weights=dict(saa_weights),
        saa_x_benchmark=saa_x_benchmark,
        saa_x_composite=saa_x_composite,
        actual_portfolio_returns=aligned_actual,
        period_start=common_index.min().date(),
        period_end=common_index.max().date(),
    )


__all__ = [
    "AssetClassCompositeSeries",
    "BenchmarkComparisonBundle",
    "BenchmarkComparisonMetrics",
    "MonthlyNAVSeries",
    "MonthlyReturnSeries",
    "SAAHypotheticalSeries",
    "compute_asset_class_composites",
    "compute_benchmark_comparison",
    "compute_saa_hypothetical_series",
    "normalise_monthly_index",
]
