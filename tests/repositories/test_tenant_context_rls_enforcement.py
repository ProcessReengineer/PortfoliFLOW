# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression tests for ADR-0078 — RLS enforcement in ``tenant_context``.

PostgreSQL never enforces RLS for a superuser. The CLI connects as the
superuser, so before ADR-0078 a ``tenant_context`` opened on that engine
set ``app.tenant_id`` while no policy ever evaluated it: every RLS-reliant
repository read silently saw rows from *all* tenants, and per-tenant
seeding no-op'd for every non-primary tenant.

``tenant_context`` now switches to the unprivileged application role for
the tenant-scoped transaction, so RLS is enforced regardless of the
connecting role. These tests pin that behaviour against the live compose
Postgres using the **superuser** engine — the exact path that exhibited
the bug:

* ``get_by_code`` cannot see another tenant's row (the original defect).
* ``seed_tenant_defaults`` installs each tenant's *own* default
  catalogue rather than no-op'ing on the first tenant's rows.
* the GUC-based ``super_admin_audit`` policy still admits rows after the
  role switch, so legitimate cross-tenant super-admin reads survive.

The fixtures (``superuser_engine``, ``seed_tenant``, autouse
``reset_schema``) come from ``tests/_db_fixtures`` via the repositories
``conftest``; the whole module skips when the DB URLs are unset.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    UserRepository,
    tenant_context,
)
from core.tenant_constants import SYSTEM_TENANT_ID
from services.super_admin import (
    create_super_admin_idempotent,
    seed_tenant_defaults,
)


async def test_get_by_code_hides_other_tenant_under_superuser_engine(
    superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The original defect: under the superuser engine, an existence
    check must not see another tenant's row.

    Tenant A is given an ``unclassified`` asset class; tenant B has
    none. ``get_by_code("unclassified")`` opened in tenant B's context
    must return ``None``. Without the ADR-0078 role switch the superuser
    bypasses RLS and this returns tenant A's row instead.
    """
    tenant_a = await seed_tenant(name="Tenant A", subdomain="adr0078-a")
    tenant_b = await seed_tenant(name="Tenant B", subdomain="adr0078-b")

    # Seed tenant A's unclassified asset class via the superuser engine
    # (the CLI path). The actor user satisfies the audit trigger.
    async with tenant_context(superuser_engine, tenant_a) as session:
        actor_a = await UserRepository(session).create(
            email="a@adr0078.example", password_hash="x" * 8
        )
    async with tenant_context(superuser_engine, tenant_a, user_id=actor_a.id) as session:
        await AssetClassRepository(session).create(code="unclassified", display_name="Unclassified")

    # Tenant B's context must not see tenant A's row.
    async with tenant_context(superuser_engine, tenant_b) as session:
        found = await AssetClassRepository(session).get_by_code("unclassified")

    assert found is None


async def test_seed_tenant_defaults_installs_own_catalogue_per_tenant(
    superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """Seeding parity: two tenants seeded on the superuser engine each
    end up with their *own* full default catalogue.

    Before ADR-0078 the second tenant's installers saw the first
    tenant's rows through the RLS-bypassing superuser and skipped every
    step, leaving it empty.
    """
    tenant_a = await seed_tenant(name="Tenant A", subdomain="adr0078-seed-a")
    tenant_b = await seed_tenant(name="Tenant B", subdomain="adr0078-seed-b")

    async with tenant_context(superuser_engine, tenant_a) as session:
        owner_a = await UserRepository(session).create(
            email="owner-a@adr0078.example", password_hash="x" * 8
        )
    async with tenant_context(superuser_engine, tenant_b) as session:
        owner_b = await UserRepository(session).create(
            email="owner-b@adr0078.example", password_hash="x" * 8
        )

    await seed_tenant_defaults(superuser_engine, tenant_id=tenant_a, actor_user_id=owner_a.id)
    await seed_tenant_defaults(superuser_engine, tenant_id=tenant_b, actor_user_id=owner_b.id)

    async with tenant_context(superuser_engine, tenant_a) as session:
        a_unclassified = await AssetClassRepository(session).get_by_code("unclassified")
        a_all = await AssetClassRepository(session).list_all()
    async with tenant_context(superuser_engine, tenant_b) as session:
        b_unclassified = await AssetClassRepository(session).get_by_code("unclassified")
        b_all = await AssetClassRepository(session).list_all()

    # Each tenant got its own seed rows.
    assert a_unclassified is not None
    assert b_unclassified is not None
    assert a_unclassified.id != b_unclassified.id
    assert a_unclassified.tenant_id == tenant_a
    assert b_unclassified.tenant_id == tenant_b

    # The second tenant received the full default catalogue, and each
    # tenant's RLS-enforced view contains only its own rows (not the
    # union of both).
    assert len(b_all) > 0
    assert {ac.tenant_id for ac in a_all} == {tenant_a}
    assert {ac.tenant_id for ac in b_all} == {tenant_b}
    assert len(a_all) == len(b_all)


async def test_super_admin_audit_reads_survive_role_switch(
    superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """The GUC-based ``super_admin_audit`` policy still admits rows after
    the switch to the unprivileged application role.

    A plain tenant context (role switch active, no super-admin GUC) sees
    no audit rows — proving the switch is in effect. With
    ``is_super_admin=True`` the same role sees them, because the policy
    keys off ``app.is_super_admin`` rather than the connecting role.
    """
    # The system tenant must exist for the super-admin FK (reset_schema
    # truncated it). Inserted via the superuser engine — a platform op.
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, 'System', 'adr0078-admin')"
            ),
            {"id": str(SYSTEM_TENANT_ID)},
        )

    # Bootstrap the first super-admin (actor=None). This writes a
    # ``create_super_admin`` row into super_admin_audit.
    async with superuser_engine.begin() as conn:
        await create_super_admin_idempotent(
            conn,
            email="sa@adr0078.example",
            password="pw-not-logged",
            actor_super_admin_id=None,
        )

    some_tenant = await seed_tenant(name="T", subdomain="adr0078-sa")

    # Plain tenant context: the role switch is active and no super-admin
    # GUC is set, so the policy hides every row.
    async with tenant_context(superuser_engine, some_tenant) as session:
        hidden = await session.execute(text("SELECT COUNT(*) FROM super_admin_audit"))
        assert int(hidden.scalar_one()) == 0

    # With the super-admin GUC the GUC-based policy admits the rows even
    # though the session now runs as the unprivileged application role.
    async with tenant_context(superuser_engine, some_tenant, is_super_admin=True) as session:
        visible = await session.execute(text("SELECT COUNT(*) FROM super_admin_audit"))
        assert int(visible.scalar_one()) >= 1
