# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Login, logout, and the Phase-2b protected landing page.

Implements the auth chain end-to-end:

- ``GET /login`` renders the login form with a pre-session CSRF token.
  An already-authenticated user is redirected to ``/``.
- ``POST /login`` validates pre-session CSRF, calls
  :class:`LocalPasswordAuthBackend.authenticate`, creates a session
  on success, sets the cookie, redirects to ``/``. On failure, re-
  renders the form with a generic "Invalid credentials" message.
- ``POST /logout`` validates session-bound CSRF, deletes the session,
  clears the cookie, redirects to ``/login``.
- ``GET /`` is registered by ``web/routes/chat.py`` and redirects
  authenticated users to ``/front-office`` (sub-stream 6F-1 /
  ADR-0046; the chat surface itself lives at ``/assistants#shirley``
  per ADR-0051).
"""

from __future__ import annotations

import secrets
from typing import cast

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from uuid import UUID

from core.repositories._session import tenant_context
from core.tenant_constants import SYSTEM_TENANT_ID
from services.auth.local_password import LocalPasswordAuthBackend
from services.auth.session import SessionDTO, SessionRepository
from services.tenant_resolution.dependencies import resolve_request_tenant
from web.auth import (
    clear_pre_session_csrf,
    clear_session_cookie,
    get_optional_session,
    require_session,
    set_session_cookie,
    verify_pre_session_csrf,
)
from web.routes.chat import _drop_history
from web.routes.scraper import drop_scraper_runs_for_session
from web.settings import WebSettings

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _settings(request: Request) -> WebSettings:
    return cast(WebSettings, request.app.state.settings)


def _auth_backend(request: Request) -> LocalPasswordAuthBackend:
    backend = request.app.state.auth_backend
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication backend is not configured.",
        )
    return cast(LocalPasswordAuthBackend, backend)


def _engine(request: Request):  # type: ignore[no-untyped-def]
    engine = request.app.state.engine
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database engine is not configured.",
        )
    return engine


def _render_login(
    request: Request,
    *,
    error: str | None,
    email: str | None,
) -> HTMLResponse:
    """Build a login-page response with a freshly minted CSRF token.

    Mints the token once, embeds it in the form, and writes it into
    the pre-session cookie on the same response — cookie value and
    form value stay in lockstep.
    """
    templates = _templates(request)
    settings = _settings(request)

    csrf_token = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request,
        "login.html",
        {"csrf_token": csrf_token, "error": error, "email": email},
    )
    # The pre-session CSRF cookie is deliberately **not** HttpOnly: the
    # login form needs to read the same value to embed it as a hidden
    # input, and ``verify_pre_session_csrf`` compares the cookie value
    # against the form value on submit.
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf_token,
        httponly=False,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return cast(HTMLResponse, response)


# ---------------------------------------------------------------------------
# GET /login
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    """Render the login form with a pre-session CSRF token.

    Already-authenticated callers are redirected to ``/``.
    """
    existing = await get_optional_session(request)
    if existing is not None:
        return cast(HTMLResponse, RedirectResponse(url="/", status_code=303))

    return _render_login(request, error=None, email=None)


# ---------------------------------------------------------------------------
# POST /login
# ---------------------------------------------------------------------------


@router.post("/login")
async def login_submit(
    request: Request,
    _csrf: None = Depends(verify_pre_session_csrf),
    email: str = Form(...),
    password: str = Form(...),
    resolved_tenant_id: UUID = Depends(resolve_request_tenant),
) -> Response:
    """Verify credentials, create a session, redirect to ``/``.

    Per ADR-0063 §1 the tenant is resolved from the request's host
    via :class:`SubdomainTenantResolver` **before** any credential
    verification runs. A host that does not map to an active tenant
    raises 404 from the resolver dependency.

    Per ADR-0064 §2 the post-auth invariant check verifies that
    super-admin users only authenticate against the system tenant
    and tenant-subdomain hosts only accept non-super-admin users —
    defence in depth against a hypothetical schema-CHECK violation.

    Failures re-render the login form with a generic "Invalid
    credentials" message — the form never distinguishes "no such
    user" from "wrong password" in user-facing text.
    """
    backend = _auth_backend(request)
    engine = _engine(request)
    settings = _settings(request)

    user_agent = request.headers.get("user-agent")
    ip_address = request.client.host if request.client else None

    user = await backend.authenticate(
        email,
        password,
        tenant_id=resolved_tenant_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    if user is None:
        return _render_login(
            request,
            error="Invalid credentials.",
            email=email,
        )

    # Post-auth invariants (ADR-0064 §2):
    #   - system tenant subdomain accepts only super-admins
    #   - other subdomains accept only non-super-admins
    # The schema CHECK on users.is_super_admin already enforces the
    # implication at the DB level; this route-layer check is defence
    # in depth and refuses with the generic Invalid Credentials
    # message so an attacker cannot probe for super-admin existence.
    is_admin_host = resolved_tenant_id == SYSTEM_TENANT_ID
    if is_admin_host != user.is_super_admin:
        return _render_login(
            request,
            error="Invalid credentials.",
            email=email,
        )

    # Create the session inside a tenant-scoped DB session bound to
    # the just-authenticated user. ``app.user_id`` is set so the
    # audit-trigger captures who created the session row.
    async with tenant_context(engine, user.tenant_id, user_id=user.id) as db:
        session = await SessionRepository(db).create_session(user, ip=ip_address, ua=user_agent)

    response = RedirectResponse(url="/", status_code=303)
    set_session_cookie(response, settings, session.session_token)
    clear_pre_session_csrf(response, settings)
    return response


# ---------------------------------------------------------------------------
# POST /logout
# ---------------------------------------------------------------------------


@router.post("/logout")
async def logout(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> Response:
    """Delete the session row and clear the cookie."""
    settings = _settings(request)
    engine = _engine(request)

    # Inline CSRF check rather than Depends(verify_csrf) so we get the
    # SessionDTO for the delete in the same handler. The token comes
    # from the X-CSRF-Token header or the form's csrf_token field.
    candidate = request.headers.get("X-CSRF-Token")
    if candidate is None:
        form = await request.form()
        candidate = form.get("csrf_token")  # type: ignore[assignment]
    if not isinstance(candidate, str) or not secrets.compare_digest(candidate, session.csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing or invalid.",
        )

    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db:
        await SessionRepository(db).delete(session.id)

    # Evict the in-memory chat history for this session (ADR-0050)
    # and cancel any in-flight scraper runs (ADR-0053). Server-side
    # cleanup mirrors the user's expectation that logout ends the
    # conversation and discards work-in-flight.
    _drop_history(request, str(session.id))
    drop_scraper_runs_for_session(request, str(session.id))

    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response, settings)
    return response


# Note: ``GET /`` is registered by ``web/routes/chat.py`` in sub-stream
# 2c — it now redirects authenticated users to ``/front-office`` (6F-1 /
# ADR-0046); the chat surface lives at ``/assistants#shirley`` (ADR-0051).
# The original 2c comment referred to ``/chat`` (Shirley). The
# 2b placeholder ``home`` route is removed; chat is the landing page.
