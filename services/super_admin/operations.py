# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared platform-operations helpers for super-admin actions.

Per ADR-0064 §1, §3 and §4. Every helper in this module:

- Takes a pre-opened :class:`AsyncConnection` from the caller (CLI
  opens it from the superuser engine; web routes open it from the
  audit engine).
- Writes the matching ``super_admin_audit`` row **in the caller's
  transaction**. An audit failure rolls back the whole operation —
  there is no ``try/except`` around the audit insert. This is the
  hardening referenced in the prompt: the b012 → b013 transition
  guard pattern (``try/except`` around the audit INSERT, logging the
  failure and continuing) violated the audit-trail integrity that
  ADR-0064 §4 commits to.
- Raises typed exceptions for validation / business-rule violations
  so the calling layer can translate them into the appropriate
  surface response (CLI: typer exit code; web: re-render with banner).

The seed-installation entry point (:func:`seed_tenant_defaults`) is
a separate concern. It runs after :func:`create_tenant_idempotent`'s
transaction commits, in independent tenant-scoped transactions per
the established :mod:`cli.bootstrap` pattern — a seed failure must
not roll back tenant creation.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.exceptions import PortfoliFlowError
from core.repositories import (
    AssetClassRepository,
    RegionRepository,
    SAAAssetClassInputRepository,
    SAAConfigurationRepository,
    SAACorrelationRepository,
    SectorRepository,
    UserRepository,
    tenant_context,
)
from core.repositories.country_repository import CountryRepository
from core.tenant_constants import (
    PRIMARY_TENANT_ID,
    SYSTEM_TENANT_ID,
)
from services.password_hashing import hash_password
from services.saa import SAAService, install_seeds_for_tenant

# EmailInvalidError / RoleInvalidError are re-imported rather than defined
# here since ADR-0121 §2 moved them alongside the validators they belong
# to. Both names stay part of this module's public surface — the package
# ``__init__`` and every CLI / web caller import them from here.
from services.user_validation import (  # noqa: F401 — re-exported public surface
    EmailInvalidError,
    RoleInvalidError,
    validate_email,
    validate_roles,
)
from services.watch_desk.seeding import install_default_watchpoints_for_tenant

_LOG = logging.getLogger("portfoliflow.super_admin")


# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------


# Subdomain validation per ADR-0064 §3 (mirrors cli/create_tenant.py).
_SUBDOMAIN_RE: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9-]*$")
_RESERVED_SUBDOMAINS: frozenset[str] = frozenset({"admin", "www", "api"})
_SUBDOMAIN_MIN_LEN: int = 3
_SUBDOMAIN_MAX_LEN: int = 63


# ---------------------------------------------------------------------------
# Typed result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantSummary:
    """Read-model returned from :func:`list_tenants` and creation calls.

    ``user_count`` is computed via a scalar subquery; the helper does
    not load user rows. The summary deliberately omits any tenant-data
    field (investment count, NAV freshness, etc.) so the super-admin
    surface stays compliant with ADR-0064 §1 (no tenant-data exposure).
    """

    id: UUID
    name: str
    subdomain: str
    is_active: bool
    created_at: datetime
    user_count: int


@dataclass(frozen=True)
class SuperAdminSummary:
    """Read-model returned from :func:`list_super_admins`.

    Includes deactivated super-admins (``is_active = FALSE``) so the
    UI can surface both states. ``last_login_at`` is best-effort and
    may be ``None`` for an account that has never logged in.
    """

    id: UUID
    email: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SuperAdminOperationError(PortfoliFlowError):
    """Base exception for platform-operations errors."""


class SubdomainTakenError(SuperAdminOperationError):
    """The requested subdomain is already in use by a different tenant."""


class SubdomainReservedError(SuperAdminOperationError):
    """The requested subdomain is in the reserved set."""


class SubdomainInvalidError(SuperAdminOperationError):
    """The subdomain doesn't match the required pattern."""


class TenantNotFoundError(SuperAdminOperationError):
    """No tenant matches the given subdomain or id."""


class UserNotFoundError(SuperAdminOperationError):
    """No user matches the given email and tenant."""


class OwnerNotFoundError(SuperAdminOperationError):
    """The tenant has no user with role ``'owner'`` to reset."""


class CannotDeactivateLastSuperAdminError(SuperAdminOperationError):
    """Refuse to deactivate the only remaining active super-admin."""


class CannotDeactivateSystemTenantError(SuperAdminOperationError):
    """Refuse to deactivate the system tenant."""


