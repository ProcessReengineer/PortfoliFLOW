# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Trade-ticket validation vocabulary and its pure derivations (ADR-0128 §4).

One concern: **everything the propose-time validation needs that is a
function of values rather than of the database**. The warning DTOs the
service returns live here alongside the arithmetic those warnings are made
of — flow classification, the cash effect, the price deviation — so each is
directly testable without a session, a tenant, or a seeded ledger, and so
:mod:`services.transactions.ticket_service` stays the orchestration it is
meant to be (load, check, flip).

Nothing here reaches the database, and nothing here decides *policy*: which
gaps block and which merely warn is the service's, stated once in
:meth:`services.transactions.ticket_service.TicketService.propose`. These
functions only answer "what is this ticket, and what does it move".

The module observes the same stdlib-plus-``core.exceptions``-free discipline
as :mod:`services.investments.holdings`, but is not machine-pinned to it:
it carries no purity guard because, unlike the analytics and overlay layers,
nothing downstream depends on its import graph being provably book-free.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from services.transactions.constants import (
    DIRECTION_BUY,
    KIND_COMMITMENT,
    KIND_ORDER,
    KIND_SECONDARY,
)

# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TicketWarning:
    """One propose-time warning: an identifier plus the values it needs.

    A warning **never blocks anything** (MD-5, D-2). It states a consequence
    and steps aside; the contents of a transaction stay the fund manager's
    responsibility.

    No rendered prose is carried. MD-9 fixes the copy in the mockups and S4
    lifts it verbatim into templates, so what crosses this seam is the
    identifier plus the structured values the copy interpolates. That split
    keeps the wording changeable without touching a service, and keeps these
    tests free of assertions on prose.

    Attributes:
        identifier: One of
            :data:`services.transactions.constants.WARNING_IDENTIFIERS`.
        data: The structured values the message needs. The shape is fixed
            per identifier:

            * ``price_deviation`` — ``reference_price`` (:class:`~decimal.Decimal`),
              ``reference_date`` (:class:`~datetime.date`), ``deviation_ratio``
              (:class:`~decimal.Decimal`, the absolute ratio, so ``0.08``
              means 8 % away in either direction).
            * ``negative_cash`` — ``resulting_balance``
              (:class:`~decimal.Decimal`, signed, the settlement position's
              balance *after* this ticket books), ``currency`` (:class:`str`).
            * ``net_non_positive`` — ``net_amount``
              (:class:`~decimal.Decimal`, the derived net cash effect, ``<= 0``),
              ``currency`` (:class:`str`).
            * ``future_trade_date`` — ``trade_date``
              (:class:`~datetime.date`), ``today`` (:class:`~datetime.date`,
              the caller-injected current date).
    """

    identifier: str
    data: Mapping[str, object]


@dataclass(frozen=True)
class TicketWarnings:
    """The warnings one :meth:`propose` call collected, in detection order.

    Collected exhaustively rather than short-circuited: a composer shows all
    of them at once, and a caller that saw only the first would have to
    re-propose to discover the second.

    Attributes:
        warnings: The warnings, in the order the service produced them.
    """

    warnings: tuple[TicketWarning, ...] = ()

    @property
    def identifiers(self) -> tuple[str, ...]:
        """The identifiers only, in the same order — the convenient assertion."""
        return tuple(warning.identifier for warning in self.warnings)

    def __bool__(self) -> bool:
        """True when anything was warned about."""
        return bool(self.warnings)

    def __len__(self) -> int:
        return len(self.warnings)


# ---------------------------------------------------------------------------
# Flow classification
# ---------------------------------------------------------------------------


def is_investment_creating(
    *,
    kind: str,
    direction: str,
    investment_id: object | None,
    master_data: Mapping[str, object] | None,
) -> bool:
    """Whether this ticket's booking will *create* the investment row.

    The classification S2 reuses at emission time, stated once here.

    Under MD-12 the ``investments`` row is an **emission effect, not a
    precondition**: the master data lives on the ticket as payload until
    booking creates the row, so ``investment_id IS NULL`` is the necessary
    condition. It is not sufficient — a plain order draft mid-composition
    has no investment yet either, and it is not creating one. The three
    creating flows are therefore named concretely (MD-21's gate scope, and
    MD-15's reported-form inventory):

    * **U-NEW** — ``kind='order'``, ``direction='buy'``, no
      ``investment_id``, and a ``master_data`` payload present. The payload
      is what distinguishes the wizard from a bare order draft.
    * **R-COMMIT** — ``kind='commitment'``. A commitment always records a
      new position; there is nothing else it could mean.
    * **R-SEC-BUY** — ``kind='secondary'``, ``direction='buy'``.

    Args:
        kind: The ticket kind.
        direction: The ticket direction.
        investment_id: The traded investment, or ``None``.
        master_data: The master-data payload, or ``None``.

    Returns:
        ``True`` for U-NEW, R-COMMIT and R-SEC-BUY; ``False`` otherwise.
    """
    if investment_id is not None:
        return False
    if kind == KIND_COMMITMENT:
        return True
    if kind == KIND_SECONDARY and direction == DIRECTION_BUY:
        return True
    if kind == KIND_ORDER and direction == DIRECTION_BUY:
        return bool(master_data)
    return False


