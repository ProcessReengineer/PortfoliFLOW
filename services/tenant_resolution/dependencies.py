# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""FastAPI wiring for :class:`services.tenant_resolution.TenantResolver`.

Per ADR-0063 §1. The resolver is constructed once at application
startup and stored on ``app.state.tenant_resolver``; the
:func:`get_tenant_resolver` dependency hands it out per-request.

:func:`resolve_request_tenant` is the high-level dependency: it
calls the resolver against the request's ``Host`` header and
returns the resolved tenant id, converting a resolution failure
into HTTP 404. Routes that need a tenant id without yet having a
session (the login route is the canonical example) take it as
their first dependency.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request, status

from services.tenant_resolution.resolver import (
    TenantResolver,
    UnknownSubdomainError,
)


def get_tenant_resolver(request: Request) -> TenantResolver:
    """Return the resolver constructed at app startup.

    Raises:
        HTTPException(500): If the resolver was never installed on
            ``app.state``. This is a programming error — the lifespan
            must populate it before routes are served.
    """
    resolver: TenantResolver | None = getattr(request.app.state, "tenant_resolver", None)
    if resolver is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="tenant resolver not configured",
        )
    return resolver


async def resolve_request_tenant(request: Request) -> UUID:
    """Resolve the request's ``Host`` header to a tenant id.

    Raises:
        HTTPException(404): If no active tenant matches the host.
            The 404 is deliberate — a 400 would leak the existence
            of a "this host is not a valid tenant" check, which the
            attacker can deduce only by enumeration. The route layer
            treats unknown subdomains as plain "page not found".
    """
    resolver = get_tenant_resolver(request)
    host = request.headers.get("host", "")
    try:
        return await resolver.resolve(host=host)
    except UnknownSubdomainError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="tenant not found",
        ) from exc
