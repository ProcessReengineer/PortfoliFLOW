# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The unity-price constraint — what a cash position's prices must be.

ADR-0103 §1 makes cash the *degenerate* unitised case: units are currency
units, so ``balance ≡ holdings``, and the price series is a run of **stored**
rows of exactly one, one per statement date, in the position currency.
``holdings × 1`` then reproduces the statement balance, and the unchanged
ADR-0098 materialisation values a cash position with no special case
anywhere in the book path.

**Why stored, not implied.** The ADR-0098 materialised set is defined as one
NAV row *per ``instrument_prices`` date* on or after the first ledger date.
An implied constant price would give a cash position at most zero
materialisable dates and would force a cash branch into the materialisation
service — exactly what the compatibility annex §A.1 forbids. One redundant-
looking price row per statement date is the entire cost of running cash
through the existing machinery unchanged.

Two facts make a "price" on a cash position meaningful only as unity, and
this module owns both:

1. **The value is one.** Any other number would silently redefine the
   balance, since the materialised NAV is ``holdings × price``.
2. **The currency matches the position** — the ADR-0097 §5 currency-equality
   rule, restated for the cash case. A unity price in the wrong currency
   would be a 1:1 FX conversion smuggled into the write path, which
   ADR-0099 forbids outright (never a silent 1:1 fallback).

This module is the **definition** — the single seam, following the
:mod:`services.investments.flow_type_invariants` precedent. It does not
enforce: no write path consults it yet, because the two writers it exists
for do not exist yet. Both owe it:

* **the Cash-sheet importer** (ADR-0103 §3/§4), which writes one unity row
  per statement date; and
* **the ADR-0100-row migration** (ADR-0103 §9), which backfills one unity
  row per existing NAV date before flipping those rows to ``'unitised'``.

Each must route every cash price through :func:`unity_price_violation`
rather than restate the rule — a second formulation drifts from this one the
first time either changes.

**Live ingest is never a legal unity-price writer.** Cash is excluded from
the market-linked predicate on type alone, permanently (ADR-0103 §1,
:data:`services.investments.market_linked.MARKET_LINKED_TYPES`), so no
provider price can ever land on a cash position. The only prices a cash
position carries are the ones the two writers above put there.

Import-pure — stdlib only, no database, no FastAPI, no provider SDK —
guarded by ``tests/regression/test_unity_price_pure.py``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

#: The scale of ``instrument_prices.price`` — ``Numeric(20, 8)``
#: (:class:`core.models.instrument_price.InstrumentPrice`, migration b024).
#: ADR-0103 §1 writes the unity price as "1.0000"; the column stores eight
#: fractional digits, and the two spell the same value. Comparison happens at
#: this scale, so a price is unity iff the number Postgres would *store* is
#: one.
PRICE_SCALE: int = 8

#: The only price a cash position may carry (ADR-0103 §1), quantised to the
#: ``instrument_prices`` scale so a round-trip through the column returns an
#: identical representation. Decimal throughout: a binary float cannot hold
#: this contract, since the whole point is exactness.
UNITY_PRICE: Decimal = Decimal(1).quantize(Decimal(1).scaleb(-PRICE_SCALE))


def _is_unity(price: Decimal) -> bool:
    """Return whether ``price`` is one at the stored scale.

    Quantising to :data:`PRICE_SCALE` before comparing makes the check agree
    with the database: the test is whether the value Postgres would store in
    ``Numeric(20, 8)`` is one. Representation is therefore irrelevant —
    ``Decimal('1')``, ``Decimal('1.0000')`` and ``Decimal('1.00000000')`` all
    pass — while any deviation the column could actually record fails.
    """
    try:
        return price.quantize(UNITY_PRICE) == UNITY_PRICE
    except (InvalidOperation, ArithmeticError):
        # A value too large to express at this scale (it would not fit the
        # column either) is, trivially, not unity.
        return False


def unity_price_violation(
    price: Decimal,
    price_currency: str,
    investment_currency: str,
) -> str | None:
    """Return why this price row may not sit on a cash position.

    The ADR-0103 §1 constraint as a single predicate, mirroring the idiom of
    :func:`services.investments.valuation_mode.flip_precondition_error`: a
    ``None`` return is permission, and any other return is one
    operator-facing sentence naming the violation.

    Args:
        price: The candidate ``instrument_prices.price`` value. Compared at
            the stored scale, so any exact spelling of one is accepted.
        price_currency: The candidate row's ``currency``.
        investment_currency: The cash position's ``investments.currency``.

    Returns:
        ``None`` when the price is exactly one *and* the currencies match —
        the only price a cash position may carry; otherwise a single
        operator-facing sentence naming the blocking condition, suitable for
        direct display or for a rejected import row's message.
    """
    if not _is_unity(price):
        return (
            f"A cash position's price must be exactly {UNITY_PRICE}, not "
            f"{price}: its balance is carried by the ledger, and any other "
            "price would silently restate it."
        )
    if price_currency != investment_currency:
        return (
            f"Price currency {price_currency!r} does not match the cash "
            f"position's currency {investment_currency!r}; a unity price is "
            "stated in the position's own currency, never converted."
        )
    return None


def is_unity_price(
    price: Decimal,
    price_currency: str,
    investment_currency: str,
) -> bool:
    """Return whether this price row satisfies the cash unity constraint.

    The boolean complement of :func:`unity_price_violation`, for callers that
    need the decision without the explanation.

    Args:
        price: The candidate ``instrument_prices.price`` value.
        price_currency: The candidate row's ``currency``.
        investment_currency: The cash position's ``investments.currency``.

    Returns:
        ``True`` iff the price is one at the stored scale and the currencies
        match.
    """
    return unity_price_violation(price, price_currency, investment_currency) is None


__all__ = [
    "PRICE_SCALE",
    "UNITY_PRICE",
    "is_unity_price",
    "unity_price_violation",
]
