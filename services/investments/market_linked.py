# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The market-linked predicate — which investments a live import may attempt.

Per ADR-0090 §"Market-linked predicate" as extended by ADR-0097 §9: an
investment is **live-series-eligible** iff

1. its ``investment_type`` is one of the two realistically market-linked
   types (``listed_equity`` / ``listed_bonds``) — the other six of the
   eight canonical types are private-markets instruments or a ``cash``
   balance, none of which carry a market identifier; **and**
2. it carries a **primary** identifier whose scheme is market-usable
   (``isin`` / ``ticker`` / ``figi``). ``cusip`` and ``internal`` are
   valid identifier schemes (ADR-0090) but are **not** in the market-usable
   set for provider addressing, so a primary ``cusip``/``internal`` alone
   does not make an investment eligible; **and**
3. its ``valuation_mode`` is ``'unitised'`` (ADR-0097 §9). A listed
   instrument without unit information stays ``'reported'`` and is
   live-series-ineligible: a live per-share price / dividend has no
   correct landing spot in its NAV-driven book (findings F1/F6). Clause
   (3) lands in strand S3 together with the ADR-0098 write-path
   re-routing; it is additive and orthogonal to clauses (1)/(2).

**Cash is denied on clause (1), permanently** (ADR-0103 §1). Since that
ADR a cash position *is* ``valuation_mode='unitised'``, so it clears
clause (3) — and clause (1) is what still refuses it, on type alone, even
were an operator to hang a primary ISIN on it. A currency has no market
price to fetch; a cash position's prices are the stored ``1.0000`` unity
rows, which come only from the import path (and the ADR-0103 §9
migration), never from a provider. This is the first divergence between
this set and
:data:`services.investments.valuation_mode.UNITISABLE_TYPES` — cash is
unitisable but never live-addressable — and it is why the two were kept
as separate constants while their membership still coincided.

The predicate lives in the investment/service layer (not as a stored
column, and deliberately **not** under ``services/market_data/`` which
stays DB-free) so it stays derivable and cannot drift. The mode arrives
as a plain parameter — the predicate takes no DB dependency to read it.
The live-import refresh core (:mod:`services.investments.live_refresh`,
ADR-0093) uses it to skip illiquid and non-unitised positions cleanly
rather than failing on them; the service-level re-routing refusal
(ADR-0098 §4) is defence in depth behind this gate.

There is at most one primary identifier per investment (the partial
unique index ``uq_investment_identifiers_primary_per_investment``), so
:func:`primary_market_identifier` returns a single row or ``None``.
"""

from __future__ import annotations

from collections.abc import Iterable

from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierDTO,
)

#: The two ``investment_type`` values a live import may attempt (ADR-0090).
#: The other six canonical types are private-markets instruments or a
#: ``cash`` balance — none carry a market identifier. ``cash`` stays out
#: **permanently** (ADR-0103 §1): unlike the other five it is now unitised,
#: but a currency has no fetchable market price, so its exclusion rests on
#: type rather than on mode or identifier. Widening this set to admit cash
#: would enable live ingest for it; ``test_market_linked.py`` pins the
#: membership so that cannot happen silently.
MARKET_LINKED_TYPES: frozenset[str] = frozenset({"listed_equity", "listed_bonds"})

#: Identifier schemes usable to address an external market-data provider
#: (ADR-0090 §"Market-linked predicate"). A subset of the closed identifier
#: scheme set — ``cusip`` and ``internal`` are excluded here.
MARKET_USABLE_SCHEMES: frozenset[str] = frozenset({"isin", "ticker", "figi"})


def primary_market_identifier(
    identifiers: Iterable[InvestmentIdentifierDTO],
) -> InvestmentIdentifierDTO | None:
    """Return the investment's primary market-usable identifier, if any.

    Args:
        identifiers: The investment's identifier rows (any order).

    Returns:
        The single identifier that is both ``is_primary`` and carries a
        :data:`MARKET_USABLE_SCHEMES` scheme, or ``None`` if the investment
        has no such primary identifier (a private-markets instrument, or a
        listed one whose primary identifier is ``cusip``/``internal``).
    """
    for identifier in identifiers:
        if identifier.is_primary and identifier.scheme in MARKET_USABLE_SCHEMES:
            return identifier
    return None


def is_market_linked(
    investment_type: str,
    identifiers: Iterable[InvestmentIdentifierDTO],
    valuation_mode: str,
) -> bool:
    """Return whether an investment is live-series-eligible (ADR-0090/0097 §9).

    All three conditions must hold: the type is market-linked, a primary
    market-usable identifier exists, **and** the valuation mode is
    ``'unitised'``.

    Args:
        investment_type: The investment's ``investment_type`` value.
        identifiers: The investment's identifier rows.
        valuation_mode: The investment's ``valuation_mode`` — ``'reported'``
            or ``'unitised'``. Only ``'unitised'`` is eligible (ADR-0097 §9);
            the mode is passed in so the predicate stays DB-free.

    Returns:
        ``True`` iff ``investment_type`` ∈ :data:`MARKET_LINKED_TYPES`,
        ``valuation_mode == 'unitised'``, and
        :func:`primary_market_identifier` finds a primary market-usable
        identifier; ``False`` otherwise.
    """
    if investment_type not in MARKET_LINKED_TYPES:
        return False
    if valuation_mode != "unitised":
        return False
    # `identifiers` may be a one-shot iterable; only consult it once and
    # only when the type and mode gates already passed.
    return primary_market_identifier(identifiers) is not None


__all__ = [
    "MARKET_LINKED_TYPES",
    "MARKET_USABLE_SCHEMES",
    "is_market_linked",
    "primary_market_identifier",
]
