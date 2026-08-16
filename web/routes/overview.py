# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web Overview surface — Front-Office portfolio headline KPI strip.

The Overview module is embedded as the **first** section of
``/front-office#overview`` (ADR-0067). It renders a hero AUM figure plus a
four-card metric grid (IRR / TVPI / DPI / active investment count). A single
backend round-trip suffices — unlike Charts there is no per-tile deferred
loading, because the payload is a handful of scalars.

Endpoint:

* ``GET /api/overview/section`` — Returns the section body (hero + metric
  grid) or the empty-state copy when the universe is empty. Lazy-loaded on
  first visibility via ``hx-trigger="revealed"`` in the section lazy-shell.
"""

from __future__ import annotations

import logging
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.exceptions import MissingFxRateError
from core.repositories._session import tenant_context
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_nav_repository import (
    InvestmentNavRepository,
)
from core.repositories.investment_region_weights_repository import (
    InvestmentRegionWeightsRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.investment_sector_weights_repository import (
    InvestmentSectorWeightsRepository,
)
from core.repositories.region_repository import RegionRepository
from core.repositories.sector_repository import SectorRepository
from core.repositories.tenant_repository import TenantRepository
from services.analytics.portfolio_aggregation import (
    compute_concentration,
    group_fund_composition,
)
from services.auth.session import SessionDTO, SessionRepository
from services.chart_specs import (
    build_currency_exposure_spec,
    build_fund_composition_spec,
    build_invested_capital_nav_spec,
    build_yearly_cashflows_spec,
)
from services.front_office_overview import (
    FrontOfficeOverviewService,
    OverviewResult,
)
from services.money_format import format_money_compact
from services.portfolio_review.portfolio_review_service import (
    PortfolioReviewService,
)
from web.auth import require_session

logger = logging.getLogger(__name__)
router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


def _build_service(db_session: AsyncSession) -> FrontOfficeOverviewService:
    """Construct :class:`FrontOfficeOverviewService` with standard wiring.

    Combines the nine-repository :class:`PortfolioReviewService` (the source
    of truth for IRR / TVPI / DPI / count) with the four repositories the
    AUM definition needs (ADR-0103 §2: investments and their NAVs, plus the
    ADR-0099 §4 conversion seam). All are tenant-scoped via the active
    session.
    """
    review_service = PortfolioReviewService(
        investments=InvestmentRepository(db_session),
        navs=InvestmentNavRepository(db_session),
        cashflows=InvestmentCashflowRepository(db_session),
        region_weights=InvestmentRegionWeightsRepository(db_session),
        sector_weights=InvestmentSectorWeightsRepository(db_session),
        regions=RegionRepository(db_session),
        sectors=SectorRepository(db_session),
        tenants=TenantRepository(db_session),
        fx_rates=FxRateRepository(db_session),
    )
    return FrontOfficeOverviewService(
        review_service,
        investment_repository=InvestmentRepository(db_session),
        nav_repository=InvestmentNavRepository(db_session),
        tenant_repository=TenantRepository(db_session),
        fx_rate_repository=FxRateRepository(db_session),
    )


# The Overview's money strings (ADR-0067: formatting is the route's job).
# ADR-0101 §3 generalised the EUR-only ``_format_eur_compact`` to take the
# tenant's functional currency; the thresholds and rounding are unchanged, so
# a EUR tenant's strings are byte-identical to the pre-block output (§4). The
# implementation lives in ``services/money_format.py`` because the money-
# bearing chart specs must apply the very same prefix rule.
_format_money_compact = format_money_compact


@router.get(
    "/api/overview/section",
    response_class=HTMLResponse,
)
async def get_overview_section(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the Front-Office Overview section body.

    Lazy-loaded on first visibility via ``hx-trigger="revealed"`` in the
    section lazy-shell. There is no as-of-date control in v1 — the latest
    activity date is the default (ADR-0067).

    The ADR-0099 §4 conversion error (:class:`MissingFxRateError`, raised
    when a foreign-currency position lacks an FX rate it needs) is caught
    and rendered through a dedicated error-state partial, mirroring the
    limits and portfolio-review sections.

    Args:
        request: The FastAPI request (provides app state).
        session: The active :class:`SessionDTO` resolved by
            :func:`require_session`.

    Returns:
        An :class:`HTMLResponse` carrying the rendered section body, the
        empty-state copy when the universe is empty, or the FX-error state
        when a required rate is missing. Always HTTP 200 — the body is an
        HTMX section swap.
    """
    engine = _engine(request)
    try:
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
            await SessionRepository(db_session).touch_throttled(session.id)
            service = _build_service(db_session)
            result = await service.get_overview()

        context = _build_context(result, tenant_id=session.tenant_id)
    except MissingFxRateError as exc:
        # ADR-0099 §4: a foreign-currency position lacks a rate it needs.
        # The Overview is the landing surface of the whole application — an
        # unhandled 500 here reads as "the app is broken" when the real
        # story is "one FX rate is missing". Surface it, the way the limits
        # and portfolio-review sections already do. HTTP 200 deliberately:
        # this body is an HTMX section swap, and an error status would leave
        # the lazy shell in place instead of showing the message.
        logger.warning(
            "overview section: FX rate missing (tenant=%s): %s",
            session.tenant_id,
            exc,
        )
        return cast(
            HTMLResponse,
            _templates(request).TemplateResponse(
                request,
                "_partials/overview_error.html",
                {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            ),
        )

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/overview_section.html",
            context,
        ),
    )


