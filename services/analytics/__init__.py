# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Analytics service foundation — pure calculation layer.

Per ADR-0045 §3, this package holds the calculation layer extracted
from the QT modules. Functions are pure: they take pandas DataFrames
or numpy arrays as arguments and return plain Python data structures.
None of the analytics functions reach into the database directly;
that is the service-layer caller's responsibility (e.g.
:class:`InvestmentService` for the web side).

The package is organised by domain rather than by chart so that the
same building blocks back multiple chart families:

- :mod:`investment_returns` — per-investment returns, net capital
  gain, rolling multiples, rolling IRR. **Implemented in sub-stream
  5b.**
- :mod:`statistics` — risk and distribution statistics. **Implemented
  in sub-stream 5c — see ADR-0045 §3.**
- :mod:`correlation` — pairwise correlation matrix. **Implemented in
  sub-stream 5c — see ADR-0045 §3.**
- :mod:`portfolio_aggregation` — portfolio-level roll-ups.
  *Implemented in sub-stream 5e — see ADR-0045 §3.*
- :mod:`efficient_frontier` — investment-universe efficient frontier.
  **Implemented in sub-stream 5d — see ADR-0045 §3.**
"""

from services.analytics._dtos import (
    DistributionStats,
    InvestmentWithClassCodeDTO,
    KeyMetricsCard,
    LimitSetWithLimitsDTO,
    NotchWeightedRating,
    RiskMetrics,
    TrailingReturns,
)
from services.analytics.benchmark_comparison import (
    AssetClassCompositeSeries,
    BenchmarkComparisonBundle,
    BenchmarkComparisonMetrics,
    MonthlyNAVSeries,
    MonthlyReturnSeries,
    SAAHypotheticalSeries,
    compute_asset_class_composites,
    compute_benchmark_comparison,
    compute_saa_hypothetical_series,
)
from services.analytics.correlation import compute_correlation_matrix
from services.analytics.efficient_frontier import (
    CapitalMarketLine,
    EfficientFrontierResult,
    MinVariancePortfolio,
    TangencyPortfolio,
    compute_capital_market_line,
    compute_current_portfolio_position,
    compute_efficient_frontier,
    compute_min_variance_portfolio,
    compute_tangency_portfolio,
    derive_expected_returns_and_cov,
)
from services.analytics.fixed_income import (
    compute_notch_weighted_average_rating,
)
from services.analytics.investment_returns import (
    compute_cashflow_adjusted_return_series,
    compute_net_capital_gain,
    compute_rolling_irr_since_inception,
    compute_rolling_multiples,
    compute_total_return_series,
    compute_trailing_returns,
)
from services.analytics.limit_coverage import (
    CoverageEngineResult,
    FamilyCoverageResult,
    compute_coverage,
)
from services.analytics.portfolio_aggregation import (
    InvestedCapitalNavSeries,
    PortfolioCashflowSeries,
    PortfolioMultiplesSeries,
    RegionBreakdown,
    RegionBreakdownRow,
    SectorBreakdown,
    SectorBreakdownRow,
    VintageDistribution,
    aggregate_invested_capital_and_nav,
    aggregate_portfolio_cashflows,
    aggregate_portfolio_multiples,
    aggregate_region_breakdown,
    aggregate_sector_breakdown,
    aggregate_vintage_distribution,
    compute_total_return_index_series,
)
from services.analytics.statistics import (
    PERIODS_PER_YEAR_DAILY,
    PERIODS_PER_YEAR_MONTHLY,
    annualise_mean_return,
    annualise_std_dev,
    compute_autocorrelation,
    compute_conditional_value_at_risk,
    compute_downside_deviation,
    compute_full_distribution_stats,
    compute_kurtosis,
    compute_lag_1_autocorrelation,
    compute_max_drawdown,
    compute_max_drawdown_from_returns,
    compute_max_return,
    compute_mean_return,
    compute_median_return,
    compute_min_return,
    compute_risk_metrics,
    compute_rolling_sharpe,
    compute_rolling_volatility,
    compute_sharpe_ratio,
    compute_skewness,
    compute_sortino_ratio,
    compute_std_dev,
    compute_ulcer_index,
    compute_underwater_series,
    compute_value_at_risk,
    compute_variance,
)

__all__ = [
    "PERIODS_PER_YEAR_DAILY",
    "PERIODS_PER_YEAR_MONTHLY",
    "AssetClassCompositeSeries",
    "BenchmarkComparisonBundle",
    "BenchmarkComparisonMetrics",
    "CapitalMarketLine",
    "CoverageEngineResult",
    "DistributionStats",
    "EfficientFrontierResult",
    "FamilyCoverageResult",
    "InvestedCapitalNavSeries",
    "InvestmentWithClassCodeDTO",
    "KeyMetricsCard",
    "LimitSetWithLimitsDTO",
    "MinVariancePortfolio",
    "MonthlyNAVSeries",
    "MonthlyReturnSeries",
    "NotchWeightedRating",
    "PortfolioCashflowSeries",
    "PortfolioMultiplesSeries",
    "RegionBreakdown",
    "RegionBreakdownRow",
    "RiskMetrics",
    "SAAHypotheticalSeries",
    "SectorBreakdown",
    "SectorBreakdownRow",
    "TangencyPortfolio",
    "TrailingReturns",
    "VintageDistribution",
    "aggregate_invested_capital_and_nav",
    "aggregate_portfolio_cashflows",
    "aggregate_portfolio_multiples",
    "aggregate_region_breakdown",
    "aggregate_sector_breakdown",
    "aggregate_vintage_distribution",
    "annualise_mean_return",
    "annualise_std_dev",
    "compute_asset_class_composites",
    "compute_autocorrelation",
    "compute_benchmark_comparison",
    "compute_capital_market_line",
    "compute_cashflow_adjusted_return_series",
    "compute_conditional_value_at_risk",
    "compute_correlation_matrix",
    "compute_coverage",
    "compute_current_portfolio_position",
    "compute_downside_deviation",
    "compute_efficient_frontier",
    "compute_full_distribution_stats",
    "compute_kurtosis",
    "compute_lag_1_autocorrelation",
    "compute_max_drawdown",
    "compute_max_drawdown_from_returns",
    "compute_max_return",
    "compute_mean_return",
    "compute_median_return",
    "compute_min_return",
    "compute_min_variance_portfolio",
    "compute_net_capital_gain",
    "compute_notch_weighted_average_rating",
    "compute_risk_metrics",
    "compute_rolling_irr_since_inception",
    "compute_rolling_multiples",
    "compute_rolling_sharpe",
    "compute_rolling_volatility",
    "compute_saa_hypothetical_series",
    "compute_sharpe_ratio",
    "compute_skewness",
    "compute_sortino_ratio",
    "compute_std_dev",
    "compute_tangency_portfolio",
    "compute_total_return_index_series",
    "compute_total_return_series",
    "compute_trailing_returns",
    "compute_ulcer_index",
    "compute_underwater_series",
    "compute_value_at_risk",
    "compute_variance",
    "derive_expected_returns_and_cov",
]
