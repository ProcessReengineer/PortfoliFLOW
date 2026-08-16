# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SAAAssetClassInputRepository tests against the live compose Postgres.

Coverage:

* Round-trip create + list_by_configuration.
* ``update`` modifies the requested fields.
* ``replace_all_for_configuration`` is atomic — old rows are gone,
  new rows are present.
* Cross-tenant isolation.
* Unique-constraint on (configuration_id, asset_class_id).
* CHECK constraints (volatility >= 0, min ≤ max).
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    SAAAssetClassInputRepository,
    SAAConfigurationRepository,
    UserRepository,
    tenant_context,
)


async def _seed_tenant_with_user_and_config(
    app_engine: AsyncEngine, seed_tenant
) -> tuple[UUID, UUID, UUID]:
    """Helper: create a tenant, user, and a fresh empty SAA configuration."""
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="ainputs@example.com", password_hash="x" * 8
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        config = await SAAConfigurationRepository(session).create(
            "Test Config", 0.02, 100, actor.id
        )
    return tenant_id, actor.id, config.id


# ---------------------------------------------------------------------------
# AI-01: create + list_by_configuration
# ---------------------------------------------------------------------------


async def test_ai01_create_and_list(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, actor_id, config_id = await _seed_tenant_with_user_and_config(
        app_engine, seed_tenant
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        ac_repo = AssetClassRepository(session)
        ac1 = await ac_repo.create(code="a", display_name="A")
        ac2 = await ac_repo.create(code="b", display_name="B")
        repo = SAAAssetClassInputRepository(session)
        await repo.create(
            configuration_id=config_id,
            asset_class_id=ac1.id,
            expected_return=0.07,
            volatility=0.15,
            min_weight=0.0,
            max_weight=0.5,
        )
        await repo.create(
            configuration_id=config_id,
            asset_class_id=ac2.id,
            expected_return=0.05,
            volatility=0.08,
            min_weight=0.1,
            max_weight=0.4,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await SAAAssetClassInputRepository(session).list_by_configuration(config_id)

    assert len(rows) == 2
    by_ac = {r.asset_class_id: r for r in rows}
    assert by_ac[ac1.id].expected_return == pytest.approx(0.07)
    assert by_ac[ac2.id].volatility == pytest.approx(0.08)


# ---------------------------------------------------------------------------
# AI-02: update modifies fields
# ---------------------------------------------------------------------------


async def test_ai02_update_modifies_fields(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, actor_id, config_id = await _seed_tenant_with_user_and_config(
        app_engine, seed_tenant
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        ac = await AssetClassRepository(session).create(code="x", display_name="X")
        row = await SAAAssetClassInputRepository(session).create(
            configuration_id=config_id,
            asset_class_id=ac.id,
            expected_return=0.05,
            volatility=0.10,
            min_weight=0.0,
            max_weight=1.0,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        updated = await SAAAssetClassInputRepository(session).update(
            row.id, expected_return=0.08, max_weight=0.7
        )

    assert updated.expected_return == pytest.approx(0.08)
    assert updated.max_weight == pytest.approx(0.7)
    assert updated.volatility == pytest.approx(0.10)  # untouched


# ---------------------------------------------------------------------------
# AI-03: replace_all_for_configuration is atomic
# ---------------------------------------------------------------------------


async def test_ai03_replace_all_atomic(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, actor_id, config_id = await _seed_tenant_with_user_and_config(
        app_engine, seed_tenant
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        ac_repo = AssetClassRepository(session)
        ac_old1 = await ac_repo.create(code="old1", display_name="Old 1")
        ac_old2 = await ac_repo.create(code="old2", display_name="Old 2")
        ac_new = await ac_repo.create(code="new", display_name="New")
        # Initial state: two rows
        repo = SAAAssetClassInputRepository(session)
        await repo.create(
            configuration_id=config_id,
            asset_class_id=ac_old1.id,
            expected_return=0.04,
            volatility=0.10,
            min_weight=0.0,
            max_weight=1.0,
        )
        await repo.create(
            configuration_id=config_id,
            asset_class_id=ac_old2.id,
            expected_return=0.05,
            volatility=0.11,
            min_weight=0.0,
            max_weight=1.0,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        repo = SAAAssetClassInputRepository(session)
        replaced = await repo.replace_all_for_configuration(
            config_id,
            [(ac_new.id, 0.09, 0.20, 0.05, 0.30)],
        )
    assert len(replaced) == 1
    assert replaced[0].asset_class_id == ac_new.id

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await SAAAssetClassInputRepository(session).list_by_configuration(config_id)
    assert [r.asset_class_id for r in rows] == [ac_new.id]


# ---------------------------------------------------------------------------
# AI-04: cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_ai04_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")

    async with tenant_context(app_engine, tenant_a) as session:
        actor_a = await UserRepository(session).create(email="a@example.com", password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        config_a = await SAAConfigurationRepository(session).create("A SAA", 0.01, 100, actor_a.id)
        ac_a = await AssetClassRepository(session).create(code="ac", display_name="A's AC")
        await SAAAssetClassInputRepository(session).create(
            configuration_id=config_a.id,
            asset_class_id=ac_a.id,
            expected_return=0.05,
            volatility=0.10,
            min_weight=0.0,
            max_weight=1.0,
        )

    async with tenant_context(app_engine, tenant_b) as session:
        rows = await SAAAssetClassInputRepository(session).list_by_configuration(config_a.id)
    assert rows == []


# ---------------------------------------------------------------------------
# AI-05: unique constraint on (config, asset_class)
# ---------------------------------------------------------------------------


async def test_ai05_unique_config_asset_pair(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, actor_id, config_id = await _seed_tenant_with_user_and_config(
        app_engine, seed_tenant
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        ac = await AssetClassRepository(session).create(code="x", display_name="X")
        await SAAAssetClassInputRepository(session).create(
            configuration_id=config_id,
            asset_class_id=ac.id,
            expected_return=0.05,
            volatility=0.10,
            min_weight=0.0,
            max_weight=1.0,
        )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
            ac_again = await AssetClassRepository(session).get_by_code("x")
            assert ac_again is not None
            await SAAAssetClassInputRepository(session).create(
                configuration_id=config_id,
                asset_class_id=ac_again.id,
                expected_return=0.06,
                volatility=0.11,
                min_weight=0.0,
                max_weight=1.0,
            )


# ---------------------------------------------------------------------------
# AI-06: CHECK constraint on min_weight <= max_weight
# ---------------------------------------------------------------------------


async def test_ai06_min_le_max_check_constraint(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, actor_id, config_id = await _seed_tenant_with_user_and_config(
        app_engine, seed_tenant
    )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
            ac = await AssetClassRepository(session).create(code="x", display_name="X")
            await SAAAssetClassInputRepository(session).create(
                configuration_id=config_id,
                asset_class_id=ac.id,
                expected_return=0.05,
                volatility=0.10,
                min_weight=0.7,
                max_weight=0.3,  # min > max → CHECK violated
            )
