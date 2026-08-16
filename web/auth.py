# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Authentication and session dependencies for the FastAPI variant.

Wires the auth backend / session store (``services/auth/``) into the
FastAPI request lifecycle:

1. **Cookie wiring** — set / read / clear the session cookie. Cookie
   attributes follow ADR-0036 §1: ``HttpOnly``, ``Secure`` (in
   production), ``SameSite=Lax``, ``Path=/``,
   ``Max-Age=ABSOLUTE_TIMEOUT``.

2. **Session resolution** — :func:`get_optional_session` returns the
   :class:`SessionDTO` or ``None``. Per ADR-0063 §4, the pre-tenant
   session-token resolve runs against the audit engine (RLS bypass)
   so the cookie can be mapped to its tenant *before* opening a
   tenant-scoped session. :func:`require_session` redirects on
   absence (303 for ordinary GETs, 401 + ``HX-Redirect`` for HTMX
   fragments).

3. **CSRF** — :func:`verify_csrf` validates against the session-bound
   token on every mutating route. The login form uses a separate
   pre-session token (cookie + hidden form field) validated by
   :func:`verify_pre_session_csrf`.

Transaction-lifetime discipline (ADR-0065)
------------------------------------------
The authentication path **no longer holds a request-scoped
transaction**. :func:`require_authenticated_session` is a
*non-yielding* dependency: it resolves the :class:`SessionDTO`, runs a
throttled ``touch`` inside its own short ``tenant_context`` that opens,
updates, and commits in milliseconds, and returns the DTO. The lock on
the ``sessions`` row is taken and released within a single statement
instead of being held for the request's full lifetime.

The retired :func:`get_authenticated_session` yield-dependency used to
keep a ``tenant_context`` — and thus a ``sessions``-row lock and a
pooled connection — open until the request ended; that is what caused
the deterministic upload hang (a second in-handler touch blocked on the
dependency's still-open lock, which could not commit until the request
ended, which could not end because the second touch was blocked). The
sanctioned pattern is now: *dependencies resolve and return context;
handlers open a ``tenant_context`` per discrete unit of DB work and let
it commit promptly.*
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import cast

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories._session import tenant_context
from services.auth.session import (
    ABSOLUTE_TIMEOUT,
    SessionDTO,
    SessionRepository,
)
from web.settings import WebSettings


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------


def _settings(request: Request) -> WebSettings:
    return cast(WebSettings, request.app.state.settings)


def _engine(request: Request) -> AsyncEngine:
    engine = request.app.state.engine
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database engine is not configured.",
        )
    return cast(AsyncEngine, engine)


def _audit_engine(request: Request) -> AsyncEngine:
    engine = getattr(request.app.state, "audit_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audit engine is not configured.",
        )
    return cast(AsyncEngine, engine)


def set_session_cookie(response: Response, settings: WebSettings, session_token: str) -> None:
    """Attach the session cookie to ``response``."""
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_token,
        max_age=int(ABSOLUTE_TIMEOUT.total_seconds()),
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: WebSettings) -> None:
    """Remove the session cookie."""
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
    )


# ---------------------------------------------------------------------------
# Session resolution
# ---------------------------------------------------------------------------


