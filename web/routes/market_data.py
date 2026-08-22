# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web surface for the market-data live-import schedule (ADR-0093).

A deliberately **small** Admin-area surface (``/admin#market-data``): a
thin CRUD over the tenant's ``market_data_schedule`` row plus a "Refresh
now" action. It is the on-demand trigger of ADR-0093 §"On-demand trigger
shares the same core": the request layer never runs blocking provider
work and never spawns a process — "Refresh now" only sets the schedule
row due (``next_due_at := now``), and the systemd timer's next firing
picks it up. This keeps the async web layer clean and consistent with the
async-first provider port (ADR-0091).

Endpoints
---------

* ``POST /api/market-data/schedule`` — persist enable/disable + cadence
  (``cadence`` / ``preferred_hour`` / ``timezone`` / ``enabled``),
  recomputing ``next_due_at`` via
  :func:`services.irene.scheduling.compute_next_due_at` (the shared cadence
  arithmetic — never duplicated). Returns the refreshed panel fragment.
  The panel offers ``every_15m`` / ``every_30m`` / ``hourly`` / ``daily``
  (ADR-0125 §2) — this surface's own list, not the shared vocabulary in
  full, and a fresh tenant is seeded ``every_15m`` (ADR-0125 §3).
* ``POST /api/market-data/refresh-now`` — set the tenant schedule due now
  (:meth:`MarketDataScheduleRepository.enqueue_due_now`). No provider work,
  no process spawn. Returns a small "queued for the next tick" confirmation.

The section render is server-side on ``/admin`` load via
:func:`load_market_data_section_context` (imported by ``web/routes/areas.py``).

This module imports **only** the schedule repository — never the refresh
core (``services.investments.live_refresh``) or any provider adapter — so
the web layer holds no provider dependency (ADR-0093 verification gate).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import IreneCadenceInvalid
from core.repositories._session import tenant_context
from core.repositories.market_data_schedule_repository import (
    MarketDataScheduleRepository,
)
from services.auth.session import SessionDTO
from services.irene.scheduling import compute_next_due_at
from web.auth import require_session, verify_csrf
from web.errors import user_safe_error

logger = logging.getLogger(__name__)
router = APIRouter()


# Cadence vocabulary v2 offered in the panel (ADR-0125 §1/§2), ordered
# finest-first the way an interval picker reads. A **separate** tuple by
# decision — deliberately not derived from ``_SUPPORTED_CADENCES``: which
# cadences a domain offers is that domain's own call, and the Watch Desk
# has taken a different one (ADR-0125 §2 withholds the sub-hourly members
# there, an LLM-cost decision that area has not taken). ``every_5m`` is
# explicitly not offered.
#
# This tuple is an *offer*, never a second validator: the sole check of a
# submitted cadence stays :func:`services.irene.scheduling.compute_next_due_at`.
# Pinned by ``tests/web/test_market_data_cadence_choices.py``.
_CADENCE_CHOICES: tuple[str, ...] = ("every_15m", "every_30m", "hourly", "daily")

# Display labels for the offered cadences — same pattern as the Watch Desk
# router's ``CADENCE_LABELS``, and for the same reason: a ``|capitalize`` in
# the template would render "Every_15m" (ADR-0119 §3). Defined locally, not
# imported from the Watch Desk router, because the two surfaces offer
# different vocabularies by decision (ADR-0125 §2) and must stay free to
# diverge further.
CADENCE_LABELS: dict[str, str] = {
    "every_15m": "Every 15 minutes",
    "every_30m": "Every 30 minutes",
    "hourly": "Every hour",
    "daily": "Daily",
}


def cadence_label(cadence: str) -> str:
    """Return the display label for a cadence value.

    Falls back to a ``|capitalize``-equivalent of the raw value on any
    vocabulary drift, so an unknown cadence degrades to a legible label
    rather than a ``KeyError``.
    """
    return CADENCE_LABELS.get(cadence, cadence.capitalize())


# Panel defaults for a tenant with no schedule row yet. Every tenant is
# seeded a (disabled) row, so this is a defensive fallback only — it mirrors
# the seed (``every_15m`` anchored at hour 0, ADR-0125 §3) so the panel can
# never show a defensive default that disagrees with what provisioning
# actually wrote.
_DEFAULT_TIMEZONE: str = "Europe/Berlin"
_DEFAULT_PREFERRED_HOUR: int = 0


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


def _now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _panel_context(
    schedule: Any,
    csrf_token: str,
    *,
    saved: bool = False,
    error: str | None = None,
    refresh_message: str | None = None,
) -> dict[str, Any]:
    """Build the market-data schedule panel context from a schedule DTO.

    Shared by the section render and the save/refresh responses so the
    panel round-trips identically. ``schedule`` is ``None`` only for a
    tenant provisioned before slice 5 that was never backfilled.
    """
    if schedule is None:
        current = {
            "cadence": "every_15m",
            "preferred_hour": _DEFAULT_PREFERRED_HOUR,
            "timezone": _DEFAULT_TIMEZONE,
            "enabled": False,
            "next_due_at": None,
            "last_run_at": None,
            "configured": False,
        }
    else:
        current = {
            "cadence": schedule.cadence,
            "preferred_hour": (
                schedule.preferred_hour
                if schedule.preferred_hour is not None
                else _DEFAULT_PREFERRED_HOUR
            ),
            "timezone": schedule.timezone,
            "enabled": schedule.enabled,
            "next_due_at": schedule.next_due_at,
            "last_run_at": schedule.last_run_at,
            "configured": True,
        }
    return {
        "csrf_token": csrf_token,
        "cadence_choices": _CADENCE_CHOICES,
        # Built through the helper so every rendered choice is guaranteed a
        # label — the template can index this map without an Undefined.
        "cadence_labels": {choice: cadence_label(choice) for choice in _CADENCE_CHOICES},
        "hours": list(range(24)),
        "current": current,
        "schedule_saved": saved,
        "schedule_error": error,
        "refresh_message": refresh_message,
    }


async def load_market_data_section_context(request: Request, session: SessionDTO) -> dict[str, Any]:
    """Return the context for the embedded Market Data section (``/admin``).

    Reads the tenant's schedule row so the panel renders its current state
    server-side on initial page load.

    Args:
        request: Active FastAPI request (used to reach the DB engine).
        session: Authenticated session DTO.

    Returns:
        The context keys ``_partials/market_data_section.html`` needs.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        schedule = await MarketDataScheduleRepository(db).get_for_tenant()
    return _panel_context(schedule, session.csrf_token)


@router.post("/api/market-data/schedule", response_class=HTMLResponse)
async def save_schedule(
    request: Request,
    cadence: str = Form(...),
    preferred_hour: int = Form(...),
    timezone_name: str = Form(..., alias="timezone"),
    enabled: str | None = Form(None),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Persist the tenant's market-data schedule and return the panel.

    Recomputes ``next_due_at`` via
    :func:`services.irene.scheduling.compute_next_due_at` (the single source
    of cadence arithmetic) before upserting through
    :meth:`MarketDataScheduleRepository.upsert_tenant_schedule`. An
    unsupported cadence or unknown timezone returns a 422 with the panel
    re-rendered carrying the error, never a 500.
    """
    enabled_bool = enabled is not None
    cleaned_tz = timezone_name.strip()
    now = _now()

    # Validate + compute next_due_at before any DB write. compute_next_due_at
    # raises IreneCadenceInvalid for a bad cadence (the shared arithmetic is
    # the only validator; the panel's own offer is :data:`_CADENCE_CHOICES`);
    # ZoneInfo raises for an unknown timezone.
    try:
        ZoneInfo(cleaned_tz)
        next_due_at = compute_next_due_at(now, cadence, preferred_hour, cleaned_tz)
    except (IreneCadenceInvalid, ZoneInfoNotFoundError, ValueError) as exc:
        logger.info(
            "market-data schedule: rejected (cadence=%r tz=%r): %s",
            cadence,
            cleaned_tz,
            exc,
        )
        user_msg, _error_id = user_safe_error(exc)
        context = _panel_context(None, session.csrf_token, error=user_msg)
        context["current"].update(
            {
                "cadence": cadence,
                "preferred_hour": preferred_hour,
                "timezone": cleaned_tz,
                "enabled": enabled_bool,
            }
        )
        return cast(
            HTMLResponse,
            _templates(request).TemplateResponse(
                request,
                "_partials/market_data_panel.html",
                context,
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            ),
        )

    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        schedule = await MarketDataScheduleRepository(db).upsert_tenant_schedule(
            cadence=cadence,
            preferred_hour=preferred_hour,
            timezone=cleaned_tz,
            enabled=enabled_bool,
            next_due_at=next_due_at,
        )

    logger.info(
        "market-data schedule: tenant=%s user=%s cadence=%s hour=%s tz=%s "
        "enabled=%s next_due_at=%s",
        session.tenant_id,
        session.user_id,
        cadence,
        preferred_hour,
        cleaned_tz,
        enabled_bool,
        next_due_at.isoformat(),
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/market_data_panel.html",
            _panel_context(schedule, session.csrf_token, saved=True),
        ),
    )


