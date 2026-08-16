# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Front-Office Overview service package.

Re-exports the thin orchestrator and its result dataclass so callers can
import both from the package root (ADR-0067).
"""

from services.front_office_overview.overview_service import (
    CashPositionRow,
    FrontOfficeOverviewService,
    OverviewKpis,
    OverviewResult,
)

__all__ = [
    "CashPositionRow",
    "FrontOfficeOverviewService",
    "OverviewKpis",
    "OverviewResult",
]
