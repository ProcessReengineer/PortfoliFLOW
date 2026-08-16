# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Users surface under ``/admin#users`` — a tenant owner's own user list.

The ADR-0121 §6 web surface over :mod:`services.tenant_users`: the one
place a tenant owner lists, creates, deactivates, reactivates, re-roles
and password-resets the users of their **own** tenant, without an
operator running ``portfoliflow create-user`` for them. It is a sibling
of Providers & Credentials, not a new navigation concept, and it follows
that surface's structure exactly (ADR-0112 §6): a lazy section body, one
render path, and a rejected write answering 400 with the same body.

This module contains **no** user-management logic. Every rule — who may
be deactivated, which role may be assigned, what a password must look
like — lives in :mod:`services.tenant_users`, and the four guards it
enforces are enforced inside the writing transaction. What lives here is
the three things a web surface owes:

* **The gate.** Every route, *including the section GET*, takes
  ``Depends(require_role("owner"))``; every POST additionally takes
  ``verify_csrf``. The plain-403 semantics of ``require_role`` are
  adopted unchanged (ADR-0121 §6) — no redirect variant is introduced
  for this one surface. The route is authoritative: the Admin shell's
  owner conditional (``is_tenant_owner``) is cosmetic mirroring, so a
  member who hand-crafts the URL is refused by the route rather than by
  the absence of a link.
* **The transaction.** Each POST opens exactly one short
  ``tenant_context(engine, tenant_id, user_id=actor)`` (Pattern B,
  ADR-0065) and hands the session to the service, because that context is
  what makes the write tenant-confined under RLS *and* attributed to the
  acting owner by ``users_audit_trigger`` via the ``app.user_id`` GUC.
  The audit trail is therefore the schema's, not this module's — nothing
  here writes an audit row or logs a mutation, unlike the
  ``scoped_settings`` surface next door, whose table carries no trigger.
* **The copy.** :data:`_ERROR_COPY` maps each typed service error to
  owner-facing text. A service message is never rendered verbatim — it is
  written for a log line and names ids. The one deliberate exception is
  the password policy's :class:`~core.exceptions.ValidationError`, whose
  message *is* the rule ("at least 12 characters…") and is written to be
  surfaced; suppressing it would leave the owner guessing.

Two self-targeting acts answer with an ``HX-Redirect`` instead of a
fragment, because after them the section the swap would land in is no
longer the caller's to see:

* **Resetting your own password** succeeds and deletes your own sessions
  (ADR-0121 §4.5). A swapped-in section would sit on a dead cookie, so
  the browser is sent to ``/login``.
* **Demoting yourself** is allowed while another active owner exists
  (ADR-0121 §4.3). The session stays valid but no longer passes the
  owner gate, so the browser is sent to ``/admin`` for a full reload —
  the Users section disappears in one motion rather than the next HTMX
  call dying on a 403.

Endpoints:

* ``GET  /admin/users/section`` — the lazy section body.
* ``POST /admin/users/create``
* ``POST /admin/users/{user_id}/deactivate``
* ``POST /admin/users/{user_id}/reactivate``
* ``POST /admin/users/{user_id}/reset-password``
* ``POST /admin/users/{user_id}/role``
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import ValidationError
from core.repositories._session import tenant_context
from core.repositories.user_repository import UserDTO
from services.auth.session import SessionDTO
from services.tenant_users import (
    OWNER_ROLE,
    CannotDeactivateLastOwnerError,
    CannotDeactivateSelfError,
    CannotDemoteLastOwnerError,
    EmailTakenError,
    TenantUserError,
    UserNotFoundError,
    change_role,
    create_user,
    deactivate_user,
    list_users,
    reactivate_user,
    reset_password,
)
from services.user_validation import EmailInvalidError, RoleInvalidError
from web.auth import require_session, verify_csrf
from web.permissions import require_role

router = APIRouter()

_SECTION_TEMPLATE = "_partials/tenant_users_section.html"

_SECTION_ENDPOINT = "/admin/users/section"
_CREATE_ENDPOINT = "/admin/users/create"
_DEACTIVATE_ENDPOINT = "/admin/users/{user_id}/deactivate"
_REACTIVATE_ENDPOINT = "/admin/users/{user_id}/reactivate"
_RESET_PASSWORD_ENDPOINT = "/admin/users/{user_id}/reset-password"
_ROLE_ENDPOINT = "/admin/users/{user_id}/role"

