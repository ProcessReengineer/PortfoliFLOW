# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Back Office — Benchmarks & Attribution module.

Purpose:
    Three-stage benchmark comparison for portfolio managers
    (per-investment, per-asset-class composite, SAA-hypothetical).
    Read-only surface; benchmark data is sourced from the Excel
    import path (no in-app CRUD in Phase 1).

Inputs:
    - Benchmarks and benchmark observations (from b011 schema).
    - Asset-class-to-benchmark mappings.
    - Investment NAVs and total returns (actuals only).
    - Risk-free rates (sourced from the active SAA configuration
      per ADR-0061 §Phase 1 notes — interest-rate persistence is a
      follow-up).
    - SAA configurations and optimization outputs.

Outputs:
    - Per-investment metrics table (Stage a).
    - Per-asset-class composite vs. benchmark small-multiples
      (Stage b).
    - SAA-hypothetical three-line chart (Stage c).

Dependencies (internal):
    - ``core.base_module.BaseModule``
    - ``services.benchmark_comparison.benchmark_comparison_service``

Dependencies (external):
    - ``pandas``

Reference: ADR-0061.
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class BenchmarksAttribution(BaseModule):
    """Benchmarks & Attribution monitoring for back-office reporting.

    The web surface lives at ``/back-office#benchmarks-attribution``
    and is implemented in ``web/routes/benchmarks_attribution.py``
    plus ``services/benchmark_comparison/``. This module exists for
    registry completeness and for future programmatic consumption
    (Phase 2 — Brinson attribution, PME extension).

    Attributes:
        module_name: ``"benchmarks_attribution"``
        module_area: ``"back_office"``
    """

    module_name = "benchmarks_attribution"
    module_area = "back_office"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """Stub run method — see web surface for the actual workflow."""
        return {"status": "stub"}
