# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web Portfolio Analysis surface — HTMX section + compute endpoints.

The Portfolio Analysis module is embedded in
``/front-office#portfolio-optimizer`` (sub-stream 6F-3c). The
standalone ``/portfolio-analysis`` URL was retired in 6F-1; this
module exposes two endpoints:

* ``GET  /api/portfolio-analysis/section``
  — Returns the lazy-load section body (Compute form + empty chart
    container) on first reveal.
* ``POST /api/portfolio-analysis/section/compute``
  — Runs :meth:`PortfolioAnalysisService.compute_frontier` with the
    form's parameters and returns ONLY the chart partial (summary
    cards + frontier chart). HTMX swaps this into the chart
    container; the form is not re-rendered.

CSRF: the POST endpoint requires :func:`verify_csrf`. The GET is
read-only and unauthenticated callers redirect to ``/login`` via
:func:`require_session`.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import MissingFxRateError
from core.repositories._session import tenant_context
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.investment_nav_repository import (
    InvestmentNavRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.tenant_repository import TenantRepository
from services.auth.session import SessionDTO, SessionRepository
from services.chart_specs import build_portfolio_analysis_frontier_spec
from services.portfolio_analysis import (
    PortfolioAnalysisBundle,
    PortfolioAnalysisService,
)
from web.auth import require_session, verify_csrf
from web.permissions import require_role

logger = logging.getLogger(__name__)
router = APIRouter()

# Form parameter bounds — mirror the orphan template's HTML
# attributes (sub-stream 6F-3a). Kept conservative: the optimiser
# does not benefit from > 500 frontier samples in practice, and
# negative risk-free rates below -5 % are well outside any plausible
# operator scenario.
_MIN_FRONTIER_POINTS = 20
_MAX_FRONTIER_POINTS = 500
_DEFAULT_FRONTIER_POINTS = 100
_MIN_RISK_FREE_RATE_PCT = -5.0
_MAX_RISK_FREE_RATE_PCT = 20.0
_DEFAULT_RISK_FREE_RATE_PCT = 2.5


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


@router.get(
    "/api/portfolio-analysis/section",
    response_class=HTMLResponse,
)
async def get_portfolio_analysis_section(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the section body — Compute form + empty chart container.

    No computation happens here; the bundle is ``None`` until the
    operator clicks Compute. Cheap render — the lazy shell uses
    ``hx-trigger="revealed"`` to fetch this on first visibility.

    Args:
        request: The FastAPI request (provides app state).
        session: The active :class:`SessionDTO` (resolved by
            :func:`require_session`).

    Returns:
        An :class:`HTMLResponse` carrying the rendered section body
        (Compute form + empty chart container).
    """
    context: dict[str, Any] = {
        "csrf_token": session.csrf_token,
        "min_n_points": _MIN_FRONTIER_POINTS,
        "max_n_points": _MAX_FRONTIER_POINTS,
        "default_n_points": _DEFAULT_FRONTIER_POINTS,
        "min_risk_free_rate_pct": _MIN_RISK_FREE_RATE_PCT,
        "max_risk_free_rate_pct": _MAX_RISK_FREE_RATE_PCT,
        "default_risk_free_rate_pct": _DEFAULT_RISK_FREE_RATE_PCT,
        "chart_payload": None,
    }
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/portfolio_analysis_section.html",
            context,
        ),
    )


@router.post(
    "/api/portfolio-analysis/section/compute",
    response_class=HTMLResponse,
    dependencies=[Depends(require_role("owner", "member"))],
)
async def post_portfolio_analysis_section_compute(
    request: Request,
    frontier_points: int = Form(..., ge=_MIN_FRONTIER_POINTS, le=_MAX_FRONTIER_POINTS),
    risk_free_rate: float = Form(..., ge=_MIN_RISK_FREE_RATE_PCT, le=_MAX_RISK_FREE_RATE_PCT),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Run the analytics and return ONLY the chart partial.

    HTMX swaps the response into ``#pa-chart-container``; the form
    above remains in place with its current inputs preserved.

    This is the section's only FX-dependent endpoint — the section GET
    renders the form without touching the database. The ADR-0099 §4
    conversion error (:class:`MissingFxRateError`) is therefore caught
    here and swapped into the chart container as an error state, so a
    missing rate reads as a named, actionable message rather than a 500
    from a form submit.

    Args:
        request: The FastAPI request (provides app state).
        frontier_points: Number of frontier samples to compute.
            Bounded ``[_MIN_FRONTIER_POINTS, _MAX_FRONTIER_POINTS]``.
        risk_free_rate: Annualised risk-free rate **in percent** as
            entered in the form. Bounded
            ``[_MIN_RISK_FREE_RATE_PCT, _MAX_RISK_FREE_RATE_PCT]``.
            Divided by 100 before being passed to the service so it
            receives the decimal form.
        session: The active :class:`SessionDTO`.
        _csrf: CSRF gate (consumed via :func:`verify_csrf`).

    Returns:
        An :class:`HTMLResponse` carrying the chart partial — summary
        cards and the rendered frontier chart, the empty-state copy
        when the universe is too small, or the FX-error state when a
        required rate is missing. Always HTTP 200 — the body is an
        HTMX swap into the chart container.
    """
    engine = _engine(request)
    try:
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            await SessionRepository(db).touch_throttled(session.id)
            service = PortfolioAnalysisService(
                investments=InvestmentRepository(db),
                navs=InvestmentNavRepository(db),
                cashflows=InvestmentCashflowRepository(db),
                tenants=TenantRepository(db),
                fx_rates=FxRateRepository(db),
            )
            bundle = await service.compute_frontier(
                n_points=frontier_points,
                risk_free_rate=risk_free_rate / 100.0,
            )
    except MissingFxRateError as exc:
        # ADR-0099 §4: a foreign-currency position lacks a rate it needs.
        # HTTP 200 deliberately — this body is an HTMX swap into the chart
        # container, and an error status would leave the previous chart (or
        # the empty state) in place with no explanation.
        logger.warning(
            "portfolio-analysis compute: FX rate missing (tenant=%s): %s",
            session.tenant_id,
            exc,
        )
        return cast(
            HTMLResponse,
            _templates(request).TemplateResponse(
                request,
                "_partials/portfolio_analysis_error.html",
                {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            ),
        )

    chart_payload = _build_chart_payload(bundle, risk_free_rate_pct=risk_free_rate)
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/portfolio_analysis_chart_partial.html",
            {"chart_payload": chart_payload},
        ),
    )


def _build_chart_payload(
    bundle: PortfolioAnalysisBundle | None,
    *,
    risk_free_rate_pct: float,
) -> dict[str, Any] | None:
    """Translate a :class:`PortfolioAnalysisBundle` into template context.

    Returns ``None`` when the bundle is ``None`` (empty universe,
    fewer than two investments with overlapping NAV histories, or an
    optimiser failure) — the template renders the empty-state copy
    in that case.

    Args:
        bundle: The bundle returned by
            :meth:`PortfolioAnalysisService.compute_frontier`.
        risk_free_rate_pct: The risk-free rate echoed back to the
            template as a percentage for display.

    Returns:
        A dict of template context, or ``None`` when no chart can be
        rendered.
    """
    if bundle is None:
        return None

    spec = build_portfolio_analysis_frontier_spec(
        frontier=bundle.frontier_result,
        tangency=bundle.tangency,
        min_variance=bundle.min_variance,
        capital_market_line=bundle.capital_market_line,
        current_portfolio=bundle.current_portfolio,
        investment_points=bundle.investment_points,
        risk_free_rate=bundle.risk_free_rate,
    )

    cp_vol, cp_ret = bundle.current_portfolio
    current_summary: dict[str, float] | None
    # NaN test without importing math: NaN != NaN by IEEE-754.
    if cp_vol == cp_vol and cp_ret == cp_ret:
        current_summary = {
            "expected_return": cp_ret,
            "volatility": cp_vol,
        }
    else:
        current_summary = None

    # Per-asset weights comparison table (Current vs Tangency vs
    # Min-Variance). Iterate the canonical frontier asset order and
    # look weights up by name — the portfolios' own weight vectors
    # are expected to share this order, but a name-keyed lookup is
    # robust if they ever diverge.
    tangency_by_name = dict(zip(bundle.tangency.asset_names, bundle.tangency.weights))
    min_var_by_name = dict(zip(bundle.min_variance.asset_names, bundle.min_variance.weights))
    weights = [
        {
            "name": name,
            "tangency_pct": float(tangency_by_name.get(name, 0.0)) * 100.0,
            "min_var_pct": float(min_var_by_name.get(name, 0.0)) * 100.0,
            "current_pct": (
                float(bundle.current_weights.get(name, 0.0)) * 100.0
                if bundle.current_weights is not None
                else None
            ),
        }
        for name in bundle.frontier_result.asset_names
    ]

    return {
        "spec": spec,
        "tangency_summary": {
            "expected_return": bundle.tangency.expected_return,
            "volatility": bundle.tangency.volatility,
            "sharpe_ratio": bundle.tangency.sharpe_ratio,
        },
        "min_var_summary": {
            "expected_return": bundle.min_variance.expected_return,
            "volatility": bundle.min_variance.volatility,
        },
        "current_summary": current_summary,
        "weights": weights,
        "n_investments": len(bundle.investment_points),
        "n_frontier_points": len(bundle.frontier_result.frontier_returns),
        "n_points_requested": bundle.n_points_requested,
        "risk_free_rate_pct": risk_free_rate_pct,
    }
