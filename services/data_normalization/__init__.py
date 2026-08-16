# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Data normalisation services — Excel JSONB → normalised investment data.

Sub-stream 4c of the Phase-4 web migration introduces the
:class:`InvestmentExtractor`, which transforms a Phase-2 Excel JSONB
snapshot (``data_uploads`` / ``data_upload_sheets``) into the
normalised investment-domain rows that the Phase-4 schema expects
(``investments``, ``investment_navs``, ``investment_cashflows``).

Per ADR-0043 §3 the extractor is a **pure transformation** with no
FastAPI / session / audit-log awareness. The persistence orchestration
lives on :meth:`services.investments.InvestmentService
.transform_upload_to_investments`; the extractor only converts the
JSONB shape into typed dataclasses and surfaces row-level errors as
data, not exceptions.
"""

from services.data_normalization.investment_extractor import (
    ExtractionWarning,
    ImportedBenchmark,
    ImportedBenchmarkMapping,
    ImportedBenchmarkObservation,
    ImportedBondAnalytics,
    ImportedFxRate,
    ImportedIdentifier,
    ImportedMaturityWeight,
    ImportedRatingWeight,
    ImportFormatError,
    ImportRowError,
    ImportedCashflow,
    ImportedCashStatement,
    ImportedInvestment,
    ImportedNav,
    ImportedRegionWeight,
    ImportedSectorWeight,
    InvestmentExtractionResult,
    InvestmentExtractor,
    UploadNotFoundError,
    extract_benchmarks_from_snapshot,
    extract_fx_rates_from_snapshot,
)

__all__ = [
    "ExtractionWarning",
    "ImportFormatError",
    "ImportRowError",
    "ImportedBenchmark",
    "ImportedBenchmarkMapping",
    "ImportedBenchmarkObservation",
    "ImportedBondAnalytics",
    "ImportedCashStatement",
    "ImportedCashflow",
    "ImportedFxRate",
    "ImportedIdentifier",
    "ImportedInvestment",
    "ImportedMaturityWeight",
    "ImportedNav",
    "ImportedRatingWeight",
    "ImportedRegionWeight",
    "ImportedSectorWeight",
    "InvestmentExtractionResult",
    "InvestmentExtractor",
    "UploadNotFoundError",
    "extract_benchmarks_from_snapshot",
    "extract_fx_rates_from_snapshot",
]
