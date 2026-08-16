# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Back Office — Investment Limits module.

Purpose:
    Monitor coverage of investment limits (SAA family and AnlV
    family) against the tenant's AUM. Read-only surface in V1;
    limit-set editing is deferred to a follow-up.

Inputs:
    - Limit sets (SAA + AnlV families) with effective_from
      historisation.
    - Daily AUM time-series.
    - Investment NAVs (actual + plan) and asset-class / anlv_code
      classifications.

Outputs:
    - Per-class coverage status (OK / WARN / BREACH / NO_LIMIT /
      UNALLOCATED).
    - Coverage time series for chart rendering.

Dependencies (internal):
    - ``core.base_module.BaseModule``
    - ``services.limits.limits_coverage_service``

Dependencies (external):
    - ``pandas``
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class InvestmentLimits(BaseModule):
    """Investment-Limits monitoring for back-office compliance.

    The web surface lives at ``/back-office#limits`` and is
    implemented in ``web/routes/limits.py`` plus
    ``services/limits/``. This module exists for registry
    completeness and for future programmatic consumption.

    Attributes:
        module_name: ``"limits"``
        module_area: ``"back_office"``
    """

    module_name = "limits"
    module_area = "back_office"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """Run the limits coverage analysis.

        Returns:
            dict with key ``"status"``. Programmatic API is deferred
            to a follow-up; the registered module exists so the
            registry surface is complete.
        """
        return {"status": "stub"}
