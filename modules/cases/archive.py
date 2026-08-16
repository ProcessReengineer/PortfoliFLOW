# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Cases — Archive module.

Purpose:
    The closed-case archive search (ADR-0107): a collapsed search affordance
    over titles and closing notes only — never attachment contents (the DMS
    boundary, ADR-0107 §7; the repository's ``search_archive`` already
    enforces the scope).

The web surface lives at ``/cases#archive`` and is implemented in
``web/routes/cases.py`` plus its templates. This module exists for registry
completeness (ADR-0058).
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class Archive(BaseModule):
    """The Cases area's archive search.

    Attributes:
        module_name: ``"archive"``
        module_area: ``"cases"``
    """

    module_name = "archive"
    module_area = "cases"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No programmatic entry point; the web surface does the work.

        Returns:
            dict with key ``"status"``.
        """
        return {"status": "stub"}
