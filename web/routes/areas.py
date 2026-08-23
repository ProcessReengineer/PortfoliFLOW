# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-area page routes — Phase 6 Block 1, sub-stream 6F-1.

One GET endpoint per area in ``modules/module_registry.py``, in the
``web/shell.py`` ``_AREAS`` sidebar order:

    * ``GET /front-office``
    * ``GET /back-office``
    * ``GET /watch-desk`` (ADR-0089)
    * ``GET /cases`` (ADR-0107)
    * ``GET /planning-desk`` (ADR-0104 §6)
    * ``GET /investor-communication``
    * ``GET /assistants``
    * ``GET /admin``

Each handler renders an area template that contains the area's
modules as sequential ``<section id="{slug}">`` blocks. For 6F-1 the
sections are placeholder shells with stable HTML ids; 6F-2 layers
sticky headers and a section indicator on top, 6F-3 polishes KPI
cards and tile aesthetics inside the sections, and 6F-4 re-renders
the Charts module as a universe-wide briefing surface.

The handlers also branch on the ``HX-Request`` header (via the
``is_htmx_request`` dependency on ``web/shell.py``): HTMX area
switches receive a partial fragment plus an out-of-band sidebar
update; direct navigation receives the full layout. Commit 4 of
6F-1 wires the partial branch — this commit ships the routes and
templates with the full-layout branch only; HTMX requests fall
through to the same render and still work because base.html is
emitted in full.
"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.repositories._session import tenant_context
from core.repositories.tenant_repository import TenantRepository
from core.repositories.user_repository import UserRepository
from services.auth.session import SessionDTO, SessionRepository
from web.auth import require_session
from web.routes.chat import (
    _ai_core,
    _resolve_voice_enabled,
    resolve_active_brief_banner,
)
from web.routes.data_import import load_data_import_section_context
from web.routes.market_data import load_market_data_section_context
from web.shell import is_htmx_request, section_index_for

logger = logging.getLogger(__name__)
router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


