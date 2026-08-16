# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""BenchmarkRepository tests against the live compose Postgres.

Tests run as the unprivileged ``portfoliflow_app`` role so RLS
evaluates exactly as in production. Tenant creation goes through
the ``seed_tenant`` superuser fixture. Coverage:

* Round-trip create + read by id and by code.
* ``upsert_by_code`` refreshes mutable fields on conflict.
* ``list_all`` orders by ``display_name``.
* Cross-tenant isolation.
* Unique-constraint conflict on duplicate ``(tenant_id, code)``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    BenchmarkRepository,
    UserRepository,
    tenant_context,
)


async def test_bm01_create_and_get_by_id(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="bm01@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = BenchmarkRepository(session)
        created = await repo.create(
            code="BM_EQUITIES_DM",
            display_name="MSCI World",
            description="Developed Markets Equities",
            provider_hint="Synthetic / future: MSCI World NR EUR",
            created_by=actor.id,
        )

    assert created.code == "BM_EQUITIES_DM"
    assert created.display_name == "MSCI World"
    assert created.tenant_id == tenant_id

    async with tenant_context(app_engine, tenant_id) as session:
        fetched = await BenchmarkRepository(session).get_by_id(created.id)
    assert fetched is not None
    assert fetched.code == "BM_EQUITIES_DM"
    assert fetched.provider_hint is not None
    assert fetched.provider_hint.startswith("Synthetic")


async def test_bm02_get_by_code(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="bm02@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await BenchmarkRepository(session).create(
            code="BM_BONDS_DM",
            display_name="Bonds DM",
            description=None,
            provider_hint=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = BenchmarkRepository(session)
        match = await repo.get_by_code("BM_BONDS_DM")
        miss = await repo.get_by_code("does_not_exist")
        empty = await repo.get_by_code("")
        spaced = await repo.get_by_code("  BM_BONDS_DM  ")

    assert match is not None
    assert match.display_name == "Bonds DM"
    assert miss is None
    assert empty is None
    assert spaced is not None and spaced.id == match.id


async def test_bm03_upsert_by_code_refreshes_mutable_fields(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="bm03@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = BenchmarkRepository(session)
        first = await repo.upsert_by_code(
            code="BM_EM",
            display_name="MSCI EM",
            description="Emerging Markets",
            provider_hint=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = BenchmarkRepository(session)
        second = await repo.upsert_by_code(
            code="BM_EM",
            display_name="MSCI Emerging Markets",
            description="Updated description",
            provider_hint="Updated provider",
            created_by=actor.id,
        )

    # Same row (same id) but mutable fields refreshed.
    assert second.id == first.id
    assert second.display_name == "MSCI Emerging Markets"
    assert second.description == "Updated description"
    assert second.provider_hint == "Updated provider"


async def test_bm04_list_all_orders_by_display_name(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="bm04@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = BenchmarkRepository(session)
        await repo.create(
            code="Z",
            display_name="Real Estate Idx",
            description=None,
            provider_hint=None,
            created_by=actor.id,
        )
        await repo.create(
            code="A",
            display_name="Bonds Idx",
            description=None,
            provider_hint=None,
            created_by=actor.id,
        )
        await repo.create(
            code="M",
            display_name="Equity Idx",
            description=None,
            provider_hint=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        ordered = await BenchmarkRepository(session).list_all()
    assert [b.display_name for b in ordered] == [
        "Bonds Idx",
        "Equity Idx",
        "Real Estate Idx",
    ]


async def test_bm05_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    async with tenant_context(app_engine, tenant_a) as session:
        actor_a = await UserRepository(session).create(
            email="bm05a@example.com", password_hash="x" * 8
        )
    async with tenant_context(app_engine, tenant_b) as session:
        actor_b = await UserRepository(session).create(
            email="bm05b@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await BenchmarkRepository(session).create(
            code="shared_code",
            display_name="A's BM",
            description=None,
            provider_hint=None,
            created_by=actor_a.id,
        )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await BenchmarkRepository(session).create(
            code="shared_code",
            display_name="B's BM",
            description=None,
            provider_hint=None,
            created_by=actor_b.id,
        )

    async with tenant_context(app_engine, tenant_a) as session:
        a_view = await BenchmarkRepository(session).list_all()
    async with tenant_context(app_engine, tenant_b) as session:
        b_view = await BenchmarkRepository(session).list_all()

    assert [b.display_name for b in a_view] == ["A's BM"]
    assert [b.display_name for b in b_view] == ["B's BM"]


async def test_bm06_duplicate_code_in_same_tenant_raises(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="bm06@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await BenchmarkRepository(session).create(
            code="dupe",
            display_name="First",
            description=None,
            provider_hint=None,
            created_by=actor.id,
        )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await BenchmarkRepository(session).create(
                code="dupe",
                display_name="Second",
                description=None,
                provider_hint=None,
                created_by=actor.id,
            )