class CannotDeactivatePrimaryTenantError(SuperAdminOperationError):
    """Refuse to deactivate the primary tenant (Minathena Capital)."""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_subdomain(value: str) -> str:
    cleaned = value.strip().lower()
    if not (_SUBDOMAIN_MIN_LEN <= len(cleaned) <= _SUBDOMAIN_MAX_LEN):
        raise SubdomainInvalidError(
            f"subdomain length must be {_SUBDOMAIN_MIN_LEN}..{_SUBDOMAIN_MAX_LEN} characters"
        )
    if not _SUBDOMAIN_RE.match(cleaned):
        raise SubdomainInvalidError("subdomain must match ^[a-z][a-z0-9-]*$")
    if cleaned in _RESERVED_SUBDOMAINS:
        raise SubdomainReservedError(f"subdomain {cleaned!r} is reserved")
    return cleaned


# The email and role validators live in :mod:`services.user_validation`
# since ADR-0121 §2 — one rule shared with the tenant-scoped user
# service. These aliases keep the call sites below unchanged.
_validate_email = validate_email
_validate_roles = validate_roles


# ---------------------------------------------------------------------------
# Audit-write helper
# ---------------------------------------------------------------------------


async def _write_audit(
    conn: AsyncConnection,
    *,
    actor_super_admin_id: UUID | None,
    action: str,
    target_tenant_id: UUID | None,
    target_user_id: UUID | None,
    reason: str | None,
    payload: dict[str, Any] | None,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    """Write one row to ``super_admin_audit`` in the caller's transaction.

    No ``try/except`` wrapping — an INSERT failure rolls back the
    surrounding transaction, taking the underlying operation with it.
    That is the integrity guarantee ADR-0064 §4 commits to.

    ``actor_super_admin_id`` may be ``None`` on the bootstrap pathway
    (creating the first super-admin, before any super-admin exists).
    Migration b014 makes ``super_admin_audit.super_admin_user_id``
    nullable so the audit row honestly records "no prior actor".
    """
    await conn.execute(
        text(
            """
            INSERT INTO super_admin_audit
                (super_admin_user_id, action, target_tenant_id,
                 target_user_id, reason, payload, ip_address, user_agent)
            VALUES
                (:actor, :action, :target_tenant, :target_user,
                 :reason, CAST(:payload AS JSONB),
                 CAST(:ip AS INET), :ua)
            """
        ),
        {
            "actor": (str(actor_super_admin_id) if actor_super_admin_id else None),
            "action": action,
            "target_tenant": (str(target_tenant_id) if target_tenant_id else None),
            "target_user": (str(target_user_id) if target_user_id else None),
            "reason": reason,
            "payload": json.dumps(payload) if payload is not None else None,
            "ip": ip_address,
            "ua": user_agent,
        },
    )


# ---------------------------------------------------------------------------
# Tenant lookups
# ---------------------------------------------------------------------------


async def resolve_tenant_id_by_subdomain(conn: AsyncConnection, subdomain: str) -> UUID:
    """Look up a tenant id by subdomain.

    Returns the tenant id even when the tenant is deactivated. Callers
    that should refuse on deactivated tenants check ``is_active`` via
    :func:`list_tenants` or a dedicated lookup.

    Raises:
        TenantNotFoundError: If no tenant has the given subdomain.
    """
    result = await conn.execute(
        text("SELECT id FROM tenants WHERE subdomain = :sd"),
        {"sd": subdomain.strip().lower()},
    )
    row = result.first()
    if row is None:
        raise TenantNotFoundError(f"no tenant with subdomain {subdomain!r}")
    return UUID(str(row.id))


async def _get_tenant_summary(conn: AsyncConnection, tenant_id: UUID) -> TenantSummary:
    """Fetch one tenant row plus its user-count subquery."""
    result = await conn.execute(
        text(
            """
            SELECT t.id, t.name, t.subdomain, t.is_active,
                   t.created_at,
                   (SELECT COUNT(*) FROM users u
                    WHERE u.tenant_id = t.id)::int AS user_count
            FROM tenants t
            WHERE t.id = :tid
            """
        ),
        {"tid": str(tenant_id)},
    )
    row = result.first()
    if row is None:
        raise TenantNotFoundError(f"no tenant with id {tenant_id}")
    return TenantSummary(
        id=UUID(str(row.id)),
        name=row.name,
        subdomain=row.subdomain,
        is_active=bool(row.is_active),
        created_at=row.created_at,
        user_count=int(row.user_count),
    )


# ---------------------------------------------------------------------------
# Creation helpers
# ---------------------------------------------------------------------------


async def create_tenant_idempotent(
    conn: AsyncConnection,
    *,
    name: str,
    subdomain: str,
    owner_email: str,
    owner_password: str,
    actor_super_admin_id: UUID,
    actor_ip: str | None = None,
    actor_user_agent: str | None = None,
    owner_display_name: str | None = None,
) -> TenantSummary:
    """Create a tenant with its initial owner — atomic with the audit row.

    Idempotent on subdomain: re-running with the same ``(name,
    subdomain)`` returns the existing :class:`TenantSummary` without
    changes. The audit row is written **only on first creation**;
    idempotent no-ops do not generate audit traffic.

    Performs in order, inside ``conn``'s transaction:

    1. Validate name / subdomain / email (raise on failure).
    2. Check for an existing tenant by subdomain.
    3. If absent: INSERT ``tenants`` row, set ``app.tenant_id`` GUC,
       INSERT ``users`` row with role ``['owner']``, INSERT
       ``super_admin_audit`` row.
    4. Re-fetch and return the tenant summary with ``user_count``.

    Seed installation (asset classes, sectors, regions) is a separate
    concern, run by the caller via :func:`seed_tenant_defaults` after
    this transaction commits. Coupling seeds into the same
    transaction would entangle a heavyweight cross-table sequence
    with the audit-integrity invariant; the bootstrap.py pattern of
    independent per-step transactions is the established convention.

    Args:
        conn: An :class:`AsyncConnection` already inside a transaction
            (the caller's ``engine.begin()`` context). The audit row
            is written on the same connection — an audit failure rolls
            back the tenant and user inserts together.
        name: Tenant display name.
        subdomain: URL subdomain (lowercase letters / digits / hyphens,
            3..63 chars, not reserved).
        owner_email: Initial owner's email address.
        owner_password: Initial owner's plaintext password. Hashed
            once via :func:`hash_password` before insert; never logged.
        owner_display_name: Optional human display name for the initial
            owner (ADR-0068). ``None`` by default — nullable and never
            required.
        actor_super_admin_id: The super-admin performing the action.
            Required on this code path — only the bootstrap path
            (``create_super_admin_idempotent`` with no prior actor)
            is allowed to pass ``None``.
        actor_ip: Best-effort client IP for the audit row.
        actor_user_agent: Client user-agent string for the audit row.

    Returns:
        The :class:`TenantSummary` for the (now-existing) tenant.

    Raises:
        SubdomainInvalidError, SubdomainReservedError,
        SubdomainTakenError, EmailInvalidError on validation failures.
    """
    clean_name = name.strip()
    clean_subdomain = _validate_subdomain(subdomain)
    clean_email = _validate_email(owner_email)
    if not clean_name:
        raise SuperAdminOperationError("tenant name must not be empty")

    # Lookup by subdomain — idempotent path.
    existing = await conn.execute(
        text("SELECT id, name FROM tenants WHERE subdomain = :sd"),
        {"sd": clean_subdomain},
    )
    existing_row = existing.first()

    if existing_row is not None:
        # Re-running with a different name on the same subdomain is a
        # collision, not an idempotent no-op. The two-tenant case (two
        # operators both wanting subdomain "foo") is the one the
        # SubdomainTaken sentinel exists for.
        if existing_row.name != clean_name:
            raise SubdomainTakenError(
                f"subdomain {clean_subdomain!r} is already in use by tenant {existing_row.name!r}"
            )
        tenant_id = UUID(str(existing_row.id))
        _LOG.info("create-tenant: tenant %s already exists (no-op)", tenant_id)
        return await _get_tenant_summary(conn, tenant_id)

    tenant_id = uuid4()
    await conn.execute(
        text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, :subdomain)"),
        {
            "id": str(tenant_id),
            "name": clean_name,
            "subdomain": clean_subdomain,
        },
    )

    # Set the tenant GUC so the audit trigger on the user insert
    # records the right tenant. The GUC scope is LOCAL via
    # set_config(..., true), which ties it to the current
    # transaction — the next caller picking up this pooled
    # connection will not inherit the tenant id.
    await conn.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )

    hashed = hash_password(owner_password)
    await conn.execute(
        text(
            "INSERT INTO users "
            "(tenant_id, email, display_name, password_hash, roles, "
            " is_active) "
            "VALUES (:tid, :email, :display_name, :hash, "
            "ARRAY['owner']::text[], TRUE)"
        ),
        {
            "tid": str(tenant_id),
            "email": clean_email,
            "display_name": owner_display_name,
            "hash": hashed,
        },
    )

    await _write_audit(
        conn,
        actor_super_admin_id=actor_super_admin_id,
        action="create_tenant",
        target_tenant_id=tenant_id,
        target_user_id=None,
        reason=None,
        payload={
            "name": clean_name,
            "subdomain": clean_subdomain,
            "owner_email": clean_email,
        },
        ip_address=actor_ip,
        user_agent=actor_user_agent,
    )

    _LOG.info(
        "create-tenant: created tenant %s (subdomain=%r, owner=%s)",
        tenant_id,
        clean_subdomain,
        clean_email,
    )
    return await _get_tenant_summary(conn, tenant_id)


