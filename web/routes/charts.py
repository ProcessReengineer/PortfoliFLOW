# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web Charts surface — universe-wide briefing.

The Charts module is embedded in ``/front-office#charts`` (sub-stream
6F-4). The surface renders one ``<article>`` per active investment,
each with a per-investment lazy-loader that fetches an
**archetype-aware** tile-set (ADR-0082):

* **Capital-Account** (private markets) — Total Return · Cashflows &
  NAV · TVPI/DPI.
* **Total-Return-Equity** (listed equity) — benchmark hero ·
  underwater · sector|region composition.
* **Fixed-Income** (listed bonds) — benchmark hero · YTM/OAS &
  duration · rating|maturity composition.
* **NAV-only** (``other`` / unknown) — a single full-width NAV
  time-series tile.

Endpoints:

* ``GET /api/charts/section`` — Returns the section shell with one
  article per active investment. Per-investment lazy-loaders trigger
  the per-archetype tile fetches on scroll.
* ``GET /api/charts/investment/{investment_id}`` — Resolves the
  investment's archetype and returns the matching tile fragment plus a
  KPI caption and the Plotly bootstrap script. An unknown /
  cross-tenant id renders a neutral empty state with HTTP 200 (the row
  existence is never leaked; ADR-0082 compliance, ADR-0073 precedent).
