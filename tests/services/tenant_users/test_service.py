# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""End-to-end tests for ``services.tenant_users`` (ADR-0121 U1).

Every call runs under the unprivileged ``portfoliflow_app`` role inside
``tenant_context``, exactly as the U2 routes will run it. That is
load-bearing for two of the invariants under test: the cross-tenant cases
are decided by RLS rather than by an application check, and the audit
attribution comes from the ``users_audit_trigger`` reading the
``app.user_id`` GUC.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import ValidationError
from core.repositories import UserRepository, tenant_context
from core.repositories.user_repository import UserDTO
from services.auth.session import SessionRepository
from services.tenant_users import (
    CannotDeactivateLastOwnerError,
    CannotDeactivateSelfError,
    CannotDemoteLastOwnerError,
    EmailTakenError,
    UserNotFoundError,
    change_role,
    create_user,
    deactivate_user,
    list_users,
    reactivate_user,
    reset_password,
)
from services.user_validation import EmailInvalidError, RoleInvalidError

#: Satisfies the policy (>= 12 chars, >= 2 character classes).
STRONG_PASSWORD = "Str0ng-Passphrase!"
#: Fails the length floor.
WEAK_PASSWORD = "short"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_user(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    email: str,
    roles: tuple[str, ...] = ("owner",),
) -> UserDTO:
    """Create a user directly through the repository.

    Used for the *given* half of each test — the actor and the bystanders
    — so a failure in the service under test cannot be mistaken for a
    broken fixture. A placeholder hash is enough: no test here logs in.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        return await UserRepository(session).create(
            email=email, password_hash="x" * 8, roles=list(roles)
        )


async def _seed_login_session(app_engine: AsyncEngine, tenant_id: UUID, user: UserDTO) -> None:
    """Give ``user`` one live session row."""
    async with tenant_context(app_engine, tenant_id) as session:
        await SessionRepository(session).create_session(user, None, None)


async def _count_sessions(app_engine: AsyncEngine, tenant_id: UUID, user_id: UUID) -> int:
    """Count live session rows for ``user_id``."""
    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(
            text("SELECT count(*) FROM sessions WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
        return int(result.scalar_one())


async def _get_user(app_engine: AsyncEngine, tenant_id: UUID, user_id: UUID) -> UserDTO | None:
    """Re-read a user in a fresh session — the state that actually landed."""
    async with tenant_context(app_engine, tenant_id) as session:
        return await UserRepository(session).get_by_id(user_id)


# ---------------------------------------------------------------------------
# list_users
# ---------------------------------------------------------------------------


async def test_list_users_returns_the_tenants_users(app_engine: AsyncEngine, seed_tenant) -> None:
    """Both active and deactivated users are listed; other tenants are not."""
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")
    owner = await _seed_user(app_engine, tenant_a, "owner@a.example")
    member = await _seed_user(app_engine, tenant_a, "member@a.example", roles=("member",))
    await _seed_user(app_engine, tenant_b, "owner@b.example")

    async with tenant_context(app_engine, tenant_a, user_id=owner.id) as session:
        await deactivate_user(session, actor_user_id=owner.id, user_id=member.id)

    async with tenant_context(app_engine, tenant_a, user_id=owner.id) as session:
        listed = await list_users(session)

    assert [u.email for u in listed] == ["owner@a.example", "member@a.example"]
    assert [u.is_active for u in listed] == [True, False]


# ---------------------------------------------------------------------------
# create_user
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["member", "owner"])
async def test_create_user_happy_path(app_engine: AsyncEngine, seed_tenant, role: str) -> None:
    """A created user is active, single-roled, and has a usable hash."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        created = await create_user(
            session,
            actor_user_id=actor.id,
            email="  New.User@example.com  ",
            password=STRONG_PASSWORD,
            role=role,
            display_name="  New User  ",
        )

    assert created.email == "New.User@example.com"  # stripped, case preserved
    assert created.roles == (role,)
    assert created.is_active is True
    assert created.display_name == "New User"
    assert created.password_hash is not None
    assert STRONG_PASSWORD not in created.password_hash

    persisted = await _get_user(app_engine, tenant_id, created.id)
    assert persisted is not None
    assert persisted.email == "New.User@example.com"


