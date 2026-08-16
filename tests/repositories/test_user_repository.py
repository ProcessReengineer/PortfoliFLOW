# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""UserRepository tests against the live compose Postgres.

The numbered tests (B-01 ... B-07) implement the stream B acceptance
criteria. They run as the unprivileged ``portfoliflow_app`` role so
RLS evaluates exactly as it will in production. Tenant creation is
driven via the ``seed_tenant`` superuser fixture.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import UserRepository, tenant_context


# ---------------------------------------------------------------------------
# B-01: connection setup and schema apply work
# ---------------------------------------------------------------------------


async def test_b01_database_is_reachable(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar_one() == 1


async def test_b01_schema_is_applied(app_engine: AsyncEngine) -> None:
    """Every Phase-1 domain table must exist."""
    async with app_engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT relname
                FROM pg_class
                WHERE relnamespace = 'public'::regnamespace
                  AND relkind = 'r'
                  AND relname IN ('tenants', 'users', 'audit_log')
                ORDER BY relname
                """
            )
        )
        names = {row[0] for row in rows.fetchall()}
    assert names == {"audit_log", "tenants", "users"}


# ---------------------------------------------------------------------------
# B-02: tenant_context sets app.tenant_id correctly
# ---------------------------------------------------------------------------


async def test_b02_tenant_context_sets_guc(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(text("SELECT current_setting('app.tenant_id')::uuid AS tid"))
        assert result.scalar_one() == tenant_id


# ---------------------------------------------------------------------------
# B-03: UserRepository.create writes a row whose tenant_id matches the GUC
# ---------------------------------------------------------------------------


async def test_b03_create_assigns_active_tenant(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        repo = UserRepository(session)
        created = await repo.create(email="alice@example.com", password_hash="x" * 8)
    assert created.email == "alice@example.com"
    assert created.tenant_id == tenant_id

    # And we can read it back through the same context.
    async with tenant_context(app_engine, tenant_id) as session:
        repo = UserRepository(session)
        roundtrip = await repo.get_by_id(created.id)
    assert roundtrip is not None
    assert roundtrip.email == "alice@example.com"
    assert roundtrip.tenant_id == tenant_id


# ---------------------------------------------------------------------------
# ADR-0068: display_name round-trips and defaults to None
# ---------------------------------------------------------------------------


async def test_display_name_round_trips(app_engine: AsyncEngine, seed_tenant) -> None:
    """``create(display_name=…)`` persists and reads back unchanged."""
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        repo = UserRepository(session)
        created = await repo.create(
            email="named@example.com",
            password_hash="x" * 8,
            display_name="Alex Harper",
        )
    assert created.display_name == "Alex Harper"

    async with tenant_context(app_engine, tenant_id) as session:
        roundtrip = await UserRepository(session).get_by_id(created.id)
    assert roundtrip is not None
    assert roundtrip.display_name == "Alex Harper"


async def test_display_name_defaults_to_none(app_engine: AsyncEngine, seed_tenant) -> None:
    """Omitting ``display_name`` leaves the column NULL (no breakage)."""
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        created = await UserRepository(session).create(
            email="anon@example.com", password_hash="x" * 8
        )
    assert created.display_name is None

    async with tenant_context(app_engine, tenant_id) as session:
        roundtrip = await UserRepository(session).get_by_id(created.id)
    assert roundtrip is not None
    assert roundtrip.display_name is None


# ---------------------------------------------------------------------------
# B-04: RLS isolation between tenants — the central RLS test
# ---------------------------------------------------------------------------


async def test_b04_two_tenants_cannot_see_each_other(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    async with tenant_context(app_engine, tenant_a) as session:
        await UserRepository(session).create(email="a@example.com", password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_b) as session:
        await UserRepository(session).create(email="b@example.com", password_hash="x" * 8)

    # Tenant A sees only Tenant A's user.
    async with tenant_context(app_engine, tenant_a) as session:
        users_a = await UserRepository(session).list_all()
    assert [u.email for u in users_a] == ["a@example.com"]
    assert all(u.tenant_id == tenant_a for u in users_a)

    # Tenant B sees only Tenant B's user.
    async with tenant_context(app_engine, tenant_b) as session:
        users_b = await UserRepository(session).list_all()
    assert [u.email for u in users_b] == ["b@example.com"]
    assert all(u.tenant_id == tenant_b for u in users_b)


# ---------------------------------------------------------------------------
# B-05: WITH CHECK blocks cross-tenant inserts
# ---------------------------------------------------------------------------


async def test_b05_cross_tenant_insert_is_blocked(app_engine: AsyncEngine, seed_tenant) -> None:
    """A session bound to tenant X cannot insert a row with tenant Y.

    UserRepository.create() always writes the active tenant, so we
    bypass it for this test and INSERT directly with a foreign tenant
    id. RLS WITH CHECK must reject the row.
    """
    tenant_active = await seed_tenant(name="Active")
    tenant_other = await seed_tenant(name="Other")

    with pytest.raises(DBAPIError):
        async with tenant_context(app_engine, tenant_active) as session:
            await session.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email) "
                    "VALUES (gen_random_uuid(), :tid, :email)"
                ),
                {"tid": str(tenant_other), "email": "trespasser@example.com"},
            )


# ---------------------------------------------------------------------------
# B-06: audit trigger writes a corresponding row on INSERT
# ---------------------------------------------------------------------------


async def test_b06_audit_trigger_logs_user_insert(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()

    async with tenant_context(app_engine, tenant_id) as session:
        created = await UserRepository(session).create(
            email="audited@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await session.execute(
            text(
                """
                SELECT tenant_id, table_name, operation, record_id,
                       new_data ->> 'email' AS new_email,
                       user_id
                FROM audit_log
                WHERE table_name = 'users'
                  AND record_id  = :rid
                """
            ),
            {"rid": str(created.id)},
        )
        entry = rows.mappings().one()

    assert entry["tenant_id"] == tenant_id
    assert entry["table_name"] == "users"
    assert entry["operation"] == "INSERT"
    assert entry["record_id"] == created.id
    assert entry["new_email"] == "audited@example.com"
    # Phase 1 has no auth, so user_id is expected to be NULL.
    assert entry["user_id"] is None


# ---------------------------------------------------------------------------
# B-07: test isolation — autouse reset_schema fixture truncates between tests
# ---------------------------------------------------------------------------


async def test_b07_isolation_first_test_inserts(app_engine: AsyncEngine, seed_tenant) -> None:
    """Insert a user; the next test must see an empty table."""
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        await UserRepository(session).create(email="leftover@example.com", password_hash="x" * 8)
    # Sanity check that the row is here right now.
    async with tenant_context(app_engine, tenant_id) as session:
        users = await UserRepository(session).list_all()
    assert len(users) == 1


async def test_b07_isolation_second_test_sees_empty(app_engine: AsyncEngine, seed_tenant) -> None:
    """The truncate-between-tests fixture must wipe the previous test."""
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        users = await UserRepository(session).list_all()
    assert users == []

    # And no audit_log spillover either.
    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(text("SELECT count(*) FROM audit_log"))
        assert result.scalar_one() == 0


# ---------------------------------------------------------------------------
# Bonus: querying without a tenant context returns nothing (defence
# in depth; Postgres defaults app.tenant_id to '' and the cast fails,
# which raises rather than silently passing).
# ---------------------------------------------------------------------------


async def test_unset_tenant_context_blocks_access(app_engine: AsyncEngine, seed_tenant) -> None:
    """A session that never sets app.tenant_id cannot read users."""
    # Seed a row first so the table is non-empty.
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        await UserRepository(session).create(email="hidden@example.com", password_hash="x" * 8)

    # Now query with NO tenant context. Postgres raises when the
    # current_setting('app.tenant_id') cast to uuid fails on the
    # empty string default — the policy is structurally non-bypassable.
    from core.repositories._session import create_session_factory

    factory = create_session_factory(app_engine)
    async with factory() as session, session.begin():
        with pytest.raises(DBAPIError):
            await session.execute(text("SELECT * FROM users"))