#: Where the browser lands after each of the two self-targeting acts.
_LOGIN_URL = "/login"
_ADMIN_URL = "/admin"

#: The role selector's options, in display order. The *set* is the
#: service's :data:`~services.tenant_users.MANAGEABLE_ROLES` (ADR-0121
#: §6 — ``auditor`` gates nothing and is not offered); that is a
#: frozenset, so the order a form needs is stated here rather than
#: derived from it. Pinned against the service constant by
#: ``tests/web/test_tenant_users_routes.py``.
_ROLE_OPTIONS: tuple[tuple[str, str], ...] = (
    (OWNER_ROLE, "Owner"),
    ("member", "Member"),
)

#: Owner-facing text per typed service error (ADR-0121 §4). Service
#: messages name user ids and are written for a log line, so none of them
#: is rendered verbatim. ``TenantUserError`` sits last as the catch-all,
#: so a future guard type surfaces as prose rather than as a traceback.
_ERROR_COPY: dict[type[Exception], str] = {
    EmailTakenError: (
        "That email already has an account in this tenant. "
        "Reactivate it instead of creating a second one."
    ),
    EmailInvalidError: "That is not a valid email address.",
    RoleInvalidError: "Choose a role: owner or member.",
    UserNotFoundError: "That user is no longer in this tenant.",
    CannotDeactivateSelfError: (
        "You cannot deactivate your own account. Another owner has to do it."
    ),
    CannotDeactivateLastOwnerError: (
        "This is the tenant's last active owner. Make someone else an owner first."
    ),
    CannotDemoteLastOwnerError: (
        "This is the tenant's last active owner. Make someone else an owner first."
    ),
    TenantUserError: "That change could not be applied.",
}

#: What a handler catches. Everything else propagates — an unexpected
#: exception is not a rejected write and must not be dressed up as one.
_HANDLED_ERRORS = (TenantUserError, EmailInvalidError, RoleInvalidError, ValidationError)


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


def _banner_for(exc: Exception) -> str:
    """Return the owner-facing copy for a service error.

    Args:
        exc: An instance of one of :data:`_HANDLED_ERRORS`.

    Returns:
        Text safe to render inline. The password policy's
        :class:`~core.exceptions.ValidationError` is the one error
        surfaced verbatim: its message states the rule the owner just
        broke, and it is written for exactly that.
    """
    if isinstance(exc, ValidationError):
        return str(exc)
    for error_type, copy in _ERROR_COPY.items():
        if isinstance(exc, error_type):
            return copy
    return _ERROR_COPY[TenantUserError]


def _htmx_redirect(location: str, *, status_code: int) -> HTMLResponse:
    """Return an empty response that navigates the browser to ``location``.

    The ``HX-Redirect`` shape ``web.auth.require_session`` uses for its
    HTMX branch: htmx reads the header before it looks at the status or
    the body, so the browser performs a full-page navigation instead of
    swapping a fragment. The body is deliberately empty — nothing is ever
    swapped in.

    Args:
        location: Where to send the browser.
        status_code: ``401`` when the caller's session is gone (matching
            :func:`web.auth.require_session`), ``200`` when the act
            succeeded and the session is still valid.

    Returns:
        The header-only response.
    """
    return HTMLResponse("", status_code=status_code, headers={"HX-Redirect": location})


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _user_view(user: UserDTO, *, actor_user_id: UUID) -> dict[str, Any]:
    """Project one user row into template data.

    ``roles`` renders what is *stored*, joined verbatim — including the
    dormant ``auditor`` a CLI or super-admin path may have set. The role
    selector below it offers owner and member only (ADR-0121 §6), so the
    display never quietly re-labels a role the surface cannot assign.

    Args:
        user: The row as the service returned it.
        actor_user_id: The signed-in owner, for the self-targeting hints.

    Returns:
        Template data, with each row action's URL resolved from the
        endpoint constants so the template never builds a path.
    """
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name or "",
        "roles": ", ".join(user.roles) if user.roles else "—",
        "current_role": OWNER_ROLE if OWNER_ROLE in user.roles else "member",
        "is_active": user.is_active,
        "is_self": user.id == actor_user_id,
        "deactivate_url": _DEACTIVATE_ENDPOINT.format(user_id=user.id),
        "reactivate_url": _REACTIVATE_ENDPOINT.format(user_id=user.id),
        "reset_password_url": _RESET_PASSWORD_ENDPOINT.format(user_id=user.id),
        "role_url": _ROLE_ENDPOINT.format(user_id=user.id),
    }