async def test_create_user_rejects_weak_password_before_any_db_work(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The policy runs first — a weak password leaves no row behind."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(ValidationError, match="at least 12 characters"):
            await create_user(
                session,
                actor_user_id=actor.id,
                email="weak@example.com",
                password=WEAK_PASSWORD,
                role="member",
            )

    async with tenant_context(app_engine, tenant_id) as session:
        assert await UserRepository(session).get_by_email("weak@example.com") is None


async def test_create_user_rejects_duplicate_email_in_tenant(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The pre-check fires for an existing row — even a deactivated one."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")
    existing = await _seed_user(app_engine, tenant_id, "taken@example.com", roles=("member",))

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(EmailTakenError):
            await create_user(
                session,
                actor_user_id=actor.id,
                email="taken@example.com",
                password=STRONG_PASSWORD,
                role="member",
            )

    # Deactivating the existing user does not free the address.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await deactivate_user(session, actor_user_id=actor.id, user_id=existing.id)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(EmailTakenError):
            await create_user(
                session,
                actor_user_id=actor.id,
                email="taken@example.com",
                password=STRONG_PASSWORD,
                role="member",
            )


async def test_create_user_allows_same_email_in_another_tenant(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """``uq_users_tenant_email`` is per tenant — the address may recur."""
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")
    actor_a = await _seed_user(app_engine, tenant_a, "actor@a.example")
    actor_b = await _seed_user(app_engine, tenant_b, "actor@b.example")

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        in_a = await create_user(
            session,
            actor_user_id=actor_a.id,
            email="shared@example.com",
            password=STRONG_PASSWORD,
            role="member",
        )

    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        in_b = await create_user(
            session,
            actor_user_id=actor_b.id,
            email="shared@example.com",
            password=STRONG_PASSWORD,
            role="member",
        )

    assert in_a.id != in_b.id
    assert in_a.tenant_id == tenant_a
    assert in_b.tenant_id == tenant_b


async def test_create_user_rejects_invalid_email(app_engine: AsyncEngine, seed_tenant) -> None:
    """The shared validator decides what an email looks like."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(EmailInvalidError):
            await create_user(
                session,
                actor_user_id=actor.id,
                email="not-an-email",
                password=STRONG_PASSWORD,
                role="member",
            )


@pytest.mark.parametrize("role", ["superuser", "", "auditor"])
async def test_create_user_rejects_unassignable_roles(
    app_engine: AsyncEngine, seed_tenant, role: str
) -> None:
    """Unknown roles and ``auditor`` alike are refused at this surface.

    ``auditor`` is a legal value in the schema and in ``ALLOWED_ROLES``;
    it is the *tenant surface* that declines to assign it (ADR-0121 §6)
    because it gates nothing anywhere in the codebase.
    """
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(RoleInvalidError):
            await create_user(
                session,
                actor_user_id=actor.id,
                email="rolecheck@example.com",
                password=STRONG_PASSWORD,
                role=role,
            )


# ---------------------------------------------------------------------------
# deactivate_user / reactivate_user
# ---------------------------------------------------------------------------


async def test_deactivate_self_is_blocked_even_with_other_owners(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Self-deactivation is refused regardless of how many owners remain."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")
    await _seed_user(app_engine, tenant_id, "second-owner@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(CannotDeactivateSelfError):
            await deactivate_user(session, actor_user_id=actor.id, user_id=actor.id)

    still_there = await _get_user(app_engine, tenant_id, actor.id)
    assert still_there is not None
    assert still_there.is_active is True


async def test_deactivate_last_active_owner_is_blocked(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The tenant keeps at least one active owner.

    The actor here is a *member* acting on the sole owner, which is the
    only way to reach this guard without tripping the self-guard first.
    """
    tenant_id = await seed_tenant()
    sole_owner = await _seed_user(app_engine, tenant_id, "owner@example.com")
    actor = await _seed_user(app_engine, tenant_id, "member@example.com", roles=("member",))

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(CannotDeactivateLastOwnerError):
            await deactivate_user(session, actor_user_id=actor.id, user_id=sole_owner.id)

    still_there = await _get_user(app_engine, tenant_id, sole_owner.id)
    assert still_there is not None
    assert still_there.is_active is True


async def test_owner_can_deactivate_another_owner(app_engine: AsyncEngine, seed_tenant) -> None:
    """Owners are peers — with a second owner present the guard clears."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "first-owner@example.com")
    other = await _seed_user(app_engine, tenant_id, "second-owner@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        deactivated = await deactivate_user(session, actor_user_id=actor.id, user_id=other.id)

    assert deactivated.is_active is False
    persisted = await _get_user(app_engine, tenant_id, other.id)
    assert persisted is not None
    assert persisted.is_active is False


async def test_deactivation_drops_the_users_sessions(app_engine: AsyncEngine, seed_tenant) -> None:
    """A deactivated user cannot ride an already-issued cookie."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")
    target = await _seed_user(app_engine, tenant_id, "target@example.com", roles=("member",))
    await _seed_login_session(app_engine, tenant_id, target)
    await _seed_login_session(app_engine, tenant_id, target)
    await _seed_login_session(app_engine, tenant_id, actor)
    assert await _count_sessions(app_engine, tenant_id, target.id) == 2

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await deactivate_user(session, actor_user_id=actor.id, user_id=target.id)

    assert await _count_sessions(app_engine, tenant_id, target.id) == 0
    # The actor's own session is untouched.
    assert await _count_sessions(app_engine, tenant_id, actor.id) == 1


async def test_reactivation_restores_is_active(app_engine: AsyncEngine, seed_tenant) -> None:
    """Reactivation is the inverse, and needs no guard of its own."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")
    target = await _seed_user(app_engine, tenant_id, "target@example.com", roles=("member",))

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await deactivate_user(session, actor_user_id=actor.id, user_id=target.id)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        reactivated = await reactivate_user(session, actor_user_id=actor.id, user_id=target.id)

    assert reactivated.is_active is True
    persisted = await _get_user(app_engine, tenant_id, target.id)
    assert persisted is not None
    assert persisted.is_active is True


async def test_reactivate_unknown_user_raises(app_engine: AsyncEngine, seed_tenant) -> None:
    """An id that resolves to nothing is a miss, not a silent no-op."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(UserNotFoundError):
            await reactivate_user(session, actor_user_id=actor.id, user_id=uuid4())


# ---------------------------------------------------------------------------
# reset_password
# ---------------------------------------------------------------------------


async def test_reset_password_rotates_hash_and_drops_sessions(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The hash changes and every session of that user goes with it."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")
    target = await _seed_user(app_engine, tenant_id, "target@example.com", roles=("member",))
    await _seed_login_session(app_engine, tenant_id, target)
    assert await _count_sessions(app_engine, tenant_id, target.id) == 1

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        updated = await reset_password(
            session,
            actor_user_id=actor.id,
            user_id=target.id,
            new_password=STRONG_PASSWORD,
        )

    assert updated.password_hash is not None
    assert updated.password_hash != target.password_hash
    assert STRONG_PASSWORD not in updated.password_hash
    assert await _count_sessions(app_engine, tenant_id, target.id) == 0

    persisted = await _get_user(app_engine, tenant_id, target.id)
    assert persisted is not None
    assert persisted.password_hash == updated.password_hash


async def test_reset_password_rejects_weak_password(app_engine: AsyncEngine, seed_tenant) -> None:
    """The same policy as creation, and it runs before the write."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")
    target = await _seed_user(app_engine, tenant_id, "target@example.com", roles=("member",))
    await _seed_login_session(app_engine, tenant_id, target)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(ValidationError):
            await reset_password(
                session,
                actor_user_id=actor.id,
                user_id=target.id,
                new_password=WEAK_PASSWORD,
            )

    persisted = await _get_user(app_engine, tenant_id, target.id)
    assert persisted is not None
    assert persisted.password_hash == target.password_hash
    assert await _count_sessions(app_engine, tenant_id, target.id) == 1


# ---------------------------------------------------------------------------
# change_role
# ---------------------------------------------------------------------------


async def test_change_role_promotes_member_to_owner(app_engine: AsyncEngine, seed_tenant) -> None:
    """Promotion replaces the role set wholesale."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")
    target = await _seed_user(app_engine, tenant_id, "target@example.com", roles=("member",))

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        updated = await change_role(
            session, actor_user_id=actor.id, user_id=target.id, new_role="owner"
        )

    assert updated.roles == ("owner",)
    persisted = await _get_user(app_engine, tenant_id, target.id)
    assert persisted is not None
    assert persisted.roles == ("owner",)


