# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web Statistics surface — HTMX section endpoint.

The Statistics module is embedded in ``/front-office#statistics``
(sub-stream 6F-3b). The standalone ``/statistics`` URL was retired
in 6F-1; this module exposes a single endpoint:

* ``GET /api/statistics/section`` — Returns the KPI strip,
  correlation heatmap and detail-tables fragment.

The endpoint is fetched lazily on first visibility via the
section template's ``hx-trigger="revealed"``. The route handler
calls :meth:`StatisticsService.get_universe_statistics` and packs
the result for the template; chart specs are produced inline the
same way the (now-sunset) standalone route did.
"""

from __future__ import annotations

import logging
import math
from datetime import date as _date
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import MissingFxRateError
from core.repositories._session import tenant_context
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_nav_repository import (
    InvestmentNavRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.tenant_repository import TenantRepository
from services.auth.session import SessionDTO, SessionRepository
from services.chart_specs import (
    build_correlation_heatmap_spec,
    build_sparkline_spec,
)
from services.statistics import StatisticsService, UniverseStatisticsBundle
from web.auth import require_session

logger = logging.getLogger(__name__)
router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


@router.get(
    "/api/statistics/section",
    response_class=HTMLResponse,
)
async def get_statistics_section(
    request: Request,
    session: SessionDTO = Depends(require_session),
    investment_id: list[UUID] | None = Query(
        default=None,
        description="Optional investment-id filter; repeatable.",
    ),
    as_of_date: _date | None = Query(default=None),
    risk_free_rate: float = Query(default=0.0),
) -> HTMLResponse:
    """Return the Statistics section fragment.

    Lazy-loaded on first visibility via ``hx-trigger="revealed"``
    in the section template. Pure read path — no CSRF.

    The ADR-0099 §4 conversion error (:class:`MissingFxRateError`,
    raised when a foreign-currency position lacks an FX rate the
    return series it feeds needs) is caught and rendered through a
    dedicated error-state partial, mirroring the overview and limits
    sections.

    Args:
        request: The FastAPI request (provides app state).
        session: The active :class:`SessionDTO` (resolved by
            :func:`require_session`).
        investment_id: Optional repeated query param scoping the
            universe to a subset of investments.
        as_of_date: Optional analysis as-of date.
        risk_free_rate: Annualised risk-free rate (decimal) for
            the Sharpe-ratio calculation; defaults to 0.0.

    Returns:
        An :class:`HTMLResponse` carrying the rendered section body,
        or the FX-error state when a required rate is missing. Always
        HTTP 200 — the body is an HTMX section swap.
    """
    engine = _engine(request)
    try:
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            await SessionRepository(db).touch_throttled(session.id)
            service = StatisticsService(
                investments=InvestmentRepository(db),
                navs=InvestmentNavRepository(db),
                tenants=TenantRepository(db),
                fx_rates=FxRateRepository(db),
            )
            bundle = await service.get_universe_statistics(
                investment_ids=investment_id,
                as_of_date=as_of_date,
                risk_free_rate=risk_free_rate,
            )
    except MissingFxRateError as exc:
        # ADR-0099 §4: a foreign-currency position lacks a rate it needs.
        # HTTP 200 deliberately — this body is an HTMX section swap, and an
        # error status would leave the lazy shell in place instead of
        # showing the message.
        logger.warning(
            "statistics section: FX rate missing (tenant=%s): %s",
            session.tenant_id,
            exc,
        )
        return cast(
            HTMLResponse,
            _templates(request).TemplateResponse(
                request,
                "_partials/statistics_error.html",
                {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            ),
        )

    context = _build_section_context(bundle)
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/statistics_section.html",
            context,
        ),
    )


def _nan_to_none(value: float | None) -> float | None:
    """Collapse ``NaN`` floats to ``None`` for template rendering.

    The Statistics service emits ``NaN`` when a calculation could
    not converge (e.g. Sharpe ratio over a zero-variance series).
    The section template tests with ``is not none`` to decide
    between rendering a numeric value and the ``N/A`` fallback —
    funneling ``NaN`` to ``None`` here keeps the Jinja path
    single-branch.

    Args:
        value: Float or ``None`` straight off the
            :class:`RiskMetrics` dataclass.

    Returns:
        ``None`` when ``value`` is ``None`` or ``NaN``; otherwise
        the original float.
    """
    if value is None or math.isnan(value):
        return None
    return value


def _project_distribution_rows(
    bundle: UniverseStatisticsBundle,
) -> list[dict[str, Any]]:
    """Project Distribution statistics as one dict per investment.

    The pre-pivot shape (metric-as-row, investment-as-column) is
    transformed into investment-as-row with metrics flattened as
    fields. NaN floats are collapsed to None per the section
    template's Jinja convention.

    Args:
        bundle: The bundle returned by
            :meth:`StatisticsService.get_universe_statistics`.

    Returns:
        List of dicts; each carries ``name`` plus the ten
        Distribution metric fields. Order matches
        ``bundle.investment_names``.
    """
    out: list[dict[str, Any]] = []
    for name in bundle.investment_names:
        stats = bundle.distribution_stats.get(name)
        if stats is None:
            out.append(
                {
                    "name": name,
                    "mean_daily": None,
                    "mean_annualised": None,
                    "std_daily": None,
                    "std_annualised": None,
                    "variance_daily": None,
                    "skewness": None,
                    "kurtosis_excess": None,
                    "median": None,
                    "min_return": None,
                    "max_return": None,
                }
            )
            continue
        out.append(
            {
                "name": name,
                "mean_daily": _nan_to_none(stats.mean_daily),
                "mean_annualised": _nan_to_none(stats.mean_annualised),
                "std_daily": _nan_to_none(stats.std_daily),
                "std_annualised": _nan_to_none(stats.std_annualised),
                "variance_daily": _nan_to_none(stats.variance_daily),
                "skewness": _nan_to_none(stats.skewness),
                "kurtosis_excess": _nan_to_none(stats.kurtosis_excess),
                "median": _nan_to_none(stats.median),
                "min_return": _nan_to_none(stats.min_return),
                "max_return": _nan_to_none(stats.max_return),
            }
        )
    return out


def _project_risk_rows_pivot(
    bundle: UniverseStatisticsBundle,
) -> list[dict[str, Any]]:
    """Project Risk + Risk/Return + Autocorrelation as one dict per investment.

    The three sub-tables share an investment-as-row pivot. The
    Risk-table fields, Risk/Return fields, and four Autocorrelation
    lags are flattened onto one row per investment, since they all
    come from the same ``RiskMetrics`` DTO.

    Args:
        bundle: The bundle returned by
            :meth:`StatisticsService.get_universe_statistics`.

    Returns:
        List of dicts; each carries ``name`` plus the 13 RiskMetrics
        fields. Order matches ``bundle.investment_names``.
    """
    out: list[dict[str, Any]] = []
    for name in bundle.investment_names:
        metrics = bundle.risk_metrics.get(name)
        if metrics is None:
            out.append(
                {
                    "name": name,
                    "var_90_daily": None,
                    "var_95_daily": None,
                    "var_99_daily": None,
                    "cvar_95_daily": None,
                    "max_drawdown": None,
                    "ulcer_index": None,
                    "downside_deviation": None,
                    "sharpe_ratio": None,
                    "sortino_ratio": None,
                    "lag_1_autocorrelation": None,
                    "lag_2_autocorrelation": None,
                    "lag_3_autocorrelation": None,
                    "lag_4_autocorrelation": None,
                }
            )
            continue
        out.append(
            {
                "name": name,
                "var_90_daily": _nan_to_none(metrics.var_90_daily),
                "var_95_daily": _nan_to_none(metrics.var_95_daily),
                "var_99_daily": _nan_to_none(metrics.var_99_daily),
                "cvar_95_daily": _nan_to_none(metrics.cvar_95_daily),
                "max_drawdown": _nan_to_none(metrics.max_drawdown),
                "ulcer_index": _nan_to_none(metrics.ulcer_index),
                "downside_deviation": _nan_to_none(metrics.downside_deviation),
                "sharpe_ratio": _nan_to_none(metrics.sharpe_ratio),
                "sortino_ratio": _nan_to_none(metrics.sortino_ratio),
                "lag_1_autocorrelation": _nan_to_none(metrics.lag_1_autocorrelation),
                "lag_2_autocorrelation": _nan_to_none(metrics.lag_2_autocorrelation),
                "lag_3_autocorrelation": _nan_to_none(metrics.lag_3_autocorrelation),
                "lag_4_autocorrelation": _nan_to_none(metrics.lag_4_autocorrelation),
            }
        )
    return out


def _build_section_context(
    bundle: UniverseStatisticsBundle,
) -> dict[str, Any]:
    """Translate a :class:`UniverseStatisticsBundle` into template context.

    The template expects a flat dict of rows / specs rather than
    the typed bundle, matching the shape the (sunset) standalone
    Statistics route used.

    Args:
        bundle: The bundle returned by
            :meth:`StatisticsService.get_universe_statistics`.

    Returns:
        A dict keyed by the template variable names expected by
        ``_partials/statistics_section.html``.
    """
    key_metrics_rows: list[dict[str, Any]] = []
    for name in bundle.investment_names:
        card = bundle.key_metrics.get(name)
        if card is None:
            continue
        spark_spec = build_sparkline_spec(card.sparkline_values)
        key_metrics_rows.append(
            {
                "name": card.investment_name,
                "latest_nav": card.latest_nav,
                "currency": card.currency,
                "annualised_return": card.annualised_return,
                "sharpe_ratio": card.sharpe_ratio,
                "has_sparkline": bool(card.sparkline_values),
                "sparkline_spec": spark_spec,
            }
        )

    has_correlation = (
        not bundle.correlation_matrix.empty and bundle.correlation_matrix.shape[0] >= 2
    )
    correlation_spec: dict[str, Any] = (
        build_correlation_heatmap_spec(bundle.correlation_matrix) if has_correlation else {}
    )

    distribution_rows = _project_distribution_rows(bundle)
    risk_rows = _project_risk_rows_pivot(bundle)

    return {
        "investment_names": bundle.investment_names,
        "key_metrics_rows": key_metrics_rows,
        "has_correlation": has_correlation,
        "correlation_spec": correlation_spec,
        "distribution_rows": distribution_rows,
        "risk_rows": risk_rows,
        "risk_free_rate_pct": bundle.risk_free_rate * 100.0,
        "as_of_date": (bundle.as_of_date.isoformat() if bundle.as_of_date else ""),
    }
