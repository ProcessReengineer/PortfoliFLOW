# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web Portfolio Review surface — one continuous per-investment stack.

The Portfolio Review section is embedded in
``/investor-communication#portfolio-review``. Per ADR-0073 the section
body is the full report: the portfolio-aggregate "six-tile review" — a
four-card header KPI strip plus a 3×2 grid of Plotly tiles — led,
followed by one **per-investment** six-tile grid per active investment,
each loaded via a nested lazy-load (``hx-trigger="revealed"``). A
persistent as-of-date form re-submits the section body via HTMX without
a full page reload; every per-investment placeholder carries the
overview's resolved as-of date so the whole report shares one date.

Endpoints:

* ``GET /api/portfolio-review/section`` — Returns the section body
  (KPI strip, meta line, portfolio six-tile grid, as-of-date form, and
  one per-investment placeholder ``<article>`` per active investment),
  or the empty-state copy when the universe is empty. Accepts an
  optional ``as_of_date`` query parameter (ISO ``YYYY-MM-DD``); invalid
  values are silently ignored (the service falls back to the latest
  activity date).
* ``GET /api/portfolio-review/investment/{investment_id}/section`` —
  Returns the six-tile fragment for one investment (tile 4 is the Total
  Return index). Renders a neutral "review unavailable" fragment with
  HTTP 200 for unknown / cross-tenant ids — it never raises 404 nor
  leaks whether the id exists.
