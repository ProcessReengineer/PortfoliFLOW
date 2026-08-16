# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The six tenant-scoped user operations a tenant owner performs.

Per ADR-0121 §1, §3 and §4: list, create, deactivate, reactivate, reset
password, change role — over the users of **one** tenant, the tenant
whose context the caller has already opened.

Every function takes an :class:`AsyncSession` that is already inside
``tenant_context(engine, tenant_id, user_id=actor_user_id)``, so it runs
under the unprivileged application role (ADR-0078). Two properties follow
from that, and they are why this module writes no security code of its
own:

- **RLS confines the reach.** A user id belonging to another tenant is
  invisible to every read here, so it surfaces as
  :class:`~services.tenant_users.errors.UserNotFoundError` rather than as
  a write that crossed the boundary.
- **The audit trigger attributes the actor.** ``users_audit_trigger``
  reads the ``app.user_id`` GUC that ``tenant_context`` set, so each
  write lands in ``audit_log`` with the acting owner attached. Nothing
  here writes ``super_admin_audit`` — that table belongs to the platform
  surface (ADR-0064), and a tenant-level act is not a platform event.

The caller owns the transaction: guard reads, the write, and session
invalidation all happen inside it, and a rejected operation leaves no
partial state behind.

``actor_user_id`` is passed explicitly rather than read back from the
GUC. The guards are a property of the call, and a service that re-derived
its actor from the connection would be trusting the caller's context to
say something the caller never stated.

**Concurrency posture.** The last-active-owner guards read inside the
writing transaction without ``FOR UPDATE`` or an advisory lock — the same
posture as ``deactivate_super_admin``'s last-super-admin guard
(ADR-0064). Two owners deactivating each other in genuinely concurrent
transactions can therefore both pass their guard under READ COMMITTED.
The exposure is a two-owner tenant acting simultaneously on itself, the
recovery is the operator CLI, and matching the established posture beats
inventing a stricter one for the tenant side alone.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.repositories.user_repository import UserDTO, UserRepository
from services.auth.password_policy import validate_password_strength
from services.auth.session import SessionRepository
from services.password_hashing import hash_password
from services.tenant_users.errors import (
    CannotDeactivateLastOwnerError,
    CannotDeactivateSelfError,
    CannotDemoteLastOwnerError,
    EmailTakenError,
    UserNotFoundError,
)
from services.user_validation import RoleInvalidError, validate_email, validate_roles

__all__ = [
    "MANAGEABLE_ROLES",
    "OWNER_ROLE",
    "change_role",
    "create_user",
    "deactivate_user",
    "list_users",
    "reactivate_user",
    "reset_password",
]

_LOG = logging.getLogger("portfoliflow.services.tenant_users")

#: The role the last-active-owner guards count.
OWNER_ROLE: str = "owner"

#: The roles assignable from the tenant surface. ADR-0121 §6 narrows to
#: owner and member: ``auditor`` is legal in the schema and in
#: :data:`core.repositories.user_repository.ALLOWED_ROLES`, and the CLI
#: and super-admin paths still accept it, but it gates nothing anywhere
#: in the codebase — offering it here would promise a capability that
#: does not exist.
MANAGEABLE_ROLES: frozenset[str] = frozenset({"owner", "member"})


def _require_manageable_role(role: str) -> str:
    """Return the cleaned role, or raise if it is not owner / member.

    Runs the shared :func:`services.user_validation.validate_roles` first
    so an unknown value reports the same message it would anywhere else,
    then applies the ADR-0121 §6 narrowing.

    Args:
        role: The requested role, in any case.

    Returns:
        The cleaned, lower-cased role.

    Raises:
        RoleInvalidError: If the role is empty, unknown, or known but not
            assignable from the tenant surface.
    """
    cleaned = validate_roles([role])[0]
    if cleaned not in MANAGEABLE_ROLES:
        raise RoleInvalidError(
            f"role {cleaned!r} cannot be assigned from the tenant surface; "
            f"allowed {sorted(MANAGEABLE_ROLES)!r}"
        )
    return cleaned


async def _count_active_owners(users: UserRepository) -> int:
    """Count the tenant's active owner users.

    The single counting helper both last-owner guards share (ADR-0121
    §4). It reads through :meth:`UserRepository.list_all`, so RLS decides
    the population and the count is over this tenant by construction. A
    tenant's user list is small enough that counting in Python costs
    nothing and keeps the read on the repository seam.

    Args:
        users: A repository bound to the caller's tenant-scoped session.

    Returns:
        The number of active users holding :data:`OWNER_ROLE`.
    """
    return sum(1 for u in await users.list_all() if u.is_active and OWNER_ROLE in u.roles)


