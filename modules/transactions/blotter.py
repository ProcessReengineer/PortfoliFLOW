# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Transactions — Blotter module.

Purpose:
    The live ticket list (ADR-0128 §7): every ticket still in flight —
    ``draft``, ``proposed``, ``approved`` — with the station it sits at and
    the action that advances it.

The web surface lives at ``/transactions#blotter`` and is implemented in
``web/routes/areas.py`` plus its templates; the list itself arrives with S5.
This module exists for registry completeness (ADR-0058).
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class Blotter(BaseModule):
    """The Transactions area's in-flight ticket list.

    Attributes:
        module_name: ``"blotter"``
        module_area: ``"transactions"``
    """

    module_name = "blotter"
    module_area = "transactions"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No programmatic entry point; the web surface does the work.

        Returns:
            dict with key ``"status"``.
        """
        return {"status": "stub"}
