# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Service layer for the investment-universe Portfolio Analysis surface
(sub-stream 5d).

Aggregates the Phase-4 :class:`InvestmentRepository` and
:class:`InvestmentNavRepository` into the
:class:`PortfolioAnalysisBundle` consumed by the
``/portfolio-analysis`` web route and its chart-spec generators. The
orchestration lives in this package so the web route stays a thin
transport seam and the analytics layer stays DB-free.
"""

from services.portfolio_analysis.portfolio_analysis_service import (
    PortfolioAnalysisBundle,
    PortfolioAnalysisService,
)

__all__ = [
    "PortfolioAnalysisBundle",
    "PortfolioAnalysisService",
]
