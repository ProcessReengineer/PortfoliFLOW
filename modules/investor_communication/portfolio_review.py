# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Investor Communication — Portfolio Review module.

Purpose:
    Produce a multi-tile in-app report covering the entire portfolio plus
    each individual investment.  The tiles consist of a NAV / IRR / TVPI /
    DPI key-figures strip and a 2×3 grid of charts (cashflows by year,
    multiples, IRR, strategy split, country split, sector split).

Inputs (from the DataStore):
    * ``attributes`` — investment metadata; column order defines tile order.
    * ``navs_actual``, ``cash_flow_in_actual``, ``cash_flow_out_actual`` —
      per-investment time series.

Outputs:
    A list of :class:`services.reporting.report_engine.ReportTile` instances
    returned via :meth:`run`.
"""

from __future__ import annotations

import logging
from typing import Any

from core.base_module import BaseModule
from core.exceptions import ModuleError
from modules.module_registry import registry
from services.reporting.report_engine import ReportEngine

logger = logging.getLogger(__name__)


@registry.register
class PortfolioReview(BaseModule):
    """Portfolio Review module — assembles the multi-tile report.

    Attributes:
        module_name: ``"portfolio_review"``
        module_area: ``"investor_communication"``
    """

    module_name = "portfolio_review"
    module_area = "investor_communication"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """Generate the Portfolio Review report tiles.

        Keyword Args:
            action (str): Currently only ``"generate"`` is supported.

        Returns:
            On success::

                {"status": "ok", "tiles": [ReportTile, ...]}

            ``tiles`` is empty when the DataStore has no ``attributes`` or
            ``navs_actual`` data — this is treated as a normal "no data" state,
            not an error.

            On failure::

                {"status": "error", "error": "..."}
        """
        action: str = kwargs.get("action", "generate")
        if action != "generate":
            return {"status": "error", "error": f"Unknown action: {action}"}

        try:
            engine = ReportEngine()
            tiles = engine.build_report()
        except ModuleError as exc:
            self._logger.exception("Report generation failed (ModuleError).")
            return {"status": "error", "error": str(exc)}
        except (KeyError, ValueError, TypeError) as exc:
            self._logger.exception("Report generation failed.")
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

        return {"status": "ok", "tiles": tiles}
