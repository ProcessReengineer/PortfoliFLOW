# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shell-control endpoints — sidebar collapse toggle.

Sub-stream 6F-1 of Phase 6 Block 1. The sidebar's collapse state
persists across requests via a server-side cookie
(``pf_sidebar_collapsed``). This route flips the cookie value on
every POST and returns a 303 redirect back to the source page so
the sidebar re-renders in the new state.

A POST endpoint (rather than a GET toggle link) is used so the
cookie write goes through the CSRF gate that already protects every
mutating route. CSRF verification reuses :func:`web.auth.verify_csrf`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from services.auth.session import SessionDTO
from web.auth import require_session, verify_csrf

logger = logging.getLogger(__name__)
router = APIRouter()


_COOKIE_NAME: str = "pf_sidebar_collapsed"
_COOKIE_MAX_AGE_SECONDS: int = 60 * 60 * 24 * 365  # one year


def _safe_redirect_target(raw: str | None) -> str:
    """Validate a redirect target — must be a same-origin path.

    Anything other than a leading ``/`` (and not starting with ``//``,
    which is a protocol-relative URL) is replaced with the area
    landing page. Defends the toggle endpoint against open-redirect
    abuse.
    """
    if not raw:
        return "/front-office"
    if not raw.startswith("/") or raw.startswith("//"):
        return "/front-office"
    return raw


@router.post("/shell/sidebar/toggle")
async def toggle_sidebar(
    request: Request,
    redirect_to: str = Form("/front-office"),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> Response:
    """Flip the sidebar-collapsed cookie and redirect back.

    Args:
        request: The current request (used to read the existing
            cookie value).
        redirect_to: Same-origin path to redirect the browser to;
            external URLs are sanitised to ``/front-office``.
        session: Required for CSRF context (``Depends(require_session)``
            is the gate).
        _csrf: CSRF verification side effect.
    """
    del session  # only needed for the CSRF dependency wiring
    current = request.cookies.get(_COOKIE_NAME)
    new_value = "false" if current == "true" else "true"

    target = _safe_redirect_target(redirect_to)
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        key=_COOKIE_NAME,
        value=new_value,
        max_age=_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    logger.debug("sidebar toggle: %s -> %s (target=%s)", current, new_value, target)
    return response
