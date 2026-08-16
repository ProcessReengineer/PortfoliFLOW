# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web Benchmarks & Attribution surface — read-only section.

Embedded in ``/back-office#benchmarks-attribution`` (Roadmap A12,
Phase 1a). The section renders three blocks:

  - Block 1 (Stage a): Investments vs. Benchmarks — table of twelve
    risk-adjusted metrics per investment, plus an investment-picker
    dropdown that triggers a per-investment detail chart.
  - Block 2 (Stage b): Asset-Class Composites vs. Benchmarks — a
    Plotly small-multiples grid, one subplot per asset class.
  - Block 3 (Stage c): SAA Hypothetical Comparison — a three-line
    chart of (Actual, SAA × Benchmark, SAA × Composite) cumulative
    returns, driven by two HTMX-bound dropdowns for SAA configuration
    and weight set.

Endpoints:

* ``GET /api/back-office/benchmarks-attribution/section`` —
  Section body (Block 1 + Block 2 fully rendered, Block 3 selectors
  rendered with no chart until the operator picks a weight set).
* ``GET /api/back-office/benchmarks-attribution/stage-a/investment-detail?investment_id=<uuid>``
  — Detail chart partial for one investment.
* ``GET /api/back-office/benchmarks-attribution/stage-c/hypothetical?config_id=<uuid>&weight_set=<tangency|min_var>``
  — Re-renders the Stage-c chart for the selected SAA configuration
  and weight set.
* ``GET /api/back-office/benchmarks-attribution/stage-c/configuration-switch?config_id=<uuid>``
  — Re-renders the controls + chart region when the SAA config
  selector changes (the available weight sets may differ per config).
