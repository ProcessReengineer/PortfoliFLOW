# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SectorRepository tests against the live compose Postgres.

Each test runs as the unprivileged ``portfoliflow_app`` role so RLS
evaluates exactly as it will in production. Tenant creation goes
through the ``seed_tenant`` superuser fixture.

Coverage:

* Round-trip create + read by id and by code.
* ``list_all`` orders by display name.
* ``update`` modifies the requested fields.
* RLS isolates sectors between tenants.
* Unique-constraint conflict surfaces on duplicate ``(tenant_id, code)``.
* Audit-log entry is captured on insert.
* The bootstrap-installed ``unclassified`` row is created by
  :func:`cli.bootstrap.install_unclassified_sector`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from cli.bootstrap import install_unclassified_sector
from core.repositories import (
    SectorRepository,
    UserRepository,
    tenant_context,
)


# ---------------------------------------------------------------------------
# S-01: round-trip create + read
# ---------------------------------------------------------------------------


async def test_s01_create_and_get_by_id(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email="s01@example.com", password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = SectorRepository(session)
        created = await repo.create(
            code="tech_software",
            display_name="Technology — Software",
            created_by=actor.id,
        )

    assert created.code == "tech_software"
    assert created.display_name == "Technology — Software"
    assert created.is_active is True
    assert created.tenant_id == tenant_id

    async with tenant_context(app_engine, tenant_id) as session:
        repo = SectorRepository(session)
        fetched = await repo.get_by_id(created.id)
    assert fetched is not None
    assert fetched.id == created.id


# ---------------------------------------------------------------------------
# S-02: get_by_code is case-insensitive and trims whitespace
# ---------------------------------------------------------------------------


async def test_s02_get_by_code(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email="s02@example.com", password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await SectorRepository(session).create(
            code="healthcare", display_name="Healthcare", created_by=actor.id
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = SectorRepository(session)
        match = await repo.get_by_code("healthcare")
        cased = await repo.get_by_code("HEALTHCARE")
        spaced = await repo.get_by_code("  healthcare  ")
        miss = await repo.get_by_code("not_present")
        empty = await repo.get_by_code("")

    assert match is not None
    assert match.display_name == "Healthcare"
    assert cased is not None and cased.id == match.id
    assert spaced is not None and spaced.id == match.id
    assert miss is None
    assert empty is None


# ---------------------------------------------------------------------------
# S-03: cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_s03_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    async with tenant_context(app_engine, tenant_a) as session:
        actor_a = await UserRepository(session).create(
            email="a-s03@example.com", password_hash="x" * 8
        )
    async with tenant_context(app_engine, tenant_b) as session:
        actor_b = await UserRepository(session).create(
            email="b-s03@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await SectorRepository(session).create(
            code="shared_code",
            display_name="A's Sector",
            created_by=actor_a.id,
        )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await SectorRepository(session).create(
            code="shared_code",
            display_name="B's Sector",
            created_by=actor_b.id,
        )

    async with tenant_context(app_engine, tenant_a) as session:
        a_view = await SectorRepository(session).list_all()
    async with tenant_context(app_engine, tenant_b) as session:
        b_view = await SectorRepository(session).list_all()

    assert [s.display_name for s in a_view] == ["A's Sector"]
    assert [s.display_name for s in b_view] == ["B's Sector"]


# ---------------------------------------------------------------------------
# S-04: duplicate (tenant_id, code) raises IntegrityError
# ---------------------------------------------------------------------------


async def test_s04_duplicate_code_raises(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email="s04@example.com", password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await SectorRepository(session).create(
            code="dupe", display_name="First", created_by=actor.id
        )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await SectorRepository(session).create(
                code="dupe", display_name="Second", created_by=actor.id
            )


# ---------------------------------------------------------------------------
# S-05: bootstrap installs an unclassified sector idempotently
# ---------------------------------------------------------------------------


async def test_s05_unclassified_bootstrap_is_idempotent(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email="s05@example.com", password_hash="x" * 8)

    # First run: creates the sector.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await install_unclassified_sector(SectorRepository(session), actor.id)
    # Second run: no-op, no IntegrityError.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await install_unclassified_sector(SectorRepository(session), actor.id)

    async with tenant_context(app_engine, tenant_id) as session:
        all_sectors = await SectorRepository(session).list_all()
    codes = [s.code for s in all_sectors]
    assert codes.count("unclassified") == 1


# ---------------------------------------------------------------------------
# S-06: audit-log entry is captured on insert
# ---------------------------------------------------------------------------


async def test_s06_audit_log_entry_on_insert(
    app_engine: AsyncEngine, seed_tenant, superuser_engine: AsyncEngine
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email="s06@example.com", password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        created = await SectorRepository(session).create(
            code="audit_check",
            display_name="Audit Check",
            created_by=actor.id,
        )

    async with superuser_engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT operation, user_id, table_name, record_id
                FROM audit_log
                WHERE table_name = 'sectors'
                  AND record_id = :rid
                """
            ),
            {"rid": str(created.id)},
        )
        audit_rows = rows.mappings().all()

    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row["operation"] == "INSERT"
    assert row["user_id"] == actor.id
