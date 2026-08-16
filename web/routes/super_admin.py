# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Super-admin route surface — platform-operations endpoints.

Per ADR-0064 §1. Every handler is gated on
:func:`web.permissions.require_super_admin`; every mutating handler
also runs ``Depends(verify_csrf)``. The shared implementation lives
in :mod:`services.super_admin.operations` — both the CLI subcommands
and these routes call into that single module so behaviour matches
exactly across surfaces.

The handlers operate **outside any tenant context** — super-admin
actions span tenant boundaries (create one, deactivate another) and
the audit-engine SQLAlchemy connection is the sanctioned RLS-bypass
seam for this exact purpose. The :func:`_audit_conn` async context
manager opens one ``begin()`` block per request so the operation and
its audit row roll back together; see ADR-0064 §4 for the integrity
expectation.

Per ADR-0064 §1 the surface does **not** read tenant-data tables. A
regression test (``tests/regression/test_super_admin_routes_no_tenant_data.py``)
walks this file's import AST and enforces the rule.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncConnection

from core.repositories.user_repository import UserDTO
from services.super_admin import (
    CannotDeactivateLastSuperAdminError,
    CannotDeactivatePrimaryTenantError,
    CannotDeactivateSystemTenantError,
    EmailInvalidError,
    OwnerNotFoundError,
    SubdomainInvalidError,
    SubdomainReservedError,
    SubdomainTakenError,
    SuperAdminOperationError,
    TenantNotFoundError,
    UserNotFoundError,
    create_super_admin_idempotent,
    create_tenant_idempotent,
    deactivate_super_admin,
    deactivate_tenant,
    list_super_admins,
    list_tenants,
    reactivate_tenant,
    reset_owner_password,
    seed_tenant_defaults,
)
from web.auth import verify_csrf
from web.permissions import require_super_admin
from web.tick_scheduler import read_tick_scheduler_view

router = APIRouter(prefix="/super-admin", tags=["super-admin"])


# ---------------------------------------------------------------------------
# Audit-engine connection helper
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _audit_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """Open one transaction on the audit engine for a super-admin op.

    Super-admin routes operate outside any tenant context. The audit
    engine is the only sanctioned RLS-bypass surface that can read
    ``tenants`` cross-tenant and write ``super_admin_audit``. The
    transaction wraps the full operation so audit and underlying SQL
    succeed or roll back together — see ADR-0064 §4 on integrity.

    Not a FastAPI ``Depends`` because we want the transaction scope
    to be the route-handler scope; a ``Depends`` with a wrapping
    yield would commit before the response renders, losing the
    rollback-on-render-error semantics this surface relies on.
    """
    engine = getattr(request.app.state, "audit_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audit engine not configured.",
        )
    async with engine.begin() as conn:
        yield conn


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _client_ip(request: Request) -> str | None:
    """Best-effort client IP.

    Prefers ``request.client.host`` (uvicorn's direct view). Returns
    ``None`` when the value is missing or not parseable as a real
    IPv4/IPv6 address; the audit table's ``ip_address`` column is
    INET and rejects non-IP strings (e.g. Starlette's TestClient
    reports ``'testclient'``).

    A future reverse-proxy deployment will need ``X-Forwarded-For``
    parsing against a trusted-proxy list (roadmap C0 item); for now
    the direct, validated value is the right thing.
    """
    import ipaddress

    raw = request.client.host if request.client else None
    if not raw:
        return None
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        return None
    return raw


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


async def _render_tenants_page(
    request: Request,
    user: UserDTO,
    *,
    error: str | None = None,
    message: str | None = None,
) -> HTMLResponse:
    """Re-render the tenants page with optional banner state.

    Also carries the platform-status facts (ADR-0117 §5). They are read
    from in-process state at render time — no query, no polling: a page
    reload is the refresh.
    """
    async with _audit_conn(request) as conn:
        tenants = await list_tenants(conn, include_system=False)
    return _templates(request).TemplateResponse(
        request,
        "super_admin/tenants.html",
        {
            "tenants": tenants,
            "user_email": user.email,
            "error": error,
            "message": message,
            "active_area": None,
            "platform_status": read_tick_scheduler_view(request.app.state),
        },
    )


async def _render_tenants_partial(request: Request, user: UserDTO) -> HTMLResponse:
    """Render just the tenants table partial (HTMX swap target)."""
    async with _audit_conn(request) as conn:
        tenants = await list_tenants(conn, include_system=False)
    return _templates(request).TemplateResponse(
        request,
        "super_admin/_partials/_tenants_table.html",
        {"tenants": tenants, "user_email": user.email},
    )


