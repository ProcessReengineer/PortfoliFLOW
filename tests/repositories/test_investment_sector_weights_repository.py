# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentSectorWeightsRepository tests against the live compose Postgres.

Mirrors :mod:`test_investment_country_weights_repository` for sectors,
and exercises the ADR-0080 historisation contract:
``replace_snapshot_for_investment`` is date-scoped, ``list_latest_*``
returns the most-recent snapshot and ``list_by_investments`` returns
the full history.
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
    InvestmentRepository,
    InvestmentSectorWeightsRepository,
    SectorRepository,
    SectorWeightInput,
    UserRepository,
    tenant_context,
)

# Two statement dates used across the historisation tests.
_D1 = date(2024, 3, 31)
_D2 = date(2024, 6, 30)


async def _seed_actor_investment_sector(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    sector_code: str = "tech_software",
    investment_name: str = "Investment X",
):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(code="ac_for_sw", display_name="AC")
        inv = await InvestmentRepository(session).create(
            name=investment_name,
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        sector = await SectorRepository(session).create(
            code=sector_code,
            display_name=sector_code.replace("_", " ").title(),
            created_by=actor.id,
        )
    return actor, inv, sector


# ---------------------------------------------------------------------------
# SW-01: replace_snapshot is idempotent and replaces in place
# ---------------------------------------------------------------------------


async def test_sw01_replace_snapshot_replaces_in_place(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv, sector = await _seed_actor_investment_sector(
        app_engine, tenant_id, email="sw01@example.com"
    )

    # Add a second sector for the replace test.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        sector_b = await SectorRepository(session).create(
            code="healthcare",
            display_name="Healthcare",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentSectorWeightsRepository(session)
        first = await repo.replace_snapshot_for_investment(
            inv.id,
            _D1,
            [
                SectorWeightInput(sector_id=sector.id, weight_pct=Decimal("60")),
                SectorWeightInput(sector_id=sector_b.id, weight_pct=Decimal("40")),
            ],
            basis="reported",
            created_by=actor.id,
        )
    assert {w.sector_id for w in first} == {sector.id, sector_b.id}
    assert all(w.as_of_date == _D1 for w in first)
    assert all(w.basis == "reported" for w in first)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentSectorWeightsRepository(session)
        second = await repo.replace_snapshot_for_investment(
            inv.id,
            _D1,
            [
                SectorWeightInput(sector_id=sector.id, weight_pct=Decimal("100")),
            ],
            basis="reported",
            created_by=actor.id,
        )
    assert len(second) == 1
    assert second[0].sector_id == sector.id


# ---------------------------------------------------------------------------
# SW-02: cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_sw02_cross_tenant_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    actor_a, inv_a, sector_a = await _seed_actor_investment_sector(
        app_engine,
        tenant_a,
        email="sw02-a@example.com",
        investment_name="Inv-A",
    )
    actor_b, inv_b, sector_b = await _seed_actor_investment_sector(
        app_engine,
        tenant_b,
        email="sw02-b@example.com",
        investment_name="Inv-B",
    )

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await InvestmentSectorWeightsRepository(session).replace_snapshot_for_investment(
            inv_a.id,
            _D1,
            [SectorWeightInput(sector_id=sector_a.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor_a.id,
        )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await InvestmentSectorWeightsRepository(session).replace_snapshot_for_investment(
            inv_b.id,
            _D1,
            [SectorWeightInput(sector_id=sector_b.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor_b.id,
        )

    async with tenant_context(app_engine, tenant_b) as session:
        repo = InvestmentSectorWeightsRepository(session)
        rows_for_a = await repo.list_for_investment(inv_a.id)
        rows_for_b = await repo.list_for_investment(inv_b.id)
    assert rows_for_a == []
    assert len(rows_for_b) == 1


# ---------------------------------------------------------------------------
# SW-03: weight_pct range CHECK rejects out-of-range values
# ---------------------------------------------------------------------------


async def test_sw03_weight_pct_range_check(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv, sector = await _seed_actor_investment_sector(
        app_engine, tenant_id, email="sw03@example.com"
    )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            repo = InvestmentSectorWeightsRepository(session)
            await repo.replace_snapshot_for_investment(
                inv.id,
                _D1,
                [SectorWeightInput(sector_id=sector.id, weight_pct=Decimal("-1"))],
                basis="reported",
                created_by=actor.id,
            )


# ---------------------------------------------------------------------------
# SW-04: empty input clears the snapshot
# ---------------------------------------------------------------------------


async def test_sw04_empty_input_clears_existing(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv, sector = await _seed_actor_investment_sector(
        app_engine, tenant_id, email="sw04@example.com"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentSectorWeightsRepository(session)
        await repo.replace_snapshot_for_investment(
            inv.id,
            _D1,
            [SectorWeightInput(sector_id=sector.id, weight_pct=Decimal("75"))],
            basis="reported",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentSectorWeightsRepository(session)
        result = await repo.replace_snapshot_for_investment(
            inv.id, _D1, [], basis="reported", created_by=actor.id
        )
    assert result == []

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentSectorWeightsRepository(session).list_for_investment(inv.id)
    assert rows == []


# ---------------------------------------------------------------------------
# Batched plural method (P6-H)
# ---------------------------------------------------------------------------


async def _seed_three_investments_with_sector_weights(
    app_engine: AsyncEngine, tenant_id, *, email: str
):
    """Seed actor, asset class, three investments, two sectors and weights.

    Returns ``(actor, [inv_a, inv_b, inv_c])``: Inv A has two weight
    rows, Inv B has one, Inv C has none. All at snapshot ``_D1``.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="batched_class", display_name="Batched Class"
        )
        sector_t = await SectorRepository(session).create(
            code="tech_software",
            display_name="Tech Software",
            created_by=actor.id,
        )
        sector_h = await SectorRepository(session).create(
            code="healthcare",
            display_name="Healthcare",
            created_by=actor.id,
        )
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
        weights_repo = InvestmentSectorWeightsRepository(session)
        await weights_repo.replace_snapshot_for_investment(
            inv_a.id,
            _D1,
            [
                SectorWeightInput(sector_id=sector_t.id, weight_pct=Decimal("40")),
                SectorWeightInput(sector_id=sector_h.id, weight_pct=Decimal("60")),
            ],
            basis="reported",
            created_by=actor.id,
        )
        await weights_repo.replace_snapshot_for_investment(
            inv_b.id,
            _D1,
            [SectorWeightInput(sector_id=sector_t.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )
    return actor, [inv_a, inv_b, inv_c]


async def test_sw05_list_by_investments_matches_singular(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    _actor, invs = await _seed_three_investments_with_sector_weights(
        app_engine, tenant_id, email="sw05@example.com"
    )
    inv_a, inv_b, inv_c = invs

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentSectorWeightsRepository(session)
        singular = {
            inv_a.id: await repo.list_for_investment(inv_a.id),
            inv_b.id: await repo.list_for_investment(inv_b.id),
            inv_c.id: await repo.list_for_investment(inv_c.id),
        }
        batched = await repo.list_by_investments([inv_a.id, inv_b.id, inv_c.id])

    assert set(batched.keys()) == {inv_a.id, inv_b.id, inv_c.id}
    for inv_id, rows in singular.items():
        assert batched[inv_id] == rows


async def test_sw06_list_by_investments_empty_input_returns_empty_dict(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        result = await InvestmentSectorWeightsRepository(session).list_by_investments([])
    assert result == {}


async def test_sw07_list_by_investments_missing_id_maps_to_empty_list(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    _actor, invs = await _seed_three_investments_with_sector_weights(
        app_engine, tenant_id, email="sw07@example.com"
    )
    inv_a, inv_b, _inv_c = invs
    fresh_id = uuid4()

    async with tenant_context(app_engine, tenant_id) as session:
        result = await InvestmentSectorWeightsRepository(session).list_by_investments(
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
    """Seed one investment with two sectors and two snapshots.

    Snapshot ``_D1`` holds only ``sector_t`` (100%); snapshot ``_D2``
    (later) holds only ``sector_h`` (100%). Returns
    ``(actor, inv, sector_t, sector_h)``.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="hist_class", display_name="Hist Class"
        )
        sector_t = await SectorRepository(session).create(
            code="tech_software",
            display_name="Tech Software",
            created_by=actor.id,
        )
        sector_h = await SectorRepository(session).create(
            code="healthcare",
            display_name="Healthcare",
            created_by=actor.id,
        )
        inv = await InvestmentRepository(session).create(
            name="Historised",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        repo = InvestmentSectorWeightsRepository(session)
        await repo.replace_snapshot_for_investment(
            inv.id,
            _D1,
            [SectorWeightInput(sector_id=sector_t.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )
        await repo.replace_snapshot_for_investment(
            inv.id,
            _D2,
            [SectorWeightInput(sector_id=sector_h.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )
    return actor, inv, sector_t, sector_h


async def test_sw08_two_snapshots_latest_vs_full_history(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """ADR-0080 Test 2: two snapshots coexist; latest reader picks D2."""
    tenant_id = await seed_tenant()
    _actor, inv, _sector_t, sector_h = await _seed_two_snapshots(
        app_engine, tenant_id, email="sw08@example.com"
    )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentSectorWeightsRepository(session)
        full = await repo.list_by_investments([inv.id])
        latest = await repo.list_latest_by_investments([inv.id])

    # Full history carries both snapshots.
    assert len(full[inv.id]) == 2
    assert {r.as_of_date for r in full[inv.id]} == {_D1, _D2}

    # Latest reader returns only the D2 snapshot.
    assert len(latest[inv.id]) == 1
    assert latest[inv.id][0].as_of_date == _D2
    assert latest[inv.id][0].sector_id == sector_h.id


async def test_sw09_date_scoped_replace_leaves_other_snapshot(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """ADR-0080 Test 3: replacing D2 does not touch D1."""
    tenant_id = await seed_tenant()
    actor, inv, sector_t, _sector_h = await _seed_two_snapshots(
        app_engine, tenant_id, email="sw09@example.com"
    )

    # Replace the D2 snapshot with a different mix.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentSectorWeightsRepository(session)
        await repo.replace_snapshot_for_investment(
            inv.id,
            _D2,
            [SectorWeightInput(sector_id=sector_t.id, weight_pct=Decimal("100"))],
            basis="reported",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentSectorWeightsRepository(session)
        d1_rows = await repo.list_for_investment(inv.id, as_of_cutoff=_D1)
        d2_rows = await repo.list_latest_for_investment(inv.id)

    # D1 snapshot is untouched: still the original single sector_t row.
    assert len(d1_rows) == 1
    assert d1_rows[0].as_of_date == _D1
    assert d1_rows[0].sector_id == sector_t.id
    # D2 snapshot now holds sector_t after the replace.
    assert len(d2_rows) == 1
    assert d2_rows[0].as_of_date == _D2
    assert d2_rows[0].sector_id == sector_t.id


async def test_sw10_latest_cutoff_selects_earlier_snapshot(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """ADR-0080 Test 4: list_latest_* with cutoff=D1 returns the D1 snapshot."""
    tenant_id = await seed_tenant()
    _actor, inv, sector_t, _sector_h = await _seed_two_snapshots(
        app_engine, tenant_id, email="sw10@example.com"
    )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = InvestmentSectorWeightsRepository(session)
        latest_for = await repo.list_latest_for_investment(inv.id, as_of_cutoff=_D1)
        latest_by = await repo.list_latest_by_investments([inv.id], as_of_cutoff=_D1)

    assert len(latest_for) == 1
    assert latest_for[0].as_of_date == _D1
    assert latest_for[0].sector_id == sector_t.id

    assert len(latest_by[inv.id]) == 1
    assert latest_by[inv.id][0].as_of_date == _D1
    assert latest_by[inv.id][0].sector_id == sector_t.id
