# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""End-to-end tests for ``services.super_admin.operations``.

Each helper is exercised against the live compose Postgres. The
load-bearing invariant is the audit-write integrity: every
mutating call must produce one ``super_admin_audit`` row, and an
induced audit failure must roll back the surrounding operation.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.tenant_constants import PRIMARY_TENANT_ID, SYSTEM_TENANT_ID
from services.password_hashing import hash_password
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
    TenantNotFoundError,
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
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_actor_super_admin(
    superuser_engine: AsyncEngine, email: str = "actor@super.example"
) -> UUID:
    """Insert one super-admin in the system tenant, return its id.

    Used as the ``actor_super_admin_id`` parameter so the audit
    rows the operations write have a valid FK target. The fixture
    ``reset_schema`` truncates the user table before each test, so
    this helper must be invoked from inside the test.
    """
    hashed = hash_password("doesntmatter")
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :n, :s) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": str(SYSTEM_TENANT_ID),
                "n": "Platform Administration",
                "s": "admin",
            },
        )
        result = await conn.execute(
            text(
                "INSERT INTO users "
                "(tenant_id, email, password_hash, roles, "
                " is_super_admin, is_active) "
                "VALUES (:tid, :email, :hash, "
                "ARRAY['owner']::text[], TRUE, TRUE) "
                "RETURNING id"
            ),
            {
                "tid": str(SYSTEM_TENANT_ID),
                "email": email,
                "hash": hashed,
            },
        )
        return UUID(str(result.scalar_one()))


async def _audit_count(superuser_engine: AsyncEngine, *, action: str) -> int:
    async with superuser_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*)::int AS n FROM super_admin_audit WHERE action = :a"),
            {"a": action},
        )
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# create_tenant_idempotent
# ---------------------------------------------------------------------------


async def test_create_tenant_idempotent_creates_tenant_owner_and_audit(
    superuser_engine: AsyncEngine,
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)

    async with superuser_engine.begin() as conn:
        summary = await create_tenant_idempotent(
            conn,
            name="Test Customer",
            subdomain="testcust",
            owner_email="owner@testcust.example",
            owner_password="correct-horse-battery-staple",
            actor_super_admin_id=actor_id,
        )

    assert summary.subdomain == "testcust"
    assert summary.user_count == 1
    assert summary.is_active is True

    # Verify audit row exists.
    assert await _audit_count(superuser_engine, action="create_tenant") == 1


async def test_create_tenant_idempotent_is_noop_on_second_call(
    superuser_engine: AsyncEngine,
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)

    async with superuser_engine.begin() as conn:
        first = await create_tenant_idempotent(
            conn,
            name="Test Customer",
            subdomain="testcust",
            owner_email="owner@testcust.example",
            owner_password="pw",
            actor_super_admin_id=actor_id,
        )
    async with superuser_engine.begin() as conn:
        second = await create_tenant_idempotent(
            conn,
            name="Test Customer",
            subdomain="testcust",
            owner_email="owner@testcust.example",
            owner_password="pw",
            actor_super_admin_id=actor_id,
        )

    assert first.id == second.id
    # Audit row written only on first creation.
    assert await _audit_count(superuser_engine, action="create_tenant") == 1