async def _render_users_page(
    request: Request,
    user: UserDTO,
    *,
    error: str | None = None,
    message: str | None = None,
) -> HTMLResponse:
    """Re-render the super-admin users page with optional banners."""
    async with _audit_conn(request) as conn:
        users = await list_super_admins(conn)
    return _templates(request).TemplateResponse(
        request,
        "super_admin/users.html",
        {
            "super_admins": users,
            "user_email": user.email,
            "error": error,
            "message": message,
            "active_area": None,
        },
    )


async def _render_users_partial(request: Request, user: UserDTO) -> HTMLResponse:
    async with _audit_conn(request) as conn:
        users = await list_super_admins(conn)
    return _templates(request).TemplateResponse(
        request,
        "super_admin/_partials/_users_table.html",
        {"super_admins": users, "user_email": user.email},
    )


# ---------------------------------------------------------------------------
# Tenants endpoints
# ---------------------------------------------------------------------------


@router.get("/tenants", response_class=HTMLResponse)
async def list_tenants_route(
    request: Request,
    user: UserDTO = Depends(require_super_admin),
) -> HTMLResponse:
    """Render the tenants page (table + create form)."""
    return await _render_tenants_page(request, user)


@router.get("/tenants/partial", response_class=HTMLResponse)
async def list_tenants_partial(
    request: Request,
    user: UserDTO = Depends(require_super_admin),
) -> HTMLResponse:
    """Render just the tenants table partial (HTMX swap target)."""
    return await _render_tenants_partial(request, user)


@router.post("/tenants", response_class=HTMLResponse)
async def create_tenant_route(
    request: Request,
    _csrf: None = Depends(verify_csrf),
    user: UserDTO = Depends(require_super_admin),
    name: str = Form(...),
    subdomain: str = Form(...),
    owner_email: str = Form(...),
    owner_password: str = Form(...),
) -> HTMLResponse:
    """Create a tenant — atomic with the audit row.

    On validation failure, re-renders the page with the error banner.
    On success, runs the seed-installation step (best-effort) before
    returning the updated tenants partial.
    """
    new_tenant_id: UUID | None = None
    owner_user_id: UUID | None = None
    try:
        async with _audit_conn(request) as conn:
            summary = await create_tenant_idempotent(
                conn,
                name=name,
                subdomain=subdomain,
                owner_email=owner_email,
                owner_password=owner_password,
                actor_super_admin_id=user.id,
                actor_ip=_client_ip(request),
                actor_user_agent=_user_agent(request),
            )
            new_tenant_id = summary.id
            # Resolve owner id for seed attribution; the user always
            # exists at this point (just created or pre-existing).
            from sqlalchemy import text  # noqa: PLC0415

            owner_row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE tenant_id = :tid AND email = :email"),
                    {"tid": str(new_tenant_id), "email": owner_email.strip()},
                )
            ).first()
            owner_user_id = UUID(str(owner_row.id)) if owner_row is not None else user.id
    except (
        SubdomainInvalidError,
        SubdomainReservedError,
        SubdomainTakenError,
        EmailInvalidError,
    ) as exc:
        return await _render_tenants_page(request, user, error=str(exc))
    except SuperAdminOperationError as exc:
        return await _render_tenants_page(request, user, error=str(exc))

    # Post-commit seed installation — best-effort. Failures here leave
    # the tenant present but unseeded; the operator can re-run seeds
    # via the CLI. The banner surfaces the partial-success state so
    # the operator knows to follow up.
    seed_warning: str | None = None
    if new_tenant_id is not None and owner_user_id is not None:
        engine = request.app.state.audit_engine
        try:
            await seed_tenant_defaults(
                engine,
                tenant_id=new_tenant_id,
                actor_user_id=owner_user_id,
            )
        except Exception as exc:  # noqa: BLE001
            seed_warning = (
                f"Tenant created, but seed installation failed: {exc}. "
                "The tenant is usable; seed it from the CLI to populate "
                "SAA templates and the default region catalogue."
            )

    if seed_warning is not None:
        return await _render_tenants_page(request, user, message=seed_warning)
    return await _render_tenants_partial(request, user)


