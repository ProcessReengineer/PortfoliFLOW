# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Service layer for the Transactions area (ADR-0128).

The trade-ticket workflow seam: :class:`TicketService` composes, validates
and advances the tickets that :class:`~core.repositories.trade_ticket_repository.TradeTicketRepository`
stores. The repository is mechanism (move a ticket between stations, record
what a booking emitted); this package is policy (which transitions are
legal, what "complete and validated" means per flow, what to warn about).

Three modules, three concerns:

* :mod:`services.transactions.constants` — the vocabulary decision record
  §2.9 fixes: the price-deviation threshold, the warning and block
  identifiers, the lifecycle / kind / direction vocabularies, and the
  master-data payload keys. Services and (from S4) templates reference it;
  nothing re-declares these strings.
* :mod:`services.transactions.validation` — the warning DTOs and the pure
  derivations behind them (flow classification, cash effect, price
  deviation), testable without a database.
* :mod:`services.transactions.ticket_service` — the async workflow.

Strand S1 covers ``create_draft`` / ``update_draft`` / ``propose`` /
``cancel``. Booking and effect emission are S2, reversal is S2c, and the
routes and composer surfaces are S3/S4 — see :class:`TicketService`'s
docstring for why each is absent rather than forgotten.

The package holds no state and opens no session: the caller opens
``core.repositories.tenant_context(...)`` and hands in tenant-scoped
repositories.
"""

from services.transactions.constants import (
    BLOCK_IDENTIFIERS,
    CANCEL_REASON_REQUIRED_STATUSES,
    CANCELLABLE_STATUSES,
    COMPLETENESS_IDENTIFIERS,
    DIRECTIONS,
    KINDS,
    PRICE_DEVIATION_WARN_RATIO,
    STATUSES,
    V1_REACHABLE_STATUSES,
    WARNING_IDENTIFIERS,
)
from services.transactions.ticket_service import TicketService
from services.transactions.validation import (
    TicketWarning,
    TicketWarnings,
    derive_cash_effect,
    is_cash_moving,
    is_investment_creating,
)

__all__ = [
    "BLOCK_IDENTIFIERS",
    "CANCELLABLE_STATUSES",
    "CANCEL_REASON_REQUIRED_STATUSES",
    "COMPLETENESS_IDENTIFIERS",
    "DIRECTIONS",
    "KINDS",
    "PRICE_DEVIATION_WARN_RATIO",
    "STATUSES",
    "V1_REACHABLE_STATUSES",
    "WARNING_IDENTIFIERS",
    "TicketService",
    "TicketWarning",
    "TicketWarnings",
    "derive_cash_effect",
    "is_cash_moving",
    "is_investment_creating",
]
