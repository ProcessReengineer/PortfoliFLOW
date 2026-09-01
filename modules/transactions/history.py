# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Transactions — History module.

Purpose:
    The settled-ticket record (ADR-0128 §7): ``booked`` and ``cancelled``
    tickets, filterable, each resolving to what its booking emitted so the
    provenance from question to booking stays readable.

The web surface lives at ``/transactions#history`` and is implemented in
``web/routes/areas.py`` plus its templates; the list itself arrives with S5.
This module exists for registry completeness (ADR-0058).
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class History(BaseModule):
    """The Transactions area's settled-ticket record.

    Attributes:
        module_name: ``"history"``
        module_area: ``"transactions"``
    """

    module_name = "history"
    module_area = "transactions"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No programmatic entry point; the web surface does the work.

        Returns:
            dict with key ``"status"``.
        """
        return {"status": "stub"}
