# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Super-admin platform operations.

Per ADR-0064. The package holds pure-function helpers that perform
super-admin actions (tenant creation, user management) and write the
matching ``super_admin_audit`` row in the same transaction. Both the
CLI subcommands (cli/create_tenant.py, cli/create_user.py,
cli/create_super_admin.py, cli/bootstrap.py) and the web routes
(web/routes/super_admin.py) call into this layer — there is no
second implementation.
"""

from services.super_admin.operations import (
    CannotDeactivateLastSuperAdminError,
    CannotDeactivatePrimaryTenantError,
    CannotDeactivateSystemTenantError,
    EmailInvalidError,
    OwnerNotFoundError,
    RoleInvalidError,
    SubdomainInvalidError,
    SubdomainReservedError,
    SubdomainTakenError,
    SuperAdminOperationError,
    SuperAdminSummary,
    TenantNotFoundError,
    TenantSummary,
    UserNotFoundError,
    create_super_admin_idempotent,
    create_tenant_idempotent,
    create_user_idempotent,
    deactivate_super_admin,
    deactivate_tenant,
    list_super_admins,
    list_tenants,
    reactivate_tenant,
    reset_owner_password,
    resolve_tenant_id_by_subdomain,
    seed_tenant_defaults,
)

__all__ = [
    "CannotDeactivateLastSuperAdminError",
    "CannotDeactivatePrimaryTenantError",
    "CannotDeactivateSystemTenantError",
    "EmailInvalidError",
    "OwnerNotFoundError",
    "RoleInvalidError",
    "SubdomainInvalidError",
    "SubdomainReservedError",
    "SubdomainTakenError",
    "SuperAdminOperationError",
    "SuperAdminSummary",
    "TenantNotFoundError",
    "TenantSummary",
    "UserNotFoundError",
    "create_super_admin_idempotent",
    "create_tenant_idempotent",
    "create_user_idempotent",
    "deactivate_super_admin",
    "deactivate_tenant",
    "list_super_admins",
    "list_tenants",
    "reactivate_tenant",
    "reset_owner_password",
    "resolve_tenant_id_by_subdomain",
    "seed_tenant_defaults",
]