async def test_create_tenant_idempotent_rejects_invalid_subdomain(
    superuser_engine: AsyncEngine,
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    async with superuser_engine.begin() as conn:
        # Starts with a digit — fails the regex even after lowercasing.
        with pytest.raises(SubdomainInvalidError):
            await create_tenant_idempotent(
                conn,
                name="X",
                subdomain="1abc",
                owner_email="x@y.example",
                owner_password="pw",
                actor_super_admin_id=actor_id,
            )


async def test_create_tenant_idempotent_rejects_reserved_subdomain(
    superuser_engine: AsyncEngine,
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    async with superuser_engine.begin() as conn:
        with pytest.raises(SubdomainReservedError):
            await create_tenant_idempotent(
                conn,
                name="X",
                subdomain="admin",
                owner_email="x@y.example",
                owner_password="pw",
                actor_super_admin_id=actor_id,
            )


async def test_create_tenant_idempotent_rejects_subdomain_collision(
    superuser_engine: AsyncEngine,
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    async with superuser_engine.begin() as conn:
        await create_tenant_idempotent(
            conn,
            name="Customer A",
            subdomain="cust",
            owner_email="a@cust.example",
            owner_password="pw",
            actor_super_admin_id=actor_id,
        )
    async with superuser_engine.begin() as conn:
        with pytest.raises(SubdomainTakenError):
            await create_tenant_idempotent(
                conn,
                name="Customer B",
                subdomain="cust",
                owner_email="b@cust.example",
                owner_password="pw",
                actor_super_admin_id=actor_id,
            )


async def test_create_tenant_idempotent_rolls_back_on_audit_failure(
    superuser_engine: AsyncEngine,
) -> None:
    """Force an audit-write failure; tenant insert must roll back.

    The contrivance: pass a non-existent ``actor_super_admin_id``
    UUID. The b013 FK on ``super_admin_audit.super_admin_user_id``
    points at ``users.id``; an unknown UUID violates it and the
    transaction is rolled back.
    """
    bogus_actor = uuid4()  # not in users.id

    async with superuser_engine.begin() as conn:
        with pytest.raises(Exception):  # noqa: PT011 - FK violation
            await create_tenant_idempotent(
                conn,
                name="Should not exist",
                subdomain="orphan",
                owner_email="orphan@x.example",
                owner_password="pw",
                actor_super_admin_id=bogus_actor,
            )

    # No tenant row should have been created.
    async with superuser_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*)::int AS n FROM tenants WHERE subdomain = :s"),
            {"s": "orphan"},
        )
        assert int(result.scalar_one()) == 0


# ---------------------------------------------------------------------------
# create_user_idempotent
# ---------------------------------------------------------------------------


async def test_create_user_idempotent_creates_user(
    superuser_engine: AsyncEngine, seed_tenant
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    tenant_id = await seed_tenant()

    async with superuser_engine.begin() as conn:
        user_id = await create_user_idempotent(
            conn,
            tenant_id=tenant_id,
            email="new@user.example",
            roles=["member"],
            password="pw",
            actor_super_admin_id=actor_id,
        )

    assert isinstance(user_id, UUID)
    assert await _audit_count(superuser_engine, action="create_user") == 1


async def test_create_user_idempotent_is_noop_on_second_call(
    superuser_engine: AsyncEngine, seed_tenant
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    tenant_id = await seed_tenant()

    async with superuser_engine.begin() as conn:
        first = await create_user_idempotent(
            conn,
            tenant_id=tenant_id,
            email="dupe@user.example",
            roles=["member"],
            password="pw",
            actor_super_admin_id=actor_id,
        )
    async with superuser_engine.begin() as conn:
        second = await create_user_idempotent(
            conn,
            tenant_id=tenant_id,
            email="dupe@user.example",
            roles=["member"],
            password="pw",
            actor_super_admin_id=actor_id,
        )

    assert first == second
    assert await _audit_count(superuser_engine, action="create_user") == 1


async def test_create_user_idempotent_rejects_unknown_role(
    superuser_engine: AsyncEngine, seed_tenant
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    tenant_id = await seed_tenant()
    async with superuser_engine.begin() as conn:
        with pytest.raises(RoleInvalidError):
            await create_user_idempotent(
                conn,
                tenant_id=tenant_id,
                email="x@y.example",
                roles=["god"],
                password="pw",
                actor_super_admin_id=actor_id,
            )


async def test_create_user_idempotent_rejects_unknown_tenant(
    superuser_engine: AsyncEngine,
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    async with superuser_engine.begin() as conn:
        with pytest.raises(TenantNotFoundError):
            await create_user_idempotent(
                conn,
                tenant_id=uuid4(),
                email="x@y.example",
                roles=["member"],
                password="pw",
                actor_super_admin_id=actor_id,
            )


# ---------------------------------------------------------------------------
# create_super_admin_idempotent
# ---------------------------------------------------------------------------


async def test_create_super_admin_idempotent_with_existing_actor(
    superuser_engine: AsyncEngine,
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    async with superuser_engine.begin() as conn:
        new_id = await create_super_admin_idempotent(
            conn,
            email="another@super.example",
            password="pw",
            actor_super_admin_id=actor_id,
        )
    assert new_id != actor_id
    assert await _audit_count(superuser_engine, action="create_super_admin") == 1


async def test_create_super_admin_idempotent_bootstrap_path_writes_null_actor(
    superuser_engine: AsyncEngine,
) -> None:
    """The first super-admin is created with no prior actor.

    After migration b014, ``super_admin_audit.super_admin_user_id``
    is nullable. The bootstrap path passes ``None`` and the audit
    row records that honestly — no self-attribution, no fictitious
    "the new user created themselves" attribution.
    """
    # The system tenant must exist for the user insert FK.
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :n, :s) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": str(SYSTEM_TENANT_ID),
                "n": "Platform Administration",
                "s": "admin",
            },
        )
        new_id = await create_super_admin_idempotent(
            conn,
            email="first@super.example",
            password="pw",
            actor_super_admin_id=None,
        )

    # The audit row exists and its actor is NULL — no self-attribution.
    async with superuser_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT super_admin_user_id, target_user_id "
                    "FROM super_admin_audit "
                    "WHERE action = 'create_super_admin'"
                )
            )
        ).first()
    assert row is not None
    assert row.super_admin_user_id is None
    # The new user is still recorded as the target.
    assert UUID(str(row.target_user_id)) == new_id