"""

from __future__ import annotations

import logging
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.exceptions import MissingFxRateError
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
from core.repositories.investment_nav_repository import (
    InvestmentNavRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.saa_asset_class_input_repository import (
    SAAAssetClassInputRepository,
)
from core.repositories.saa_configuration_repository import (
    SAAConfigurationRepository,
)
from core.repositories.saa_correlation_repository import (
    SAACorrelationRepository,
)
from core.repositories.tenant_repository import TenantRepository
from services.auth.session import SessionDTO, SessionRepository
from services.benchmark_comparison import (
    AssetClassCompositesBundle,
    BenchmarkComparisonService,
    InvestmentComparisonsBundle,
    PortfolioSummaryKPIs,
    SAAHypotheticalBundle,
    WeightSet,
)
from services.chart_specs import (
    build_benchmark_asset_class_composite_spec,
    build_benchmark_investment_total_return_spec,
    build_benchmark_saa_hypothetical_spec,
)
from services.saa import SAAService
from web.auth import require_session

logger = logging.getLogger(__name__)
router = APIRouter()

_VALID_WEIGHT_SETS: frozenset[str] = frozenset({"tangency", "min_var"})


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    engine = request.app.state.engine
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database engine is not configured.",
        )
    return cast(AsyncEngine, engine)


def _build_service(db_session: AsyncSession) -> BenchmarkComparisonService:
    """Construct the orchestrator with all tenant-scoped repositories.

    The SAA sub-service is constructed inline because it carries its
    own four-repository fan-out; passing the already-constructed
    repositories down avoids re-creating them in two places.
    """
    saa_service = SAAService(
        configurations=SAAConfigurationRepository(db_session),
        asset_classes=AssetClassRepository(db_session),
        inputs=SAAAssetClassInputRepository(db_session),
        correlations=SAACorrelationRepository(db_session),
    )
    return BenchmarkComparisonService(
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


def _resolve_weight_set(raw: str | None) -> WeightSet:
    if raw is None:
        return "tangency"
    normalised = raw.strip().lower()
    if normalised not in _VALID_WEIGHT_SETS:
        return "tangency"
    return cast(WeightSet, normalised)


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------


def _project_stage_a_rows(
    bundle: InvestmentComparisonsBundle,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in bundle.rows:
        rows.append(
            {
                "investment_id": str(row.investment_id),
                "investment_name": row.investment_name,
                "asset_class_code": row.asset_class_code,
                "benchmark_display_name": row.benchmark_display_name,
                "excess_return_annualised": row.excess_return_annualised,
                "alpha_annualised": row.alpha_annualised,
                "beta": row.beta,
                "r_squared": row.r_squared,
                "tracking_error_annualised": row.tracking_error_annualised,
                "information_ratio": row.information_ratio,
                "up_capture_ratio": row.up_capture_ratio,
                "down_capture_ratio": row.down_capture_ratio,
                "sharpe_investment": row.sharpe_investment,
                "sharpe_benchmark": row.sharpe_benchmark,
                "n_observations": row.n_observations,
                "period_start_iso": row.period_start_iso,
                "period_end_iso": row.period_end_iso,
            }
        )
    return rows


_MINUS_SIGN = "−"


def _format_pct_signed(value: float) -> str:
    """Format a fractional value as a signed percentage string.

    Uses the true minus sign (U+2212) for negatives.
    """
    pct = value * 100.0
    if pct < 0:
        return f"{_MINUS_SIGN}{abs(pct):.1f}%"
    if pct > 0:
        return f"+{pct:.1f}%"
    return "0.0%"


def _excess_variant(value: float | None) -> str:
    if value is None:
        return "neutral"
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def _ir_variant(value: float | None) -> str:
    if value is None:
        return "neutral"
    if value >= 0.5:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def _hit_rate_variant(value: float | None) -> str:
    if value is None:
        return "neutral"
    if value >= 0.5:
        return "positive"
    return "neutral"


def _project_portfolio_kpis(kpis: PortfolioSummaryKPIs) -> list[dict[str, Any]]:
    """Project the aggregate KPIs into four card dicts for the strip.

    Card order is fixed: Mapping Coverage → Median Excess p.a. →
    Hit Rate → Median IR. The ``variant`` key drives the colour
    treatment in benchmarks_attribution.css (.ba-kpi-strip__card--*).
    """
    mapping_card = {
        "label": "Mapping Coverage",
        "value": (f"{kpis.investments_with_benchmark_count} / {kpis.active_investments_count}"),
        "variant": "neutral",
    }

    excess = kpis.median_excess_return_annualised
    excess_card = {
        "label": "Median Excess p.a.",
        "value": "—" if excess is None else _format_pct_signed(excess),
        "variant": _excess_variant(excess),
    }

    hit = kpis.hit_rate
    hit_card = {
        "label": "Hit Rate",
        "value": "—" if hit is None else f"{round(hit * 100)}%",
        "variant": _hit_rate_variant(hit),
    }

    ir = kpis.median_information_ratio
    ir_card = {
        "label": "Median IR",
        "value": "—" if ir is None else f"{ir:.2f}",
        "variant": _ir_variant(ir),
    }

    return [mapping_card, excess_card, hit_card, ir_card]


def _build_stage_b_chart_rows(
    bundle: AssetClassCompositesBundle,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in bundle.rows:
        rows.append(
            {
                "asset_class_display_name": row.asset_class_display_name,
                "benchmark_display_name": row.benchmark_display_name,
                "composite_cumulative": row.composite_cumulative_returns,
                "benchmark_cumulative": row.benchmark_cumulative_returns,
                "excess_return_annualised": row.excess_return_annualised,
                "information_ratio": row.information_ratio,
                "n_investments": row.n_investments,
            }
        )
    return rows


def _build_stage_c_context(
    bundle: SAAHypotheticalBundle,
) -> dict[str, Any]:
    chart_spec: dict[str, Any] | None
    series_meta: dict[str, str] | None
    if bundle.series is None:
        chart_spec = None
        series_meta = None
    else:
        chart_spec = build_benchmark_saa_hypothetical_spec(
            actual=bundle.series.actual_portfolio_returns,
            saa_x_benchmark=bundle.series.saa_x_benchmark,
            saa_x_composite=bundle.series.saa_x_composite,
            saa_label=bundle.series.saa_label,
            effects=bundle.effects,
        )
        series_meta = {
            "period_start_iso": bundle.series.period_start.isoformat(),
            "period_end_iso": bundle.series.period_end.isoformat(),
            "saa_label": bundle.series.saa_label,
        }

    return {
        "saa_configuration_options": [
            {
                "saa_configuration_id": str(opt.saa_configuration_id),
                "name": opt.name,
                "is_active": opt.is_active,
            }
            for opt in bundle.saa_configuration_options
        ],
        "weight_set_options": [
            {
                "code": opt.code,
                "display_name": opt.display_name,
                "available": opt.available,
                "unavailable_hint": opt.unavailable_hint,
            }
            for opt in bundle.weight_set_options
        ],
        "selected_configuration_id": (
            str(bundle.selected_configuration_id)
            if bundle.selected_configuration_id is not None
            else ""
        ),
        "selected_weight_set": bundle.selected_weight_set,
        "stage_c_chart_spec": chart_spec,
        "stage_c_series_meta": series_meta,
        "has_series": bundle.series is not None,
    }


# ---------------------------------------------------------------------------
# GET /section
# ---------------------------------------------------------------------------


@router.get(
    "/api/back-office/benchmarks-attribution/section",
    response_class=HTMLResponse,
)
async def get_benchmarks_attribution_section(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the Benchmarks & Attribution section body.

    Lazy-loaded on first visibility via ``hx-trigger="revealed"`` in
    the section lazy-shell. The endpoint synchronously computes
    Stage a and Stage b in one render; Stage c renders the selector
    dropdowns and an empty-state hint until the operator submits.

    The ADR-0099 §4 conversion error (:class:`MissingFxRateError`,
    raised when a foreign-currency position lacks an FX rate the
    return series it feeds needs) is caught and rendered through a
    dedicated error-state partial, mirroring the overview and limits
    sections. The sub-fragment endpoints below need no such guard:
    they are reachable only from controls this body renders, so a
    missing rate stops the operator here.
    """
    engine = _engine(request)
    try:
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
            await SessionRepository(db_session).touch_throttled(session.id)
            service = _build_service(db_session)
            stage_a_bundle = await service.get_investment_comparisons()
            portfolio_kpis = await service.get_portfolio_summary_kpis()
            stage_b_bundle = await service.get_asset_class_composites()
            stage_c_bundle = await service.get_saa_hypothetical()
    except MissingFxRateError as exc:
        # ADR-0099 §4: a foreign-currency position lacks a rate it needs.
        # HTTP 200 deliberately — this body is an HTMX section swap, and an
        # error status would leave the lazy shell in place instead of
        # showing the message.
        logger.warning(
            "benchmarks-attribution section: FX rate missing (tenant=%s): %s",
            session.tenant_id,
            exc,
        )
        return cast(
            HTMLResponse,
            _templates(request).TemplateResponse(
                request,
                "_partials/benchmarks_attribution_error.html",
                {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            ),
        )

    has_data = bool(stage_a_bundle.rows) or bool(stage_b_bundle.rows)

    stage_a_rows = _project_stage_a_rows(stage_a_bundle)
    portfolio_kpi_cards = _project_portfolio_kpis(portfolio_kpis)
    stage_b_chart_rows = _build_stage_b_chart_rows(stage_b_bundle)
    stage_b_chart_spec = build_benchmark_asset_class_composite_spec(stage_b_chart_rows)

    context: dict[str, Any] = {
        "csrf_token": session.csrf_token,
        "has_data": has_data,
        # KPI strip
        "portfolio_kpi_cards": portfolio_kpi_cards,
        # Stage a
        "stage_a_rows": stage_a_rows,
        "stage_a_investments_without_benchmark": (stage_a_bundle.investments_without_benchmark),
        # Stage b
        "stage_b_chart_spec": stage_b_chart_spec,
        "stage_b_asset_classes_without_benchmark": (stage_b_bundle.asset_classes_without_benchmark),
    }
    context.update(_build_stage_c_context(stage_c_bundle))

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/benchmarks_attribution_section.html",
            context,
        ),
    )