@router.post("/api/market-data/refresh-now", response_class=HTMLResponse)
async def refresh_now(
    request: Request,
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Bring the tenant's market-data schedule due now (ADR-0093 §0.3).

    Sets ``next_due_at := now`` via
    :meth:`MarketDataScheduleRepository.enqueue_due_now`; the next systemd
    tick claims the tenant and refreshes it. It runs **no** provider work
    and spawns **no** process — the async web layer stays clean.

    "Due now" only takes effect on an **enabled** schedule (the tick's due
    read gates on ``enabled AND next_due_at <= now()``). A disabled or
    unconfigured schedule returns a "enable first" notice and is not moved.
    On success the panel is re-rendered with a "queued for the next tick"
    message.
    """
    engine = _engine(request)
    now = _now()
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        repo = MarketDataScheduleRepository(db)
        schedule = await repo.get_for_tenant()
        can_queue = schedule is not None and schedule.enabled
        if can_queue:
            await repo.enqueue_due_now(now=now)
            schedule = await repo.get_for_tenant()

    if can_queue:
        message = "Refresh queued — it will run on the next tick."
    elif schedule is None:
        message = "No schedule configured yet — save a cadence first."
    else:
        message = "Enable the schedule first, then request a refresh."

    logger.info(
        "market-data refresh-now: tenant=%s user=%s queued=%s",
        session.tenant_id,
        session.user_id,
        can_queue,
    )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/market_data_panel.html",
            _panel_context(schedule, session.csrf_token, refresh_message=message),
        ),
    )