@router.post("/tenants/{tenant_id}/deactivate", response_class=HTMLResponse)
async def deactivate_tenant_route(
    request: Request,
    tenant_id: UUID,
    _csrf: None = Depends(verify_csrf),
    user: UserDTO = Depends(require_super_admin),
) -> HTMLResponse:
    """Deactivate a tenant — refuses system + primary tenants."""
    try:
        async with _audit_conn(request) as conn:
            await deactivate_tenant(
                conn,
                tenant_id=tenant_id,
                actor_super_admin_id=user.id,
                actor_ip=_client_ip(request),
                actor_user_agent=_user_agent(request),
            )
    except (
        CannotDeactivateSystemTenantError,
        CannotDeactivatePrimaryTenantError,
        TenantNotFoundError,
    ) as exc:
        return await _render_tenants_page(request, user, error=str(exc))
    return await _render_tenants_partial(request, user)


@router.post("/tenants/{tenant_id}/reactivate", response_class=HTMLResponse)
async def reactivate_tenant_route(
    request: Request,
    tenant_id: UUID,
    _csrf: None = Depends(verify_csrf),
    user: UserDTO = Depends(require_super_admin),
) -> HTMLResponse:
    """Reactivate a tenant."""
    try:
        async with _audit_conn(request) as conn:
            await reactivate_tenant(
                conn,
                tenant_id=tenant_id,
                actor_super_admin_id=user.id,
                actor_ip=_client_ip(request),
                actor_user_agent=_user_agent(request),
            )
    except TenantNotFoundError as exc:
        return await _render_tenants_page(request, user, error=str(exc))
    return await _render_tenants_partial(request, user)


@router.post("/tenants/{tenant_id}/reset-owner", response_class=HTMLResponse)
async def reset_owner_route(
    request: Request,
    tenant_id: UUID,
    _csrf: None = Depends(verify_csrf),
    user: UserDTO = Depends(require_super_admin),
    new_password: str = Form(...),
) -> HTMLResponse:
    """Reset the tenant owner's password — invalidates their sessions."""
    try:
        async with _audit_conn(request) as conn:
            owner_email = await reset_owner_password(
                conn,
                tenant_id=tenant_id,
                new_password=new_password,
                actor_super_admin_id=user.id,
                actor_ip=_client_ip(request),
                actor_user_agent=_user_agent(request),
            )
    except (OwnerNotFoundError, TenantNotFoundError) as exc:
        return await _render_tenants_page(request, user, error=str(exc))
    return await _render_tenants_page(
        request,
        user,
        message=(
            f"Owner password reset for {owner_email!r}. Communicate the "
            "new password to the tenant owner out of band; all of their "
            "active sessions have been invalidated."
        ),
    )


# ---------------------------------------------------------------------------
# Super-admin user endpoints
# ---------------------------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
async def list_super_admins_route(
    request: Request,
    user: UserDTO = Depends(require_super_admin),
) -> HTMLResponse:
    """Render the super-admin users page."""
    return await _render_users_page(request, user)


@router.get("/users/partial", response_class=HTMLResponse)
async def list_super_admins_partial(
    request: Request,
    user: UserDTO = Depends(require_super_admin),
) -> HTMLResponse:
    return await _render_users_partial(request, user)


@router.post("/users", response_class=HTMLResponse)
async def create_super_admin_route(
    request: Request,
    _csrf: None = Depends(verify_csrf),
    user: UserDTO = Depends(require_super_admin),
    email: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse:
    """Create a super-admin user."""
    try:
        async with _audit_conn(request) as conn:
            await create_super_admin_idempotent(
                conn,
                email=email,
                password=password,
                actor_super_admin_id=user.id,
                actor_ip=_client_ip(request),
                actor_user_agent=_user_agent(request),
            )
    except EmailInvalidError as exc:
        return await _render_users_page(request, user, error=str(exc))
    except SuperAdminOperationError as exc:
        return await _render_users_page(request, user, error=str(exc))
    return await _render_users_partial(request, user)


@router.post("/users/{user_id}/deactivate", response_class=HTMLResponse)
async def deactivate_super_admin_route(
    request: Request,
    user_id: UUID,
    _csrf: None = Depends(verify_csrf),
    user: UserDTO = Depends(require_super_admin),
) -> HTMLResponse:
    """Deactivate a super-admin — refuses if it leaves zero active."""
    try:
        async with _audit_conn(request) as conn:
            await deactivate_super_admin(
                conn,
                user_id=user_id,
                actor_super_admin_id=user.id,
                actor_ip=_client_ip(request),
                actor_user_agent=_user_agent(request),
            )
    except (
        CannotDeactivateLastSuperAdminError,
        UserNotFoundError,
    ) as exc:
        return await _render_users_page(request, user, error=str(exc))
    return await _render_users_partial(request, user)