async def list_users(session: AsyncSession) -> list[UserDTO]:
    """Return every user of the tenant, oldest first.

    Args:
        session: A session inside the caller's ``tenant_context``.

    Returns:
        The tenant's users — active and deactivated alike, since the
        owner surface has to show both to be able to reactivate.
    """
    return await UserRepository(session).list_all()


async def create_user(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    email: str,
    password: str,
    role: str,
    display_name: str | None = None,
) -> UserDTO:
    """Create an active user in the tenant with a single role.

    Validation runs before any database work, so a rejected request costs
    a round trip to nothing. The password policy is the existing
    :func:`services.auth.password_policy.validate_password_strength` —
    one policy, one source (ADR-0121 §3).

    The duplicate-email pre-check runs inside the caller's transaction
    for the error message's sake; ``uq_users_tenant_email`` remains the
    real guarantee. Note that uniqueness is **per tenant** — the same
    address may hold an account in another tenant, and that is a
    supported case, not a collision.

    Args:
        session: A session inside the caller's ``tenant_context``.
        actor_user_id: The owner performing the creation. Recorded in
            ``audit_log`` by the trigger via the ``app.user_id`` GUC; the
            argument is what the caller states, and is logged.
        email: The new user's email address.
        password: The initial plaintext password. Hashed once via
            :func:`services.password_hashing.hash_password`; never
            logged, never returned.
        role: ``'owner'`` or ``'member'`` (ADR-0121 §6).
        display_name: Optional human display name (ADR-0068). Blank input
            is normalised to ``None`` so the column holds a name or
            nothing, never an empty string.

    Returns:
        The created :class:`UserDTO`.

    Raises:
        EmailInvalidError: If the email is not email-shaped.
        RoleInvalidError: If the role is unknown or not assignable here.
        ValidationError: If the password fails the strength policy.
        EmailTakenError: If the tenant already has a user with that
            email, active or deactivated.
    """
    clean_email = validate_email(email)
    clean_role = _require_manageable_role(role)
    validate_password_strength(password)
    clean_display_name = display_name.strip() if display_name else None

    users = UserRepository(session)
    if await users.get_by_email(clean_email) is not None:
        raise EmailTakenError(f"a user with email {clean_email!r} already exists in this tenant")

    created = await users.create(
        email=clean_email,
        password_hash=hash_password(password),
        roles=[clean_role],
        display_name=clean_display_name or None,
    )
    _LOG.info(
        "tenant-users: user %s created %s (%s, role=%s)",
        actor_user_id,
        created.id,
        clean_email,
        clean_role,
    )
    return created


async def deactivate_user(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    user_id: UUID,
) -> UserDTO:
    """Deactivate a user and drop every session they hold.

    Two guards, in this order (ADR-0121 §4):

    1. **No self-deactivation**, whoever else is left. Checked before the
       target is even read — the rule does not depend on the target's
       state.
    2. **The last active owner stays.** Deactivating the only active
       owner would leave the tenant unable to administer itself.

    Session deletion runs in the caller's transaction, so a deactivated
    user cannot continue on an already-issued cookie (ADR-0121 §4.5).

    Args:
        session: A session inside the caller's ``tenant_context``.
        actor_user_id: The owner performing the deactivation.
        user_id: The user to deactivate.

    Returns:
        The deactivated :class:`UserDTO`.

    Raises:
        CannotDeactivateSelfError: If ``user_id`` is the actor.
        UserNotFoundError: If the id does not resolve in this tenant.
        CannotDeactivateLastOwnerError: If the target is the tenant's
            last active owner.
    """
    if user_id == actor_user_id:
        raise CannotDeactivateSelfError(
            "you cannot deactivate your own account; another owner must do it"
        )

    users = UserRepository(session)
    target = await users.get_by_id(user_id)
    if target is None:
        raise UserNotFoundError(f"no user with id {user_id} in this tenant")

    if target.is_active and OWNER_ROLE in target.roles and await _count_active_owners(users) <= 1:
        raise CannotDeactivateLastOwnerError(
            "cannot deactivate the only remaining active owner of this tenant"
        )

    updated = await users.set_active(user_id, False)
    if updated is None:  # pragma: no cover - unreachable after the read above
        raise UserNotFoundError(f"no user with id {user_id} in this tenant")

    await SessionRepository(session).delete_all_for_user(user_id)
    _LOG.info(
        "tenant-users: user %s deactivated %s (%s) — sessions cleared",
        actor_user_id,
        user_id,
        updated.email,
    )
    return updated


