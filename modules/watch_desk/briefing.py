# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Watch Desk — Briefing module.

Purpose:
    The calm-by-default landing of the Watch Desk (ADR-0089): since
    ADR-0133 the newest written briefing of the last check (two parts,
    with its sources line; "Briefing pending" while the newest check wrote
    none), the standing note per switched-off quota family (D-WD-17), the
    watch strip, then the card feed of open findings read via
    ``IreneFindingRepository.list_open()``, where the PM's resolution is
    recorded. It introduces no materiality logic of its own; the briefing
    row is read, never written here.

The web surface lives at ``/watch-desk#briefing`` and is
implemented in ``web/routes/watch_desk.py`` plus its templates. This
module exists for registry completeness (ADR-0058).
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class Briefing(BaseModule):
    """The Watch Desk Briefing (latest briefing, strip and the open-finding feed).

    Attributes:
        module_name: ``"briefing"``
        module_area: ``"watch_desk"``
    """

    module_name = "briefing"
    module_area = "watch_desk"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No programmatic entry point; the web surface does the work.

        Returns:
            dict with key ``"status"``.
        """
        return {"status": "stub"}