def _build_context(result: OverviewResult | None, *, tenant_id: UUID) -> dict[str, Any]:
    """Assemble the template context for one Overview bundle.

    Split out of the route so the ADR-0099 §4 conversion boundary has a
    single ``try`` to guard: every FX-dependent read happens either inside
    :meth:`FrontOfficeOverviewService.get_overview` or in the money
    formatting below, and both now sit under one ``except``.

    Args:
        result: The :class:`OverviewResult`, or ``None`` when the tenant has
            no active investments.
        tenant_id: The active tenant, for the empty-universe log line.

    Returns:
        The Jinja context for ``_partials/overview_section.html``.
    """
    kpis = result.kpis if result is not None else None

    if kpis is None:
        logger.debug(
            "overview section: empty universe (tenant=%s).",
            tenant_id,
        )
        context: dict[str, Any] = {
            "kpis": None,
            "overview_charts": [],
            "cash_positions": [],
        }
    else:
        # The hero is AUM = Invested + Cash, all three resolved from the one
        # definition (ADR-0103 §2), so there is no "no AUM series" fallback
        # left to branch on. The route precomputes every display string; the
        # multiples stay raw and are formatted in the template exactly like
        # the .pr-strip cards.
        hero_value = kpis.aum_eur
        # Every monetary figure on the bundle is in the tenant's functional
        # currency (ADR-0099 §4); the labels now say so (ADR-0101 §3).
        currency = result.functional_currency
        # The three-chart row under the metric grid (ADR-0072). Left and
        # middle reuse the Portfolio Review specs; the right is the new
        # fund-composition Pareto.
        overview_charts = [
            {
                "id": "ov-chart-invested-nav",
                "title": "Invested Capital & NAV",
                "spec": build_invested_capital_nav_spec(
                    result.invested_capital_nav,
                    title="Invested Capital & NAV",
                    currency=currency,
                ),
                "has_data": bool(result.invested_capital_nav.years),
            },
            {
                "id": "ov-chart-cashflows",
                "title": "Cashflows",
                "spec": build_yearly_cashflows_spec(
                    result.cashflows, title="Cashflows", currency=currency
                ),
                "has_data": bool(result.cashflows.years),
            },
            {
                "id": "ov-chart-composition",
                "title": "NAV by fund",
                "spec": build_fund_composition_spec(
                    group_fund_composition(result.fund_composition, top_n=10),
                    concentration=compute_concentration(result.fund_composition),
                    # The card header already shows "NAV by fund"; an empty
                    # Plotly title drops the redundant centred title and lets
                    # the spec trim its top margin so the plot's top edge
                    # aligns with the left/middle tiles (ADR-0072 r1.2).
                    title="",
                    currency=currency,
                ),
                "has_data": bool(result.fund_composition.rows),
            },
        ]
        # The fourth tile exists only where it says something: a portfolio
        # denominated entirely in one currency has no exposure to show, and
        # a one-slice donut would be noise. This is the visible half of the
        # ADR-0101 §4 invisibility guarantee.
        exposure = result.currency_exposure
        if exposure.currency_count > 1:
            overview_charts.append(
                {
                    "id": "ov-chart-currency",
                    "title": "Currency exposure",
                    "spec": build_currency_exposure_spec(
                        exposure,
                        functional_currency=currency,
                        title="",
                    ),
                    "has_data": True,
                }
            )
        context = {
            "kpis": kpis,
            "aum_display": (
                _format_money_compact(hero_value, currency) if hero_value is not None else None
            ),
            "invested_display": (
                _format_money_compact(kpis.invested_eur, currency)
                if kpis.invested_eur is not None
                else None
            ),
            # Cash shows only where the book holds some. A tenant with no cash
            # position has nothing to say here, and a "Cash €0" line would be
            # noise — the ADR-0101 §4 invisibility principle applied to the
            # figure ADR-0103 §2 promoted out of the residual.
            "cash_display": (
                _format_money_compact(kpis.cash_eur, currency) if kpis.cash_eur else None
            ),
            "overview_charts": overview_charts,
            # ADR-0101 §2 — one line per foreign-currency cash balance. The
            # native amount formats in its *own* currency, the equivalent in
            # the functional one; the template renders nothing when empty.
            "cash_positions": [
                {
                    "name": row.name,
                    "currency": row.currency,
                    "native_display": _format_money_compact(row.native_balance, row.currency),
                    "functional_display": _format_money_compact(row.functional_value, currency),
                    "as_of_date": row.as_of_date.isoformat(),
                }
                for row in result.cash_positions
            ],
        }

    return context
