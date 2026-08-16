# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Data-transfer objects shared by the analytics layer — sub-stream 5c.

Per ADR-0045 §3 the analytics layer is pure-functional and DB-free.
Where multiple statistics share a natural grouping (distribution
descriptors, risk metrics) the calculation functions emit a small
frozen dataclass so that the service layer above can pass the bundle
through to the chart-spec generators / templates without re-packing
intermediate dicts.

The dataclasses live here rather than next to a single calculation
function so that a future analytics module (sub-stream 5d efficient
frontier, sub-stream 5e portfolio aggregations) can reuse the same
shape without circular imports.

A second class of shapes lives here as well: composition DTOs that
join two repository DTOs into a pre-resolved view consumed by an
analytics function. Per ADR-0045 §3 ``services/analytics/`` modules
may import repository DTO dataclasses; the compositions below let the
caller materialise cross-table joins (e.g. asset-class code lookup,
limit-set ↔ limits) once instead of per evaluation date inside the
computation loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.repositories.investment_repository import InvestmentDTO
from core.repositories.limits_repository import LimitSetDTO


@dataclass(frozen=True)
class DistributionStats:
    """Bundled distribution descriptors for one return series.

    Mirrors the rows of the QT Distribution table in
    ``gui/widgets/statistics_widgets.py::DistributionTableWidget``.
    Annualisations follow the QT conventions: arithmetic-mean
    annualisation (``mean * 252``) for the mean, ``sqrt(252)``
    scaling for the standard deviation. Skewness and kurtosis are
    the scipy defaults (biased estimator, Fisher / excess kurtosis).

    Attributes:
        mean_daily: Arithmetic mean of the periodic return series.
        mean_annualised: ``mean_daily * 252``.
        std_daily: Sample standard deviation (``ddof=1``).
        std_annualised: ``std_daily * sqrt(252)``.
        variance_daily: Sample variance (``ddof=1``).
        skewness: ``scipy.stats.skew(returns)`` — biased estimator.
        kurtosis_excess: ``scipy.stats.kurtosis(returns)`` —
            Fisher's excess kurtosis (normal distribution = 0).
        median: Median of the series.
        min_return: Minimum value.
        max_return: Maximum value.
    """

    mean_daily: float
    mean_annualised: float
    std_daily: float
    std_annualised: float
    variance_daily: float
    skewness: float
    kurtosis_excess: float
    median: float
    min_return: float
    max_return: float


@dataclass(frozen=True)
class RiskMetrics:
    """Bundled risk metrics for one investment.

    Mirrors the four Details tables shown in the QT Statistics
    section: Risk (7 rows), Risk/Return (2 rows), Autocorrelation
    (4 rows), plus the Distribution-pill µ headline rendered
    separately by the service layer.

    Conventions follow ``gui/widgets/statistics_widgets.py`` —
    every field's calculation is QT-consistency-tested to 1e-12
    in ``tests/services/analytics/test_statistics.py``.

    Attributes:
        var_90_daily: Historical VaR at 90% confidence (10th
            percentile of returns). Negative decimal for losses.
        var_95_daily: Historical VaR at 95% confidence.
        var_99_daily: Historical VaR at 99% confidence.
        cvar_95_daily: Conditional VaR / Expected Shortfall at
            95% (mean of returns at or below the VaR-95 threshold).
        max_drawdown: Maximum drawdown computed from the return
            series (NOT from the NAV series — matches the QT
            Risk-table convention). Negative decimal.
        ulcer_index: RMS of percentage drawdowns in
            percentage-point units (e.g. 11.29 for a moderate-vol
            track record).
        downside_deviation: ``sqrt(mean(min(r, 0)**2))``,
            un-annualised. Non-negative decimal.
        sharpe_ratio: Annualised Sharpe ratio at the bundle's
            risk-free rate.
        sortino_ratio: Annualised Sortino ratio (Mean / DD with
            DD annualised).
        lag_1_autocorrelation: ``series.autocorr(lag=1)``.
        lag_2_autocorrelation: ``series.autocorr(lag=2)``.
        lag_3_autocorrelation: ``series.autocorr(lag=3)``.
        lag_4_autocorrelation: ``series.autocorr(lag=4)``.
    """

    # Risk
    var_90_daily: float
    var_95_daily: float
    var_99_daily: float
    cvar_95_daily: float
    max_drawdown: float
    ulcer_index: float
    downside_deviation: float
    # Risk / Return
    sharpe_ratio: float
    sortino_ratio: float
    # Autocorrelation
    lag_1_autocorrelation: float
    lag_2_autocorrelation: float
    lag_3_autocorrelation: float
    lag_4_autocorrelation: float


