# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Watch Desk — Calibration module.

Purpose:
    The tuning surface (ADR-0089), renamed from Watchlist per DC4/D5: the
    read-only Floor Config threshold facts (WARN threshold, per-family
    re-trigger delta, urgency band boundaries) alongside the tenant's Irene
    cadence settings. Calibration edits are threaded through the Floor
    Config surface, never a parallel store.

The web surface lives at ``/watch-desk#calibration`` and is
implemented in ``web/routes/watch_desk.py`` plus its templates. This
module exists for registry completeness (ADR-0058).
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class Calibration(BaseModule):
    """The Watch Desk Calibration surface (threshold facts + cadence).

    Attributes:
        module_name: ``"calibration"``
        module_area: ``"watch_desk"``
    """

    module_name = "calibration"
    module_area = "watch_desk"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No programmatic entry point; the web surface does the work.

        Returns:
            dict with key ``"status"``.
        """
        return {"status": "stub"}
