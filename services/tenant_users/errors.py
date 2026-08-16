# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Typed errors for the tenant-scoped user operations.

Per ADR-0121 §4. Each guard the service enforces has its own type so the
calling surface (U2's owner-gated routes) can render a specific banner
without parsing messages. The base class exists for the catch-all clause
a route needs after the specific ones.

Only the *guards* live here. Validation failures keep the types they
already had — :class:`services.user_validation.EmailInvalidError`,
:class:`services.user_validation.RoleInvalidError`, and
:class:`core.exceptions.ValidationError` from the password policy — so
the same condition raises the same type whichever surface triggers it.
"""

from __future__ import annotations

from core.exceptions import PortfoliFlowError

__all__ = [
    "CannotDeactivateLastOwnerError",
    "CannotDeactivateSelfError",
    "CannotDemoteLastOwnerError",
    "EmailTakenError",
    "TenantUserError",
    "UserNotFoundError",
]


class TenantUserError(PortfoliFlowError):
    """Base exception for tenant-scoped user-management errors."""


class EmailTakenError(TenantUserError):
    """A user with that email already exists in this tenant.

    Raised for an existing row whether it is active or deactivated:
    ``uq_users_tenant_email`` does not care about ``is_active``, and
    reactivating the existing account is the correct move rather than
    creating a second row for the same person.
    """


class UserNotFoundError(TenantUserError):
    """No user with that id in the current tenant context.

    Covers the cross-tenant case as well: a user id belonging to another
    tenant is invisible under RLS, so it arrives here as a miss rather
    than as a write that reached across the boundary (ADR-0121 §1).
    """


class CannotDeactivateSelfError(TenantUserError):
    """Refuse to let an owner deactivate their own account.

    Per ADR-0121 §4.2 the rule holds even when other owners remain — an
    owner who wants to leave has another owner deactivate them, which
    keeps the act attributable to someone who is still there.
    """


class CannotDeactivateLastOwnerError(TenantUserError):
    """Refuse to deactivate the only remaining active owner.

    Per ADR-0121 §4.1. A tenant without an active owner cannot administer
    itself: no one could create users, reset passwords, or reach any
    other owner-gated surface without operator intervention.
    """


class CannotDemoteLastOwnerError(TenantUserError):
    """Refuse to demote the only remaining active owner to member.

    The sibling of :class:`CannotDeactivateLastOwnerError` — the same
    invariant (a tenant keeps at least one active owner) reached by the
    other route. Per ADR-0121 §4.1.
    """