async def get_optional_session(request: Request) -> SessionDTO | None:
    """Return the session bound to the request cookie, or ``None``.

    Per ADR-0063 §4, the pre-tenant session-token resolve runs
    against the **audit engine** (the existing RLS-bypass surface
    used for ``login_audit`` writes). The query reads only the
    minimum required fields plus the idle and absolute timeouts
    inlined as SQL — keeping the filter at the DB layer means we
    don't have to load and reject expired rows in Python.

    Once ``tenant_id`` is known, downstream consumers
    (:func:`require_authenticated_session`) open a normal
    tenant-scoped session via :func:`tenant_context`. The audit
    engine is **never** used for anything beyond the three sanctioned
    paths enforced by
    ``tests/regression/test_audit_engine_only_writes_login_audit.py``.
    """
    settings = _settings(request)
    cookie = request.cookies.get(settings.session_cookie_name)
    if not cookie:
        return None
    audit_engine = _audit_engine(request)

    async with audit_engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT id, tenant_id, user_id, csrf_token, created_at,
                       last_seen_at, expires_at
                FROM sessions
                WHERE session_token = :token
                  AND expires_at > NOW()
                  AND last_seen_at + INTERVAL '8 hours' > NOW()
                """
            ),
            {"token": cookie},
        )
        row = result.first()
    if row is None:
        return None
    return SessionDTO(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        session_token=cookie,
        csrf_token=row.csrf_token,
        created_at=cast(datetime, row.created_at),
        last_seen_at=cast(datetime, row.last_seen_at),
        expires_at=cast(datetime, row.expires_at),
    )


async def require_session(request: Request) -> SessionDTO:
    """Return the session, redirecting to ``/login`` on absence.

    Plain GET requests get a 303 with ``Location: /login``. HTMX
    requests get 401 + ``HX-Redirect: /login`` so the browser
    performs a full-page navigation rather than swapping a fragment
    into the unauthorised page.

    This dependency performs **no** database write — it is a pure DTO
    resolve. It is the right dependency for read-only GET routes that
    open their own work transaction (Pattern B). Routes that want the
    idle-timer reset handled for them depend on
    :func:`require_authenticated_session` instead.
    """
    session = await get_optional_session(request)
    if session is not None:
        return session

    if request.headers.get("HX-Request"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"HX-Redirect": "/login"},
        )
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": "/login"},
    )


async def require_authenticated_session(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> SessionDTO:
    """Resolve the session and reset the idle timer — no held transaction.

    Per ADR-0065 §1a. This **replaces** the retired
    ``get_authenticated_session`` yield-dependency. Instead of opening
    a ``tenant_context`` and yielding the live session for the whole
    request, it opens a short self-contained context, runs the
    throttled touch, commits, and returns the plain DTO. No lock and no
    pooled connection survive the dependency call.

    The throttle (``touch_throttled``) makes this cheap: on the common
    path the conditional UPDATE matches zero rows and holds no lock.

    RLS / audit scoping is preserved: the ``tenant_context`` re-applies
    the ``SET LOCAL`` GUCs on entry, so the touch UPDATE runs correctly
    scoped exactly as before.

    Returns the :class:`SessionDTO`; handlers open their own
    ``tenant_context`` for the actual work.
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        await SessionRepository(db_session).touch_throttled(session.id)
    return session


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


async def _read_csrf_token_from_request(request: Request) -> str | None:
    """Pull the CSRF token from header or form body.

    Header takes precedence for HTMX / AJAX paths. The parsed form is
    cached on ``request.state.form`` so a downstream handler can
    re-use it without paying the parsing cost twice.
    """
    header = request.headers.get("X-CSRF-Token")
    if header:
        return header

    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        request.state.form = form
        token = form.get("csrf_token")
        return str(token) if token else None
    return None


async def verify_csrf(
    request: Request,
    session: SessionDTO = Depends(require_session),
) -> None:
    """Compare the request CSRF token to the session-bound value.

    Raises 403 on mismatch. Constant-time comparison via
    :func:`secrets.compare_digest`.
    """
    candidate = await _read_csrf_token_from_request(request)
    if candidate is None or not secrets.compare_digest(candidate, session.csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing or invalid.",
        )


# ---- Pre-session CSRF (login form) -------------------------------------


def clear_pre_session_csrf(response: Response, settings: WebSettings) -> None:
    """Drop the pre-session CSRF cookie after successful login."""
    response.delete_cookie(key=settings.csrf_cookie_name, path="/")


async def verify_pre_session_csrf(request: Request) -> None:
    """Compare the pre-session CSRF cookie with the form-supplied value.

    Raises 403 on mismatch. Used by ``POST /login`` before any session
    exists.
    """
    settings = _settings(request)
    cookie = request.cookies.get(settings.csrf_cookie_name)
    candidate = await _read_csrf_token_from_request(request)
    if not cookie or not candidate or not secrets.compare_digest(candidate, cookie):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Login CSRF token missing or invalid.",
        )