async def create_user_idempotent(
    conn: AsyncConnection,
    *,
    tenant_id: UUID,
    email: str,
    roles: Sequence[str],
    password: str,
    actor_super_admin_id: UUID,
    actor_ip: str | None = None,
    actor_user_agent: str | None = None,
    display_name: str | None = None,
) -> UUID:
    """Create a user in a target tenant — atomic with the audit row.

    Idempotent on ``(tenant_id, email)``. Returns the user id.
    Audit row written only on first creation. An optional
    ``display_name`` (ADR-0068) is captured on first creation; on the
    idempotent no-op path the existing row's name is left untouched.

    Raises:
        EmailInvalidError, RoleInvalidError, TenantNotFoundError.
    """
    clean_email = _validate_email(email)
    clean_roles = _validate_roles(roles)

    # Confirm the tenant exists — the FK on users.tenant_id would
    # catch this anyway, but a clean exception beats an opaque FK
    # error message for the route's banner.
    tenant_check = await conn.execute(
        text("SELECT 1 FROM tenants WHERE id = :tid"),
        {"tid": str(tenant_id)},
    )
    if tenant_check.first() is None:
        raise TenantNotFoundError(f"no tenant with id {tenant_id}")

    await conn.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )

    existing = await conn.execute(
        text("SELECT id FROM users WHERE tenant_id = :tid AND email = :email"),
        {"tid": str(tenant_id), "email": clean_email},
    )
    existing_row = existing.first()
    if existing_row is not None:
        _LOG.info(
            "create-user: %s already exists in tenant %s (no-op)",
            clean_email,
            tenant_id,
        )
        return UUID(str(existing_row.id))

    hashed = hash_password(password)
    inserted = await conn.execute(
        text(
            "INSERT INTO users "
            "(tenant_id, email, display_name, password_hash, roles, "
            " is_active) "
            "VALUES (:tid, :email, :display_name, :hash, :roles, TRUE) "
            "RETURNING id"
        ),
        {
            "tid": str(tenant_id),
            "email": clean_email,
            "display_name": display_name,
            "hash": hashed,
            "roles": clean_roles,
        },
    )
    new_user_id = UUID(str(inserted.scalar_one()))

    await _write_audit(
        conn,
        actor_super_admin_id=actor_super_admin_id,
        action="create_user",
        target_tenant_id=tenant_id,
        target_user_id=new_user_id,
        reason=None,
        payload={"email": clean_email, "roles": clean_roles},
        ip_address=actor_ip,
        user_agent=actor_user_agent,
    )

    _LOG.info(
        "create-user: created %s in tenant %s (roles=%r)",
        clean_email,
        tenant_id,
        clean_roles,
    )
    return new_user_id


