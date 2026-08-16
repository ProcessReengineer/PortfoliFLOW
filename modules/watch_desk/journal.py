# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Watch Desk — Journal module.

Purpose:
    The read-only history of resolved findings (ADR-0089), read via
    ``IreneFindingRepository.list_journal()``. Findings are append-only
    (ADR-0085); the Journal never mutates them.

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
    """The Watch Desk Journal (resolved-finding history).

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