async def _render_section(
    request: Request,
    *,
    session: SessionDTO,
    success: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the section body — the single render path for GET and every POST.

    Re-reads the user list inside one short ``tenant_context`` after the
    write has committed (Pattern B, ADR-0065), so a banner is always
    rendered against rows as they now are rather than against the
    writer's idea of them.

    Args:
        request: The active request.
        session: The authenticated session, for the tenant, the actor id
            and the CSRF token.
        success: Inline success banner text.
        error: Inline error banner text.
        status_code: 200 on success, 400 on a rejected write.

    Returns:
        The rendered section body for an ``outerHTML`` swap.
    """
    async with tenant_context(
        _engine(request), session.tenant_id, user_id=session.user_id
    ) as db_session:
        users = await list_users(db_session)

    views = [_user_view(user, actor_user_id=session.user_id) for user in users]
    context: dict[str, Any] = {
        "csrf_token": session.csrf_token,
        "create_endpoint": _CREATE_ENDPOINT,
        "active_users": [view for view in views if view["is_active"]],
        "deactivated_users": [view for view in views if not view["is_active"]],
        "role_options": _ROLE_OPTIONS,
        "success": success,
        "error": error,
    }
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            _SECTION_TEMPLATE,
            context,
            status_code=status_code,
        ),
    )


async def _rejected(request: Request, session: SessionDTO, exc: Exception) -> HTMLResponse:
    """Re-render the section at 400 with the mapped copy for ``exc``.

    The ADR-0112 §6 rejection idiom: same body, inline banner, 400 — so
    the swap lands and the owner sees why, instead of an error page
    replacing the section.
    """
    return await _render_section(
        request,
        session=session,
        error=_banner_for(exc),
        status_code=400,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(_SECTION_ENDPOINT, response_class=HTMLResponse)
async def get_tenant_users_section(
    request: Request,
    _owner: UserDTO = Depends(require_role("owner")),
    session: SessionDTO = Depends(require_session),
) -> HTMLResponse:
    """Return the Users section body.

    Lazy-loaded on first visibility via ``hx-trigger="revealed"``. The
    owner gate is on the GET as much as on the writes (ADR-0121 §6): a
    member gets no list at all, not a read-only one.
    """
    return await _render_section(request, session=session)


@router.post(_CREATE_ENDPOINT, response_class=HTMLResponse)
async def create_tenant_user(
    request: Request,
    email: str = Form(""),
    display_name: str = Form(""),
    password: str = Form(""),
    role: str = Form(""),
    _owner: UserDTO = Depends(require_role("owner")),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Create an active user in this tenant with one role.

    The owner sets the initial password directly — there is no invitation
    flow and no email delivery in v1 (ADR-0121 §3), so the password
    travels from owner to user out of band. It is validated against the
    one shared strength policy on the way in.
    """
    try:
        async with tenant_context(
            _engine(request), session.tenant_id, user_id=session.user_id
        ) as db_session:
            created = await create_user(
                db_session,
                actor_user_id=session.user_id,
                email=email,
                password=password,
                role=role,
                display_name=display_name or None,
            )
    except _HANDLED_ERRORS as exc:
        return await _rejected(request, session, exc)

    role_label = created.roles[0] if created.roles else "user"
    return await _render_section(
        request,
        session=session,
        success=f"{created.email} added as {role_label}.",
    )


@router.post(_DEACTIVATE_ENDPOINT, response_class=HTMLResponse)
async def deactivate_tenant_user(
    request: Request,
    user_id: UUID,
    _owner: UserDTO = Depends(require_role("owner")),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Deactivate a user and end every session they hold.

    Refused for the caller's own account whoever else is left, and for
    the tenant's last active owner (ADR-0121 §4.1, §4.2) — both inside
    the writing transaction, both surfacing as an inline banner.
    """
    try:
        async with tenant_context(
            _engine(request), session.tenant_id, user_id=session.user_id
        ) as db_session:
            updated = await deactivate_user(
                db_session,
                actor_user_id=session.user_id,
                user_id=user_id,
            )
    except _HANDLED_ERRORS as exc:
        return await _rejected(request, session, exc)

    return await _render_section(
        request,
        session=session,
        success=f"{updated.email} deactivated. Their sessions were ended.",
    )


@router.post(_REACTIVATE_ENDPOINT, response_class=HTMLResponse)
async def reactivate_tenant_user(
    request: Request,
    user_id: UUID,
    _owner: UserDTO = Depends(require_role("owner")),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Reactivate a deactivated user.

    They get no session back — signing in again is the point of having
    had their sessions dropped.
    """
    try:
        async with tenant_context(
            _engine(request), session.tenant_id, user_id=session.user_id
        ) as db_session:
            updated = await reactivate_user(
                db_session,
                actor_user_id=session.user_id,
                user_id=user_id,
            )
    except _HANDLED_ERRORS as exc:
        return await _rejected(request, session, exc)

    return await _render_section(
        request,
        session=session,
        success=f"{updated.email} reactivated. They can sign in again.",
    )


@router.post(_RESET_PASSWORD_ENDPOINT, response_class=HTMLResponse)
async def reset_tenant_user_password(
    request: Request,
    user_id: UUID,
    new_password: str = Form(""),
    _owner: UserDTO = Depends(require_role("owner")),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Set a user's password and end every session they hold.

    Resetting **your own** password ends your own sessions along with
    everyone else's copy of them, so the response is an ``HX-Redirect``
    to ``/login`` rather than a section swap: the cookie the swap would
    return to no longer resolves. The 401 matches
    :func:`web.auth.require_session`'s HTMX branch — by the time the
    response is written, the caller genuinely is unauthenticated.

    Whether the reset targeted the caller is decided *before* the service
    runs; the redirect still only happens on success, so a weak password
    is rejected inline exactly as it is for anyone else.
    """
    targets_self = user_id == session.user_id
    try:
        async with tenant_context(
            _engine(request), session.tenant_id, user_id=session.user_id
        ) as db_session:
            updated = await reset_password(
                db_session,
                actor_user_id=session.user_id,
                user_id=user_id,
                new_password=new_password,
            )
    except _HANDLED_ERRORS as exc:
        return await _rejected(request, session, exc)

    if targets_self:
        return _htmx_redirect(_LOGIN_URL, status_code=status.HTTP_401_UNAUTHORIZED)

    return await _render_section(
        request,
        session=session,
        success=f"Password reset for {updated.email}. Their sessions were ended.",
    )


@router.post(_ROLE_ENDPOINT, response_class=HTMLResponse)
async def change_tenant_user_role(
    request: Request,
    user_id: UUID,
    new_role: str = Form(""),
    _owner: UserDTO = Depends(require_role("owner")),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Set a user's role to owner or member.

    Demoting **yourself** is legal while another active owner remains
    (ADR-0121 §4.3) — the hand-over case. The session stays valid, so
    nothing forces a re-login; what changes is that the owner gate now
    refuses it. The response is therefore an ``HX-Redirect`` to
    ``/admin`` at 200: the page reloads without the Users section, rather
    than leaving a section on screen whose next call would 403.

    The redirect fires on what the service *returned* — the caller no
    longer holding ``owner`` — so an owner→owner no-op re-renders in
    place like any other write.
    """
    targets_self = user_id == session.user_id
    try:
        async with tenant_context(
            _engine(request), session.tenant_id, user_id=session.user_id
        ) as db_session:
            updated = await change_role(
                db_session,
                actor_user_id=session.user_id,
                user_id=user_id,
                new_role=new_role,
            )
    except _HANDLED_ERRORS as exc:
        return await _rejected(request, session, exc)

    if targets_self and not updated.has_role(OWNER_ROLE):
        return _htmx_redirect(_ADMIN_URL, status_code=status.HTTP_200_OK)

    role_label = updated.roles[0] if updated.roles else "—"
    return await _render_section(
        request,
        session=session,
        success=f"{updated.email} is now {role_label}.",
    )


__all__ = ["router"]