async def create_super_admin_idempotent(
    conn: AsyncConnection,
    *,
    email: str,
    password: str,
    actor_super_admin_id: UUID | None,
    actor_ip: str | None = None,
    actor_user_agent: str | None = None,
    display_name: str | None = None,
) -> UUID:
    """Create a super-admin in the system tenant — atomic with the audit row.

    Idempotent on email. Roles are ``['owner']`` by convention
    (per ADR-0064 §3 the platform gate is ``is_super_admin``, not
    role-membership). An optional ``display_name`` (ADR-0068) is
    captured on first creation.

    ``actor_super_admin_id`` may be ``None`` on the bootstrap path
    (creating the very first super-admin, before any super-admin
    exists). After migration b014 the audit table accepts a NULL
    actor, so the bootstrap row honestly records "no prior actor".

    Raises:
        EmailInvalidError.
    """
    clean_email = _validate_email(email)

    await conn.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(SYSTEM_TENANT_ID)},
    )

    existing = await conn.execute(
        text("SELECT id FROM users WHERE tenant_id = :tid AND email = :email"),
        {"tid": str(SYSTEM_TENANT_ID), "email": clean_email},
    )
    existing_row = existing.first()
    if existing_row is not None:
        _LOG.info("create-super-admin: %s already exists (no-op)", clean_email)
        return UUID(str(existing_row.id))

    hashed = hash_password(password)
    inserted = await conn.execute(
        text(
            "INSERT INTO users "
            "(tenant_id, email, display_name, password_hash, roles, "
            " is_super_admin, is_active) "
            "VALUES (:tid, :email, :display_name, :hash, "
            "ARRAY['owner']::text[], TRUE, TRUE) "
            "RETURNING id"
        ),
        {
            "tid": str(SYSTEM_TENANT_ID),
            "email": clean_email,
            "display_name": display_name,
            "hash": hashed,
        },
    )
    new_user_id = UUID(str(inserted.scalar_one()))

    await _write_audit(
        conn,
        actor_super_admin_id=actor_super_admin_id,
        action="create_super_admin",
        target_tenant_id=SYSTEM_TENANT_ID,
        target_user_id=new_user_id,
        reason=None,
        payload={"email": clean_email},
        ip_address=actor_ip,
        user_agent=actor_user_agent,
    )

    _LOG.info(
        "create-super-admin: created super-admin %s (%s)",
        clean_email,
        new_user_id,
    )
    return new_user_id


