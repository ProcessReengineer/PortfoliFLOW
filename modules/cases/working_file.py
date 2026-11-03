# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Cases — the one Cases module (ADR-0138 §12).

Purpose:
    The Cases area is one Section, ``cases``: the list with the filter
    Open · Waiting · Closed · All, *Mine* and search, and the case opened as
    the Section's sub-surface — the working file in front, the record behind.
    One Section, one module (ADR-0058).

The web surface (``web/routes/cases.py`` plus its templates, the page URLs in
``web/routes/areas.py``) and the persistence layer
(``core/repositories/case_repository.py`` and its working-file siblings) do
the work. This module exists for registry completeness.
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class Cases(BaseModule):
    """The Cases area's one module — the list and the case file.

    Attributes:
        module_name: ``"cases"``
        module_area: ``"cases"``
    """

    module_name = "cases"
    module_area = "cases"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No programmatic entry point; the web surface does the work.

        Returns:
            dict with key ``"status"``.
        """
        return {"status": "stub"}