async def test_change_role_demotes_owner_when_another_owner_remains(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Demotion clears the guard while a second active owner exists."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")
    other = await _seed_user(app_engine, tenant_id, "second-owner@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        updated = await change_role(
            session, actor_user_id=actor.id, user_id=other.id, new_role="member"
        )

    assert updated.roles == ("member",)


async def test_change_role_blocks_demoting_the_last_active_owner(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The last active owner cannot be demoted — the §4.1 sibling guard."""
    tenant_id = await seed_tenant()
    sole_owner = await _seed_user(app_engine, tenant_id, "owner@example.com")
    actor = await _seed_user(app_engine, tenant_id, "member@example.com", roles=("member",))

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(CannotDemoteLastOwnerError):
            await change_role(
                session, actor_user_id=actor.id, user_id=sole_owner.id, new_role="member"
            )

    persisted = await _get_user(app_engine, tenant_id, sole_owner.id)
    assert persisted is not None
    assert persisted.roles == ("owner",)


async def test_change_role_allows_self_demotion_with_a_second_owner(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The hand-over case (ADR-0121 §4.3): stepping down is allowed."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")
    await _seed_user(app_engine, tenant_id, "successor@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        updated = await change_role(
            session, actor_user_id=actor.id, user_id=actor.id, new_role="member"
        )

    assert updated.roles == ("member",)


async def test_change_role_blocks_self_demotion_of_the_last_owner(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Self-demotion is not a way around the last-owner invariant."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(CannotDemoteLastOwnerError):
            await change_role(session, actor_user_id=actor.id, user_id=actor.id, new_role="member")


async def test_change_role_rejects_auditor(app_engine: AsyncEngine, seed_tenant) -> None:
    """``auditor`` is not assignable from the tenant surface (§6)."""
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")
    target = await _seed_user(app_engine, tenant_id, "target@example.com", roles=("member",))

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(RoleInvalidError):
            await change_role(
                session, actor_user_id=actor.id, user_id=target.id, new_role="auditor"
            )


# ---------------------------------------------------------------------------
# RLS confinement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    ["deactivate", "reactivate", "reset_password", "change_role"],
)
async def test_foreign_tenant_user_id_is_a_miss_not_a_write(
    app_engine: AsyncEngine, seed_tenant, operation: str
) -> None:
    """Handed another tenant's user id, every operation reports a miss.

    RLS makes the row invisible, so the service never has the chance to
    write across the boundary — the confinement is structural rather than
    an application check (ADR-0121 §1).
    """
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")
    actor = await _seed_user(app_engine, tenant_a, "actor@a.example")
    victim = await _seed_user(app_engine, tenant_b, "victim@b.example")

    async with tenant_context(app_engine, tenant_a, user_id=actor.id) as session:
        with pytest.raises(UserNotFoundError):
            if operation == "deactivate":
                await deactivate_user(session, actor_user_id=actor.id, user_id=victim.id)
            elif operation == "reactivate":
                await reactivate_user(session, actor_user_id=actor.id, user_id=victim.id)
            elif operation == "reset_password":
                await reset_password(
                    session,
                    actor_user_id=actor.id,
                    user_id=victim.id,
                    new_password=STRONG_PASSWORD,
                )
            else:
                await change_role(
                    session, actor_user_id=actor.id, user_id=victim.id, new_role="member"
                )

    untouched = await _get_user(app_engine, tenant_b, victim.id)
    assert untouched is not None
    assert untouched.is_active is True
    assert untouched.roles == ("owner",)
    assert untouched.password_hash == victim.password_hash


# ---------------------------------------------------------------------------
# Audit attribution
# ---------------------------------------------------------------------------


async def test_writes_are_audited_with_the_acting_owner(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The trigger attributes both writes to the ``app.user_id`` actor.

    No service-level audit code exists — this asserts the substrate
    ADR-0121 §1 relies on, in the same shape as
    ``tests/repositories/test_tenant_context_user_id.py``.
    """
    tenant_id = await seed_tenant()
    actor = await _seed_user(app_engine, tenant_id, "actor@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        created = await create_user(
            session,
            actor_user_id=actor.id,
            email="audited@example.com",
            password=STRONG_PASSWORD,
            role="member",
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await deactivate_user(session, actor_user_id=actor.id, user_id=created.id)

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await session.execute(
            text(
                """
                SELECT operation, user_id
                FROM audit_log
                WHERE table_name = 'users'
                  AND record_id  = :rid
                ORDER BY created_at ASC
                """
            ),
            {"rid": str(created.id)},
        )
        entries = rows.mappings().all()

    assert [e["operation"] for e in entries] == ["INSERT", "UPDATE"]
    assert {e["user_id"] for e in entries} == {actor.id}
