# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SAAService end-to-end tests against the live compose Postgres.

The service is the right place to test the full read/write/compute
contract: it owns the cross-repository orchestration that route
handlers consume.

Coverage:

* ``list_configurations`` and ``get_configuration_full`` happy path
  with multiple configurations.
* ``get_configuration_full`` returns ``None`` for an unknown id.
* Lifecycle: create → save inputs/correlations → activate → delete.
* ``save_inputs_and_correlations`` rolls back on validation failure.
* ``run_optimization`` returns numerically plausible results.
* ``run_optimization`` raises on a configuration with < 2 inputs.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    SAAAssetClassInputRepository,
    SAAConfigurationRepository,
    SAACorrelationRepository,
    UserRepository,
    tenant_context,
)
from services.saa import (
    SAAAssetClassInputSpec,
    SAACorrelationSpec,
    SAAService,
    SAAValidationError,
)


def _build_service(session) -> SAAService:
    """Construct an SAAService bound to the given session."""
    return SAAService(
        configurations=SAAConfigurationRepository(session),
        asset_classes=AssetClassRepository(session),
        inputs=SAAAssetClassInputRepository(session),
        correlations=SAACorrelationRepository(session),
    )


# ---------------------------------------------------------------------------
# SS-01: list and get_configuration_full happy path
# ---------------------------------------------------------------------------


