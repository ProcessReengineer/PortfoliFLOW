# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Portfolio Review service package — sub-stream 5e (ADR-0045 §3).

Composition layer above the analytics primitives in
:mod:`services.analytics.portfolio_aggregation`. Hides per-tile
provider plumbing behind two bundle-shaped methods consumed by the
web Portfolio Review surface and (eventually) by the Reporting
Engine PDF / PPTX export.
"""

from services.portfolio_review.portfolio_review_service import (
    CashPositionRow,
    InvestmentHeaderMetrics,
    PortfolioHeaderMetrics,
    PortfolioOverviewBundle,
    PortfolioReviewService,
    SingleInvestmentReviewBundle,
)

__all__ = [
    "CashPositionRow",
    "InvestmentHeaderMetrics",
    "PortfolioHeaderMetrics",
    "PortfolioOverviewBundle",
    "PortfolioReviewService",
    "SingleInvestmentReviewBundle",
]
