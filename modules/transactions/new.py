# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Transactions — New transaction module.

Purpose:
    The order and record-flow composer (ADR-0128 §7): pick a flow — buy or
    sell units, buy a new instrument, a commitment, a secondary buy or sell,
    a reported flow — compose the ticket, and carry it to ``proposed``.

The web surface lives at ``/transactions#new`` and is implemented in
``web/routes/areas.py`` plus its templates; the composer itself arrives with
S4. This module exists for registry completeness (ADR-0058).
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class NewTransaction(BaseModule):
    """The Transactions area's record-flow composer.

    Attributes:
        module_name: ``"new"``
        module_area: ``"transactions"``
    """

    module_name = "new"
    module_area = "transactions"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No programmatic entry point; the web surface does the work.

        Returns:
            dict with key ``"status"``.
        """
        return {"status": "stub"}
