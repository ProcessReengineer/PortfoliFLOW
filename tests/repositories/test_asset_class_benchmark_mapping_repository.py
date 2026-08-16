# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AssetClassBenchmarkMappingRepository tests against the live compose Postgres.

Tests run as ``portfoliflow_app``; tenant + asset-class + benchmark
prep go through the seed_tenant + UserRepository / AssetClassRepository
/ BenchmarkRepository helpers. Coverage:

* ``upsert_mapping`` inserts then refreshes the weight on conflict.
* ``delete_mappings_for_asset_class`` clears the previous generation.
* ``list_all`` and ``list_for_asset_class`` return rows ordered as
  documented.
* Cross-tenant isolation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassBenchmarkMappingRepository,
    AssetClassRepository,
    BenchmarkRepository,
    UserRepository,
    tenant_context,
)


async def _seed_tenant_and_actor(app_engine, seed_tenant, email: str):
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    return tenant_id, actor


async def test_ma01_upsert_inserts_then_refreshes(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, actor = await _seed_tenant_and_actor(app_engine, seed_tenant, "ma01@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(code="equities", display_name="Equities")
        bm = await BenchmarkRepository(session).create(
            code="BM_EQ",
            display_name="MSCI",
            description=None,
            provider_hint=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassBenchmarkMappingRepository(session)
        first = await repo.upsert_mapping(
            asset_class_id=ac.id,
            benchmark_id=bm.id,
            weight=Decimal("1"),
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassBenchmarkMappingRepository(session)
        second = await repo.upsert_mapping(
            asset_class_id=ac.id,
            benchmark_id=bm.id,
            weight=Decimal("0.7"),
        )

    assert second.id == first.id
    assert second.weight == Decimal("0.7000")


async def test_ma02_delete_for_asset_class_clears_generation(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, actor = await _seed_tenant_and_actor(app_engine, seed_tenant, "ma02@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(code="bonds", display_name="Bonds")
        bm_a = await BenchmarkRepository(session).create(
            code="BM1",
            display_name="BM1",
            description=None,
            provider_hint=None,
            created_by=actor.id,
        )
        bm_b = await BenchmarkRepository(session).create(
            code="BM2",
            display_name="BM2",
            description=None,
            provider_hint=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassBenchmarkMappingRepository(session)
        await repo.upsert_mapping(ac.id, bm_a.id, Decimal("0.5"))
        await repo.upsert_mapping(ac.id, bm_b.id, Decimal("0.5"))

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassBenchmarkMappingRepository(session)
        deleted = await repo.delete_mappings_for_asset_class(ac.id)
    assert deleted == 2

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await AssetClassBenchmarkMappingRepository(session).list_for_asset_class(ac.id)
    assert rows == []


async def test_ma03_check_constraint_rejects_out_of_range_weight(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, actor = await _seed_tenant_and_actor(app_engine, seed_tenant, "ma03@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(code="ac", display_name="AC")
        bm = await BenchmarkRepository(session).create(
            code="BM",
            display_name="BM",
            description=None,
            provider_hint=None,
            created_by=actor.id,
        )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await AssetClassBenchmarkMappingRepository(session).upsert_mapping(
                asset_class_id=ac.id,
                benchmark_id=bm.id,
                weight=Decimal("1.5"),
            )


async def test_ma04_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a, actor_a = await _seed_tenant_and_actor(app_engine, seed_tenant, "ma04a@example.com")
    tenant_b, _actor_b = await _seed_tenant_and_actor(app_engine, seed_tenant, "ma04b@example.com")

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        ac_a = await AssetClassRepository(session).create(code="equities", display_name="Eq")
        bm_a = await BenchmarkRepository(session).create(
            code="BM",
            display_name="BM",
            description=None,
            provider_hint=None,
            created_by=actor_a.id,
        )
        await AssetClassBenchmarkMappingRepository(session).upsert_mapping(
            asset_class_id=ac_a.id,
            benchmark_id=bm_a.id,
            weight=Decimal("1"),
        )

    # Tenant B sees nothing.
    async with tenant_context(app_engine, tenant_b) as session:
        rows = await AssetClassBenchmarkMappingRepository(session).list_all()
    assert rows == []
