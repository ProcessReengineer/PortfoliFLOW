# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Service layer for the Benchmarks & Attribution feature.

Sub-stream A12 Phase 1a orchestration layer: fetches inputs through
repositories, threads them through the pure analytics layer
(``services.analytics.benchmark_comparison``), and returns ready-to-
render bundles for the web surface.

See ADR-0061 for the architecture decision.
"""

from services.benchmark_comparison.benchmark_comparison_service import (
    AssetClassCompositeRowDTO,
    AssetClassCompositesBundle,
    BenchmarkComparisonService,
    InvestmentBenchmarkRowDTO,
    InvestmentComparisonDetailDTO,
    InvestmentComparisonsBundle,
    PortfolioSummaryKPIs,
    SAAConfigurationOptionDTO,
    SAAHypotheticalBundle,
    SAAHypotheticalEffects,
    WeightSet,
    WeightSetOptionDTO,
)

__all__ = [
    "AssetClassCompositeRowDTO",
    "AssetClassCompositesBundle",
    "BenchmarkComparisonService",
    "InvestmentBenchmarkRowDTO",
    "InvestmentComparisonDetailDTO",
    "InvestmentComparisonsBundle",
    "PortfolioSummaryKPIs",
    "SAAConfigurationOptionDTO",
    "SAAHypotheticalBundle",
    "SAAHypotheticalEffects",
    "WeightSet",
    "WeightSetOptionDTO",
]