# ---------------------------------------------------------------------------
# Deactivation / reactivation / password reset
# ---------------------------------------------------------------------------


async def deactivate_tenant(
    conn: AsyncConnection,
    *,
    tenant_id: UUID,
    actor_super_admin_id: UUID,
    actor_ip: str | None = None,
    actor_user_agent: str | None = None,
) -> TenantSummary:
    """Set ``tenants.is_active = FALSE`` on the target tenant.

    Side effects in the same transaction:

    - DELETE all sessions for users in the target tenant — a
      deactivated tenant must not have any live sessions left.
    - Write the audit row.

    Refuses to deactivate the system tenant or the primary tenant.
    Both are infrastructure invariants — deactivating either is a
    catastrophic operation that must not be possible from the web UI.

    Raises:
        CannotDeactivateSystemTenantError,
        CannotDeactivatePrimaryTenantError, TenantNotFoundError.
    """
    if tenant_id == SYSTEM_TENANT_ID:
        raise CannotDeactivateSystemTenantError("cannot deactivate the system tenant")
    if tenant_id == PRIMARY_TENANT_ID:
        raise CannotDeactivatePrimaryTenantError("cannot deactivate the primary tenant")

    result = await conn.execute(
        text("UPDATE tenants SET is_active = FALSE WHERE id = :tid RETURNING id"),
        {"tid": str(tenant_id)},
    )
    if result.first() is None:
        raise TenantNotFoundError(f"no tenant with id {tenant_id}")

    # Invalidate every active session for the tenant. The audit
    # engine bypasses RLS so a cross-tenant DELETE works here.
    await conn.execute(
        text("DELETE FROM sessions WHERE tenant_id = :tid"),
        {"tid": str(tenant_id)},
    )

    await _write_audit(
        conn,
        actor_super_admin_id=actor_super_admin_id,
        action="deactivate_tenant",
        target_tenant_id=tenant_id,
        target_user_id=None,
        reason=None,
        payload=None,
        ip_address=actor_ip,
        user_agent=actor_user_agent,
    )

    _LOG.info("deactivate-tenant: tenant %s deactivated", tenant_id)
    return await _get_tenant_summary(conn, tenant_id)


async def reactivate_tenant(
    conn: AsyncConnection,
    *,
    tenant_id: UUID,
    actor_super_admin_id: UUID,
    actor_ip: str | None = None,
    actor_user_agent: str | None = None,
) -> TenantSummary:
    """Set ``tenants.is_active = TRUE`` on the target tenant.

    No business-rule guards — reactivating the system or primary
    tenant is a no-op (both are already active by invariant), and
    other tenants are freely reactivatable. The audit row records
    the intent regardless.

    Raises:
        TenantNotFoundError.
    """
    result = await conn.execute(
        text("UPDATE tenants SET is_active = TRUE WHERE id = :tid RETURNING id"),
        {"tid": str(tenant_id)},
    )
    if result.first() is None:
        raise TenantNotFoundError(f"no tenant with id {tenant_id}")

    await _write_audit(
        conn,
        actor_super_admin_id=actor_super_admin_id,
        action="reactivate_tenant",
        target_tenant_id=tenant_id,
        target_user_id=None,
        reason=None,
        payload=None,
        ip_address=actor_ip,
        user_agent=actor_user_agent,
    )

    _LOG.info("reactivate-tenant: tenant %s reactivated", tenant_id)
    return await _get_tenant_summary(conn, tenant_id)


