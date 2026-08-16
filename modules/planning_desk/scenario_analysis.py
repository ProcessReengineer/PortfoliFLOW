# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Planning Desk — Scenario Analysis module (placeholder).

Purpose:
    The anchor for Feature #034 (ADR-0104 §6/§8). It inherits that anchor
    role from the retired Watch Desk ``scenarios`` stub: #034
    re-anchors here because the Planning Desk owns what *could* happen,
    while the Watch Desk owns what *has*. v0 renders a descriptive
    placeholder panel only — there is no scenario logic yet.

The web surface lives at ``/planning-desk#scenario-analysis`` and ships with
#034, against the four-kind overlay contract in ``services/overlay/``
(``market_shock`` and ``fx_shock`` are the two kinds it adds a surface for).
This module exists for registry completeness (ADR-0058).
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class ScenarioAnalysis(BaseModule):
    """The Planning Desk Scenario Analysis placeholder (Feature #034 anchor).

    Attributes:
        module_name: ``"scenario_analysis"``
        module_area: ``"planning_desk"``
    """

    module_name = "scenario_analysis"
    module_area = "planning_desk"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No programmatic entry point; a placeholder panel only.

        Returns:
            dict with key ``"status"``.
        """
        return {"status": "stub"}
