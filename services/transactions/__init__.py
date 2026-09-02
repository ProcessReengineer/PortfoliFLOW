# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Service layer for the Transactions area (ADR-0128).

The trade-ticket workflow seam: :class:`TicketService` composes, validates
and advances the tickets that :class:`~core.repositories.trade_ticket_repository.TradeTicketRepository`
stores. The repository is mechanism (move a ticket between stations, record
what a booking emitted); this package is policy (which transitions are
legal, what "complete and validated" means per flow, what to warn about).

Four modules, four concerns:

* :mod:`services.transactions.constants` — the vocabulary decision record
  §2.9 fixes: the price-deviation threshold, the warning and block
  identifiers, the lifecycle / kind / direction vocabularies, and the
  master-data payload keys. Services and (from S4) templates reference it;
  nothing re-declares these strings.
* :mod:`services.transactions.validation` — the warning, block and preview
  DTOs and the pure derivations behind them (flow classification, cash
  effect, price deviation), testable without a database.
* :mod:`services.transactions.emission` — what a booking *writes*: the pure
  derivation from a ticket's columns to ledger rows, the atomic emission
  that hands them to the one sanctioned write seam (ADR-0128 §2), and the
  inverse that undoes them (ADR-0128 §6).
* :mod:`services.transactions.ticket_service` — the async workflow.

Strand S1 and S2 cover ``create_draft`` / ``update_draft`` / ``propose`` /
``cancel``, ``book`` for all six flows — including the three that *create*
their ``investments`` row as an emission effect (MD-12) — and ``reverse``,
which undoes a booking whole and cancels the ticket with a reason. Ahead of
S4, ``preview`` (P-0b) runs those same propose-time derivations read-only
against a ticket that need not exist, so the composer can state a
consequence without causing one. The routes and composer surfaces are S3/S4
— see :class:`TicketService`'s docstring for why each is absent rather than
forgotten.

The package holds no state and opens no session: the caller opens
``core.repositories.tenant_context(...)`` and hands in tenant-scoped
repositories.
"""

from services.transactions.constants import (
    BLOCK_IDENTIFIERS,
    BOOKABLE_STATUSES,
    CANCEL_REASON_REQUIRED_STATUSES,
    CANCELLABLE_STATUSES,
    COMPLETENESS_IDENTIFIERS,
    DIRECTIONS,
    KINDS,
    PRICE_DEVIATION_WARN_RATIO,
    REVERSAL_CAUSES,
    STATUSES,
    V1_REACHABLE_STATUSES,
    WARNING_IDENTIFIERS,
)
from services.transactions.emission import (
    MasterData,
    ReversalReport,
    ShellOutcome,
    emit_commitment,
    emit_new_order,
    emit_order,
    emit_secondary_buy,
    emit_secondary_sell,
    investment_before_image,
    parse_master_data,
    provenance,
)
from services.transactions.ticket_service import TicketService
from services.transactions.validation import (
    TicketBlock,
    TicketPreview,
    TicketWarning,
    TicketWarnings,
    derive_cash_effect,
    is_cash_moving,
    is_investment_creating,
)

__all__ = [
    "BLOCK_IDENTIFIERS",
    "BOOKABLE_STATUSES",
    "CANCELLABLE_STATUSES",
    "CANCEL_REASON_REQUIRED_STATUSES",
    "COMPLETENESS_IDENTIFIERS",
    "DIRECTIONS",
    "KINDS",
    "PRICE_DEVIATION_WARN_RATIO",
    "REVERSAL_CAUSES",
    "STATUSES",
    "V1_REACHABLE_STATUSES",
    "WARNING_IDENTIFIERS",
    "MasterData",
    "ReversalReport",
    "ShellOutcome",
    "TicketBlock",
    "TicketPreview",
    "TicketService",
    "TicketWarning",
    "TicketWarnings",
    "derive_cash_effect",
    "emit_commitment",
    "emit_new_order",
    "emit_order",
    "emit_secondary_buy",
    "emit_secondary_sell",
    "investment_before_image",
    "is_cash_moving",
    "is_investment_creating",
    "parse_master_data",
    "provenance",
]
