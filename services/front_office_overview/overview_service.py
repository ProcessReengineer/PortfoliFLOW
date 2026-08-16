# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""FrontOfficeOverviewService — composes the portfolio headline KPI strip.

The Front-Office "Overview" section (ADR-0067) surfaces five portfolio-level
headline figures: AUM (the hero), IRR, TVPI, DPI and the active investment
count. None of these are recomputed here.

The four multiples and the investment count are read straight off
:meth:`PortfolioReviewService.get_portfolio_overview`, so the Overview's
performance figures are numerically identical to the Portfolio Review
surface by construction.

The three money figures come from the AUM definition instead
(:mod:`services.investments.aum`, ADR-0103 §2):

.. code-block:: text

    AUM = Invested + Cash

    AUM      = Σ nav_functional  over every investment
    Cash     = Σ nav_functional  over the explicit cash positions
    Invested = Σ nav_functional  over everything else

There is no ``portfolio_aum`` series behind the hero any more, and no
residual: cash is read off the book (it is an investment now, ADR-0103 §1),
not inferred from the gap between an independently persisted AUM row and the
NAV roll-up. The gap *was* the cash; now the cash is the data, so the gap is
zero by construction and stating it would say nothing.

Note the narrowing this implies against the Review surface: ``invested_eur``
here **excludes** cash, while the Review's headline ``nav_eur`` remains the
full universe with cash inside it (ADR-0100 §4). Same book, two questions —
"what is invested" and "what is the book worth" — and since ADR-0103 they are
no longer the same number. ``kpis.aum_eur`` is the figure that equals
``bundle.header_metrics.nav_eur``.

This service is pure orchestration over already-tenant-scoped collaborators
(ADR-0001): it neither reads nor sets ``app.tenant_id`` and performs no
calculation of its own. All formatting and presentation live in the web
route, not here — :class:`OverviewKpis` carries plain numbers only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.tenant_repository import TenantRepository
from services.analytics.portfolio_aggregation import (
    CurrencyExposure,
    FundCompositionBreakdown,
    InvestedCapitalNavSeries,
    PortfolioCashflowSeries,
)
from services.fx.functional_currency import build_portfolio_fx_converter
from services.investments.aum import compute_aum, load_nav_series
from services.portfolio_review.portfolio_review_service import (
    CashPositionRow,
    PortfolioReviewService,
)


@dataclass(frozen=True)
class _AumFigures:
    """The three money figures of the strip — ``total = invested + cash``.

    Internal carrier between :meth:`FrontOfficeOverviewService._resolve_aum`
    and the KPI assembly; the public shape is :class:`OverviewKpis`.
    """

    total: float | None
    invested: float | None
    cash: float | None


@dataclass(frozen=True)
class OverviewKpis:
    """Plain numeric KPIs for the Front-Office Overview strip.

    All monetary figures are in the tenant's functional currency
    (ADR-0099 §4); the currency itself travels on the enclosing
    :class:`OverviewResult`, not here. The legacy ``*_eur`` field names are
    retained — the rename is a separate follow-up (ADR-0099 §Follow-ups).
    Formatting is the route's concern; this dataclass carries numbers only
    (ADR-0067).

    Attributes:
        aum_eur: AUM at the resolved as-of date — ``Σ nav_functional`` over
            every investment, cash rows included (ADR-0103 §2). ``None``
            when no investment carries a NAV at or before the as-of date.
        invested_eur: The invested book — ``Σ nav_functional`` over the
            **non-cash** investments. ``None`` under the same condition as
            ``aum_eur``. Equals ``aum_eur − cash_eur`` exactly.
        cash_eur: Cash — ``Σ nav_functional`` over the explicit cash
            positions (ADR-0103 §1), functional-currency cash included.
            Read off the book, not derived as a residual, so it is never
            negative-by-staleness: the ADR-0055/0067 negative-suppression
            rule retired with the residual it protected against (ADR-0103
            §2). ``0.0`` for a tenant holding no cash position, and
            ``None`` only when the book has no NAV at all.
        irr: Portfolio IRR-since-inception as a decimal, or ``None``.
        tvpi: Aggregate TVPI multiple, or ``None``.
        dpi: Aggregate DPI multiple, or ``None``.
        investment_count: Number of active investments included.
        as_of_date: Resolved as-of date for the figures.
    """

    aum_eur: float | None
    invested_eur: float | None
    cash_eur: float | None
    irr: float | None
    tvpi: float | None
    dpi: float | None
    investment_count: int
    as_of_date: date


@dataclass(frozen=True)
class OverviewResult:
    """Bundle of Overview KPIs plus the chart inputs (ADR-0072, ADR-0101).

    Attributes:
        kpis: The headline KPI strip values.
        invested_capital_nav: Year-end invested-capital / NAV series.
        cashflows: Year-end calls / distributions / NAV / NCG series.
        fund_composition: NAV-weighted composition by fund.
        currency_exposure: NAV share by position currency (ADR-0101 §1).
            The route renders the fourth chart tile only when this carries
            more than one currency, so a single-currency tenant sees the
            unchanged three-tile row. Defaults to an empty exposure.
        cash_positions: Explicit foreign-currency cash balances with their
            native amounts (ADR-0101 §2). The route renders the FX-cash
            card only when non-empty. Defaults to empty.
        functional_currency: The currency every monetary figure in this
            bundle is denominated in (ADR-0099 §4). Drives the money
            labels across the surface (ADR-0101 §3). Defaults to ``"EUR"``.
    """

    kpis: OverviewKpis
    invested_capital_nav: InvestedCapitalNavSeries
    cashflows: PortfolioCashflowSeries
    fund_composition: FundCompositionBreakdown
    currency_exposure: CurrencyExposure = field(default_factory=lambda: CurrencyExposure(rows=[]))
    cash_positions: list[CashPositionRow] = field(default_factory=list)
    functional_currency: str = "EUR"