"""

from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.exceptions import MissingFxRateError
from core.repositories._session import tenant_context
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
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
from services.auth.session import SessionDTO, SessionRepository
from services.chart_specs import (
    build_invested_capital_nav_spec,
    build_multiples_stacked_spec,
    build_region_treemap_spec,
    build_sector_treemap_spec,
    build_total_return_index_spec,
    build_vintage_bar_spec,
    build_yearly_cashflows_spec,
)
from services.portfolio_review.portfolio_review_service import (
    PortfolioOverviewBundle,
    PortfolioReviewService,
    SingleInvestmentReviewBundle,
)
from web.auth import require_session

logger = logging.getLogger(__name__)
router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


def _build_service(db_session: AsyncSession) -> PortfolioReviewService:
    """Construct :class:`PortfolioReviewService` with the standard wiring."""
    return PortfolioReviewService(
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


def _parse_as_of_date(raw: str | None) -> _date | None:
    """Parse an ISO ``YYYY-MM-DD`` query value; treat junk as ``None``."""
    if not raw:
        return None
    try:
        return _date.fromisoformat(raw)
    except ValueError:
        return None


def _build_tile_specs(
    bundle: PortfolioOverviewBundle,
) -> list[dict[str, Any]]:
    """Build the six tile specs from the bundle.

    A tile is marked ``has_data=False`` when its underlying series is
    empty — the template renders the empty-state copy instead of an
    empty chart.
    """
    icn = bundle.invested_capital_nav
    cf = bundle.cashflows
    mult = bundle.multiples
    # Every monetary series on the bundle is converted into the tenant's
    # functional currency at the ADR-0099 §4 seam; the money axes say so
    # (ADR-0101 §3).
    currency = bundle.header_metrics.functional_currency
    return [
        {
            "id": "pr-tile-1",
            "title": "Invested Capital & NAV",
            "spec": build_invested_capital_nav_spec(icn, currency=currency),
            "has_data": bool(icn.years),
        },
        {
            "id": "pr-tile-2",
            "title": "Cashflows",
            "spec": build_yearly_cashflows_spec(cf, currency=currency),
            "has_data": bool(cf.years),
        },
        {
            "id": "pr-tile-3",
            "title": "Multiples (TVPI / DPI / IRR)",
            "spec": build_multiples_stacked_spec(mult),
            "has_data": bool(mult.years),
        },
        {
            "id": "pr-tile-region",
            "title": "Region split",
            "spec": build_region_treemap_spec(bundle.region_breakdown),
            "has_data": bool(bundle.region_breakdown.rows),
        },
        {
            "id": "pr-tile-5",
            "title": "Vintages",
            "spec": build_vintage_bar_spec(bundle.vintage_distribution),
            "has_data": bool(bundle.vintage_distribution.vintages),
        },
        {
            "id": "pr-tile-6",
            "title": "Sector split",
            "spec": build_sector_treemap_spec(bundle.sector_breakdown),
            "has_data": bool(bundle.sector_breakdown.rows),
        },
    ]


def _build_single_investment_tile_specs(
    bundle: SingleInvestmentReviewBundle,
) -> list[dict[str, Any]]:
    """Build the six tile specs for one single-investment review.

    Mirrors :func:`_build_tile_specs` but for the single-investment tile
    set, which differs from the portfolio set in tile 4: it carries the
    **Total Return index** (rebased to 100 at inception) instead of the
    portfolio Vintages bar.

    The ``id`` values are tile-local base ids; the template suffixes each
    with the investment id so multiple stacked fragments never collide on
    DOM ids.
    """
    icn = bundle.invested_capital_nav
    cf = bundle.cashflows
    mult = bundle.multiples
    # No conversion happens on this surface (ADR-0099 §4 converts at the
    # *portfolio* seam), so these series are in the investment's own position
    # currency — which is what the money axes must name (ADR-0101 §3).
    currency = bundle.investment.currency
    return [
        {
            "id": "pr-inv-tile-1",
            "title": "Invested Capital & NAV",
            "spec": build_invested_capital_nav_spec(icn, currency=currency),
            "has_data": bool(icn.years),
        },
        {
            "id": "pr-inv-tile-2",
            "title": "Cashflows",
            "spec": build_yearly_cashflows_spec(cf, currency=currency),
            "has_data": bool(cf.years),
        },
        {
            "id": "pr-inv-tile-3",
            "title": "Multiples (TVPI / DPI / IRR)",
            "spec": build_multiples_stacked_spec(mult),
            "has_data": bool(mult.years),
        },
        {
            "id": "pr-inv-tile-4",
            "title": "Total Return (since inception)",
            "spec": build_total_return_index_spec(bundle.total_return_index),
            "has_data": not bundle.total_return_index.dropna().empty,
        },
        {
            "id": "pr-inv-tile-region",
            "title": "Region split",
            "spec": build_region_treemap_spec(bundle.region_breakdown),
            "has_data": bool(bundle.region_breakdown.rows),
        },
        {
            "id": "pr-inv-tile-6",
            "title": "Sector split",
            "spec": build_sector_treemap_spec(bundle.sector_breakdown),
            "has_data": bool(bundle.sector_breakdown.rows),
        },
    ]


@router.get(
    "/api/portfolio-review/section",
    response_class=HTMLResponse,
)
async def get_portfolio_review_section(
    request: Request,
    as_of_date: str | None = None,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the Portfolio Review section body.

    Lazy-loaded on first visibility via ``hx-trigger="revealed"`` in
    the section lazy-shell. The persistent as-of-date form posts back
    to this same endpoint and HTMX swaps only the section body.

    Args:
        request: The FastAPI request.
        as_of_date: Optional ISO date (``YYYY-MM-DD``) carried in the
            query string. Invalid values are silently ignored — the
            service falls back to the latest observed activity date.
        session: The active :class:`SessionDTO`.

    Returns:
        An :class:`HTMLResponse` carrying the rendered section body.
    """
    parsed = _parse_as_of_date(as_of_date)
    # Echo the operator-supplied value back into the form unchanged
    # when it was syntactically valid; otherwise leave the form empty
    # (mirrors the "ignore junk" semantic above).
    form_value = parsed.isoformat() if parsed is not None else ""

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        await SessionRepository(db_session).touch_throttled(session.id)
        service = _build_service(db_session)
        try:
            bundle = await service.get_portfolio_overview(as_of_date=parsed)
        except MissingFxRateError as exc:
            # ADR-0099 §4: a foreign-currency position lacks a rate it
            # needs. Surface an operator-actionable message, never a bare
            # 500 (mirrors the limits route's engine-error idiom).
            logger.debug(
                "portfolio-review section: FX rate missing (tenant=%s): %s",
                session.tenant_id,
                exc,
            )
            return cast(
                HTMLResponse,
                _templates(request).TemplateResponse(
                    request,
                    "_partials/portfolio_review_error.html",
                    {
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "as_of_date_input": form_value,
                    },
                ),
            )
        active_investments = await InvestmentRepository(db_session).list_active()

    # One lazy-load placeholder per active investment, alphabetical for
    # stable presentation (mirrors the charts_section precedent and
    # ADR-0073 §4). Empty universe → no placeholders.
    investment_articles = [
        {"id": str(inv.id), "name": inv.name}
        for inv in sorted(active_investments, key=lambda inv: inv.name)
    ]

    if bundle is None:
        logger.debug(
            "portfolio-review section: empty universe (tenant=%s).",
            session.tenant_id,
        )
        context: dict[str, Any] = {
            "payload": None,
            "tile_specs": [],
            "as_of_date_input": form_value,
            "as_of_date_display": "",
            "investment_count": 0,
            "header_metrics": None,
            "investment_articles": [],
        }
    else:
        context = {
            "payload": bundle,
            "tile_specs": _build_tile_specs(bundle),
            "as_of_date_input": form_value,
            "as_of_date_display": bundle.as_of_date.isoformat(),
            "investment_count": bundle.investment_count,
            "header_metrics": bundle.header_metrics,
            "investment_articles": investment_articles,
        }

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/portfolio_review_section.html",
            context,
        ),
    )