"""

from __future__ import annotations

import logging
import math
from datetime import date
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.repositories._session import tenant_context
from core.repositories.asset_class_benchmark_mapping_repository import (
    AssetClassBenchmarkMappingRepository,
)
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.benchmark_observation_repository import (
    BenchmarkObservationRepository,
)
from core.repositories.benchmark_repository import BenchmarkRepository
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_bond_analytics_repository import (
    InvestmentBondAnalyticsRepository,
)
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowRepository,
)
from core.repositories.investment_maturity_weights_repository import (
    InvestmentMaturityWeightsRepository,
)
from core.repositories.investment_nav_repository import (
    InvestmentNavRepository,
)
from core.repositories.investment_rating_weights_repository import (
    InvestmentRatingWeightsRepository,
)
from core.repositories.investment_region_weights_repository import (
    InvestmentRegionWeightsRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.investment_sector_weights_repository import (
    InvestmentSectorWeightsRepository,
)
from core.repositories.region_repository import RegionRepository
from core.repositories.saa_asset_class_input_repository import (
    SAAAssetClassInputRepository,
)
from core.repositories.saa_configuration_repository import (
    SAAConfigurationRepository,
)
from core.repositories.saa_correlation_repository import (
    SAACorrelationRepository,
)
from core.repositories.sector_repository import SectorRepository
from core.repositories.tenant_repository import TenantRepository
from services.auth.session import SessionDTO, SessionRepository
from services.benchmark_comparison import BenchmarkComparisonService
from services.chart_specs import (
    build_benchmark_investment_total_return_spec,
    build_cashflows_nav_spec,
    build_composition_split_spec,
    build_multiples_spec,
    build_nav_timeseries_spec,
    build_rating_maturity_split_spec,
    build_total_return_spec,
    build_underwater_spec,
    build_ytm_duration_spec,
)
from services.front_office_charts import (
    ArchetypeChartsResult,
    ArchetypeChartsService,
    CapitalAccountKPI,
    EquityKPI,
    FixedIncomeKPI,
)
from services.investments import InvestmentService
from services.investments.archetype import Archetype, resolve_archetype
from services.saa import SAAService
from web.auth import require_session

logger = logging.getLogger(__name__)
router = APIRouter()

# Placeholder shown as the hero benchmark trace name when the asset
# class carries no benchmark mapping (the hero then draws the
# investment line alone — a visible empty state, not a silent fallback).
_NO_BENCHMARK_LABEL = "(no benchmark mapped)"

# Em dash used for every missing / undefined KPI figure so the absence
# is visible rather than masked by a fabricated zero.
_EM_DASH = "—"


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


def _build_archetype_service(db_session: AsyncSession) -> ArchetypeChartsService:
    """Construct :class:`ArchetypeChartsService` with the full repository fan-out.

    The benchmark / SAA wiring mirrors
    :func:`web.routes.benchmarks_attribution._build_service` exactly so
    the hero benchmark line is computed from the same inputs on both
    surfaces. The Investment-domain service is constructed inline for
    the Capital-Account delegation; the eight composition / FI-reference
    repositories feed the listed and NAV-only archetypes.
    """
    saa_service = SAAService(
        configurations=SAAConfigurationRepository(db_session),
        asset_classes=AssetClassRepository(db_session),
        inputs=SAAAssetClassInputRepository(db_session),
        correlations=SAACorrelationRepository(db_session),
    )
    benchmarks = BenchmarkComparisonService(
        investments=InvestmentRepository(db_session),
        navs=InvestmentNavRepository(db_session),
        asset_classes=AssetClassRepository(db_session),
        benchmarks=BenchmarkRepository(db_session),
        benchmark_observations=BenchmarkObservationRepository(db_session),
        mappings=AssetClassBenchmarkMappingRepository(db_session),
        saa_service=saa_service,
        tenants=TenantRepository(db_session),
        fx_rates=FxRateRepository(db_session),
    )
    investments_service = InvestmentService(
        investments=InvestmentRepository(db_session),
        navs=InvestmentNavRepository(db_session),
        cashflows=InvestmentCashflowRepository(db_session),
    )
    return ArchetypeChartsService(
        investments=InvestmentRepository(db_session),
        navs=InvestmentNavRepository(db_session),
        cashflows=InvestmentCashflowRepository(db_session),
        bond_analytics=InvestmentBondAnalyticsRepository(db_session),
        rating_weights=InvestmentRatingWeightsRepository(db_session),
        maturity_weights=InvestmentMaturityWeightsRepository(db_session),
        sector_weights=InvestmentSectorWeightsRepository(db_session),
        region_weights=InvestmentRegionWeightsRepository(db_session),
        sectors=SectorRepository(db_session),
        regions=RegionRepository(db_session),
        investments_service=investments_service,
        benchmarks=benchmarks,
    )


# ---------------------------------------------------------------------------
# KPI-pill formatting (pure, unit-testable)
# ---------------------------------------------------------------------------


def _is_missing(value: float | None) -> bool:
    """Return ``True`` for ``None`` or a NaN float (the missing sentinels)."""
    return value is None or (isinstance(value, float) and math.isnan(value))


def _fmt_multiple(value: float | None) -> str:
    """Format a TVPI/DPI-style multiple as ``x.xx×``; ``—`` when missing."""
    if _is_missing(value):
        return _EM_DASH
    return f"{cast(float, value):.2f}×"


def _fmt_pct(value: float | None, *, decimals: int = 1) -> str:
    """Format a decimal fraction as a percentage (``×100``); ``—`` when missing."""
    if _is_missing(value):
        return _EM_DASH
    return f"{cast(float, value) * 100.0:.{decimals}f}%"


def _fmt_ratio(value: float | None, *, decimals: int = 2) -> str:
    """Format a bare ratio (Sharpe / Beta / IR) as ``x.xx``; ``—`` when missing."""
    if _is_missing(value):
        return _EM_DASH
    return f"{cast(float, value):.{decimals}f}"


def _fmt_years(value: float | None) -> str:
    """Format a duration in years as ``x.x y``; ``—`` when missing."""
    if _is_missing(value):
        return _EM_DASH
    return f"{cast(float, value):.1f} y"


def _fmt_basis_points(value: float | None) -> str:
    """Format a decimal spread as basis points (``×10000``); ``—`` when missing."""
    if _is_missing(value):
        return _EM_DASH
    return f"{round(cast(float, value) * 10000.0)} bp"


def _fmt_eur_compact(value: float | None) -> str:
    """Format an EUR amount compactly (``€ 1.2M`` / ``€ 850k``); ``—`` when missing."""
    if _is_missing(value):
        return _EM_DASH
    amount = cast(float, value)
    sign = "−" if amount < 0 else ""
    magnitude = abs(amount)
    if magnitude >= 1e9:
        return f"{sign}€ {magnitude / 1e9:.1f}B"
    if magnitude >= 1e6:
        return f"{sign}€ {magnitude / 1e6:.1f}M"
    if magnitude >= 1e3:
        return f"{sign}€ {magnitude / 1e3:.0f}k"
    return f"{sign}€ {magnitude:.0f}"


def _capital_account_pills(kpi: CapitalAccountKPI) -> list[dict[str, str]]:
    """Build the Capital-Account KPI caption pills (ADR-0082 §5)."""
    return [
        {"label": "TVPI", "value": _fmt_multiple(kpi.tvpi)},
        {"label": "DPI", "value": _fmt_multiple(kpi.dpi)},
        {"label": "Net IRR", "value": _fmt_pct(kpi.net_irr)},
        {"label": "Unfunded", "value": _fmt_eur_compact(kpi.unfunded_commitment)},
    ]


def _equity_pills(kpi: EquityKPI) -> list[dict[str, str]]:
    """Build the Total-Return-Equity KPI caption pills (ADR-0082 §5)."""
    return [
        {"label": "1Y TWR", "value": _fmt_pct(kpi.trailing.y1)},
        {"label": "Vol", "value": _fmt_pct(kpi.vol_12m)},
        {"label": "Sharpe", "value": _fmt_ratio(kpi.sharpe_12m)},
        {"label": "Beta", "value": _fmt_ratio(kpi.beta)},
        {"label": "TE", "value": _fmt_pct(kpi.tracking_error)},
        {"label": "IR", "value": _fmt_ratio(kpi.information_ratio)},
        {"label": "Div Yield", "value": _fmt_pct(kpi.dividend_yield_ttm)},
    ]


def _fixed_income_pills(kpi: FixedIncomeKPI) -> list[dict[str, str]]:
    """Build the Fixed-Income KPI caption pills (ADR-0082 §5)."""
    return [
        {"label": "TWR", "value": _fmt_pct(kpi.twr)},
        {"label": "YTM", "value": _fmt_pct(kpi.ytm)},
        {"label": "Eff. Duration", "value": _fmt_years(kpi.eff_duration)},
        {"label": "Avg Rating", "value": kpi.avg_rating.average_bucket},
        {"label": "OAS", "value": _fmt_basis_points(kpi.oas)},
    ]


def _build_archetype_tiles(
    result: ArchetypeChartsResult,
    axis_end: date | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]] | None]:
    """Dispatch on the resolved archetype to a tile list and KPI pills.

    Each tile is ``{"slot": str, "spec": dict}``; the template iterates
    the list and embeds one Plotly target per tile. ``kpi_pills`` is
    ``None`` for the NAV-only archetype (no caption).

    Args:
        result: The populated assembly result; exactly one tile bundle
            is non-``None`` for the resolved archetype.
        axis_end: The universe as-of (ADR-0113 §1), threaded into every
            time-series tile so they share one right-hand axis end. The
            two categorical tiles (composition, rating|maturity) carry
            no time axis and are not passed it. ``None`` leaves each
            tile on its own auto-range. The same date is passed a
            second time as ``plan_tail_end`` to the two NAV-space tiles
            (ADR-0113 §2): the plan tail must stop where the shared axis
            stops, but the two remain separate parameters because the
            spec builders keep the data window and the axis range as
            distinct concerns — the investment-detail surface takes
            neither.

    Returns:
        ``(tiles, kpi_pills)`` for the matching archetype.
    """
    archetype = result.archetype
    name = result.investment_name

    if archetype is Archetype.CAPITAL_ACCOUNT:
        capital_account = result.capital_account
        assert capital_account is not None  # invariant of the resolved archetype
        charts = capital_account.charts
        tiles = [
            {
                "slot": "tr",
                "spec": build_total_return_spec(
                    charts.total_return_series, name, axis_end=axis_end
                ),
            },
            {
                "slot": "cn",
                "spec": build_cashflows_nav_spec(
                    charts.cashflows_actual,
                    charts.nav_series,
                    charts.net_capital_gain,
                    name,
                    axis_end=axis_end,
                    nav_plan=capital_account.nav_plan,
                    plan_tail_end=axis_end,
                ),
            },
            {
                "slot": "mp",
                "spec": build_multiples_spec(
                    charts.rolling_multiples,
                    charts.rolling_irr,
                    name,
                    style="lines",
                    axis_end=axis_end,
                ),
            },
        ]
        return tiles, _capital_account_pills(capital_account.kpi)

    if archetype is Archetype.TOTAL_RETURN_EQUITY:
        equity = result.total_return_equity
        assert equity is not None  # invariant of the resolved archetype
        tiles = [
            {
                "slot": "hero",
                "spec": build_benchmark_investment_total_return_spec(
                    name,
                    equity.benchmark_display_name or _NO_BENCHMARK_LABEL,
                    equity.investment_cumulative,
                    equity.benchmark_cumulative,
                    equity.excess_cumulative,
                    axis_end=axis_end,
                ),
            },
            {
                "slot": "uw",
                "spec": build_underwater_spec(equity.underwater_series, name, axis_end=axis_end),
            },
            {
                "slot": "comp",
                "spec": build_composition_split_spec(
                    equity.sector_weights, equity.region_weights, name
                ),
            },
        ]
        return tiles, _equity_pills(equity.kpi)

    if archetype is Archetype.FIXED_INCOME:
        fixed_income = result.fixed_income
        assert fixed_income is not None  # invariant of the resolved archetype
        tiles = [
            {
                "slot": "hero",
                "spec": build_benchmark_investment_total_return_spec(
                    name,
                    fixed_income.benchmark_display_name or _NO_BENCHMARK_LABEL,
                    fixed_income.investment_cumulative,
                    fixed_income.benchmark_cumulative,
                    fixed_income.excess_cumulative,
                    axis_end=axis_end,
                ),
            },
            {
                "slot": "yd",
                "spec": build_ytm_duration_spec(
                    fixed_income.bond_analytics, name, axis_end=axis_end
                ),
            },
            {
                "slot": "rm",
                "spec": build_rating_maturity_split_spec(
                    fixed_income.rating_weights,
                    fixed_income.maturity_weights,
                    name,
                ),
            },
        ]
        return tiles, _fixed_income_pills(fixed_income.kpi)

    # NAV-only fallback: a single full-width NAV time-series tile, no
    # KPI caption.
    nav_only = result.nav_only
    assert nav_only is not None  # invariant of the resolved archetype
    tiles = [
        {
            "slot": "nav",
            "spec": build_nav_timeseries_spec(
                nav_only.investment,
                nav_only.navs,
                axis_end=axis_end,
                plan_tail_end=axis_end,
            ),
        }
    ]
    return tiles, None


@router.get(
    "/api/charts/section",
    response_class=HTMLResponse,
)
async def get_charts_section(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the universe-wide charts section fragment.

    Lazy-loaded on first visibility via ``hx-trigger="revealed"`` in
    the section lazy-shell. Returns one ``<article>`` per active
    investment, each with a per-investment lazy-loader for the
    actual tile-set.

    The list of investment names is fetched here so the rendered
    article skeleton already carries the names (avoids a flash of
    "Loading…" titles). The actual chart data is deferred to the
    per-investment endpoint.

    Args:
        request: The FastAPI request (provides app state).
        session: The active :class:`SessionDTO` resolved by
            :func:`require_session`.

    Returns:
        An :class:`HTMLResponse` carrying the rendered section body.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        await SessionRepository(db_session).touch_throttled(session.id)
        investments_repo = InvestmentRepository(db_session)
        investments = await investments_repo.list_active()
        asset_classes = await AssetClassRepository(db_session).list_all()

    asset_classes_by_id = {ac.id: ac for ac in asset_classes}

    # Alphabetical order for stable presentation.
    investments_sorted = sorted(investments, key=lambda inv: inv.name)
    article_list = [
        {
            "id": str(inv.id),
            "name": inv.name,
            "asset_class_name": (
                asset_classes_by_id[inv.asset_class_id].display_name
                if inv.asset_class_id in asset_classes_by_id
                else None
            ),
            # Archetype label carried for any future per-article styling;
            # harmless when the template ignores it (ADR-0082 §6 nicety).
            "archetype": str(resolve_archetype(inv.investment_type)),
        }
        for inv in investments_sorted
    ]

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/charts_section.html",
            {"investments": article_list},
        ),
    )


@router.get(
    "/api/charts/investment/{investment_id}",
    response_class=HTMLResponse,
)
async def get_charts_investment(
    request: Request,
    investment_id: UUID,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the archetype-aware tile fragment for one investment.

    Fetched by the per-investment lazy-loader inside the section shell
    when its containing article scrolls into view. Resolves the
    investment's presentation archetype (ADR-0082) and returns the
    matching tile-set, a KPI caption, and an inline Plotly bootstrap.

    An unknown or cross-tenant ``investment_id`` resolves to ``None``
    (RLS hides the row); rather than a 404 — which would leak whether
    the row exists in another tenant — the route renders a neutral
    empty state with HTTP 200 (ADR-0082 compliance, ADR-0073
    precedent).

    Args:
        request: The FastAPI request.
        investment_id: Path-bound UUID identifying the investment.
        session: The active :class:`SessionDTO`.

    Returns:
        An :class:`HTMLResponse` with the rendered tile fragment, or a
        neutral empty state (HTTP 200) when the id does not resolve.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        service = _build_archetype_service(db_session)
        result = await service.get_archetype_charts_data(investment_id)
        # ADR-0113 §1: the shared right-hand axis end, recomputed per
        # lazy per-investment fetch rather than cached across requests.
        # Concurrent fetches during an ongoing import may therefore
        # briefly disagree on the universe as-of; they converge on the
        # next reveal, which is cheaper than a cross-request cache whose
        # invalidation would have to track every NAV write.
        axis_end = await service.get_universe_axis_end()

    if result is None:
        # Neutral empty state, HTTP 200 — never leak row existence.
        return cast(
            HTMLResponse,
            _templates(request).TemplateResponse(
                request,
                "_partials/charts_investment_triplet.html",
                {
                    "investment_id": str(investment_id),
                    "investment_name": None,
                    "archetype": "",
                    "tiles": [],
                    "kpi_pills": None,
                },
            ),
        )

    tiles, kpi_pills = _build_archetype_tiles(result, axis_end)

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/charts_investment_triplet.html",
            {
                "investment_id": str(investment_id),
                "investment_name": result.investment_name,
                "archetype": str(result.archetype),
                "tiles": tiles,
                "kpi_pills": kpi_pills,
            },
        ),
    )
