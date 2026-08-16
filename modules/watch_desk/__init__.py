# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Watch Desk modules — the sixth top-level Area (ADR-0089).

The Watch Desk surfaces Irene's append-only findings and records the
portfolio manager's response. The real work lives in the web surface
(``web/routes/watch_desk.py`` plus its templates) and the lower Irene
layers (``services/irene/``, ``core/repositories/irene_*``). These three thin
:class:`~core.base_module.BaseModule` subclasses exist so the Area has its
registered Modules — one per Section — keeping the area-and-module
conceptual integrity ADR-0058 requires and so
``registry.list_by_area("watch_desk")`` resolves.

The ``scenarios`` placeholder anchor retired with ADR-0104 §8: Feature #034
re-anchors on the Planning Desk's ``scenario_analysis`` module, so the
Watch Desk watches and raises while the Planning Desk projects and simulates.

Each import triggers the ``@registry.register`` decorator on its class.
"""

from modules.watch_desk import briefing  # noqa: F401
from modules.watch_desk import calibration  # noqa: F401
from modules.watch_desk import journal  # noqa: F401