async def reset_owner_password(
    conn: AsyncConnection,
    *,
    tenant_id: UUID,
    new_password: str,
    actor_super_admin_id: UUID,
    actor_ip: str | None = None,
    actor_user_agent: str | None = None,
) -> str:
    """Reset the password of the tenant's owner.

    The "owner" is the **first user in the tenant whose roles
    contain 'owner'**, ordered by ``created_at`` ascending. If the
    tenant has zero owner users, raise :class:`OwnerNotFoundError`.
    If the tenant has multiple owners (allowed by the role model,
    if rare), only the first by ``created_at`` is touched; the
    return value tells the caller which email was affected.

    Side effects in the same transaction:

    - UPDATE ``users.password_hash`` for the resolved owner.
    - DELETE all sessions for that owner (per OWASP — a credential
      change must invalidate live sessions).
    - Audit row written.

    Returns:
        The email of the user whose password was reset.

    Raises:
        TenantNotFoundError, OwnerNotFoundError.
    """
    tenant_check = await conn.execute(
        text("SELECT 1 FROM tenants WHERE id = :tid"),
        {"tid": str(tenant_id)},
    )
    if tenant_check.first() is None:
        raise TenantNotFoundError(f"no tenant with id {tenant_id}")

    owner = await conn.execute(
        text(
            """
            SELECT id, email FROM users
            WHERE tenant_id = :tid AND 'owner' = ANY(roles)
            ORDER BY created_at ASC
            LIMIT 1
            """
        ),
        {"tid": str(tenant_id)},
    )
    owner_row = owner.first()
    if owner_row is None:
        raise OwnerNotFoundError(f"tenant {tenant_id} has no user with role 'owner'")

    owner_id = UUID(str(owner_row.id))
    owner_email = str(owner_row.email)

    await conn.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )

    hashed = hash_password(new_password)
    await conn.execute(
        text("UPDATE users SET password_hash = :hash WHERE id = :id"),
        {"hash": hashed, "id": str(owner_id)},
    )
    await conn.execute(
        text("DELETE FROM sessions WHERE user_id = :uid"),
        {"uid": str(owner_id)},
    )

    await _write_audit(
        conn,
        actor_super_admin_id=actor_super_admin_id,
        action="reset_owner_password",
        target_tenant_id=tenant_id,
        target_user_id=owner_id,
        reason=None,
        payload={"owner_email": owner_email},
        ip_address=actor_ip,
        user_agent=actor_user_agent,
    )

    _LOG.info(
        "reset-owner-password: tenant %s owner %s (%s) — sessions cleared",
        tenant_id,
        owner_email,
        owner_id,
    )
    return owner_email


async def deactivate_super_admin(
    conn: AsyncConnection,
    *,
    user_id: UUID,
    actor_super_admin_id: UUID,
    actor_ip: str | None = None,
    actor_user_agent: str | None = None,
) -> None:
    """Deactivate a super-admin user.

    Refuses if the deactivation would leave zero active super-admins.
    Sessions for the deactivated super-admin are deleted in the same
    transaction.

    Raises:
        UserNotFoundError,
        CannotDeactivateLastSuperAdminError.
    """
    # Confirm the target exists and is in the system tenant.
    check = await conn.execute(
        text(
            """
            SELECT id FROM users
            WHERE id = :uid
              AND tenant_id = :sys
              AND is_super_admin = TRUE
            """
        ),
        {"uid": str(user_id), "sys": str(SYSTEM_TENANT_ID)},
    )
    if check.first() is None:
        raise UserNotFoundError(f"no super-admin user with id {user_id}")

    # Count remaining active super-admins after the deactivation.
    remaining = await conn.execute(
        text(
            """
            SELECT COUNT(*) AS n FROM users
            WHERE tenant_id = :sys
              AND is_super_admin = TRUE
              AND is_active = TRUE
              AND id != :uid
            """
        ),
        {"sys": str(SYSTEM_TENANT_ID), "uid": str(user_id)},
    )
    remaining_count = int(remaining.scalar_one())
    if remaining_count == 0:
        raise CannotDeactivateLastSuperAdminError(
            "cannot deactivate the only remaining active super-admin"
        )

    await conn.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(SYSTEM_TENANT_ID)},
    )
    await conn.execute(
        text("UPDATE users SET is_active = FALSE WHERE id = :uid"),
        {"uid": str(user_id)},
    )
    await conn.execute(
        text("DELETE FROM sessions WHERE user_id = :uid"),
        {"uid": str(user_id)},
    )

    await _write_audit(
        conn,
        actor_super_admin_id=actor_super_admin_id,
        action="deactivate_super_admin",
        target_tenant_id=SYSTEM_TENANT_ID,
        target_user_id=user_id,
        reason=None,
        payload=None,
        ip_address=actor_ip,
        user_agent=actor_user_agent,
    )

    _LOG.info(
        "deactivate-super-admin: user %s deactivated; %d active super-admin(s) remain",
        user_id,
        remaining_count,
    )


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------


async def list_tenants(
    conn: AsyncConnection,
    *,
    include_system: bool = False,
) -> list[TenantSummary]:
    """List tenants ordered by ``created_at`` ascending.

    Args:
        include_system: When False (the web UI default), the system
            tenant is omitted from the result. The CLI may pass True
            for full visibility.
    """
    if include_system:
        result = await conn.execute(
            text(
                """
                SELECT t.id, t.name, t.subdomain, t.is_active,
                       t.created_at,
                       (SELECT COUNT(*) FROM users u
                        WHERE u.tenant_id = t.id)::int AS user_count
                FROM tenants t
                ORDER BY t.created_at ASC
                """
            )
        )
    else:
        result = await conn.execute(
            text(
                """
                SELECT t.id, t.name, t.subdomain, t.is_active,
                       t.created_at,
                       (SELECT COUNT(*) FROM users u
                        WHERE u.tenant_id = t.id)::int AS user_count
                FROM tenants t
                WHERE t.id != :sys
                ORDER BY t.created_at ASC
                """
            ),
            {"sys": str(SYSTEM_TENANT_ID)},
        )
    return [
        TenantSummary(
            id=UUID(str(row.id)),
            name=row.name,
            subdomain=row.subdomain,
            is_active=bool(row.is_active),
            created_at=row.created_at,
            user_count=int(row.user_count),
        )
        for row in result
    ]


