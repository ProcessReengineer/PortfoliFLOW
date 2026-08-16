# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Watch Desk — Briefing module.

Purpose:
    The calm-by-default card feed of open Irene findings (ADR-0089). It
    reads findings via ``IreneFindingRepository.list_open()`` and records
    the PM's resolution; it introduces no materiality logic of its own.

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
    """The Watch Desk Briefing feed.

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