async def test_create_super_admin_idempotent_rejects_invalid_email(
    superuser_engine: AsyncEngine,
) -> None:
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :n, :s) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": str(SYSTEM_TENANT_ID),
                "n": "Platform Administration",
                "s": "admin",
            },
        )
        with pytest.raises(EmailInvalidError):
            await create_super_admin_idempotent(
                conn,
                email="not-an-email",
                password="pw",
                actor_super_admin_id=None,
            )


# ---------------------------------------------------------------------------
# deactivate_tenant / reactivate_tenant
# ---------------------------------------------------------------------------


async def test_deactivate_tenant_refuses_system_tenant(
    superuser_engine: AsyncEngine,
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    async with superuser_engine.begin() as conn:
        with pytest.raises(CannotDeactivateSystemTenantError):
            await deactivate_tenant(
                conn,
                tenant_id=SYSTEM_TENANT_ID,
                actor_super_admin_id=actor_id,
            )


async def test_deactivate_tenant_refuses_primary_tenant(
    superuser_engine: AsyncEngine,
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    async with superuser_engine.begin() as conn:
        with pytest.raises(CannotDeactivatePrimaryTenantError):
            await deactivate_tenant(
                conn,
                tenant_id=PRIMARY_TENANT_ID,
                actor_super_admin_id=actor_id,
            )


async def test_deactivate_tenant_deletes_user_sessions(
    superuser_engine: AsyncEngine, seed_tenant
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    tenant_id = await seed_tenant()
    # Seed a user and a session for that user in the target tenant.
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        result = await conn.execute(
            text(
                "INSERT INTO users "
                "(tenant_id, email, password_hash, roles, is_active) "
                "VALUES (:tid, :e, :h, ARRAY['owner']::text[], TRUE) "
                "RETURNING id"
            ),
            {
                "tid": str(tenant_id),
                "e": "live@user.example",
                "h": hash_password("pw"),
            },
        )
        user_id = UUID(str(result.scalar_one()))
        await conn.execute(
            text(
                "INSERT INTO sessions "
                "(tenant_id, user_id, session_token, csrf_token, "
                " created_at, last_seen_at, expires_at) "
                "VALUES (:tid, :uid, :tok, :csrf, "
                " NOW(), NOW(), NOW() + INTERVAL '8 hours')"
            ),
            {
                "tid": str(tenant_id),
                "uid": str(user_id),
                "tok": "tok-deactivate-test",
                "csrf": "csrf-deactivate-test",
            },
        )

    async with superuser_engine.begin() as conn:
        summary = await deactivate_tenant(
            conn,
            tenant_id=tenant_id,
            actor_super_admin_id=actor_id,
        )

    assert summary.is_active is False
    async with superuser_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*)::int AS n FROM sessions WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
        assert int(result.scalar_one()) == 0


async def test_reactivate_tenant_flips_flag(superuser_engine: AsyncEngine, seed_tenant) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    tenant_id = await seed_tenant(is_active=False)
    async with superuser_engine.begin() as conn:
        summary = await reactivate_tenant(
            conn,
            tenant_id=tenant_id,
            actor_super_admin_id=actor_id,
        )
    assert summary.is_active is True


# ---------------------------------------------------------------------------
# reset_owner_password
# ---------------------------------------------------------------------------


async def test_reset_owner_password_picks_first_owner_and_clears_sessions(
    superuser_engine: AsyncEngine, seed_tenant
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    tenant_id = await seed_tenant()

    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        result = await conn.execute(
            text(
                "INSERT INTO users "
                "(tenant_id, email, password_hash, roles, is_active) "
                "VALUES (:tid, :e, :h, ARRAY['owner']::text[], TRUE) "
                "RETURNING id"
            ),
            {
                "tid": str(tenant_id),
                "e": "owner@reset.example",
                "h": hash_password("old"),
            },
        )
        owner_id = UUID(str(result.scalar_one()))
        await conn.execute(
            text(
                "INSERT INTO sessions "
                "(tenant_id, user_id, session_token, csrf_token, "
                " created_at, last_seen_at, expires_at) "
                "VALUES (:tid, :uid, :tok, :csrf, "
                " NOW(), NOW(), NOW() + INTERVAL '8 hours')"
            ),
            {
                "tid": str(tenant_id),
                "uid": str(owner_id),
                "tok": "tok-reset-test",
                "csrf": "csrf-reset-test",
            },
        )

    async with superuser_engine.begin() as conn:
        email = await reset_owner_password(
            conn,
            tenant_id=tenant_id,
            new_password="new",
            actor_super_admin_id=actor_id,
        )

    assert email == "owner@reset.example"
    # Old session is gone.
    async with superuser_engine.connect() as conn:
        n = (
            await conn.execute(
                text("SELECT COUNT(*)::int AS n FROM sessions WHERE user_id = :uid"),
                {"uid": str(owner_id)},
            )
        ).scalar_one()
        assert int(n) == 0


async def test_reset_owner_password_raises_when_no_owner(
    superuser_engine: AsyncEngine, seed_tenant
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    tenant_id = await seed_tenant()
    async with superuser_engine.begin() as conn:
        with pytest.raises(OwnerNotFoundError):
            await reset_owner_password(
                conn,
                tenant_id=tenant_id,
                new_password="new",
                actor_super_admin_id=actor_id,
            )


# ---------------------------------------------------------------------------
# deactivate_super_admin
# ---------------------------------------------------------------------------


async def test_deactivate_super_admin_refuses_when_only_one_left(
    superuser_engine: AsyncEngine,
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    async with superuser_engine.begin() as conn:
        with pytest.raises(CannotDeactivateLastSuperAdminError):
            await deactivate_super_admin(
                conn,
                user_id=actor_id,
                actor_super_admin_id=actor_id,
            )


async def test_deactivate_super_admin_succeeds_when_another_active_exists(
    superuser_engine: AsyncEngine,
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    second_id = await _seed_actor_super_admin(superuser_engine, email="second@super.example")

    async with superuser_engine.begin() as conn:
        await deactivate_super_admin(
            conn,
            user_id=second_id,
            actor_super_admin_id=actor_id,
        )

    async with superuser_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT is_active FROM users WHERE id = :uid"),
                {"uid": str(second_id)},
            )
        ).first()
        assert row is not None
        assert bool(row.is_active) is False


async def test_deactivate_super_admin_rejects_unknown_user(
    superuser_engine: AsyncEngine,
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    async with superuser_engine.begin() as conn:
        with pytest.raises(UserNotFoundError):
            await deactivate_super_admin(
                conn,
                user_id=uuid4(),
                actor_super_admin_id=actor_id,
            )


# ---------------------------------------------------------------------------
# list_tenants / list_super_admins
# ---------------------------------------------------------------------------


async def test_list_tenants_omits_system_tenant_by_default(
    superuser_engine: AsyncEngine, seed_tenant
) -> None:
    _ = await _seed_actor_super_admin(superuser_engine)
    await seed_tenant(name="A", subdomain="a-tenant")
    await seed_tenant(name="B", subdomain="b-tenant")

    async with superuser_engine.connect() as conn:
        tenants = await list_tenants(conn, include_system=False)

    ids = {t.id for t in tenants}
    assert SYSTEM_TENANT_ID not in ids
    assert {"a-tenant", "b-tenant"}.issubset({t.subdomain for t in tenants})


async def test_list_tenants_includes_system_when_requested(
    superuser_engine: AsyncEngine,
) -> None:
    _ = await _seed_actor_super_admin(superuser_engine)
    async with superuser_engine.connect() as conn:
        tenants = await list_tenants(conn, include_system=True)
    assert any(t.id == SYSTEM_TENANT_ID for t in tenants)


async def test_list_super_admins_returns_seeded_super_admin(
    superuser_engine: AsyncEngine,
) -> None:
    actor_id = await _seed_actor_super_admin(superuser_engine)
    async with superuser_engine.connect() as conn:
        sa_list = await list_super_admins(conn)
    assert len(sa_list) == 1
    assert sa_list[0].id == actor_id
    assert sa_list[0].is_active is True
