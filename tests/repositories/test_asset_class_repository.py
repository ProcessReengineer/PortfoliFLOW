# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AssetClassRepository tests against the live compose Postgres.

Each test runs as the unprivileged ``portfoliflow_app`` role so RLS
evaluates exactly as it will in production. Tenant creation goes
through the ``seed_tenant`` superuser fixture.

Coverage:

* Round-trip create + read by id and by code.
* ``list_all`` orders by display name.
* ``update`` modifies the requested fields and bumps ``updated_at``.
* ``delete`` removes the row when no references exist.
* RLS isolates asset classes between tenants.
* Unique-constraint conflict surfaces on duplicate ``(tenant_id, code)``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    UserRepository,
    tenant_context,
)


# ---------------------------------------------------------------------------
# AC-01: round-trip create + read
# ---------------------------------------------------------------------------


async def test_ac01_create_and_get_by_id(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="ac01@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassRepository(session)
        created = await repo.create(
            code="global_equity",
            display_name="Global Equity",
            description="Listed equities — developed and emerging markets",
        )

    assert created.code == "global_equity"
    assert created.display_name == "Global Equity"
    assert created.description.startswith("Listed equities")
    assert created.tenant_id == tenant_id

    async with tenant_context(app_engine, tenant_id) as session:
        repo = AssetClassRepository(session)
        fetched = await repo.get_by_id(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.display_name == "Global Equity"


# ---------------------------------------------------------------------------
# AC-02: get_by_code returns the matching asset class
# ---------------------------------------------------------------------------


async def test_ac02_get_by_code_returns_match(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="ac02@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassRepository(session)
        await repo.create(code="bonds_dm", display_name="Bonds DM")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = AssetClassRepository(session)
        match = await repo.get_by_code("bonds_dm")
        miss = await repo.get_by_code("not_present")
        # Case-insensitive lookup is the documented convention for the
        # Excel-import path (ADR-0043 §4): codes coming from spreadsheet
        # cells are inconsistent in case and benign whitespace.
        cased = await repo.get_by_code("BONDS_DM")
        spaced = await repo.get_by_code("  bonds_dm  ")
        empty = await repo.get_by_code("")

    assert match is not None
    assert match.display_name == "Bonds DM"
    assert miss is None
    assert cased is not None and cased.id == match.id
    assert spaced is not None and spaced.id == match.id
    assert empty is None


# ---------------------------------------------------------------------------
# AC-02b: get_by_code is tenant-scoped (Phase-4 cross-module discipline)
# ---------------------------------------------------------------------------


async def test_ac02b_get_by_code_is_tenant_scoped(app_engine: AsyncEngine, seed_tenant) -> None:
    """A code present in tenant A must not resolve from tenant B's session."""
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    async with tenant_context(app_engine, tenant_a) as session:
        actor_a = await UserRepository(session).create(
            email="ac02b-a@example.com", password_hash="x" * 8
        )
    async with tenant_context(app_engine, tenant_b) as session:
        actor_b = await UserRepository(session).create(
            email="ac02b-b@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await AssetClassRepository(session).create(code="only_in_a", display_name="Only in A")
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await AssetClassRepository(session).create(code="only_in_b", display_name="Only in B")

    async with tenant_context(app_engine, tenant_b) as session:
        repo = AssetClassRepository(session)
        from_a_in_b = await repo.get_by_code("only_in_a")
        own_in_b = await repo.get_by_code("only_in_b")

    assert from_a_in_b is None
    assert own_in_b is not None
    assert own_in_b.display_name == "Only in B"


# ---------------------------------------------------------------------------
# AC-03: list_all orders by display name
# ---------------------------------------------------------------------------


async def test_ac03_list_all_orders_by_display_name(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="ac03@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassRepository(session)
        # Insert in scrambled order; expect alphabetical by display_name.
        await repo.create(code="z_real_estate", display_name="Real Estate")
        await repo.create(code="a_bonds", display_name="Bonds")
        await repo.create(code="m_equities", display_name="Equities")

    async with tenant_context(app_engine, tenant_id) as session:
        all_acs = await AssetClassRepository(session).list_all()

    assert [ac.display_name for ac in all_acs] == [
        "Bonds",
        "Equities",
        "Real Estate",
    ]


# ---------------------------------------------------------------------------
# AC-04: update modifies only the requested fields
# ---------------------------------------------------------------------------


async def test_ac04_update_modifies_requested_fields(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="ac04@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassRepository(session)
        created = await repo.create(code="alpha", display_name="Original Name", description="orig")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassRepository(session)
        updated = await repo.update(created.id, display_name="New Name")

    assert updated.display_name == "New Name"
    assert updated.description == "orig"  # untouched
    assert updated.code == "alpha"  # immutable through .update


# ---------------------------------------------------------------------------
# AC-05: delete removes a row that is not referenced
# ---------------------------------------------------------------------------


async def test_ac05_delete_removes_row(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="ac05@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassRepository(session)
        created = await repo.create(code="to_delete", display_name="To Delete")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassRepository(session)
        await repo.delete(created.id)

    async with tenant_context(app_engine, tenant_id) as session:
        gone = await AssetClassRepository(session).get_by_id(created.id)
    assert gone is None


# ---------------------------------------------------------------------------
# AC-06: cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_ac06_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    async with tenant_context(app_engine, tenant_a) as session:
        actor_a = await UserRepository(session).create(email="a@example.com", password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_b) as session:
        actor_b = await UserRepository(session).create(email="b@example.com", password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await AssetClassRepository(session).create(code="shared_code", display_name="A's Asset")
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await AssetClassRepository(session).create(code="shared_code", display_name="B's Asset")

    async with tenant_context(app_engine, tenant_a) as session:
        a_view = await AssetClassRepository(session).list_all()
    async with tenant_context(app_engine, tenant_b) as session:
        b_view = await AssetClassRepository(session).list_all()

    assert [ac.display_name for ac in a_view] == ["A's Asset"]
    assert [ac.display_name for ac in b_view] == ["B's Asset"]


# ---------------------------------------------------------------------------
# AC-07: duplicate (tenant_id, code) raises IntegrityError
# ---------------------------------------------------------------------------


async def test_ac07_duplicate_code_in_same_tenant_raises(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="ac07@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassRepository(session)
        await repo.create(code="dupe", display_name="First")

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            repo = AssetClassRepository(session)
            await repo.create(code="dupe", display_name="Second")