async def _resolve_user_email(request: Request, session: SessionDTO) -> str:
    """Look up the authenticated user's email for the shell context.

    The area pages do not otherwise touch the database, but the
    sidebar's footer and the optional sign-out button render the
    email. Looking it up once per area render is cheap; falling
    through to a placeholder when the engine is unavailable keeps
    the page rendering even in degraded modes.

    The loaded :class:`UserDTO` is stashed on ``request.state.user``
    so the shell processor can derive ``show_super_admin_link``
    without a second round-trip — relevant for super-admins who
    happen to browse to a tenant-area URL.

    The active tenant's display name is resolved in the same tenant
    context and stashed on ``request.state.tenant_name`` (ADR-0068) via
    the ``tenant_self_visibility`` RLS policy (ADR-0035, no platform
    read needed). The shell status bar reads it from there, so every
    area shows the real tenant name instead of the placeholder; the
    Front Office welcome header reads the same value. The lookup is
    shared here because every area handler calls this helper.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return ""
    try:
        async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
            await SessionRepository(db).touch_throttled(session.id)
            user = await UserRepository(db).get_by_id(session.user_id)
            if user is not None:
                request.state.user = user
            request.state.tenant_name = await TenantRepository(db).get_current_name()
            return user.email if user is not None else ""
    except Exception:  # noqa: BLE001 — degraded mode is intentional
        logger.warning("area-render: user-email lookup failed", exc_info=True)
        return ""


def _derive_first_name(display_name: str | None, email: str) -> str | None:
    """Derive a greeting first name from a display name or email (ADR-0068).

    Order of preference:

    1. The first whitespace-delimited token of ``display_name``.
    2. A capitalized email local-part — but only when it reads as a
       single name (purely alphabetic: no dots, digits, hyphens or
       ``+`` tags). Email is deliberately *not* the primary source: a
       wrong name on the very first line is worse than no name, and
       ``s.surname@`` / ``info@`` would misfire.
    3. ``None`` — the header then greets without a name.

    A raw or mangled email address is never returned.
    """
    if display_name and display_name.strip():
        return display_name.split()[0]
    local = email.split("@", 1)[0] if email else ""
    if local.isalpha():
        return local[:1].upper() + local[1:]
    return None


_AREA_BODY_PARTIALS: dict[str, str] = {
    "front_office": "_partials/areas/_front_office_body.html",
    "watch_desk": "_partials/areas/_watch_desk_body.html",
    "cases": "_partials/areas/_cases_body.html",
    "planning_desk": "_partials/areas/_planning_desk_body.html",
    "back_office": "_partials/areas/_back_office_body.html",
    "admin": "_partials/areas/_admin_body.html",
    "investor_communication": "_partials/areas/_investor_communication_body.html",
    "assistants": "_partials/areas/_assistants_body.html",
}


def _render_area(
    request: Request,
    area_slug: str,
    template_name: str,
    user_email: str,
    csrf_token: str,
    htmx: bool,
    extra_context: dict | None = None,
) -> HTMLResponse:
    """Render an area template, branching on the HTMX flag.

    Direct navigation (``htmx=False``) renders the full layout via
    the area template (which extends ``base.html``). HTMX area
    switches (``htmx=True``) skip the base template and emit only
    the area's body partial plus an out-of-band sidebar fragment
    that updates the active-state highlighting in place.

    HTMX scans the response for any element bearing ``hx-swap-oob``
    and pulls it out of the main swap into its own targeted swap.

    Args:
        extra_context: Optional dict of area-specific context merged
            into the rendered template's namespace. Used by the Admin
            route to pre-load the Data Import section's Stage 1
            content server-side (sub-stream 6F-5; the surface moved
            from Front Office to Admin in the 6F-3 mid-polish).
    """
    templates = _templates(request)
    context = {
        "active_area": area_slug,
        "user_email": user_email,
        "csrf_token": csrf_token,
        "section_index": section_index_for(area_slug),
    }
    if extra_context:
        context.update(extra_context)
    if htmx:
        body_partial = _AREA_BODY_PARTIALS[area_slug]
        return cast(
            HTMLResponse,
            templates.TemplateResponse(
                request,
                "_partials/area_fragment.html",
                {**context, "area_body_partial": body_partial},
            ),
        )

    return cast(
        HTMLResponse,
        templates.TemplateResponse(
            request,
            template_name,
            context,
        ),
    )


@router.get("/front-office", response_class=HTMLResponse)
async def front_office_view(
    request: Request,
    session: SessionDTO = Depends(require_session),
    htmx: bool = Depends(is_htmx_request),
) -> HTMLResponse:
    """Render the Front Office area page.

    Prepends a server-rendered welcome header (ADR-0068):
    ``Welcome back, {first name} — {tenant} portfolio``. The first name
    derives from the authenticated user's ``display_name``; the tenant
    name is the active tenant's own ``tenants.name``, resolved and
    stashed on ``request.state.tenant_name`` by
    :func:`_resolve_user_email`. Both degrade gracefully — a missing
    first name or tenant name simply drops that clause rather than
    failing the render.
    """
    user_email = await _resolve_user_email(request, session)
    user = getattr(request.state, "user", None)
    first_name = _derive_first_name(getattr(user, "display_name", None), user_email)
    tenant_name = getattr(request.state, "tenant_name", None)
    return _render_area(
        request,
        area_slug="front_office",
        template_name="areas/front_office.html",
        user_email=user_email,
        csrf_token=session.csrf_token,
        htmx=htmx,
        extra_context={
            "first_name": first_name,
            "tenant_name": tenant_name,
        },
    )


@router.get("/watch-desk", response_class=HTMLResponse)
async def watch_desk_view(
    request: Request,
    session: SessionDTO = Depends(require_session),
    htmx: bool = Depends(is_htmx_request),
) -> HTMLResponse:
    """Render the Watch Desk area page (ADR-0089).

    The sixth top-level Area. Its three Sections (Briefing, Journal,
    Calibration) lazy-load their bodies over HTMX, mirroring the SAA / Limits
    pattern, so this handler stays a thin shell render with no DB access —
    exactly like the other area handlers. The fourth Section, the Scenarios
    placeholder, retired with ADR-0104 §8; Feature #034 re-anchors on the
    Planning Desk.
    """
    user_email = await _resolve_user_email(request, session)
    return _render_area(
        request,
        area_slug="watch_desk",
        template_name="areas/watch_desk.html",
        user_email=user_email,
        csrf_token=session.csrf_token,
        htmx=htmx,
    )


@router.get("/cases", response_class=HTMLResponse)
async def cases_view(
    request: Request,
    session: SessionDTO = Depends(require_session),
    htmx: bool = Depends(is_htmx_request),
) -> HTMLResponse:
    """Render the Cases area page (ADR-0107).

    The eighth top-level Area — open questions about the portfolio worked to
    a documented close. Its three Sections (Open cases, Recently closed,
    Archive) lazy-load their bodies over HTMX, mirroring the Watch Desk
    pattern, so this handler stays a thin shell render with no DB access. The
    case detail view (timeline + composers) is C3; the list rows this
    sub-strand renders carry no detail link yet.
    """
    user_email = await _resolve_user_email(request, session)
    return _render_area(
        request,
        area_slug="cases",
        template_name="areas/cases.html",
        user_email=user_email,
        csrf_token=session.csrf_token,
        htmx=htmx,
    )


@router.get("/planning-desk", response_class=HTMLResponse)
async def planning_desk_view(
    request: Request,
    session: SessionDTO = Depends(require_session),
    htmx: bool = Depends(is_htmx_request),
) -> HTMLResponse:
    """Render the Planning Desk area page (ADR-0104 §6).

    The seventh top-level Area — where the Watch Desk watches and
    raises, the Planning Desk projects and simulates. Its two Sections (Cash
    Flow Planning, Scenario Analysis) render static placeholder panels at
    this registration stage, so the handler is a thin shell render with no DB
    access. The sticky parameter strip and the lenses themselves land in the
    later steps of the strand.
    """
    user_email = await _resolve_user_email(request, session)
    return _render_area(
        request,
        area_slug="planning_desk",
        template_name="areas/planning_desk.html",
        user_email=user_email,
        csrf_token=session.csrf_token,
        htmx=htmx,
    )


@router.get("/back-office", response_class=HTMLResponse)
async def back_office_view(
    request: Request,
    session: SessionDTO = Depends(require_session),
    htmx: bool = Depends(is_htmx_request),
) -> HTMLResponse:
    """Render the Back Office area page."""
    user_email = await _resolve_user_email(request, session)
    return _render_area(
        request,
        area_slug="back_office",
        template_name="areas/back_office.html",
        user_email=user_email,
        csrf_token=session.csrf_token,
        htmx=htmx,
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_view(
    request: Request,
    session: SessionDTO = Depends(require_session),
    htmx: bool = Depends(is_htmx_request),
) -> HTMLResponse:
    """Render the Admin area page.

    Pre-loads the Data Import section's Stage 1 context
    (``recent_uploads``, ``uploader_emails``, ``max_upload_mb``) so
    the upload form and recent-uploads table render server-side on
    initial load. The Data Import surface moved here from Front
    Office in the 6F-3 mid-polish sub-stream.

    The Providers & Credentials section (ADR-0112 §6) adds nothing
    here: it is lazy, fetched by its own endpoint on first visibility,
    so its state is always read fresh rather than pre-rendered with the
    page. It replaced the eagerly pre-rendered ADR-0052 AI Settings
    section, which had to be server-rendered here because it mirrored
    in-process singleton state.

    The Users section (ADR-0121 §6) is lazy in the same way and adds one
    flag: ``is_tenant_owner``, derived from the :class:`UserDTO`
    :func:`_resolve_user_email` already stashed on ``request.state``, so
    the shell can omit the section for a member without a second lookup.
    It is cosmetic mirroring — ``web/routes/tenant_users.py`` carries the
    authoritative gate on every one of its endpoints — and defaults to
    ``False`` when the user could not be loaded, which is the safe way
    round for a degraded render.

    The Market Data section is owner-only too (ADR-0126), and it is *not*
    lazy — it pre-renders from a schedule read. So the same flag also
    decides whether that read happens at all: a member's ``/admin`` no
    longer pays a DB round-trip for a section the shell will not render.
    The context keys go missing in that case, harmlessly — their only
    consumer is the include inside the owner conditional.
    """
    user_email = await _resolve_user_email(request, session)
    user = getattr(request.state, "user", None)
    is_owner = user is not None and user.has_role("owner")
    data_import_ctx = await load_data_import_section_context(request, session)
    market_data_ctx = await load_market_data_section_context(request, session) if is_owner else {}
    return _render_area(
        request,
        area_slug="admin",
        template_name="areas/admin.html",
        user_email=user_email,
        csrf_token=session.csrf_token,
        htmx=htmx,
        extra_context={
            **data_import_ctx,
            **market_data_ctx,
            "is_tenant_owner": is_owner,
        },
    )


@router.get("/investor-communication", response_class=HTMLResponse)
async def investor_communication_view(
    request: Request,
    session: SessionDTO = Depends(require_session),
    htmx: bool = Depends(is_htmx_request),
) -> HTMLResponse:
    """Render the Investor Communication area page."""
    user_email = await _resolve_user_email(request, session)
    return _render_area(
        request,
        area_slug="investor_communication",
        template_name="areas/investor_communication.html",
        user_email=user_email,
        csrf_token=session.csrf_token,
        htmx=htmx,
    )


@router.get("/assistants", response_class=HTMLResponse)
async def assistants_view(
    request: Request,
    case: str | None = None,
    session: SessionDTO = Depends(require_session),
    htmx: bool = Depends(is_htmx_request),
) -> HTMLResponse:
    """Render the Assistants area page with Shirley embedded.

    ADR-0051 folded the standalone ``GET /chat`` page into the
    Assistants area's ``shirley`` section. The active model id is
    threaded into the section context so the embedded shell can show
    the "Model: …" status line previously rendered by ``chat.html``.

    ``voice_enabled`` is resolved **per tenant** (ADR-0118 §5): the one
    template-context site for the voice affordances asks the credential
    façade's ``voice.enabled`` chain rather than a process-global switch,
    at the cost of one session-scoped read per Assistants render.

    A ``?case=<id>`` marker (ADR-0107 C6) sets the session's case-brief
    stash when it names an open case, and — whether freshly set or
    carried from a previous turn — the "Consulting for CASE-NNNN"
    banner is rendered from the current stash, validated fresh.
    Malformed, unknown or closed markers are dropped silently (binding
    decision 5); the stash and banner logic lives in ``chat.py``.
    """
    user_email = await _resolve_user_email(request, session)
    model_id = _ai_core(request).get_model() or None
    brief_banner = await resolve_active_brief_banner(request, session, case)
    return _render_area(
        request,
        area_slug="assistants",
        template_name="areas/assistants.html",
        user_email=user_email,
        csrf_token=session.csrf_token,
        htmx=htmx,
        extra_context={
            "model_id": model_id,
            "voice_enabled": await _resolve_voice_enabled(request, session),
            "brief_banner": brief_banner,
        },
    )