@router.get(
    "/api/portfolio-review/investment/{investment_id}/section",
    response_class=HTMLResponse,
)
async def get_portfolio_review_investment_section(
    request: Request,
    investment_id: UUID,
    as_of_date: str | None = None,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the per-investment six-tile review fragment.

    Fetched by a per-investment lazy-loader inside the Portfolio Review
    section body when its placeholder ``<article>`` scrolls into view
    (``hx-trigger="revealed"``). The shared as-of date is carried in the
    query string so the whole report renders at one date (ADR-0073 §3).

    Args:
        request: The FastAPI request.
        investment_id: Path-bound UUID identifying the investment.
        as_of_date: Optional ISO date (``YYYY-MM-DD``); invalid values
            are silently ignored (the service falls back to the latest
            observed activity date).
        session: The active :class:`SessionDTO`.

    Returns:
        An :class:`HTMLResponse` carrying the six-tile fragment, or a
        neutral "review unavailable" fragment (still HTTP 200) when the
        id is unknown or hidden by RLS. The endpoint never raises 404
        and never leaks whether the id exists.
    """
    parsed = _parse_as_of_date(as_of_date)

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        await SessionRepository(db_session).touch_throttled(session.id)
        service = _build_service(db_session)
        bundle = await service.get_single_investment_review(investment_id, as_of_date=parsed)

    if bundle is None:
        logger.debug(
            "portfolio-review investment section: id %s unavailable "
            "(unknown or cross-tenant, tenant=%s).",
            investment_id,
            session.tenant_id,
        )
        context: dict[str, Any] = {
            "investment": None,
            "header_metrics": None,
            "tile_specs": [],
            "as_of_date_display": "",
        }
    else:
        context = {
            "investment": bundle.investment,
            "header_metrics": bundle.header_metrics,
            "tile_specs": _build_single_investment_tile_specs(bundle),
            "as_of_date_display": bundle.as_of_date.isoformat(),
        }

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/portfolio_review_investment_section.html",
            context,
        ),
    )
