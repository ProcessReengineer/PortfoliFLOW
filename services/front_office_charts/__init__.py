# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Service layer for the Front-Office archetype-charts triplet (ADR-0082).

Exposes :class:`ArchetypeChartsService` — the per-archetype data
assembly that resolves an investment's presentation archetype and
returns the pure tile inputs and KPI payload for the matching tile-set —
together with the result envelope and its tile / KPI DTOs. The route
(ADR-0082 §6) consumes this namespace and builds the Plotly specs.
"""

from services.front_office_charts._dtos import (
    ArchetypeChartsResult,
    CapitalAccountKPI,
    CapitalAccountTiles,
    EquityKPI,
    FixedIncomeKPI,
    FixedIncomeTiles,
    NavOnlyTiles,
    TotalReturnEquityTiles,
)
from services.front_office_charts.archetype_charts_service import (
    ArchetypeChartsService,
)

__all__ = [
    "ArchetypeChartsResult",
    "ArchetypeChartsService",
    "CapitalAccountKPI",
    "CapitalAccountTiles",
    "EquityKPI",
    "FixedIncomeKPI",
    "FixedIncomeTiles",
    "NavOnlyTiles",
    "TotalReturnEquityTiles",
]
