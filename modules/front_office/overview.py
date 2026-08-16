# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Front Office — Overview module.

Registers the portfolio headline KPI strip (ADR-0067) as a section of the
Front Office area. The section renders first in the long-scroll layout,
ahead of Charts.

All business logic lives in :mod:`services.front_office_overview`; the web
surface is rendered by :mod:`web.routes.overview`. This registration only
declares the section's identity so it participates in the registry-ordered
information architecture (ADR-0058).
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class Overview(BaseModule):
    """Portfolio headline KPI strip for the Front Office area.

    Attributes:
        module_name: ``"overview"``
        module_area: ``"front_office"``
    """

    module_name = "overview"
    module_area = "front_office"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No-op entry point.

        The Overview surface is rendered server-side by
        :mod:`web.routes.overview`; this module carries no runnable
        business logic of its own. Returns a trivial status dict to honour
        the :class:`BaseModule` contract.

        Returns:
            dict: ``{"status": "ok"}``.
        """
        return {"status": "ok"}
