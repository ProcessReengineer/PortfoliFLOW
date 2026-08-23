# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web surface for the market-data live-import schedule (ADR-0093).

A deliberately **small** Admin-area surface (``/admin#market-data``): a
thin CRUD over the tenant's ``market_data_schedule`` row plus a "Refresh
now" action. It is the on-demand trigger of ADR-0093 §"On-demand trigger
shares the same core": the request layer never runs blocking provider
work and never spawns a process — "Refresh now" only sets the schedule
row due (``next_due_at := now``), and the next scheduler tick picks it up:
the built-in tick scheduler (ADR-0117, 60 seconds by default) or, on an
opt-out deployment, the external timer. This keeps the async web layer
clean and consistent with the async-first provider port (ADR-0091).

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
  no process spawn. Returns a "queued for the next tick" confirmation
  carrying the poller below. Owner-gated (ADR-0125 §6) and surface-aware:
  the ``surface`` field selects *only* which confirmation is rendered —
  the Admin panel (default) or the Overview's compact inline line.
* ``GET  /api/market-data/refresh/poll`` — the time-boxed companion of that
  enqueue (ADR-0125 §5, the ADR-0120 pattern): 204 while the run is still
  pending, 286 carrying the re-rendered panel once it has landed, and 286 +
  ``HX-Reswap: none`` when there is nothing left to wait for. Started only
  by a confirmation, never on load, and self-terminating.

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
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import IreneCadenceInvalid
from core.repositories._session import tenant_context
from core.repositories.market_data_schedule_repository import (
    MarketDataScheduleRepository,
)
from core.repositories.user_repository import UserDTO
from services.auth.session import SessionDTO
from services.irene.scheduling import compute_next_due_at
from web.auth import require_session, verify_csrf
from web.errors import user_safe_error
from web.htmx_poll import (
    POLL_HORIZON,
    POLL_STOP_STATUS,
    parse_poll_since,
    poll_stop,
)
from web.permissions import require_role

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

# Which confirmation :func:`refresh_now` renders (ADR-0125 §6). The enqueue
# itself is one endpoint by decision — this field selects a *partial*, never
# a behaviour — so an unknown value degrades to the default rather than
# rejecting a harmless action.
_SURFACES: tuple[str, ...] = ("admin", "overview")
_DEFAULT_SURFACE: str = "admin"


def _display_zone(name: str | None) -> ZoneInfo:
    """Resolve a schedule's IANA timezone name, falling back to UTC.

    Deliberately local rather than shared with the Watch Desk's
    ``_resolve_zone``: that one carries a Watch-Desk-specific log line and a
    ``(zone, is_utc_fallback)`` pair its tile stamps need. Here the fallback
    needs no separate flag — every stamp this module renders carries ``%Z``,
    so a UTC substitute names itself and can never read as local time.
    """
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "market-data panel: unknown schedule timezone %r; rendering times in UTC.",
                name,
            )
    return ZoneInfo("UTC")


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
    poll_since: str | None = None,
) -> dict[str, Any]:
    """Build the market-data schedule panel context from a schedule DTO.

    Shared by the section render and the save/refresh responses so the
    panel round-trips identically. ``schedule`` is ``None`` only for a
    tenant provisioned before slice 5 that was never backfilled.

    Args:
        schedule: The tenant's schedule DTO, or ``None``.
        csrf_token: The session-bound CSRF token.
        saved: Render the "Schedule saved." flash.
        error: Inline error after a rejected save.
        refresh_message: Flash after a Refresh-now action or a landed run.
        poll_since: URL-encoded enqueue instant. Set **only** by the
            refresh-now confirmation (ADR-0125 §5): the template starts the
            poller when it is present, so the section render and every other
            response leave it ``None`` and no page polls on load.
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
        "poll_since": poll_since,
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
    _owner: UserDTO = Depends(require_role("owner")),
) -> HTMLResponse:
    """Persist the tenant's market-data schedule and return the panel.

    Recomputes ``next_due_at`` via
    :func:`services.irene.scheduling.compute_next_due_at` (the single source
    of cadence arithmetic) before upserting through
    :meth:`MarketDataScheduleRepository.upsert_tenant_schedule`. An
    unsupported cadence or unknown timezone returns a 422 with the panel
    re-rendered carrying the error, never a 500.

    **Owner-gated** (ADR-0126). The schedule is a tenant-level resource, not
    a per-user preference: its cadence, anchor hour, timezone and enabled
    flag govern how often the whole tenant spends its provider budget. The
    gate here is the authoritative one — the template conditional that hides
    the section from a member is cosmetic mirroring — so a member who posts
    directly gets the same 403 (``insufficient role``) as
    :func:`refresh_now`, the module's other mutating route.
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
    surface: str = Form(_DEFAULT_SURFACE),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
    _owner: UserDTO = Depends(require_role("owner")),
) -> HTMLResponse:
    """Bring the tenant's market-data schedule due now (ADR-0093 §0.3).

    Sets ``next_due_at := now`` via
    :meth:`MarketDataScheduleRepository.enqueue_due_now`; the next scheduler
    tick claims the tenant and refreshes it — the built-in scheduler
    (ADR-0117, 60 seconds by default) or the external timer on an opt-out
    deployment. It runs **no** provider work and spawns **no** process — the
    async web layer stays clean.

    **Owner-gated** (ADR-0125 §6). A refresh is harmless in itself, but it is
    a tenant-level action — it moves a shared cursor and spends the tenant's
    provider budget — and the affordance is owner-only on every surface that
    offers it. The gate is therefore enforced here as well as in the
    templates: a member who posts directly gets the same 403 (``insufficient
    role``) as the other owner-gated routes, via
    :func:`web.permissions.require_role`. The Admin Market Data section is
    itself owner-only under ADR-0126, so the control is offered only where
    the post will succeed.

    Args:
        request: The FastAPI request.
        surface: Which confirmation to render — ``admin`` (the whole panel,
            the default) or ``overview`` (the compact inline line the
            Front-Office freshness line swaps itself with). It selects a
            *partial only*; an unrecognised value falls back to ``admin``
            rather than 4xx-ing an action that is otherwise harmless.
        session: The authenticated session.
        _csrf: CSRF guard.
        _owner: The owner gate (ADR-0125 §6).

    Returns:
        The re-rendered panel, or the Overview's compact confirmation.

    "Due now" only takes effect on an **enabled** schedule (the tick's due
    read gates on ``enabled AND next_due_at <= now()``). A disabled or
    unconfigured schedule returns an "enable first" notice, is not moved,
    and — having enqueued nothing — starts no poller.
    """
    chosen_surface = surface if surface in _SURFACES else _DEFAULT_SURFACE
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
        message = "Refresh queued — it runs on the next tick (typically within a minute)."
    elif schedule is None:
        message = "No schedule configured yet — save a cadence first."
    else:
        message = "Enable the schedule first, then request a refresh."

    # The poller's ``since`` is **this route's** instant, never the client
    # clock, which may sit minutes off and would make the done condition fire
    # on the first tick or never. Percent-encoded because an ISO 8601 offset
    # carries a "+" — read as a space in a query string, which would make the
    # stamp unparseable and stop the poll immediately. ``None`` when nothing
    # was enqueued: there is nothing to wait for.
    poll_since = quote(now.isoformat(), safe="") if can_queue else None

    logger.info(
        "market-data refresh-now: tenant=%s user=%s surface=%s queued=%s",
        session.tenant_id,
        session.user_id,
        chosen_surface,
        can_queue,
    )
    if chosen_surface == "overview":
        return cast(
            HTMLResponse,
            _templates(request).TemplateResponse(
                request,
                "_partials/overview_refresh_result.html",
                {
                    "queued": can_queue,
                    "message": message,
                    "poll_since": poll_since,
                },
            ),
        )
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/market_data_panel.html",
            _panel_context(
                schedule,
                session.csrf_token,
                refresh_message=message,
                poll_since=poll_since,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Refresh poll — the post-enqueue feedback, time-boxed and self-terminating
# ---------------------------------------------------------------------------


@router.get("/api/market-data/refresh/poll", response_class=HTMLResponse)
async def poll_refresh(
    request: Request,
    since: str | None = None,
    session: SessionDTO = Depends(require_session),
) -> Response:
    """Answer "has a refresh landed since ``since``" for the Admin poller.

    "Refresh now" only *enqueues* (ADR-0093): the run happens a tick later
    in the scheduler (ADR-0117), and the page that fired the enqueue has no
    way to learn that it did. This endpoint closes that gap without a push
    channel, one-for-one with the Watch Desk's briefing poll (ADR-0125 §5
    adopting ADR-0120) — the confirmation partial starts a 15-second HTMX
    poll against it, and the poll ends itself:

    * **landed** — ``last_run_at >= since``: 286 carrying the re-rendered
      panel, stamped with when the run landed. 286 cancels the poll, and the
      container's ``innerHTML`` swap replaces the panel — and with it the
      poller — so no second poller can survive.
    * **pending** — the schedule exists and no run has landed yet: 204,
      which HTMX does not swap, so the panel stands and the poll continues.
      This is the branch that runs ~4 times a minute, and it is one indexed
      row read: no provider work, no render.
    * **stop, no swap** — 286 with an empty body and ``HX-Reswap: none``
      when there is nothing left to wait for: an unusable ``since``, no
      schedule row, or a ``since`` older than :data:`POLL_HORIZON`. The
      horizon is what caps a tab left open on a run that never happens; the
      client carries no timeout of its own.

    Read-only throughout, and deliberately on ``require_session`` rather
    than ``require_authenticated_session`` for the reason ADR-0120 gives: a
    poll must not keep alive a session the operator has stopped using. A
    session that expires mid-poll gets that dependency's 401 +
    ``HX-Redirect``, which navigates the tab to ``/login``.

    That is also why this endpoint stays **ungated** while the rest of the
    Market Data surface became owner-only — the documented exception in
    ADR-0126 Decision 4: gating it through :func:`web.permissions.require_role`
    would route every poll through ``require_authenticated_session`` and its
    idle-timer touch, so an abandoned tab's poller would keep the session
    alive. What a member could read by calling the URL by hand is
    configuration cosmetics (cadence, enabled flag, last-run stamp), not
    secrets, and nothing here mutates.

    Args:
        request: The FastAPI request.
        since: The enqueue instant, ISO 8601 and timezone-aware, as written
            into the poller's URL by :func:`refresh_now`.
        session: The authenticated session.

    Returns:
        286 (with or without a body) or 204, per the branches above.
    """
    parsed_since = parse_poll_since(since)
    if parsed_since is None:
        return poll_stop()

    engine = _engine(request)
    now = _now()
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        schedule = await MarketDataScheduleRepository(db).get_for_tenant()
        if schedule is None:
            # The row a refresh would stamp is gone — nothing can land.
            return poll_stop()

        last_run_at = schedule.last_run_at
        if last_run_at is None or last_run_at < parsed_since:
            if now - parsed_since > POLL_HORIZON:
                return poll_stop()
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        # Stamped in the schedule's own timezone, with ``%Z`` so a UTC
        # fallback names itself rather than reading as local time.
        stamp = last_run_at.astimezone(_display_zone(schedule.timezone)).strftime("%H:%M %Z")

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            "_partials/market_data_panel.html",
            _panel_context(
                schedule,
                session.csrf_token,
                refresh_message=f"Refreshed at {stamp}.",
            ),
            status_code=POLL_STOP_STATUS,
        ),
    )
