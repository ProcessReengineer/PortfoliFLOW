# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SAAConfigurationRepository tests against the live compose Postgres.

Coverage:

* Create + get_by_id + list_all + update + delete (happy path).
* ``set_active`` deactivates peers atomically.
* ``get_active`` returns the active row or ``None``.
* The partial unique index rejects two simultaneously-active rows
  when the deactivate-peers step is bypassed via raw SQL.
* RLS isolates configurations between tenants.
* Delete cascades to inputs / correlations.
* CHECK constraint on ``n_frontier_points`` rejects out-of-range values.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    SAAAssetClassInputRepository,
    SAAConfigurationRepository,
    SAACorrelationRepository,
    UserRepository,
    tenant_context,
)


# ---------------------------------------------------------------------------
# SC-01: create + get_by_id round-trip
# ---------------------------------------------------------------------------


async def test_sc01_create_and_get_by_id(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="sc01@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = SAAConfigurationRepository(session)
        created = await repo.create(
            name="Demo SAA",
            risk_free_rate=0.025,
            n_frontier_points=120,
            created_by=actor.id,
        )

    assert created.name == "Demo SAA"
    assert created.risk_free_rate == pytest.approx(0.025)
    assert created.n_frontier_points == 120
    assert created.is_active is False
    assert created.tenant_id == tenant_id
    assert created.created_by == actor.id

    async with tenant_context(app_engine, tenant_id) as session:
        fetched = await SAAConfigurationRepository(session).get_by_id(created.id)
    assert fetched is not None
    assert fetched.name == "Demo SAA"


# ---------------------------------------------------------------------------
# SC-02: set_active deactivates peers
# ---------------------------------------------------------------------------


async def test_sc02_set_active_deactivates_peers(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="sc02@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = SAAConfigurationRepository(session)
        first = await repo.create("First", 0.01, 100, actor.id)
        second = await repo.create("Second", 0.02, 100, actor.id)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = SAAConfigurationRepository(session)
        await repo.set_active(first.id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = SAAConfigurationRepository(session)
        active = await repo.get_active()
    assert active is not None and active.id == first.id

    # Activate the second; first must auto-deactivate.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = SAAConfigurationRepository(session)
        await repo.set_active(second.id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = SAAConfigurationRepository(session)
        active = await repo.get_active()
        all_configs = await repo.list_all()

    assert active is not None and active.id == second.id
    actives = [c for c in all_configs if c.is_active]
    assert [c.id for c in actives] == [second.id]


# ---------------------------------------------------------------------------
# SC-03: get_active returns None when no active row exists
# ---------------------------------------------------------------------------


async def test_sc03_get_active_returns_none_when_no_active(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="sc03@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await SAAConfigurationRepository(session).create("Inactive", 0.01, 100, actor.id)

    async with tenant_context(app_engine, tenant_id) as session:
        active = await SAAConfigurationRepository(session).get_active()
    assert active is None


# ---------------------------------------------------------------------------
# SC-04: partial unique index rejects raw double-activation
# ---------------------------------------------------------------------------


async def test_sc04_partial_unique_index_rejects_double_active(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Bypass the repository to verify the DB-level guarantee.

    The repository's ``set_active`` deactivates peers in the same
    transaction so the partial unique index is never violated. A
    direct UPDATE that skips that step must be rejected by Postgres
    so a future routing-handler bug cannot leave two active
    configurations behind.
    """
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="sc04@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = SAAConfigurationRepository(session)
        first = await repo.create("First", 0.01, 100, actor.id)
        second = await repo.create("Second", 0.02, 100, actor.id)
        # Activate the first via the safe path.
        await repo.set_active(first.id)

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await session.execute(
                text("UPDATE saa_configurations SET is_active = TRUE WHERE id = :cid"),
                {"cid": str(second.id)},
            )


# ---------------------------------------------------------------------------
# SC-05: cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_sc05_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")

    async with tenant_context(app_engine, tenant_a) as session:
        actor_a = await UserRepository(session).create(email="a@example.com", password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_b) as session:
        actor_b = await UserRepository(session).create(email="b@example.com", password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await SAAConfigurationRepository(session).create("Tenant A SAA", 0.01, 100, actor_a.id)
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await SAAConfigurationRepository(session).create("Tenant B SAA", 0.02, 100, actor_b.id)

    async with tenant_context(app_engine, tenant_a) as session:
        a_view = await SAAConfigurationRepository(session).list_all()
    async with tenant_context(app_engine, tenant_b) as session:
        b_view = await SAAConfigurationRepository(session).list_all()

    assert [c.name for c in a_view] == ["Tenant A SAA"]
    assert [c.name for c in b_view] == ["Tenant B SAA"]


# ---------------------------------------------------------------------------
# SC-06: delete cascades to inputs and correlations
# ---------------------------------------------------------------------------


async def test_sc06_delete_cascades_to_inputs_and_correlations(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="sc06@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        config_repo = SAAConfigurationRepository(session)
        ac_repo = AssetClassRepository(session)
        config = await config_repo.create("Cascade", 0.01, 100, actor.id)
        ac1 = await ac_repo.create(code="ac_a", display_name="A")
        ac2 = await ac_repo.create(code="ac_b", display_name="B")
        await SAAAssetClassInputRepository(session).create(
            configuration_id=config.id,
            asset_class_id=ac1.id,
            expected_return=0.07,
            volatility=0.15,
            min_weight=0.0,
            max_weight=1.0,
        )
        await SAAAssetClassInputRepository(session).create(
            configuration_id=config.id,
            asset_class_id=ac2.id,
            expected_return=0.05,
            volatility=0.10,
            min_weight=0.0,
            max_weight=1.0,
        )
        await SAACorrelationRepository(session).create(
            configuration_id=config.id,
            asset_class_a_id=ac1.id,
            asset_class_b_id=ac2.id,
            correlation=0.3,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await SAAConfigurationRepository(session).delete(config.id)

    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM saa_asset_class_inputs WHERE configuration_id = :cid"),
            {"cid": str(config.id)},
        )
        assert result.scalar_one() == 0
        result = await session.execute(
            text("SELECT COUNT(*) FROM saa_correlations WHERE configuration_id = :cid"),
            {"cid": str(config.id)},
        )
        assert result.scalar_one() == 0


# ---------------------------------------------------------------------------
# SC-07: CHECK constraint on n_frontier_points
# ---------------------------------------------------------------------------


async def test_sc07_n_frontier_points_check_constraint(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="sc07@example.com", password_hash="x" * 8
        )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await SAAConfigurationRepository(session).create(
                "Too Few",
                0.01,
                10,
                actor.id,  # below 20 → CHECK fails
            )
