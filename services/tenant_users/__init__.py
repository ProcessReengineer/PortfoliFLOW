# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tenant-scoped user management — what a tenant owner does to their own users.

Per ADR-0121 §1. The tenant-side counterpart of
:mod:`services.super_admin`: where that package acts on the platform
(tenants, super-admins) through the superuser engine and writes
``super_admin_audit``, this one acts on the users of a single tenant
through the application role inside the caller's ``tenant_context``, and
leaves auditing to the ``users_audit_trigger`` + ``app.user_id`` GUC
already in the schema.

The operations live in :mod:`services.tenant_users.service` and the guard
errors in :mod:`services.tenant_users.errors`; both are re-exported here,
so callers import from the package.

The package is deliberately inert on its own — it opens no engine, holds
no state, and starts no transaction. The caller (U2's owner-gated routes,
or a test) opens ``tenant_context(engine, tenant_id,
user_id=actor_user_id)`` and hands the session in.
"""

from services.tenant_users.errors import (
    CannotDeactivateLastOwnerError,
    CannotDeactivateSelfError,
    CannotDemoteLastOwnerError,
    EmailTakenError,
    TenantUserError,
    UserNotFoundError,
)
from services.tenant_users.service import (
    MANAGEABLE_ROLES,
    OWNER_ROLE,
    change_role,
    create_user,
    deactivate_user,
    list_users,
    reactivate_user,
    reset_password,
)

__all__ = [
    "MANAGEABLE_ROLES",
    "OWNER_ROLE",
    "CannotDeactivateLastOwnerError",
    "CannotDeactivateSelfError",
    "CannotDemoteLastOwnerError",
    "EmailTakenError",
    "TenantUserError",
    "UserNotFoundError",
    "change_role",
    "create_user",
    "deactivate_user",
    "list_users",
    "reactivate_user",
    "reset_password",
]
