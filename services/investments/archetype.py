# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Presentation-archetype resolver for the Front-Office charts triplet.

A single, pure routing seam that maps an investment's eight-value
``investment_type`` discriminator to one of four **presentation
archetypes** (ADR-0082 §1, ADR-0079 §1). The Front-Office
universe-charts triplet (and, eventually, the single-investment review
of ADR-0073) dispatches its per-archetype tile-set on the value this
module returns — keeping one routing concept in the codebase rather
than a second ``asset_class``-keyed scheme.

The module is deliberately free of any I/O or project import: it takes
a plain string and returns an :class:`Archetype` member. The
``asset_class`` refines *within* an archetype (a govie, an IG, and a HY
fund all resolve to :attr:`Archetype.FIXED_INCOME`); it never crosses
archetype boundaries (ADR-0082 §"Routing granularity").
"""

from __future__ import annotations

from enum import StrEnum


class Archetype(StrEnum):
    """The four Front-Office presentation archetypes (ADR-0082 §1).

    Members:
        CAPITAL_ACCOUNT: Money-weighted private-markets sleeve
            (private equity, private debt, real estate, infrastructure
            equity). The existing TVPI/DPI/J-curve tile-set.
        TOTAL_RETURN_EQUITY: Time-weighted listed equity. Hero
            benchmark line, underwater, sector|region composition.
        FIXED_INCOME: Time-weighted listed bonds. Hero benchmark line,
            YTM/OAS & duration, rating|maturity composition.
        NAV_ONLY: The minimal fallback for ``other`` and ``cash`` (and
            any unknown type): a single NAV time-series tile so the
            holding stays visible in the universe scan.
    """

    CAPITAL_ACCOUNT = "capital_account"
    TOTAL_RETURN_EQUITY = "total_return_equity"
    FIXED_INCOME = "fixed_income"
    NAV_ONLY = "nav_only"


# The ADR-0082 §1 map from the eight canonical ``investment_type``
# values to a presentation archetype. ``other`` and every unknown value
# fall through to NAV-only via ``dict.get(...)`` below.
_ARCHETYPE_BY_INVESTMENT_TYPE: dict[str, Archetype] = {
    "private_equity": Archetype.CAPITAL_ACCOUNT,
    "private_debt": Archetype.CAPITAL_ACCOUNT,
    "real_estate": Archetype.CAPITAL_ACCOUNT,
    "infra_equity": Archetype.CAPITAL_ACCOUNT,
    "listed_equity": Archetype.TOTAL_RETURN_EQUITY,
    "listed_bonds": Archetype.FIXED_INCOME,
    "other": Archetype.NAV_ONLY,
    # ADR-0100 §1: a cash position is a NAV-only citizen. The fallback
    # below would already catch it, but an explicit entry makes the
    # deliberate choice auditable rather than incidental — a cash row
    # shows its single NAV (balance) tile in the universe scan.
    "cash": Archetype.NAV_ONLY,
}


def resolve_archetype(investment_type: str) -> Archetype:
    """Resolve an ``investment_type`` to its presentation archetype.

    Mapping (ADR-0082 §1):

    - ``private_equity``, ``private_debt``, ``real_estate``,
      ``infra_equity`` → :attr:`Archetype.CAPITAL_ACCOUNT`
    - ``listed_equity`` → :attr:`Archetype.TOTAL_RETURN_EQUITY`
    - ``listed_bonds`` → :attr:`Archetype.FIXED_INCOME`
    - ``other``, ``cash`` **and any unknown value** →
      :attr:`Archetype.NAV_ONLY`

    The ADR-0082 §1 promotion of an "equity-like ``other``" to
    :attr:`Archetype.TOTAL_RETURN_EQUITY` is **deliberately deferred**:
    the data model carries no signal that distinguishes an equity-like
    ``other`` from any other ``other`` instrument, so promoting it would
    require a heuristic with nothing to key on (YAGNI). Until such a
    signal exists every ``other`` routes to NAV-only, where the holding
    stays visible in the universe scan.

    Args:
        investment_type: One of the eight canonical ``investments``
            discriminator values, or any other string.

    Returns:
        The matching :class:`Archetype`; :attr:`Archetype.NAV_ONLY` for
        ``other``, ``cash``, and for any value outside the canonical
        eight.
    """
    return _ARCHETYPE_BY_INVESTMENT_TYPE.get(investment_type, Archetype.NAV_ONLY)


__all__ = ["Archetype", "resolve_archetype"]