async def list_super_admins(
    conn: AsyncConnection,
) -> list[SuperAdminSummary]:
    """List super-admins (active and deactivated) by ``created_at``.

    ``last_login_at`` is sourced from the latest successful row in
    ``login_audit`` for the user; a user that has never logged in
    has ``last_login_at = None``.
    """
    result = await conn.execute(
        text(
            """
            SELECT
                u.id,
                u.email,
                u.is_active,
                u.created_at,
                (
                    SELECT MAX(la.created_at)
                    FROM login_audit la
                    WHERE la.user_id = u.id
                      AND la.success = TRUE
                ) AS last_login_at
            FROM users u
            WHERE u.tenant_id = :sys
              AND u.is_super_admin = TRUE
            ORDER BY u.created_at ASC
            """
        ),
        {"sys": str(SYSTEM_TENANT_ID)},
    )
    return [
        SuperAdminSummary(
            id=UUID(str(row.id)),
            email=row.email,
            is_active=bool(row.is_active),
            created_at=row.created_at,
            last_login_at=row.last_login_at,
        )
        for row in result
    ]


# ---------------------------------------------------------------------------
# Seed installation — post-creation, independent transactions
# ---------------------------------------------------------------------------


async def seed_tenant_defaults(
    engine: AsyncEngine,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
) -> None:
    """Install the standard per-tenant default seed data.

    Runs as a sequence of independent tenant-scoped transactions,
    mirroring the :mod:`cli.bootstrap` pattern: a failure in one seed
    step never rolls back the earlier ones, and none of them affect
    the already-committed tenant + owner + audit transaction the
    caller of :func:`create_tenant_idempotent` ran.

    Seeds installed:

    - SAA configurations (3 templates) + the asset classes they
      reference, via :func:`services.saa.install_seeds_for_tenant`.
    - The ``unclassified`` asset class (Excel-import safety net,
      ADR-0043 §1), via
      :func:`cli.bootstrap.install_unclassified_asset_class`.
    - The Phase-7 default asset-class catalogue (AnlV /
      Anlagegrenzen vocabulary), via
      :func:`cli.bootstrap.install_default_asset_classes`.
    - The ``unclassified`` sector (Excel-import safety net).
    - The default region catalogue + memberships (ADR-0046 M1).
    - The market-data system actor and the (disabled) market-data
      schedule row (Live Data Import, ADR-0093 §0.1 / §1), via the
      :mod:`cli.bootstrap` installers.
    - The (enabled) Irene schedule row (ADR-0119 §4), via
      :func:`cli.bootstrap.install_irene_schedule`, so the Watch Desk
      has a cadence out of the box.
    - The Watch Desk default watchpoints (ADR-0116 §8), via
      :func:`services.watch_desk.seeding.install_default_watchpoints_for_tenant`.

    Per ADR-0077, the two asset-class steps bring this routine to
    full per-tenant seed parity with :mod:`cli.bootstrap`, so a
    tenant provisioned via ``portfoliflow create-tenant`` ends up
    with the same default catalogue as the bootstrapped primary
    tenant. They run in the bootstrap order (unclassified first,
    then the catalogue) so the fallback row is always present even
    if the catalogue is later edited.

    The caller is the CLI (``portfoliflow create-tenant``,
    ``portfoliflow bootstrap``) or the super-admin web route. Both
    treat seed failures as best-effort warnings — the tenant exists
    and is usable; missing seeds can be re-installed by re-running
    bootstrap-equivalent flows.

    Args:
        engine: An :class:`AsyncEngine` capable of opening
            tenant-scoped sessions (typically the superuser engine
            for CLI use; the audit engine for web routes).
        tenant_id: The target tenant. Must already exist (created by
            :func:`create_tenant_idempotent` in a prior transaction).
        actor_user_id: The user attributed for the seed rows (the
            tenant owner, the bootstrap actor, etc.).
    """
    # SAA configurations + asset classes — uses the existing service
    # layer so the seeds match what bootstrap.py installs.
    async with tenant_context(engine, tenant_id, user_id=actor_user_id) as session:
        saa_service = SAAService(
            configurations=SAAConfigurationRepository(session),
            asset_classes=AssetClassRepository(session),
            inputs=SAAAssetClassInputRepository(session),
            correlations=SAACorrelationRepository(session),
        )
        await install_seeds_for_tenant(saa_service, actor_user_id)
        _LOG.info(
            "seed_tenant_defaults: SAA seeds installed for tenant %s",
            tenant_id,
        )

    # Unclassified asset class — re-uses the bootstrap installer so the
    # ADR-0043 fallback bucket is present for every tenant, not just the
    # bootstrapped primary tenant (ADR-0077). Function-scope import
    # mirrors the install_default_regions pattern below: it avoids the
    # load-time services -> cli circular import.
    from cli.bootstrap import install_unclassified_asset_class  # noqa: PLC0415

    async with tenant_context(engine, tenant_id, user_id=actor_user_id) as session:
        await install_unclassified_asset_class(AssetClassRepository(session))
        _LOG.info(
            "seed_tenant_defaults: unclassified asset class ensured for tenant %s",
            tenant_id,
        )

    # Phase-7 default asset-class catalogue — restores AnlV /
    # Anlagegrenzen parity with the primary tenant (ADR-0077). Runs after
    # the unclassified step so the fallback row is always present.
    from cli.bootstrap import install_default_asset_classes  # noqa: PLC0415

    async with tenant_context(engine, tenant_id, user_id=actor_user_id) as session:
        await install_default_asset_classes(AssetClassRepository(session))
        _LOG.info(
            "seed_tenant_defaults: default asset classes installed for tenant %s",
            tenant_id,
        )

    # Unclassified sector.
    async with tenant_context(engine, tenant_id, user_id=actor_user_id) as session:
        sectors = SectorRepository(session)
        if await sectors.get_by_code("unclassified") is None:
            await sectors.create(
                code="unclassified",
                display_name="Unclassified",
                created_by=actor_user_id,
            )
        _LOG.info(
            "seed_tenant_defaults: unclassified sector ensured for tenant %s",
            tenant_id,
        )

    # Default regions + memberships — re-imports the same catalogue
    # bootstrap.py uses, via the bootstrap helper.
    from cli.bootstrap import install_default_regions  # noqa: PLC0415

    async with tenant_context(engine, tenant_id, user_id=actor_user_id) as session:
        await install_default_regions(
            RegionRepository(session),
            CountryRepository(session),
        )
        _LOG.info(
            "seed_tenant_defaults: default regions installed for tenant %s",
            tenant_id,
        )

    # Live Data Import (#036, ADR-0093) — the per-tenant market-data system
    # actor and the (disabled) schedule row, via the same bootstrap
    # installers so create-tenant tenants reach full seed parity with the
    # bootstrapped primary tenant (ADR-0077). Function-scope imports mirror
    # the pattern above and keep the market-data refresh core / provider
    # adapters out of this module's (web-reachable) import graph.
    from cli.bootstrap import (  # noqa: PLC0415
        install_market_data_schedule,
        install_market_data_system_actor,
    )
    from core.repositories.market_data_schedule_repository import (  # noqa: PLC0415
        MarketDataScheduleRepository,
    )

    async with tenant_context(engine, tenant_id, user_id=actor_user_id) as session:
        await install_market_data_system_actor(UserRepository(session))
        await install_market_data_schedule(
            MarketDataScheduleRepository(session),
            now=datetime.now(timezone.utc),
        )
        _LOG.info(
            "seed_tenant_defaults: market-data system actor + schedule installed for tenant %s",
            tenant_id,
        )

    # Watch Desk cadence (ADR-0119 §4) — the tenant-level irene_schedule
    # row, seeded *enabled* so the area is alive from the first render
    # rather than waiting for an operator to save the cadence panel once.
    # Same function-scope-import pattern as the steps above, for the same
    # reason: it keeps cli.bootstrap out of this module's load-time graph.
    from cli.bootstrap import install_irene_schedule  # noqa: PLC0415
    from core.repositories.irene_schedule_repository import (  # noqa: PLC0415
        IreneScheduleRepository,
    )

    async with tenant_context(engine, tenant_id, user_id=actor_user_id) as session:
        await install_irene_schedule(
            IreneScheduleRepository(session),
            now=datetime.now(timezone.utc),
        )
        _LOG.info(
            "seed_tenant_defaults: irene schedule installed for tenant %s",
            tenant_id,
        )

    # Watch Desk defaults (ADR-0116 §8) — the freshness and liquidity
    # singletons plus one fx watchpoint per currency pair the book uses.
    # A freshly created tenant has no book yet, so only the singletons land
    # here; the installer is idempotent and re-runnable, and
    # `portfoliflow seed-watchpoints` picks up the rest once data arrives.
    # Called directly on the service (not through cli.bootstrap like the
    # steps above) because it lives in services/ already — no function-scope
    # import is needed to keep the import graph acyclic.
    created = await install_default_watchpoints_for_tenant(engine, tenant_id, actor_user_id)
    _LOG.info(
        "seed_tenant_defaults: %d default watchpoint(s) installed for tenant %s",
        created,
        tenant_id,
    )