def is_cash_moving(*, kind: str) -> bool:
    """Whether booking this ticket settles against a cash position.

    Every flow moves cash except a commitment (MD-19, R-3): no money moves
    with a commitment ticket — the capital calls do that, and they remain
    ordinary cashflows outside the ticket object. This is why
    ``ck_trade_tickets_commitment_shape`` forces ``cash_investment_id`` NULL
    for ``kind='commitment'`` in the schema too.

    Args:
        kind: The ticket kind.

    Returns:
        ``True`` for ``order`` and ``secondary``; ``False`` for ``commitment``.
    """
    return kind != KIND_COMMITMENT


# ---------------------------------------------------------------------------
# Cash effect
# ---------------------------------------------------------------------------


def derive_cash_effect(
    *,
    direction: str,
    net_amount: Decimal | None = None,
    gross_amount: Decimal | None = None,
    units: Decimal | None = None,
    price_per_unit: Decimal | None = None,
    fees: Decimal | None = None,
    taxes: Decimal | None = None,
) -> Decimal | None:
    """Derive the amount of cash the ticket moves, in the ticket currency.

    The **one** derivation of the cash magnitude, so the composer preview,
    the negative-cash warning, the net-proceeds warning and (in S2) the cash
    leg of the emission cannot disagree about what a ticket costs or yields.

    Three input shapes, in precedence order — a later shape is consulted only
    when the earlier one is absent:

    1. ``net_amount`` — the settlement cash effect, stated outright. Taken
       as given; fees and taxes are understood to be inside it already.
    2. ``gross_amount`` ± fees and taxes.
    3. ``units × price_per_unit`` ± fees and taxes.

    The sign convention is *magnitude, direction applied by the caller*:
    the result is what the cash position gives up on a ``buy`` and what it
    receives on a ``sell``. Hence costs are **added** on a buy
    (``gross + fees + taxes``, working document §2.2) and **subtracted** on a
    sell (``gross − fees − taxes``, §2.1). ``units`` is unsigned on the
    ticket, matching ``ck_trade_tickets_units_positive``.

    Missing ``fees`` / ``taxes`` are treated as zero — the composer leaves
    them blank rather than typing 0, and the working document's inputs table
    makes them optional with default 0.

    Args:
        direction: ``buy`` or ``sell``; decides the sign of the costs.
        net_amount: The stated settlement cash effect, if known.
        gross_amount: The stated gross consideration, if known.
        units: The unsigned unit quantity, for the units × price shape.
        price_per_unit: The execution price, for the units × price shape.
        fees: Transaction costs; ``None`` reads as zero.
        taxes: Taxes split out of fees; ``None`` reads as zero.

    Returns:
        The cash magnitude, or ``None`` when no shape is derivable — the
        ticket simply does not say yet what it moves. A ``sell`` whose costs
        exceed its gross legitimately returns a negative value; that is the
        ``net_non_positive`` warning's subject, not an error.
    """
    if net_amount is not None:
        return net_amount

    base = gross_amount
    if base is None:
        if units is None or price_per_unit is None:
            return None
        base = units * price_per_unit

    costs = (fees or Decimal(0)) + (taxes or Decimal(0))
    return base + costs if direction == DIRECTION_BUY else base - costs


# ---------------------------------------------------------------------------
# Price deviation
# ---------------------------------------------------------------------------


class PricePoint(Protocol):
    """The structural shape the price-deviation check needs from a price row.

    :class:`core.repositories.instrument_price_repository.InstrumentPriceDTO`
    satisfies it without this module importing the repository — the same
    protocol idiom :mod:`services.investments.holdings` uses for the ledger.

    The members are declared **read-only** (properties rather than plain
    attributes) because every DTO that satisfies this protocol is a frozen
    dataclass. A protocol attribute is writable by default, and a frozen
    class cannot satisfy a writable member — so the plain-attribute spelling
    would type-check as incompatible against exactly the callers it exists
    for. Reading is all this module ever does.
    """

    @property
    def as_of_date(self) -> date:
        """The statement day the price refers to."""
        ...

    @property
    def price(self) -> Decimal:
        """The price on that day, in the investment's currency."""
        ...


def nearest_price(points: Sequence[PricePoint], on: date) -> PricePoint | None:
    """Return the price row closest in time to ``on``, or ``None`` if empty.

    "At or near the trade date" (working document §2.1) resolved concretely:
    the smallest absolute day distance wins, and an exact tie between an
    earlier and a later row resolves to the **earlier** one — the price that
    was actually knowable when the trade was struck is the better reference
    for judging an execution price.

    Args:
        points: The investment's price rows, in any order.
        on: The date to measure distance from — the trade date.

    Returns:
        The nearest row, or ``None`` for an empty series. A missing series is
        not suspicious: the stored price may simply not exist.
    """
    if not points:
        return None
    return min(points, key=lambda p: (abs((p.as_of_date - on).days), p.as_of_date))


def price_deviation_ratio(*, price: Decimal, reference: Decimal) -> Decimal | None:
    """Return ``|price − reference| / reference``, or ``None`` if undefined.

    Args:
        price: The ticket's execution price.
        reference: The nearest stored price.

    Returns:
        The absolute deviation as a ratio of the reference, or ``None`` when
        the reference is zero — a zero reference makes the ratio meaningless
        rather than infinite, and there is nothing useful to warn about.
    """
    if reference == 0:
        return None
    return abs(price - reference) / reference


__all__ = [
    "PricePoint",
    "TicketWarning",
    "TicketWarnings",
    "derive_cash_effect",
    "is_cash_moving",
    "is_investment_creating",
    "nearest_price",
    "price_deviation_ratio",
]