async def test_ss01_list_and_full_read(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="ss01@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        ac1 = await svc.create_asset_class("equities", "Equities")
        ac2 = await svc.create_asset_class("bonds", "Bonds")
        config = await svc.create_configuration("Demo", 0.02, 100, actor.id)
        await svc.save_inputs_and_correlations(
            config.id,
            [
                SAAAssetClassInputSpec(
                    asset_class_id=ac1.id,
                    expected_return=0.07,
                    volatility=0.15,
                    min_weight=0.0,
                    max_weight=1.0,
                ),
                SAAAssetClassInputSpec(
                    asset_class_id=ac2.id,
                    expected_return=0.03,
                    volatility=0.05,
                    min_weight=0.0,
                    max_weight=1.0,
                ),
            ],
            [
                SAACorrelationSpec(
                    asset_class_a_id=ac1.id,
                    asset_class_b_id=ac2.id,
                    correlation=0.1,
                )
            ],
        )

    async with tenant_context(app_engine, tenant_id) as session:
        svc = _build_service(session)
        configs = await svc.list_configurations()
        detail = await svc.get_configuration_full(config.id)

    assert [c.name for c in configs] == ["Demo"]
    assert detail is not None
    assert detail.configuration.id == config.id
    assert len(detail.inputs) == 2
    assert len(detail.correlations) == 1
    assert detail.correlations[0].correlation == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# SS-02: get_configuration_full returns None for missing id
# ---------------------------------------------------------------------------


async def test_ss02_get_full_missing_returns_none(app_engine: AsyncEngine, seed_tenant) -> None:
    from uuid import uuid4

    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        await UserRepository(session).create(email="ss02@example.com", password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_id) as session:
        svc = _build_service(session)
        result = await svc.get_configuration_full(uuid4())
    assert result is None


# ---------------------------------------------------------------------------
# SS-03: lifecycle — create + activate + delete
# ---------------------------------------------------------------------------


async def test_ss03_lifecycle(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="ss03@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        config = await svc.create_configuration("Lifecycle", 0.025, 100, actor.id)
        activated = await svc.activate_configuration(config.id)
    assert activated.is_active is True

    async with tenant_context(app_engine, tenant_id) as session:
        svc = _build_service(session)
        active = await svc.get_active_configuration()
    assert active is not None and active.id == config.id

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        await svc.delete_configuration(config.id)

    async with tenant_context(app_engine, tenant_id) as session:
        svc = _build_service(session)
        gone = await svc.get_configuration(config.id)
    assert gone is None


# ---------------------------------------------------------------------------
# SS-04: save_inputs_and_correlations is atomic on validation failure
# ---------------------------------------------------------------------------


async def test_ss04_validation_failure_rolls_back(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="ss04@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        ac1 = await svc.create_asset_class("a", "A")
        ac2 = await svc.create_asset_class("b", "B")
        config = await svc.create_configuration("V", 0.02, 100, actor.id)
        # First save: valid
        await svc.save_inputs_and_correlations(
            config.id,
            [
                SAAAssetClassInputSpec(
                    asset_class_id=ac1.id,
                    expected_return=0.05,
                    volatility=0.10,
                    min_weight=0.0,
                    max_weight=1.0,
                ),
                SAAAssetClassInputSpec(
                    asset_class_id=ac2.id,
                    expected_return=0.07,
                    volatility=0.15,
                    min_weight=0.0,
                    max_weight=1.0,
                ),
            ],
            [
                SAACorrelationSpec(
                    asset_class_a_id=ac1.id,
                    asset_class_b_id=ac2.id,
                    correlation=0.2,
                )
            ],
        )

    # Second save: invalid (negative volatility) — must not touch the DB.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        with pytest.raises(SAAValidationError):
            await svc.save_inputs_and_correlations(
                config.id,
                [
                    SAAAssetClassInputSpec(
                        asset_class_id=ac1.id,
                        expected_return=0.05,
                        volatility=-0.05,  # invalid
                        min_weight=0.0,
                        max_weight=1.0,
                    ),
                ],
                [],
            )

    # The original two-input state must still be intact.
    async with tenant_context(app_engine, tenant_id) as session:
        svc = _build_service(session)
        detail = await svc.get_configuration_full(config.id)
    assert detail is not None
    assert len(detail.inputs) == 2
    assert len(detail.correlations) == 1


# ---------------------------------------------------------------------------
# SS-05: run_optimization returns numerically plausible results
# ---------------------------------------------------------------------------


async def test_ss05_run_optimization_plausible(app_engine: AsyncEngine, seed_tenant) -> None:
    """A 3-asset configuration produces a sensible frontier and tangency."""
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="ss05@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        ac_eq = await svc.create_asset_class("equities", "Equities")
        ac_pe = await svc.create_asset_class("private_equity", "Private Equity")
        ac_bd = await svc.create_asset_class("bonds", "Bonds")
        config = await svc.create_configuration("Opt", 0.025, 50, actor.id)
        await svc.save_inputs_and_correlations(
            config.id,
            [
                SAAAssetClassInputSpec(
                    asset_class_id=ac_eq.id,
                    expected_return=0.075,
                    volatility=0.155,
                    min_weight=0.0,
                    max_weight=0.6,
                ),
                SAAAssetClassInputSpec(
                    asset_class_id=ac_pe.id,
                    expected_return=0.110,
                    volatility=0.180,
                    min_weight=0.0,
                    max_weight=0.4,
                ),
                SAAAssetClassInputSpec(
                    asset_class_id=ac_bd.id,
                    expected_return=0.035,
                    volatility=0.060,
                    min_weight=0.1,
                    max_weight=0.5,
                ),
            ],
            [
                SAACorrelationSpec(
                    asset_class_a_id=ac_eq.id,
                    asset_class_b_id=ac_pe.id,
                    correlation=0.7,
                ),
                SAACorrelationSpec(
                    asset_class_a_id=ac_eq.id,
                    asset_class_b_id=ac_bd.id,
                    correlation=0.1,
                ),
                SAACorrelationSpec(
                    asset_class_a_id=ac_pe.id,
                    asset_class_b_id=ac_bd.id,
                    correlation=0.05,
                ),
            ],
        )

    async with tenant_context(app_engine, tenant_id) as session:
        svc = _build_service(session)
        result = await svc.run_optimization(config.id)

    assert len(result.asset_names) == 3
    assert len(result.frontier) > 10  # most points converge
    assert result.tangency.sharpe_ratio > 0  # rf=2.5%, max ER=11% → positive
    assert result.tangency.volatility > 0
    assert result.min_var.volatility > 0
    assert result.min_var.volatility <= result.tangency.volatility + 1e-6
    assert len(result.cml) == 50
    assert len(result.cloud) > 100  # at least some random portfolios feasible


# ---------------------------------------------------------------------------
# SS-06: run_optimization rejects a configuration with < 2 inputs
# ---------------------------------------------------------------------------


async def test_ss06_run_optimization_requires_two_assets(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="ss06@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _build_service(session)
        ac = await svc.create_asset_class("only_one", "Only One")
        config = await svc.create_configuration("OneAsset", 0.02, 100, actor.id)
        await svc.save_inputs_and_correlations(
            config.id,
            [
                SAAAssetClassInputSpec(
                    asset_class_id=ac.id,
                    expected_return=0.05,
                    volatility=0.10,
                    min_weight=0.0,
                    max_weight=1.0,
                ),
            ],
            [],
        )

    async with tenant_context(app_engine, tenant_id) as session:
        svc = _build_service(session)
        with pytest.raises(SAAValidationError):
            await svc.run_optimization(config.id)
