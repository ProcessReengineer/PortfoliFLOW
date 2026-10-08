# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Watch Desk — Journal module.

Purpose:
    The read-only check log (ADR-0089; ADR-0133 §5): resolved findings,
    closed cases and every check as a briefing row — the silent ones
    included — read through three paged repository reads
    (``IreneFindingRepository.list_resolved_before``,
    ``CaseRepository.list_closed_before``,
    ``IreneBriefingRepository.list_before``) and projected by day, with
    server-side kind / period / watch-point filters. Findings are
    append-only (ADR-0085), closed cases immutable (ADR-0107 §4),
    briefing rows never edited (ADR-0133 §1.5); the Journal mutates
    nothing. A written check row carries the "Discuss briefing with
    Shirley" hand-off (ADR-0133 §6), which sets a session context and
    writes nothing here.

The web surface lives at ``/watch-desk#journal`` and is implemented
in ``web/routes/watch_desk.py`` plus its templates. This module
exists for registry completeness (ADR-0058).
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class Journal(BaseModule):
    """The Watch Desk Journal (the check log: findings, cases, checks).

    Attributes:
        module_name: ``"journal"``
        module_area: ``"watch_desk"``
    """

    module_name = "journal"
    module_area = "watch_desk"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No programmatic entry point; the web surface does the work.

        Returns:
            dict with key ``"status"``.
        """
        return {"status": "stub"}
