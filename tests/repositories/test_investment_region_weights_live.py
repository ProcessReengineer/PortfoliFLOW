# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Excel-precedence guard for the region-weight live write path (ADR-0092).

The region weights repository is the representative composition-weight
family carrying the row-level ``upsert_live`` guard (the four sibling
weight families share the identical natural-key shape and gain the seam
when a bucketed-weight provider DTO lands — services/market_data is
frozen this slice). These tests prove, on the historised natural key
``(investment_id, as_of_date, region_id)``:

* an ``'excel'`` snapshot row is left **byte-identical** by a live write
  (``upsert_live`` returns ``None``);
* a ``'manual'`` row is equally immune;
* a live write inserts where absent and refreshes its own prior ``'live'``
  row in place.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

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

_DAY = date(2024, 12, 31)


async def _seed(app_engine: AsyncEngine, tenant_id, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="listed_equity", display_name="Listed Equity"
        )
        inv = await InvestmentRepository(session).create(
            name="Region Fund",
            investment_type="listed_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        region = await RegionRepository(session).create(
            code="dach", display_name="DACH", sort_order=10
        )
    return actor, inv, region


async def test_live_region_weight_skips_excel_snapshot(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv, region = await _seed(app_engine, tenant_id, email="rw-live-excel@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRegionWeightsRepository(session)
        # Excel snapshot (default ingest_origin='excel').
        await repo.replace_snapshot_for_investment(
            inv.id,
            _DAY,
            [RegionWeightInput(region_id=region.id, weight_pct=Decimal("60"))],
            basis="reported",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        before = await InvestmentRegionWeightsRepository(session).list_for_investment(inv.id)
    assert len(before) == 1
    original = before[0]
    assert original.ingest_origin == "excel"

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        result = await InvestmentRegionWeightsRepository(session).upsert_live(
            inv.id,
            _DAY,
            region.id,
            weight_pct=Decimal("40"),
            basis="reported",
            created_by=actor.id,
        )
    # Skip: the conditional upsert left the excel row untouched.
    assert result is None

    async with tenant_context(app_engine, tenant_id) as session:
        after = await InvestmentRegionWeightsRepository(session).list_for_investment(inv.id)
    assert len(after) == 1
    assert after[0].id == original.id
    assert after[0].weight_pct == Decimal("60.0000")
    assert after[0].ingest_origin == "excel"
    assert after[0].created_at == original.created_at


async def test_live_region_weight_skips_manual_snapshot(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv, region = await _seed(app_engine, tenant_id, email="rw-live-manual@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentRegionWeightsRepository(session).replace_snapshot_for_investment(
            inv.id,
            _DAY,
            [RegionWeightInput(region_id=region.id, weight_pct=Decimal("60"))],
            basis="reported",
            created_by=actor.id,
            ingest_origin="manual",
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        result = await InvestmentRegionWeightsRepository(session).upsert_live(
            inv.id,
            _DAY,
            region.id,
            weight_pct=Decimal("40"),
            basis="reported",
            created_by=actor.id,
        )
    assert result is None

    async with tenant_context(app_engine, tenant_id) as session:
        after = await InvestmentRegionWeightsRepository(session).list_for_investment(inv.id)
    assert len(after) == 1
    assert after[0].weight_pct == Decimal("60.0000")
    assert after[0].ingest_origin == "manual"


async def test_live_region_weight_inserts_then_self_updates(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv, region = await _seed(app_engine, tenant_id, email="rw-live-self@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = InvestmentRegionWeightsRepository(session)
        inserted = await repo.upsert_live(
            inv.id,
            _DAY,
            region.id,
            weight_pct=Decimal("55"),
            basis="reported",
            created_by=actor.id,
        )
    assert inserted is not None
    assert inserted.ingest_origin == "live"
    assert inserted.weight_pct == Decimal("55.0000")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        updated = await InvestmentRegionWeightsRepository(session).upsert_live(
            inv.id,
            _DAY,
            region.id,
            weight_pct=Decimal("70"),
            basis="reported",
            created_by=actor.id,
        )
    assert updated is not None
    assert updated.id == inserted.id
    assert updated.weight_pct == Decimal("70.0000")
    assert updated.ingest_origin == "live"

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentRegionWeightsRepository(session).list_for_investment(inv.id)
    assert len(rows) == 1
