# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Planning Desk modules — the seventh top-level Area (ADR-0104 §6).

The Planning Desk projects and simulates over the plan world, where the
Watch Desk watches and raises. The real work lives in the web surface
(``web/routes/`` plus its templates, from S2.4 onward) and the lower layers
that ADR-0104 commissions — ``services/overlay/`` (the four-kind scenario
overlay contract) and ``services/investments/plan_world`` (baseline
assembly). These two thin :class:`~core.base_module.BaseModule` subclasses
exist so the Area has its registered Modules — one per Section — keeping the
area-and-module conceptual integrity ADR-0058 requires and so
``registry.list_by_area("planning_desk")`` resolves.

Each import triggers the ``@registry.register`` decorator on its class.
"""

from modules.planning_desk import cash_flow_planning  # noqa: F401
from modules.planning_desk import scenario_analysis  # noqa: F401