@dataclass(frozen=True)
class KeyMetricsCard:
    """Per-investment KPI strip card.

    The QT KPI strip renders a small card per investment with the
    most-recent NAV, the annualised arithmetic mean return, the
    Sharpe ratio, and a sparkline derived from the cumulative
    performance series. The web migration packs these four values
    into one dataclass so the chart-spec generator and the template
    consume a single shape.

    Attributes:
        investment_name: Display name (also the dict key used by the
            service-layer bundle).
        latest_nav: Most recent NAV value, or ``None`` if no actual
            NAVs are available.
        currency: ISO 4217 currency code of the underlying NAV
            series. Empty string when the investment has no NAVs.
        annualised_return: ``mean_daily * 252`` — NaN when the
            return series is empty.
        sharpe_ratio: Annualised Sharpe ratio at the bundle's
            risk-free rate.
        sparkline_values: Cumulative-performance series ``(1 + r)
            .cumprod()`` rendered as the y-values of the spark
            line. Empty list when the return series has fewer than
            two datapoints.
    """

    investment_name: str
    latest_nav: float | None
    currency: str
    annualised_return: float
    sharpe_ratio: float
    sparkline_values: list[float]


@dataclass(frozen=True)
class TrailingReturns:
    """Trailing total-weighted returns over standard factsheet windows.

    Backs the trailing-TWR row of the mark-to-market KPI caption on the
    Front-Office universe-charts triplet (ADR-0082 §5, ADR-0079 §1). The
    inputs are derived from a total-return-index series; see
    :func:`services.analytics.investment_returns.compute_trailing_returns`.

    Windows up to and including one year are cumulative period returns;
    windows longer than one year are annualised (CAGR). Each field is
    ``None`` when the index history does not reach the window start.

    Attributes:
        m1: Trailing one-month cumulative return (decimal).
        m3: Trailing three-month cumulative return (decimal).
        ytd: Year-to-date cumulative return from 1 January of the
            ``as_of`` year (decimal).
        y1: Trailing one-year cumulative return (decimal).
        y3_annualised: Trailing three-year annualised return (CAGR,
            decimal).
        since_inception_annualised: Annualised return from the first
            datapoint to ``as_of`` (CAGR, decimal).
    """

    m1: float | None
    m3: float | None
    ytd: float | None
    y1: float | None
    y3_annualised: float | None
    since_inception_annualised: float | None


@dataclass(frozen=True)
class NotchWeightedRating:
    """Notch-weighted average credit rating of a bucket distribution.

    Backs the ``notch-weighted Ø Rating`` figure of the Fixed-Income KPI
    caption on the Front-Office universe-charts triplet (ADR-0082 §5,
    ADR-0079 §3). Computed by
    :func:`services.analytics.fixed_income.compute_notch_weighted_average_rating`.

    ``NR`` (and any unknown bucket) is excluded from the weighted mean and
    the rated weights are renormalised. ``average_bucket`` is the rated
    bucket whose notch is nearest the rounded average notch.

    Attributes:
        average_notch: Weighted-mean notch over the rated buckets on the
            ADR-0079 §2 scale (``AAA=1 … CCC_and_below=7``). ``float('nan')``
            when no rated weight is present.
        average_bucket: The rated bucket nearest the rounded
            ``average_notch``; ``'NR'`` when no rated weight is present.
        rated_weight_pct: Sum of the rated weights *before* renormalisation
            — i.e. total weight minus ``NR`` and unknown buckets. ``0.0``
            when no rated weight is present.
    """

    average_notch: float
    average_bucket: str
    rated_weight_pct: float


@dataclass(frozen=True)
class LimitSetWithLimitsDTO:
    """A limit set together with its per-class ceiling rows.

    Composed by the caller from ``LimitsRepository.list_sets()`` plus
    ``LimitsRepository.list_limits(set_id)``; the limit-coverage engine
    consumes this pre-joined view to avoid per-class repo round-trips
    inside the computation loop.

    Attributes:
        set: The ``limit_sets`` row metadata.
        limits: ``class_key -> max_pct`` (percentage points). Insertion
            order is preserved by ``dict`` and defines the row order of
            the per-family coverage DataFrame; ``LimitsRepository.list_limits``
            currently returns rows ordered by ``class_key``, so callers
            that build this dict directly from that result inherit that
            ordering.
    """

    set: LimitSetDTO
    limits: dict[str, Decimal]


@dataclass(frozen=True)
class InvestmentWithClassCodeDTO:
    """An investment with its asset-class code resolved.

    The base :class:`InvestmentDTO` carries ``asset_class_id: UUID``;
    the limit-coverage engine needs ``asset_class_code: str`` for the
    SAA family grouping. The caller joins against
    :class:`AssetClassRepository` once, not per evaluation date.

    Attributes:
        investment: The base investment row.
        asset_class_code: The ``asset_classes.code`` snapshot for
            ``investment.asset_class_id``. The FK is NOT NULL so a
            well-formed caller always supplies a value; the engine
            additionally tolerates ``None`` defensively and routes the
            investment into the SAA unallocated bucket.
    """

    investment: InvestmentDTO
    asset_class_code: str | None