class FrontOfficeOverviewService:
    """Compose the portfolio headline KPIs for the Front-Office Overview.

    The collaborators must already be tenant-scoped by the caller (the
    route resolves the tenant context and constructs them). This service
    adds no tenant handling and no calculation of its own — it composes the
    existing portfolio-review aggregation with the shared AUM definition
    (:func:`services.investments.aum.compute_aum`) and returns plain
    numbers.
    """

    def __init__(
        self,
        review_service: PortfolioReviewService,
        investment_repository: InvestmentRepository,
        nav_repository: InvestmentNavRepository,
        tenant_repository: TenantRepository,
        fx_rate_repository: FxRateRepository,
    ) -> None:
        """Store the (already tenant-scoped) collaborators.

        Args:
            review_service: A tenant-scoped :class:`PortfolioReviewService`
                — the single source of truth for IRR / TVPI / DPI / count.
            investment_repository: Supplies the active universe the AUM
                definition sums over.
            nav_repository: Supplies that universe's NAV streams.
            tenant_repository: Supplies the functional currency.
            fx_rate_repository: Supplies the rate frame — consulted only
                when a position currency differs from the functional one,
                so a single-currency tenant reads no FX row (ADR-0102).
        """
        self._review = review_service
        self._investments = investment_repository
        self._navs = nav_repository
        self._tenants = tenant_repository
        self._fx_rates = fx_rate_repository

    async def get_overview(self, as_of_date: date | None = None) -> OverviewResult | None:
        """Build the :class:`OverviewResult` for the active tenant.

        Fetches the :class:`PortfolioOverviewBundle` once and composes the
        headline KPI strip with the three Overview chart inputs (ADR-0072).
        The AUM breakdown is resolved at the bundle's own as-of date, so the
        money figures and the multiples share one as-of.

        Args:
            as_of_date: As-of date for the figures. ``None`` resolves to
                the latest activity date observed across the universe — the
                same default :meth:`get_portfolio_overview` applies.

        Returns:
            An :class:`OverviewResult`, or ``None`` when the investment
            universe is empty (the route then renders the empty state).
        """
        bundle = await self._review.get_portfolio_overview(as_of_date=as_of_date)
        if bundle is None:
            return None

        aum = await self._resolve_aum(bundle.as_of_date)

        metrics = bundle.header_metrics
        kpis = OverviewKpis(
            aum_eur=aum.total,
            invested_eur=aum.invested,
            cash_eur=aum.cash,
            irr=metrics.irr,
            tvpi=metrics.tvpi,
            dpi=metrics.dpi,
            investment_count=bundle.investment_count,
            as_of_date=bundle.as_of_date,
        )
        return OverviewResult(
            kpis=kpis,
            invested_capital_nav=bundle.invested_capital_nav,
            cashflows=bundle.cashflows,
            fund_composition=bundle.fund_composition,
            currency_exposure=bundle.currency_exposure,
            cash_positions=bundle.cash_positions,
            functional_currency=metrics.functional_currency,
        )

    async def _resolve_aum(self, as_of_date: date) -> _AumFigures:
        """Resolve ``AUM = Invested + Cash`` at ``as_of_date``.

        The thin per-consumer assembly over the shared definition
        (:func:`services.investments.aum.compute_aum`): load the active
        universe and its actual-NAV streams, build the converter, sum. The
        hero, the coverage denominator and the ``AUM``-sheet reconciliation
        control all resolve through that one function, so the three surfaces
        cannot drift apart — the property the one-definition test pins.

        Returns:
            The three figures as plain floats, or all-``None`` when the book
            carries no value at ``as_of_date`` — matching the Review's
            convention, where a headline NAV of zero reads as "nothing to
            state" rather than as a portfolio worth nothing.
        """
        series = await load_nav_series(investments=self._investments, navs=self._navs)
        if not series:
            return _AumFigures(None, None, None)

        fx = await build_portfolio_fx_converter(
            tenants=self._tenants,
            fx_rates=self._fx_rates,
            position_currencies=[inv.currency for inv, _, _ in series],
        )
        breakdown = compute_aum(series, as_of_date, fx)
        if breakdown.total <= 0:
            return _AumFigures(None, None, None)

        return _AumFigures(
            total=float(breakdown.total),
            invested=float(breakdown.non_cash),
            cash=float(breakdown.cash),
        )

    async def get_overview_kpis(self, as_of_date: date | None = None) -> OverviewKpis | None:
        """Build the :class:`OverviewKpis` for the active tenant.

        Thin wrapper over :meth:`get_overview` retained because the Shirley
        overview tool depends on the KPI-only signature.

        Args:
            as_of_date: As-of date for the figures. ``None`` resolves to
                the latest activity date observed across the universe — the
                same default :meth:`get_portfolio_overview` applies. The
                AUM breakdown is resolved at that date so the money figures
                and the multiples share one as-of.

        Returns:
            An :class:`OverviewKpis`, or ``None`` when the investment
            universe is empty (the route then renders the empty state).
        """
        result = await self.get_overview(as_of_date=as_of_date)
        return result.kpis if result is not None else None


__all__ = [
    "CashPositionRow",
    "FrontOfficeOverviewService",
    "OverviewKpis",
    "OverviewResult",
]
