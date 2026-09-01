# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure holdings derivation over the transaction ledger (ADR-0097 §4).

Holdings (units held per statement day) are **not** stored: they are a
pure derivation over ``position_transactions`` — the cumulative signed sum
of ``units`` ordered by the total tiebreak ``(trade_date, created_at,
id)``, evaluated as a step function of ``trade_date``. There is no
``holdings`` snapshot table; the materialised computed-NAV rows in
``investment_navs`` are the persisted read product (ADR-0098, strand S2).

This module mirrors the pure-predicate precedent of
:mod:`services.investments.market_linked`, but is stricter: it imports
**only the standard library** and operates on a structural
:class:`LedgerTransaction` protocol, so it takes no dependency on the
repository, the ORM, a DB session, a network client, or FastAPI. Its
purity is machine-enforced by
``tests/regression/test_holdings_pure.py``. It is deliberately **not**
placed under ``services/analytics/`` — its concern is position
bookkeeping, not analytics — but it observes the same purity.

The service layer (:class:`services.investments.InvestmentService`) calls
:func:`first_negative_holding_date` at write time to enforce the ADR-0097
§4 non-negativity invariant on **non-cash** investments and rejects the
write with a domain error. Investments of ``investment_type='cash'`` are
exempt on every write path (ADR-0130): a negative cash balance is a
permitted, surfaced state rather than an impossible one. The exemption is
that service-layer decision about whether to raise; nothing in this module
changes with it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID


class LedgerTransaction(Protocol):
    """The structural shape holdings derivation needs from a ledger row.

    :class:`core.repositories.position_transaction_repository.PositionTransactionDTO`
    satisfies this protocol without this module importing it — keeping the
    derivation free of any project dependency. Only the five fields the
    total order and the cumulative sum need are declared.
    """

    txn_type: str
    trade_date: date
    units: Decimal
    created_at: datetime
    id: UUID


@dataclass(frozen=True)
class HoldingPoint:
    """Cumulative units held from ``as_of_date`` onward (a step point).

    Attributes:
        as_of_date: The statement day the step takes effect on.
        units: The cumulative signed sum of all transactions with
            ``trade_date <= as_of_date`` — the units held from this date
            until the next :class:`HoldingPoint` (or indefinitely).
    """

    as_of_date: date
    units: Decimal


def _ordered(
    transactions: Iterable[LedgerTransaction],
) -> list[LedgerTransaction]:
    """Return the transactions in the canonical total order (ADR-0097 §2).

    The order ``(trade_date, created_at, id)`` is total and reproducible:
    ``trade_date`` is the economic ordering, ``created_at`` breaks ties
    within a day, and ``id`` breaks the (pathological) remaining ties so
    the result never depends on input iteration order.

    Args:
        transactions: The ledger rows, in any order.

    Returns:
        A new list sorted by ``(trade_date, created_at, id)``.
    """
    return sorted(
        transactions,
        key=lambda t: (t.trade_date, t.created_at, t.id),
    )


def derive_holdings(
    transactions: Iterable[LedgerTransaction],
) -> list[HoldingPoint]:
    """Derive the holdings step function from the ledger.

    Collapses the cumulative signed sum to **one point per distinct
    ``trade_date``** carrying the end-of-day cumulative units for that day.
    The result is the step function materialisation (ADR-0098, strand S2)
    samples on statement days.

    Args:
        transactions: The investment's ledger rows, in any order.

    Returns:
        Holding points sorted ascending by ``as_of_date``, one per distinct
        ``trade_date``. Empty list for an empty ledger.
    """
    ordered = _ordered(transactions)
    points: list[HoldingPoint] = []
    running = Decimal(0)
    for txn in ordered:
        running += txn.units
        if points and points[-1].as_of_date == txn.trade_date:
            # Later transaction on the same day supersedes the day's point.
            points[-1] = HoldingPoint(txn.trade_date, running)
        else:
            points.append(HoldingPoint(txn.trade_date, running))
    return points


def holdings_as_of(transactions: Iterable[LedgerTransaction], on: date) -> Decimal:
    """Return the units held on a given statement day (carry-forward).

    The holdings are a step function: the value on ``on`` is the cumulative
    signed sum of every transaction with ``trade_date <= on``. Before the
    first transaction the holding is zero.

    Args:
        transactions: The investment's ledger rows, in any order.
        on: The statement day to evaluate holdings at.

    Returns:
        The units held on ``on``. May be zero, and may legitimately be
        negative for a cash investment — an overdraft is an economic fact
        the book records (ADR-0130), surfaced rather than refused.
    """
    running = Decimal(0)
    for txn in _ordered(transactions):
        if txn.trade_date > on:
            break
        running += txn.units
    return running


def first_negative_holding_date(
    transactions: Iterable[LedgerTransaction],
) -> date | None:
    """Return the first ``trade_date`` at which holdings go negative, if any.

    The ADR-0097 §4 non-negativity invariant: no transaction may drive
    derived holdings below zero on any date. This scans the ledger in the
    canonical total order and returns the ``trade_date`` of the **first**
    transaction whose application makes the running cumulative negative.
    The scan is per-transaction, not per-day, so an intra-day overdraw
    (e.g. a ``sell`` that exceeds holdings even if a later same-day ``buy``
    would restore it) is caught — you cannot sell units you do not hold.

    This is the write-time check helper the service layer calls to reject a
    would-be negative-holdings write with a typed domain error.

    Args:
        transactions: The full candidate ledger — existing rows plus the
            transaction being validated — in any order.

    Returns:
        The earliest offending ``trade_date`` in canonical order, or
        ``None`` if holdings stay non-negative throughout.
    """
    running = Decimal(0)
    for txn in _ordered(transactions):
        running += txn.units
        if running < 0:
            return txn.trade_date
    return None


__all__ = [
    "HoldingPoint",
    "LedgerTransaction",
    "derive_holdings",
    "first_negative_holding_date",
    "holdings_as_of",
]
