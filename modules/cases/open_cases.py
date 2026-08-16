# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Cases — Open cases module.

Purpose:
    The to-do list surface (ADR-0107): every open case, newest first, with
    a "Mine" filter chip (``opened_by`` — a filter, never a data boundary,
    ADR-0107 §1) and the manual "New case" affordance.

The web surface lives at ``/cases#open-cases`` and is implemented in
``web/routes/cases.py`` plus its templates. This module exists for registry
completeness (ADR-0058).
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class OpenCases(BaseModule):
    """The Cases area's open-cases list.

    Attributes:
        module_name: ``"open_cases"``
        module_area: ``"cases"``
    """

    module_name = "open_cases"
    module_area = "cases"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No programmatic entry point; the web surface does the work.

        Returns:
            dict with key ``"status"``.
        """
        return {"status": "stub"}
