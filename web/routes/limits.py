# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web Investment-Limits surface — read-only coverage section.

The Investment-Limits module is embedded in ``/back-office#limits``
(Kickoff #3b). The section renders a four-card KPI strip, a per-
family status table for the most recent month-end Stichtag, a small-
multiples Plotly coverage chart per family, and a collapsible limit-
set history browser that lazy-loads per-set detail via HTMX.

Endpoints:

* ``GET /api/back-office/limits/section`` — Returns the section body
  (filter form, KPI strip, SAA / AnlV tables and charts, history
  browser). Accepts optional ``from_date`` and ``to_date`` ISO
  ``YYYY-MM-DD`` query parameters; invalid values are silently
  ignored. Engine exceptions are caught and routed to the
  error-state partial.
* ``GET /api/back-office/limits/sets/{set_id}/limits`` — Returns the
  history-browser detail partial for a single limit set: metadata
  plus per-class ceilings. Cross-tenant ids report 404 (RLS hides
  the row).
"""

from __future__ import annotations

import logging
from datetime import date as _date
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.exceptions import (
    CoverageInputMissing,
    CoverageInputOutOfRange,
    LimitSetNotEffective,
    MissingFxRateError,
)
from core.repositories._session import tenant_context
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_nav_repository import (
    InvestmentNavRepository,
)
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.limits_repository import LimitsRepository
from core.repositories.tenant_repository import TenantRepository
from services.analytics.limit_coverage import FamilyCoverageResult
from services.auth.session import SessionDTO, SessionRepository
from services.chart_specs import build_limits_coverage_spec
from services.limits import LimitsCoverageBundle, LimitsCoverageService
from web.auth import require_session

logger = logging.getLogger(__name__)
router = APIRouter()

_SAA: str = "saa"
_ANLV: str = "anlv"


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


def _build_service(db_session: AsyncSession) -> LimitsCoverageService:
    return LimitsCoverageService(
        investments=InvestmentRepository(db_session),
        navs=InvestmentNavRepository(db_session),
        limits=LimitsRepository(db_session),
        asset_classes=AssetClassRepository(db_session),
        tenants=TenantRepository(db_session),
        fx_rates=FxRateRepository(db_session),
    )


def _parse_date(raw: str | None) -> _date | None:
    """Parse an ISO ``YYYY-MM-DD`` query value; treat junk as ``None``."""
    if not raw:
        return None
    try:
        return _date.fromisoformat(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Status / formatting helpers
# ---------------------------------------------------------------------------


_STATUS_CLASS_MAP: dict[str, str] = {
    "OK": "ok",
    "WARN": "warn",
    "BREACH": "breach",
    "NO_LIMIT": "no-limit",
    "UNALLOCATED": "unallocated",
}


def _fmt_pct(value: Decimal | None) -> str:
    return f"{float(value):.2f}%" if value is not None else "—"


def _fmt_eur(value: Decimal | None) -> str:
    return f"{float(value):,.0f}" if value is not None else "—"


def _to_float_or_none(value: Decimal | None) -> float | None:
    """Convert a Decimal coverage / EUR value to a plain float for JSON.

    Tabulator and the Plotly data path expect plain numbers; Decimal
    instances do not serialise out of ``jsonable_encoder`` without
    a custom handler. Returns ``None`` for ``None`` inputs unchanged.
    """
    return float(value) if value is not None else None


def _coverage_to_fraction(coverage_pct: Decimal | None) -> float | None:
    """Convert the engine's percent-form coverage into a [0,1] fraction.

    The engine emits Coverage as a Decimal in percent form (e.g.
    ``Decimal("67.23")`` for 67.23%). The heatmap bucket math in
    benchmarks/limits is consistently expressed in fractional form,
    so we divide by 100 here to keep the Tabulator-side bucket
    function simple. The fmt string remains percent-formatted for
    the visible cell text.
    """
    return float(coverage_pct) / 100.0 if coverage_pct is not None else None


def _project_table_rows(
    family_result: FamilyCoverageResult,
    latest_as_of_date: _date,
) -> list[dict[str, Any]]:
    """Project the engine's coverage rows for one family into table rows.

    Rows are sorted by ``class_key`` ascending; the ``UNALLOCATED``
    row (when present) is pushed to the bottom so the operator sees
    constrained classes first.
    """
    df = family_result.coverage
    if df.empty:
        return []
    slice_df = df[df["as_of_date"] == pd.Timestamp(latest_as_of_date)]
    if slice_df.empty:
        return []

    constrained: list[dict[str, Any]] = []
    unallocated: list[dict[str, Any]] = []
    for _, row in slice_df.iterrows():
        projected = {
            "class_key": row["class_key"],
            # Display strings (existing) — kept for the Tabulator
            # formatter text output:
            "max_pct_fmt": _fmt_pct(row["max_pct"]),
            "nav_sum_fmt": _fmt_eur(row["nav_sum_eur"]),
            "coverage_pct_fmt": _fmt_pct(row["coverage_pct"]),
            "headroom_fmt": _fmt_eur(row["headroom_eur"]),
            # Raw numeric fields (new) — drive sorting and heatmap
            # bucketing:
            "max_pct": _to_float_or_none(row["max_pct"]),
            "nav_sum_eur": _to_float_or_none(row["nav_sum_eur"]),
            # Coverage in [0, 1] decimal form, e.g. 0.6723 for 67.23%.
            # The engine emits the value in percent form
            # (Decimal "67.23"); convert to fraction for the heatmap
            # bucket math, which uses the 0.90 / 0.70 thresholds
            # defined in ADR-0062 §2.1.
            #
            # Only rows that carry a limit (``max_pct`` present, i.e.
            # status OK / WARN / BREACH) get a fraction. NO_LIMIT and
            # UNALLOCATED rows have no ceiling to compare against, so
            # they surface ``None`` and the heatmap leaves them
            # untinted — the "near a limit" risk gradient is undefined
            # without a limit. (The engine still emits a coverage_pct
            # share for UNALLOCATED, which remains visible as the fmt
            # string; it just does not drive a tint.)
            "coverage_fraction": (
                _coverage_to_fraction(row["coverage_pct"]) if row["max_pct"] is not None else None
            ),
            "headroom_eur": _to_float_or_none(row["headroom_eur"]),
            # Status (existing):
            "status": row["status"],
            "status_class": _STATUS_CLASS_MAP.get(row["status"], "no-limit"),
        }
        if row["status"] == "UNALLOCATED":
            unallocated.append(projected)
        else:
            constrained.append(projected)
    constrained.sort(key=lambda r: r["class_key"])
    return constrained + unallocated


def _project_set_history(
    family_result: FamilyCoverageResult,
    family: str,
) -> list[dict[str, Any]]:
    """Project the engine's ``set_history`` triples into template rows."""
    rows: list[dict[str, Any]] = []
    for effective_from, set_id, label in family_result.set_history:
        rows.append(
            {
                "family": family,
                "set_id": str(set_id),
                "effective_from_iso": effective_from.isoformat(),
                "label": label,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/api/back-office/limits/section",
    response_class=HTMLResponse,
)
async def get_limits_section(
    request: Request,
    from_date: str | None = None,
    to_date: str | None = None,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the Investment Limits section body.

    Lazy-loaded on first visibility via ``hx-trigger="revealed"`` in
    the section lazy-shell. The persistent date-range form posts back
    to this same endpoint and HTMX swaps only the section body.

    Engine exceptions
    (:class:`LimitSetNotEffective`, :class:`CoverageInputMissing`,
    :class:`CoverageInputOutOfRange`) and the ADR-0099 §4 conversion
    error (:class:`MissingFxRateError`, raised when a foreign-currency
    position lacks an FX rate it needs) are caught and rendered through
    a dedicated error-state partial.
    """
    parsed_from = _parse_date(from_date)
    parsed_to = _parse_date(to_date)
    from_input = parsed_from.isoformat() if parsed_from is not None else ""
    to_input = parsed_to.isoformat() if parsed_to is not None else ""

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        await SessionRepository(db_session).touch_throttled(session.id)
        service = _build_service(db_session)
        try:
            bundle = await service.get_coverage(
                from_date=parsed_from,
                to_date=parsed_to,
            )
        except (
            LimitSetNotEffective,
            CoverageInputMissing,
            CoverageInputOutOfRange,
            MissingFxRateError,
        ) as exc:
            logger.debug(
                "limits section: engine raised %s (tenant=%s).",
                type(exc).__name__,
                session.tenant_id,
            )
            return cast(
                HTMLResponse,
                _templates(request).TemplateResponse(
                    request,
                    "_partials/limits_error.html",
                    {
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "from_date_input": from_input,
                        "to_date_input": to_input,
                    },
                ),
            )

    context = _build_section_context(
        bundle=bundle,
        from_date_input=from_input,
        to_date_input=to_input,
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/limits_section.html",
            context,
        ),
    )


@router.get(
    "/api/back-office/limits/sets/{set_id}/limits",
    response_class=HTMLResponse,
)
async def get_limit_set_detail(
    request: Request,
    set_id: UUID,
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the history-browser detail partial for one limit set.

    Cross-tenant ids surface as 404 because RLS hides the row from
    the tenant-scoped session — the repository correctly reports
    absence rather than raising.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        await SessionRepository(db_session).touch_throttled(session.id)
        repo = LimitsRepository(db_session)
        set_dto = await repo.get_set_by_id(set_id)
        if set_dto is None:
            raise HTTPException(status_code=404, detail="limit set not found")
        limit_rows = await repo.list_limits(set_id)

    projected = [
        {
            "class_key": row.class_key,
            "max_pct": float(row.max_pct),
        }
        for row in limit_rows
    ]
    context = {
        "set_id": str(set_dto.id),
        "family_upper": set_dto.family.upper(),
        "effective_from_iso": set_dto.effective_from.isoformat(),
        "label": set_dto.label,
        "notes": set_dto.notes,
        "limits": projected,
    }
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/limits_set_detail.html",
            context,
        ),
    )


# ---------------------------------------------------------------------------
# Section context builder
# ---------------------------------------------------------------------------


def _build_section_context(
    *,
    bundle: LimitsCoverageBundle | None,
    from_date_input: str,
    to_date_input: str,
) -> dict[str, Any]:
    """Build the Jinja context dict for the section template.

    Three branches: empty-tenant (bundle is None), range-empty
    (bundle is present but ``latest_as_of_date`` is None), and the
    happy path (bundle + latest Stichtag).
    """
    if bundle is None:
        return {
            "payload": None,
            "kpi_strip": None,
            "latest_as_of_display": "",
            "from_date_input": from_date_input,
            "to_date_input": to_date_input,
            "saa_table_rows": [],
            "saa_chart_spec": None,
            "saa_chart_has_data": False,
            "anlv_table_rows": [],
            "anlv_chart_spec": None,
            "anlv_chart_has_data": False,
            "set_history_saa": [],
            "set_history_anlv": [],
        }

    if bundle.latest_as_of_date is None:
        return {
            "payload": bundle,
            "kpi_strip": bundle.kpi_strip,
            "latest_as_of_display": "",
            "from_date_input": from_date_input or bundle.from_date.isoformat(),
            "to_date_input": to_date_input or bundle.to_date.isoformat(),
            "saa_table_rows": [],
            "saa_chart_spec": None,
            "saa_chart_has_data": False,
            "anlv_table_rows": [],
            "anlv_chart_spec": None,
            "anlv_chart_has_data": False,
            "set_history_saa": _project_set_history(bundle.saa, _SAA),
            "set_history_anlv": _project_set_history(bundle.anlv, _ANLV),
        }

    saa_rows = _project_table_rows(bundle.saa, bundle.latest_as_of_date)
    anlv_rows = _project_table_rows(bundle.anlv, bundle.latest_as_of_date)
    saa_chart = build_limits_coverage_spec(
        bundle.saa.coverage,
        bundle.limit_step_lines[_SAA],
        "SAA",
        to_date=bundle.to_date,
    )
    anlv_chart = build_limits_coverage_spec(
        bundle.anlv.coverage,
        bundle.limit_step_lines[_ANLV],
        "AnlV",
        to_date=bundle.to_date,
    )

    return {
        "payload": bundle,
        "kpi_strip": bundle.kpi_strip,
        "latest_as_of_display": bundle.latest_as_of_date.isoformat(),
        "from_date_input": from_date_input or bundle.from_date.isoformat(),
        "to_date_input": to_date_input or bundle.to_date.isoformat(),
        "saa_table_rows": saa_rows,
        "saa_chart_spec": saa_chart,
        "saa_chart_has_data": bool(saa_rows),
        "anlv_table_rows": anlv_rows,
        "anlv_chart_spec": anlv_chart,
        "anlv_chart_has_data": bool(anlv_rows),
        "set_history_saa": _project_set_history(bundle.saa, _SAA),
        "set_history_anlv": _project_set_history(bundle.anlv, _ANLV),
    }
