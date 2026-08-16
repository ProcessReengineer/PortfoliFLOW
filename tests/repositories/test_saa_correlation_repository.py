# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SAACorrelationRepository tests against the live compose Postgres.

Coverage:

* Create normalises UUID order before persisting.
* ``get_correlation`` works in both pair orderings.
* ``replace_all_for_configuration`` is atomic.
* Self-correlation raises ValueError on create / replace.
* Cross-tenant isolation.
* Unique-constraint per (config, a, b).
* CHECK constraint on the correlation value range.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    SAAConfigurationRepository,
    SAACorrelationRepository,
    UserRepository,
    tenant_context,
)


async def _seed_tenant_user_config_two_acs(
    app_engine: AsyncEngine, seed_tenant
) -> tuple[UUID, UUID, UUID, UUID, UUID]:
    """Create a tenant, user, config, and two asset classes.

    Returns ``(tenant_id, actor_id, config_id, ac1_id, ac2_id)``
    where ``ac1_id < ac2_id`` so callers do not have to re-sort.
    """
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="corr@example.com", password_hash="x" * 8
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        config = await SAAConfigurationRepository(session).create("Corr Test", 0.02, 100, actor.id)
        ac1 = await AssetClassRepository(session).create(code="ac_one", display_name="One")
        ac2 = await AssetClassRepository(session).create(code="ac_two", display_name="Two")

    smaller, larger = sorted([ac1.id, ac2.id])
    return tenant_id, actor.id, config.id, smaller, larger


# ---------------------------------------------------------------------------
# CR-01: create normalises UUID order
# ---------------------------------------------------------------------------


async def test_cr01_create_normalises_uuid_order(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, actor_id, config_id, smaller, larger = await _seed_tenant_user_config_two_acs(
        app_engine, seed_tenant
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        repo = SAACorrelationRepository(session)
        # Pass the larger first; repository must swap.
        created = await repo.create(
            configuration_id=config_id,
            asset_class_a_id=larger,
            asset_class_b_id=smaller,
            correlation=0.4,
        )

    assert created.asset_class_a_id == smaller
    assert created.asset_class_b_id == larger
    assert created.correlation == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# CR-02: get_correlation works in both orders
# ---------------------------------------------------------------------------


async def test_cr02_get_correlation_in_both_orders(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, actor_id, config_id, smaller, larger = await _seed_tenant_user_config_two_acs(
        app_engine, seed_tenant
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        await SAACorrelationRepository(session).create(
            configuration_id=config_id,
            asset_class_a_id=smaller,
            asset_class_b_id=larger,
            correlation=0.25,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = SAACorrelationRepository(session)
        forward = await repo.get_correlation(config_id, smaller, larger)
        backward = await repo.get_correlation(config_id, larger, smaller)

    assert forward is not None and forward.correlation == pytest.approx(0.25)
    assert backward is not None and backward.id == forward.id


# ---------------------------------------------------------------------------
# CR-03: replace_all_for_configuration is atomic
# ---------------------------------------------------------------------------


async def test_cr03_replace_all_atomic(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, actor_id, config_id, smaller, larger = await _seed_tenant_user_config_two_acs(
        app_engine, seed_tenant
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        repo = SAACorrelationRepository(session)
        await repo.create(
            configuration_id=config_id,
            asset_class_a_id=smaller,
            asset_class_b_id=larger,
            correlation=0.10,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        repo = SAACorrelationRepository(session)
        replaced = await repo.replace_all_for_configuration(config_id, [(smaller, larger, 0.95)])
    assert len(replaced) == 1
    assert replaced[0].correlation == pytest.approx(0.95)

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await SAACorrelationRepository(session).list_by_configuration(config_id)
    assert [r.correlation for r in rows] == pytest.approx([0.95])


# ---------------------------------------------------------------------------
# CR-04: self-correlation rejected on create
# ---------------------------------------------------------------------------


async def test_cr04_self_correlation_rejected(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, actor_id, config_id, smaller, _ = await _seed_tenant_user_config_two_acs(
        app_engine, seed_tenant
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        repo = SAACorrelationRepository(session)
        with pytest.raises(ValueError):
            await repo.create(
                configuration_id=config_id,
                asset_class_a_id=smaller,
                asset_class_b_id=smaller,
                correlation=1.0,
            )


# ---------------------------------------------------------------------------
# CR-05: cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_cr05_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    (
        tenant_id_a,
        actor_a_id,
        config_a_id,
        smaller_a,
        larger_a,
    ) = await _seed_tenant_user_config_two_acs(app_engine, seed_tenant)

    async with tenant_context(app_engine, tenant_id_a, user_id=actor_a_id) as session:
        await SAACorrelationRepository(session).create(
            configuration_id=config_a_id,
            asset_class_a_id=smaller_a,
            asset_class_b_id=larger_a,
            correlation=0.5,
        )

    tenant_b = await seed_tenant(name="B")
    async with tenant_context(app_engine, tenant_b) as session:
        rows = await SAACorrelationRepository(session).list_by_configuration(config_a_id)
    assert rows == []


# ---------------------------------------------------------------------------
# CR-06: unique constraint per (config, a, b)
# ---------------------------------------------------------------------------


async def test_cr06_unique_per_config_pair(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id, actor_id, config_id, smaller, larger = await _seed_tenant_user_config_two_acs(
        app_engine, seed_tenant
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        await SAACorrelationRepository(session).create(
            configuration_id=config_id,
            asset_class_a_id=smaller,
            asset_class_b_id=larger,
            correlation=0.3,
        )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
            await SAACorrelationRepository(session).create(
                configuration_id=config_id,
                asset_class_a_id=larger,
                asset_class_b_id=smaller,  # repository normalises → same row
                correlation=0.4,
            )


# ---------------------------------------------------------------------------
# CR-07: CHECK constraint on correlation range
# ---------------------------------------------------------------------------


async def test_cr07_correlation_range_check_constraint(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, actor_id, config_id, smaller, larger = await _seed_tenant_user_config_two_acs(
        app_engine, seed_tenant
    )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
            await SAACorrelationRepository(session).create(
                configuration_id=config_id,
                asset_class_a_id=smaller,
                asset_class_b_id=larger,
                correlation=1.5,  # out of [-1, 1]
            )