async def reactivate_user(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    user_id: UUID,
) -> UserDTO:
    """Reactivate a deactivated user.

    No guard beyond existence: restoring an account can never violate the
    last-active-owner invariant, and reactivating an already-active user
    is an idempotent no-op. The user gets no session back — they log in
    again, which is the point of having had their sessions dropped.

    Args:
        session: A session inside the caller's ``tenant_context``.
        actor_user_id: The owner performing the reactivation.
        user_id: The user to reactivate.

    Returns:
        The reactivated :class:`UserDTO`.

    Raises:
        UserNotFoundError: If the id does not resolve in this tenant.
    """
    updated = await UserRepository(session).set_active(user_id, True)
    if updated is None:
        raise UserNotFoundError(f"no user with id {user_id} in this tenant")

    _LOG.info(
        "tenant-users: user %s reactivated %s (%s)",
        actor_user_id,
        user_id,
        updated.email,
    )
    return updated


async def reset_password(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    user_id: UUID,
    new_password: str,
) -> UserDTO:
    """Set a new password for a user and drop every session they hold.

    The owner performs resets directly — there is no "must change at
    first login" flag in v1 (ADR-0121 §3). Rotating the hash and deleting
    the user's sessions in one transaction follows OWASP guidance and the
    ``portfoliflow set-password`` precedent: a credential change must not
    leave live sessions behind that the old credential established.

    Args:
        session: A session inside the caller's ``tenant_context``.
        actor_user_id: The owner performing the reset.
        user_id: The user whose password is being reset.
        new_password: The new plaintext password. Hashed once; never
            logged.

    Returns:
        The updated :class:`UserDTO`.

    Raises:
        ValidationError: If the password fails the strength policy.
        UserNotFoundError: If the id does not resolve in this tenant.
    """
    validate_password_strength(new_password)

    users = UserRepository(session)
    if await users.get_by_id(user_id) is None:
        raise UserNotFoundError(f"no user with id {user_id} in this tenant")

    await users.set_password_hash(user_id, hash_password(new_password))
    await SessionRepository(session).delete_all_for_user(user_id)

    updated = await users.get_by_id(user_id)
    if updated is None:  # pragma: no cover - unreachable after the read above
        raise UserNotFoundError(f"no user with id {user_id} in this tenant")

    _LOG.info(
        "tenant-users: user %s reset the password of %s (%s) — sessions cleared",
        actor_user_id,
        user_id,
        updated.email,
    )
    return updated


async def change_role(
    session: AsyncSession,
    *,
    actor_user_id: UUID,
    user_id: UUID,
    new_role: str,
) -> UserDTO:
    """Set a user's role to ``owner`` or ``member``.

    Roles are single-valued on this surface in v1: the new role replaces
    the set rather than joining it.

    One guard (ADR-0121 §4.1): the last active owner cannot be demoted.
    Demoting an *inactive* owner is allowed — they are not part of the
    active-owner count the invariant protects. Self-demotion is
    deliberately **not** guarded (§4.3): handing over to another owner
    and stepping down is the legitimate case, and it is only reachable
    while a second active owner exists. The demoted owner's session stays
    valid; the next owner-gated request answers 403.

    Args:
        session: A session inside the caller's ``tenant_context``.
        actor_user_id: The owner performing the change.
        user_id: The user whose role is changing.
        new_role: ``'owner'`` or ``'member'`` (ADR-0121 §6).

    Returns:
        The updated :class:`UserDTO`.

    Raises:
        RoleInvalidError: If the role is unknown or not assignable here.
        UserNotFoundError: If the id does not resolve in this tenant.
        CannotDemoteLastOwnerError: If the change would demote the
            tenant's last active owner.
    """
    clean_role = _require_manageable_role(new_role)

    users = UserRepository(session)
    target = await users.get_by_id(user_id)
    if target is None:
        raise UserNotFoundError(f"no user with id {user_id} in this tenant")

    demotes_an_owner = OWNER_ROLE in target.roles and clean_role != OWNER_ROLE
    if demotes_an_owner and target.is_active and await _count_active_owners(users) <= 1:
        raise CannotDemoteLastOwnerError(
            "cannot demote the only remaining active owner of this tenant"
        )

    updated = await users.set_roles(user_id, [clean_role])
    if updated is None:  # pragma: no cover - unreachable after the read above
        raise UserNotFoundError(f"no user with id {user_id} in this tenant")

    _LOG.info(
        "tenant-users: user %s set the role of %s (%s) to %s",
        actor_user_id,
        user_id,
        updated.email,
        clean_role,
    )
    return updated
