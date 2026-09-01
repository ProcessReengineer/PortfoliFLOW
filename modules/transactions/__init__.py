# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Transactions modules — the ninth top-level Area (ADR-0128 §7).

The Transactions area is where a portfolio change is recorded: a trade
ticket carries one intended or recorded change from ``draft`` through
``proposed`` and ``approved`` to ``booked`` (or ``cancelled``), settling
atomically against a cash position. It completes the Watch Desk → Cases →
Transactions chain — the Watch Desk raises the question, a Case carries it
to a documented close, a ticket executes the decision.

The real work lives in the web surface (``web/routes/areas.py`` plus its
templates today; the record-flow routes arrive with S4) and in the service
and persistence layers (``services/transactions/``, ``core/models/
trade_ticket.py`` and ``core/repositories/trade_ticket_repository.py``).
These three thin :class:`~core.base_module.BaseModule` subclasses exist so
the Area has its registered Modules — one per Section — keeping the
area-and-module conceptual integrity ADR-0058 requires and so
``registry.list_by_area("transactions")`` resolves.

Each import triggers the ``@registry.register`` decorator on its class.
"""

from modules.transactions import blotter  # noqa: F401
from modules.transactions import history  # noqa: F401
from modules.transactions import new  # noqa: F401
