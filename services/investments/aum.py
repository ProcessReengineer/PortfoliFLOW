# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AUM — the one definition: ``aum(t) = Σ nav_functional(t)``.

ADR-0103 §2 retires the residual. AUM ceases to be an independently
persisted series (``portfolio_aum``, ADR-0055) minus an invested book, and
becomes a *derived* quantity with exactly one formulation:

.. code-block:: text

    aum(t) = Σ nav_functional(t)      (all investments, incl. cash rows)

There is no unmodelled float: **what is not on a statement does not exist
for the platform.** The functional-currency cash that used to hide in the
residual is now an explicit cash position like any other investment
(ADR-0103 §1), so the sum is complete by construction rather than by
subtraction.

This module is the **definition** — the single seam, following the
:mod:`services.investments.flow_type_invariants` and
:mod:`services.investments.unity_price` precedent. Every surface that
states an AUM figure resolves it here:

* the Front-Office Overview hero (``AUM = Invested + Cash``),
* the ``AUM`` sheet's reconciliation control
  (:meth:`~services.investments.investment_service.InvestmentService.reconcile_aum_sheet`,
  ADR-0103 §3) — the control and the definition it checks are the same code,
* the limit-coverage denominator, which sums the *already converted* NAV
  streams it is handed (the pure engine in
  :mod:`services.analytics.limit_coverage` stays DB-free, converter-free and
  investment-type-blind per ADR-0013/0045, so it re-states the same rule over
  its own inputs rather than importing this module).

Two rules make the sum well-defined, and both are the book's existing
conventions rather than new ones:

1. **Carry-forward per investment (ADR-0060).** An investment contributes
   the NAV *in force* on the date — the latest observation at or before it.
   A NAV is a level that holds until the next statement restates it. An
   investment with no observation at or before the date was not yet in the
   book and contributes nothing — not zero, *nothing*.
2. **Conversion at the ADR-0099 §4 seam.** Each NAV converts from its
   position currency into the functional currency at its own date's rate,
   never a silent 1:1 fallback. On a single-currency book the converter is
   the identity and reads no FX row at all (ADR-0102 zero-read).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from core.repositories.investment_nav_repository import (
    InvestmentNavDTO,
    InvestmentNavRepository,
)
from core.repositories.investment_repository import (
    InvestmentDTO,
    InvestmentRepository,
)
from services.fx.functional_currency import PortfolioFxConverter

#: ``investment_type`` of an explicit cash position (ADR-0100 §2, ADR-0103 §1).
CASH_TYPE: str = "cash"

#: One ``(investment, ascending dates, values)`` triple per investment. The
#: dates are pre-sorted so the carry-forward lookup is a bisect rather than a
#: scan: an AUM sheet is daily and long, and a per-``(investment, date)`` scan
#: would dominate the import.
NavSeries = tuple[InvestmentDTO, list[date], list[Decimal]]


@dataclass(frozen=True)
class AumBreakdown:
    """AUM at one date, split into its cash and non-cash halves.

    The split is the only thing subtraction is still good for. ``total`` is
    the definition; ``cash`` and ``non_cash`` partition it so a surface can
    state "AUM = Invested + Cash" without a second pass over the book, and
    without re-deriving cash as a residual.

    Attributes:
        total: ``Σ nav_functional`` over every investment — AUM.
        cash: ``Σ nav_functional`` over the explicit cash positions
            (``investment_type == 'cash'``), functional-currency cash
            included. This is the figure the retired residual was reaching
            for; it is now read off the book rather than inferred from a gap.
        non_cash: ``Σ nav_functional`` over everything else — the invested
            book. ``non_cash + cash == total`` exactly.
    """

    total: Decimal
    cash: Decimal
    non_cash: Decimal


def compute_aum(
    series: list[NavSeries],
    as_of_date: date,
    fx: PortfolioFxConverter,
) -> AumBreakdown:
    """Return AUM at ``as_of_date`` — ``Σ nav_functional``, cash included.

    The single formulation of ADR-0103 §2. See the module docstring for the
    two rules (carry-forward per investment, conversion at the ADR-0099 §4
    seam) and for why there is no residual left to compute.

    Conversion is per investment rather than per currency-bucket, so the sum
    is the one the book itself publishes at the conversion seam
    (:meth:`services.limits.LimitsCoverageService._convert_navs`), digit for
    digit — the property the one-definition test pins.

    Args:
        series: One triple per investment under evaluation. The caller
            decides the universe (``list_active()`` at every current call
            site) and the ``nav_kind``; this function sums what it is handed.
        as_of_date: The date to value the book at.
        fx: The functional-currency converter. On a single-currency book it
            is the identity and reads no FX row (ADR-0102 zero-read).

    Returns:
        The :class:`AumBreakdown` — zero in all three components for a book
        with no observation at or before ``as_of_date``.

    Raises:
        MissingFxRateError: If a position's currency has no rate at or
            before ``as_of_date``. Never a silent 1:1 fallback (ADR-0099).
    """
    cash = Decimal(0)
    non_cash = Decimal(0)
    for investment, dates, values in series:
        position = bisect_right(dates, as_of_date) - 1
        if position < 0:
            continue
        value = fx.convert_amount(values[position], investment.currency, as_of_date)
        if investment.investment_type == CASH_TYPE:
            cash += value
        else:
            non_cash += value
    return AumBreakdown(total=cash + non_cash, cash=cash, non_cash=non_cash)


def build_nav_series(
    investments: list[InvestmentDTO],
    navs_by_investment: dict[UUID, list[InvestmentNavDTO]],
) -> list[NavSeries]:
    """Shape repository output into the :data:`NavSeries` triples.

    Pure — the assembly half of the definition, kept separate from the
    summation half so a caller that already holds the NAV streams (the
    import path) does not re-read them.

    Args:
        investments: The universe, in the order the caller wants it summed.
        navs_by_investment: NAV rows keyed by ``investment.id``, ascending by
            ``as_of_date`` (every repository read returns them so). A missing
            key means "no NAVs for this investment" — a legal, contributing-
            nothing member of the universe.

    Returns:
        One triple per investment, in the input order.
    """
    series: list[NavSeries] = []
    for investment in investments:
        rows = navs_by_investment.get(investment.id, [])
        series.append(
            (
                investment,
                [row.as_of_date for row in rows],
                [row.nav_value for row in rows],
            )
        )
    return series


async def load_nav_series(
    *,
    investments: InvestmentRepository,
    navs: InvestmentNavRepository,
    nav_kind: str = "actual",
) -> list[NavSeries]:
    """Load the active universe and its NAV streams as :data:`NavSeries`.

    The thin per-consumer assembly over :func:`build_nav_series`: two
    tenant-scoped repository reads, no calculation. The caller supplies the
    converter separately (via
    :func:`~services.fx.functional_currency.build_portfolio_fx_converter`)
    because the currencies it needs come from the universe this returns.

    Args:
        investments: Tenant-scoped investment repository.
        navs: Tenant-scoped NAV repository.
        nav_kind: ``'actual'`` (the realised book — every current caller) or
            ``'plan'``.

    Returns:
        One triple per active investment. Empty when the tenant has no
        active investment — the empty-universe case, which every caller
        surfaces as "nothing to state" rather than as an AUM of zero.
    """
    active = await investments.list_active()
    if not active:
        return []
    rows = await navs.list_by_investments_and_kind([inv.id for inv in active], nav_kind)
    return build_nav_series(active, rows)


__all__ = [
    "CASH_TYPE",
    "AumBreakdown",
    "NavSeries",
    "build_nav_series",
    "compute_aum",
    "load_nav_series",
]
