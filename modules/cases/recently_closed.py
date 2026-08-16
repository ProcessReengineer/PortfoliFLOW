# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Cases — Recently closed module.

Purpose:
    The reviewer's view (ADR-0107): the last five closed cases, each with
    owner, closed date and a closing-note excerpt. No Journal reference yet
    — the Journal deep link and its closed-case projection source arrive in
    C4 (Gate-C0 decision).

The web surface lives at ``/cases#recently-closed`` and is implemented in
``web/routes/cases.py`` plus its templates. This module exists for registry
completeness (ADR-0058).
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class RecentlyClosed(BaseModule):
    """The Cases area's recently-closed list.

    Attributes:
        module_name: ``"recently_closed"``
        module_area: ``"cases"``
    """

    module_name = "recently_closed"
    module_area = "cases"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No programmatic entry point; the web surface does the work.

        Returns:
            dict with key ``"status"``.
        """
        return {"status": "stub"}
