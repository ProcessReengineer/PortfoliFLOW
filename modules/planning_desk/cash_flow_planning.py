# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Planning Desk — Cash Flow Planning module.

Purpose:
    The Planning Desk's first lens (ADR-0104 §6): a per-currency balance
    timeline over the plan world, with pacing rows and hypothetical
    transactions. The surface ships in the later steps of this strand; the
    area registration lands first.

The web surface lives at ``/planning-desk#cash-flow-planning``. The
calculation seams are ``services/investments/plan_world`` (baseline frame
assembly) and ``services/overlay/`` (the scenario overlay contract). This
module exists for registry completeness (ADR-0058).
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class CashFlowPlanning(BaseModule):
    """The Planning Desk Cash Flow Planning lens.

    Attributes:
        module_name: ``"cash_flow_planning"``
        module_area: ``"planning_desk"``
    """

    module_name = "cash_flow_planning"
    module_area = "planning_desk"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No programmatic entry point; the web surface does the work.

        Returns:
            dict with key ``"status"``.
        """
        return {"status": "stub"}