# ---------------------------------------------------------------------------
# GET /stage-a/investment-detail
# ---------------------------------------------------------------------------


@router.get(
    "/api/back-office/benchmarks-attribution/stage-a/investment-detail",
    response_class=HTMLResponse,
)
async def get_stage_a_investment_detail(
    request: Request,
    investment_id: str | None = None,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the per-investment detail chart partial.

    An empty ``investment_id`` (the "— Select an investment —"
    placeholder option) renders the same empty-state partial the
    section starts with; this keeps the dropdown behaviour symmetric
    between "no selection" and "invalid selection".
    """
    if not investment_id:
        return _render_stage_a_empty_state(request)

    try:
        parsed_id = UUID(investment_id)
    except ValueError:
        return _render_stage_a_empty_state(
            request,
            message="Selected investment id is not a valid UUID.",
        )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        await SessionRepository(db_session).touch_throttled(session.id)
        service = _build_service(db_session)
        detail = await service.get_investment_comparison_detail(parsed_id)

    if detail is None:
        return _render_stage_a_empty_state(
            request,
            message=("No benchmark mapping or no aligned observations for this investment."),
        )

    spec = build_benchmark_investment_total_return_spec(
        investment_name=detail.investment_name,
        benchmark_display_name=detail.benchmark_display_name,
        investment_cumulative=detail.investment_cumulative_returns,
        benchmark_cumulative=detail.benchmark_cumulative_returns,
        excess_cumulative=detail.excess_cumulative_returns,
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/benchmarks_attribution_stage_a_detail_row.html",
            {
                "investment_id": str(parsed_id),
                "investment_name": detail.investment_name,
                "benchmark_display_name": detail.benchmark_display_name,
                "chart_spec": spec,
            },
        ),
    )


def _render_stage_a_empty_state(
    request: Request,
    *,
    message: str | None = None,
) -> HTMLResponse:
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/benchmarks_attribution_stage_a_detail_row.html",
            {
                "investment_id": None,
                "investment_name": None,
                "benchmark_display_name": None,
                "chart_spec": None,
                "empty_message": (
                    message or "Select an investment to see its detailed comparison."
                ),
            },
        ),
    )


# ---------------------------------------------------------------------------
# GET /stage-c/hypothetical
# ---------------------------------------------------------------------------


@router.get(
    "/api/back-office/benchmarks-attribution/stage-c/hypothetical",
    response_class=HTMLResponse,
)
async def get_stage_c_hypothetical(
    request: Request,
    config_id: str | None = None,
    weight_set: str | None = None,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Re-render only the Stage-c chart region for the selected inputs."""
    parsed_config = _parse_uuid(config_id)
    resolved_weight_set = _resolve_weight_set(weight_set)

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        await SessionRepository(db_session).touch_throttled(session.id)
        service = _build_service(db_session)
        bundle = await service.get_saa_hypothetical(
            saa_configuration_id=parsed_config,
            weight_set=resolved_weight_set,
        )

    context = _build_stage_c_context(bundle)
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/benchmarks_attribution_stage_c_chart.html",
            context,
        ),
    )


# ---------------------------------------------------------------------------
# GET /stage-c/configuration-switch
# ---------------------------------------------------------------------------


@router.get(
    "/api/back-office/benchmarks-attribution/stage-c/configuration-switch",
    response_class=HTMLResponse,
)
async def get_stage_c_configuration_switch(
    request: Request,
    config_id: str | None = None,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Re-render the controls + chart container after a config switch.

    The available weight sets depend on whether the selected
    configuration is optimizable; refreshing both pieces in one
    response keeps the UI consistent.
    """
    parsed_config = _parse_uuid(config_id)

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        await SessionRepository(db_session).touch_throttled(session.id)
        service = _build_service(db_session)
        bundle = await service.get_saa_hypothetical(
            saa_configuration_id=parsed_config,
            weight_set="tangency",
        )

    context = _build_stage_c_context(bundle)
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/benchmarks_attribution_stage_c_controls.html",
            context,
        ),
    )


def _parse_uuid(raw: str | None) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None
