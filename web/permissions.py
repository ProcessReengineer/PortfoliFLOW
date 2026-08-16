# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Role-based authorisation dependencies.

Per ADR-0063 §5. Two FastAPI dependency factories:

- :func:`require_role` — gates a route on tenant-role membership
  (``owner`` / ``member`` / ``auditor``). Raises 403 on mismatch.
- :func:`require_super_admin` — gates a route on the platform
  ``is_super_admin = TRUE`` flag, **and** verifies the user is in
  the system tenant. Defence in depth against a hypothetical
  schema-CHECK violation.

Both depend on the authenticated user, loaded by
:func:`get_authenticated_user`.

Transaction-lifetime discipline (ADR-0065 §1b)
----------------------------------------------
``get_authenticated_user`` no longer reuses a request-scoped session
yielded by the retired ``get_authenticated_session``. It depends on
:func:`require_authenticated_session` (which resolves the session DTO
and performs the throttled idle-timer touch in its own short,
committed transaction), then opens its **own** brief ``tenant_context``
for the ``users.id`` lookup and closes it immediately — consistent with
the dominant Pattern-B handler style. No transaction, and no pooled
connection, is held across the request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories._session import tenant_context
from core.repositories.user_repository import UserDTO, UserRepository
from core.tenant_constants import SYSTEM_TENANT_ID
from services.auth.session import SessionDTO
from web.auth import require_authenticated_session


def _engine(request: Request) -> AsyncEngine:
    engine = request.app.state.engine
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database engine is not configured.",
        )
    return cast(AsyncEngine, engine)


async def get_authenticated_user(
    request: Request,
    session: SessionDTO = Depends(require_authenticated_session),
) -> UserDTO:
    """Load the :class:`UserDTO` for the active session.

    Per ADR-0065 §1b: opens its own short ``tenant_context`` for the
    ``UserRepository.get_by_id`` read and lets it commit and close
    immediately, rather than inheriting a request-scoped session. The
    cost is one extra connection acquire/release per gated request for
    a sub-millisecond primary-key lookup — paid back many times over by
    not holding a connection across the whole request.

    The session DTO is supplied by :func:`require_authenticated_session`,
    which has already performed the throttled idle-timer touch in its
    own committed transaction.

    The loaded user is stashed on ``request.state.user`` so the
    shell template-context processor can derive the conditional
    ``show_super_admin_link`` flag without a second DB round-trip.

    Raises:
        HTTPException(401): If the session's ``user_id`` no longer
            resolves to an active row (the user was deactivated after
            the session was issued).
    """
    engine = _engine(request)
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        user = await UserRepository(db_session).get_by_id(session.user_id)

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user not active",
        )
    request.state.user = user
    return user


def require_role(
    *allowed_roles: str,
) -> Callable[..., Awaitable[UserDTO]]:
    """Dependency factory: 403 if the user holds none of ``allowed_roles``.

    The membership check is strict — owning ``['owner', 'auditor']``
    and being gated on ``require_role('member')`` fails, since neither
    role overlaps. There is no implicit promotion ("owner can do
    everything member can"); routes that should be open to multiple
    roles list them all.

    Args:
        *allowed_roles: One or more values from ``{'owner', 'member',
            'auditor'}``.

    Returns:
        An async dependency callable to use with ``Depends(...)``.
    """

    async def _dep(
        user: UserDTO = Depends(get_authenticated_user),
    ) -> UserDTO:
        if not user.has_role(*allowed_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient role",
            )
        return user

    return _dep


async def require_super_admin(
    user: UserDTO = Depends(get_authenticated_user),
) -> UserDTO:
    """Dependency: 403 unless the user is a super-admin.

    Verifies both ``is_super_admin = TRUE`` **and** ``tenant_id ==
    SYSTEM_TENANT_ID``. The schema CHECK enforces the implication
    at the DB layer; the route-layer check is defence in depth and
    insulates against hypothetical bugs that bypass the CHECK
    (raw SQL inserts, mis-managed migrations).
    """
    if not user.is_super_admin or user.tenant_id != SYSTEM_TENANT_ID:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="super-admin required",
        )
    return user
