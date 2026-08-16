# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentRegionWeightsRepository tests against the live compose Postgres.

Region weights gained a dedicated repository test with ADR-0080: the
table is now historised, so the same snapshot-aware contract the
sector and country repos expose must hold here too. Coverage:

* ``replace_snapshot_for_investment`` is date-scoped and replaces in
  place within one snapshot.
* Cross-tenant isolation.
* ``weight_pct`` range CHECK rejects out-of-range values.
* Empty input clears the snapshot.
* Batched ``list_by_investments`` matches the singular reader.
* Two snapshots coexist; ``list_latest_*`` returns the most recent,
  ``list_by_investments`` returns both, a date-scoped replace leaves
  the other snapshot intact, and ``as_of_cutoff`` selects the earlier.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InvestmentRegionWeightsRepository,
    InvestmentRepository,
    RegionRepository,
    RegionWeightInput,
    UserRepository,
    tenant_context,
)

# Two statement dates used across the historisation tests.
_D1 = date(2024, 3, 31)
_D2 = date(2024, 6, 30)


async def _seed_actor_investment_region(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    region_code: str = "dach",
    investment_name: str = "Investment X",
):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(code="ac_for_rw", display_name="AC")
        inv = await InvestmentRepository(session).create(
            name=investment_name,
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        region = await RegionRepository(session).create(
            code=region_code,
            display_name=region_code.upper(),
        )
    return actor, inv, region


# ---------------------------------------------------------------------------
# RW-01: replace_snapshot is idempotent and replaces in place
# ---------------------------------------------------------------------------


async def test_rw01_replace_snapshot_replaces_in_place(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv, region = await _seed_actor_investment_region(
        app_engine, tenant_id, email="rw01@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        region_b = await RegionRepository(session).create(
            code="na_usa", display_name="North America — USA"
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRegionWeightsRepository(session)
        first = await repo.replace_snapshot_for_investment(
            inv.id,
            _D1,
            [
                RegionWeightInput(region_id=region.id, weight_pct=Decimal("60")),
                RegionWeightInput(region_id=region_b.id, weight_pct=Decimal("40")),
            ],
            basis="reported",
            created_by=actor.id,
        )
    assert {w.region_id for w in first} == {region.id, region_b.id}
    assert all(w.as_of_date == _D1 for w in first)
    assert all(w.basis == "reported" for w in first)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRegionWeightsRepository(session)
        second = await repo.replace_snapshot_for_investment(
            inv.id,
            _D1,
            [RegionWeightInput(region_id=region.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )
    assert len(second) == 1
    assert second[0].region_id == region.id


# ---------------------------------------------------------------------------
# RW-02: cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_rw02_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    actor_a, inv_a, region_a = await _seed_actor_investment_region(
        app_engine,
        tenant_a,
        email="rw02-a@example.com",
        investment_name="Inv-A",
    )
    actor_b, inv_b, region_b = await _seed_actor_investment_region(
        app_engine,
        tenant_b,
        email="rw02-b@example.com",
        investment_name="Inv-B",
    )

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await InvestmentRegionWeightsRepository(session).replace_snapshot_for_investment(
            inv_a.id,
            _D1,
            [RegionWeightInput(region_id=region_a.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor_a.id,
        )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await InvestmentRegionWeightsRepository(session).replace_snapshot_for_investment(
            inv_b.id,
            _D1,
            [RegionWeightInput(region_id=region_b.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor_b.id,
        )

    async with tenant_context(app_engine, tenant_b) as session:
        repo = InvestmentRegionWeightsRepository(session)
        rows_for_a = await repo.list_for_investment(inv_a.id)
        rows_for_b = await repo.list_for_investment(inv_b.id)
    assert rows_for_a == []
    assert len(rows_for_b) == 1


# ---------------------------------------------------------------------------
# RW-03: weight_pct range CHECK rejects out-of-range values
# ---------------------------------------------------------------------------


async def test_rw03_weight_pct_range_check(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv, region = await _seed_actor_investment_region(
        app_engine, tenant_id, email="rw03@example.com"
    )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            repo = InvestmentRegionWeightsRepository(session)
            await repo.replace_snapshot_for_investment(
                inv.id,
                _D1,
                [RegionWeightInput(region_id=region.id, weight_pct=Decimal("-1"))],
                basis="reported",
                created_by=actor.id,
            )


# ---------------------------------------------------------------------------
# RW-04: empty input clears the snapshot
# ---------------------------------------------------------------------------


async def test_rw04_empty_input_clears_existing(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv, region = await _seed_actor_investment_region(
        app_engine, tenant_id, email="rw04@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRegionWeightsRepository(session)
        await repo.replace_snapshot_for_investment(
            inv.id,
            _D1,
            [RegionWeightInput(region_id=region.id, weight_pct=Decimal("75"))],
            basis="reported",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRegionWeightsRepository(session)
        result = await repo.replace_snapshot_for_investment(
            inv.id, _D1, [], basis="reported", created_by=actor.id
        )
    assert result == []

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentRegionWeightsRepository(session).list_for_investment(inv.id)
    assert rows == []


# ---------------------------------------------------------------------------
# Batched plural method
# ---------------------------------------------------------------------------


async def _seed_three_investments_with_region_weights(
    app_engine: AsyncEngine, tenant_id, *, email: str
):
    """Seed actor, asset class, three investments, two regions and weights.

    Returns ``(actor, [inv_a, inv_b, inv_c])``: Inv A has two weight
    rows, Inv B has one, Inv C has none. All at snapshot ``_D1``.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="batched_class", display_name="Batched Class"
        )
        region_repo = RegionRepository(session)
        region_d = await region_repo.create(code="dach", display_name="DACH")
        region_n = await region_repo.create(code="na_usa", display_name="North America — USA")
        inv_repo = InvestmentRepository(session)
        inv_a = await inv_repo.create(
            name="Alpha",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        inv_b = await inv_repo.create(
            name="Beta",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        inv_c = await inv_repo.create(
            name="Gamma",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        weights_repo = InvestmentRegionWeightsRepository(session)
        await weights_repo.replace_snapshot_for_investment(
            inv_a.id,
            _D1,
            [
                RegionWeightInput(region_id=region_d.id, weight_pct=Decimal("40")),
                RegionWeightInput(region_id=region_n.id, weight_pct=Decimal("60")),
            ],
            basis="reported",
            created_by=actor.id,
        )
        await weights_repo.replace_snapshot_for_investment(
            inv_b.id,
            _D1,
            [RegionWeightInput(region_id=region_d.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )
    return actor, [inv_a, inv_b, inv_c]


async def test_rw05_list_by_investments_matches_singular(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    _actor, invs = await _seed_three_investments_with_region_weights(
        app_engine, tenant_id, email="rw05@example.com"
    )
    inv_a, inv_b, inv_c = invs

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentRegionWeightsRepository(session)
        singular = {
            inv_a.id: await repo.list_for_investment(inv_a.id),
            inv_b.id: await repo.list_for_investment(inv_b.id),
            inv_c.id: await repo.list_for_investment(inv_c.id),
        }
        batched = await repo.list_by_investments([inv_a.id, inv_b.id, inv_c.id])

    assert set(batched.keys()) == {inv_a.id, inv_b.id, inv_c.id}
    for inv_id, rows in singular.items():
        assert batched[inv_id] == rows


async def test_rw06_list_by_investments_empty_input_returns_empty_dict(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        result = await InvestmentRegionWeightsRepository(session).list_by_investments([])
    assert result == {}


async def test_rw07_list_by_investments_missing_id_maps_to_empty_list(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    _actor, invs = await _seed_three_investments_with_region_weights(
        app_engine, tenant_id, email="rw07@example.com"
    )
    inv_a, inv_b, _inv_c = invs
    fresh_id = uuid4()

    async with tenant_context(app_engine, tenant_id) as session:
        result = await InvestmentRegionWeightsRepository(session).list_by_investments(
            [inv_a.id, inv_b.id, fresh_id]
        )

    assert set(result.keys()) == {inv_a.id, inv_b.id, fresh_id}
    assert result[fresh_id] == []
    assert len(result[inv_a.id]) == 2
    assert len(result[inv_b.id]) == 1


# ---------------------------------------------------------------------------
# ADR-0080 historisation behaviour (Tests 2–4)
# ---------------------------------------------------------------------------


async def _seed_two_snapshots(app_engine: AsyncEngine, tenant_id, *, email: str):
    """Seed one investment with two regions and two snapshots.

    Snapshot ``_D1`` holds only ``region_d`` (100%); snapshot ``_D2``
    (later) holds only ``region_n`` (100%). Returns
    ``(actor, inv, region_d, region_n)``.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="hist_class", display_name="Hist Class"
        )
        region_repo = RegionRepository(session)
        region_d = await region_repo.create(code="dach", display_name="DACH")
        region_n = await region_repo.create(code="na_usa", display_name="North America — USA")
        inv = await InvestmentRepository(session).create(
            name="Historised",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        repo = InvestmentRegionWeightsRepository(session)
        await repo.replace_snapshot_for_investment(
            inv.id,
            _D1,
            [RegionWeightInput(region_id=region_d.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )
        await repo.replace_snapshot_for_investment(
            inv.id,
            _D2,
            [RegionWeightInput(region_id=region_n.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )
    return actor, inv, region_d, region_n


async def test_rw08_two_snapshots_latest_vs_full_history(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """ADR-0080 Test 2: two snapshots coexist; latest reader picks D2."""
    tenant_id = await seed_tenant()
    _actor, inv, _region_d, region_n = await _seed_two_snapshots(
        app_engine, tenant_id, email="rw08@example.com"
    )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentRegionWeightsRepository(session)
        full = await repo.list_by_investments([inv.id])
        latest = await repo.list_latest_by_investments([inv.id])

    assert len(full[inv.id]) == 2
    assert {r.as_of_date for r in full[inv.id]} == {_D1, _D2}

    assert len(latest[inv.id]) == 1
    assert latest[inv.id][0].as_of_date == _D2
    assert latest[inv.id][0].region_id == region_n.id


async def test_rw09_date_scoped_replace_leaves_other_snapshot(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """ADR-0080 Test 3: replacing D2 does not touch D1."""
    tenant_id = await seed_tenant()
    actor, inv, region_d, _region_n = await _seed_two_snapshots(
        app_engine, tenant_id, email="rw09@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRegionWeightsRepository(session)
        await repo.replace_snapshot_for_investment(
            inv.id,
            _D2,
            [RegionWeightInput(region_id=region_d.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentRegionWeightsRepository(session)
        d1_rows = await repo.list_for_investment(inv.id, as_of_cutoff=_D1)
        d2_rows = await repo.list_latest_for_investment(inv.id)

    assert len(d1_rows) == 1
    assert d1_rows[0].as_of_date == _D1
    assert d1_rows[0].region_id == region_d.id
    assert len(d2_rows) == 1
    assert d2_rows[0].as_of_date == _D2
    assert d2_rows[0].region_id == region_d.id


async def test_rw10_latest_cutoff_selects_earlier_snapshot(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """ADR-0080 Test 4: list_latest_* with cutoff=D1 returns the D1 snapshot."""
    tenant_id = await seed_tenant()
    _actor, inv, region_d, _region_n = await _seed_two_snapshots(
        app_engine, tenant_id, email="rw10@example.com"
    )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentRegionWeightsRepository(session)
        latest_for = await repo.list_latest_for_investment(inv.id, as_of_cutoff=_D1)
        latest_by = await repo.list_latest_by_investments([inv.id], as_of_cutoff=_D1)

    assert len(latest_for) == 1
    assert latest_for[0].as_of_date == _D1
    assert latest_for[0].region_id == region_d.id

    assert len(latest_by[inv.id]) == 1
    assert latest_by[inv.id][0].as_of_date == _D1
    assert latest_by[inv.id][0].region_id == region_d.id
