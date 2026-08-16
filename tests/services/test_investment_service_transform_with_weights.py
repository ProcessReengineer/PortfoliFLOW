# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""End-to-end tests for ``transform_upload_to_investments`` with weights.

Exercises the opt-in region / sector weight extraction path (Phase-6
region model, ADR-0046). Each test seeds a tenant + user + asset-
class catalogue + sector catalogue + region catalogue + a
``data_uploads`` row with region and sector split rows in the
Attributes sheet, then runs the transform with all four weight
repositories wired up.

Coverage:

* W-01 round-trip: region + sector splits land in their tables.
* W-02 re-import replaces weights atomically (replace-by-investment).
* W-03 soft-delete + reactivation preserves weights.
* W-04 auto-create — unknown sector labels are created from Excel values.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    DataUploadRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRegionWeightsRepository,
    InvestmentRepository,
    InvestmentSectorWeightsRepository,
    RegionRepository,
    SectorRepository,
    UserRepository,
    tenant_context,
)
from services.data_normalization import InvestmentExtractor
from services.investments import InvestmentService


def _attributes_df(
    investments: dict[str, dict[str, object]],
    *,
    region_rows: list[tuple[str, list[object]]] | None = None,
    sector_rows: list[tuple[str, list[object]]] | None = None,
) -> pd.DataFrame:
    """Build the Attributes DataFrame with scalar attrs first, then
    sector rows, then region rows.

    Mirrors the Excel import workbook layout consumed by
    :func:`services.reporting.attributes_partition.partition_attributes`:
    the first contiguous block of numeric breakdown rows is sectors,
    the second is regions (historically labelled "country" in the
    partition module — see ADR-0046).
    """
    columns = list(investments.keys())
    base_rows = [
        "Investment Type",
        "Investment Sub-Class",
        "Asset Class",
        "Manager / Fondsname",
        "Region",
        "Vintage Year",
        "Währung",
    ]
    data: list[list[object]] = []
    index: list[str] = []
    for row_label in base_rows:
        index.append(row_label)
        data.append([investments[col].get(row_label) for col in columns])
    for label, vals in sector_rows or []:
        index.append(label)
        data.append(list(vals))
    for label, vals in region_rows or []:
        index.append(label)
        data.append(list(vals))
    return pd.DataFrame(data, index=index, columns=columns)


def _timeseries_df(columns: list[str], rows: list[tuple[str, list[object]]]) -> pd.DataFrame:
    """Build a date-indexed time-series sheet (NAVs / cashflows)."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in rows], name="Date")
    return pd.DataFrame([vals for _, vals in rows], index=idx, columns=columns)


async def _seed_full(app_engine: AsyncEngine, tenant_id: UUID, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await AssetClassRepository(session).create(code="unclassified", display_name="Unclassified")
        await AssetClassRepository(session).create(
            code="private_equity", display_name="Private Equity"
        )
        sector_tech = await SectorRepository(session).create(
            code="tech_software",
            display_name="Technology — Software",
            created_by=actor.id,
        )
        sector_health = await SectorRepository(session).create(
            code="healthcare",
            display_name="Healthcare",
            created_by=actor.id,
        )
        region_repo = RegionRepository(session)
        region_dach = await region_repo.create(code="dach", display_name="DACH", sort_order=10)
        region_uk = await region_repo.create(
            code="uk_ireland", display_name="UK & Ireland", sort_order=20
        )
        region_usa = await region_repo.create(
            code="north_america_usa",
            display_name="North America — USA",
            sort_order=60,
        )
    return actor, sector_tech, sector_health, region_dach, region_uk, region_usa


async def _create_upload_with_weights(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    investments: dict[str, dict[str, object]],
    region_rows: list[tuple[str, list[object]]] | None,
    sector_rows: list[tuple[str, list[object]]] | None,
    file_hash: str,
    nav_actual: list[tuple[str, list[object]]] | None = None,
    nav_plan: list[tuple[str, list[object]]] | None = None,
):
    columns = list(investments.keys())
    sheets = {
        "attributes": _attributes_df(investments, region_rows=region_rows, sector_rows=sector_rows)
    }
    if nav_actual is not None:
        sheets["navs_actual"] = _timeseries_df(columns, nav_actual)
    if nav_plan is not None:
        sheets["navs_plan"] = _timeseries_df(columns, nav_plan)
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        upload = await DataUploadRepository(session).create_upload(
            uploaded_by=user_id,
            filename="weights.xlsx",
            file_hash=file_hash[:64].ljust(64, "0"),
            size_bytes=1024,
            format_version="v2",
            sheets=sheets,
        )
    return upload


async def _run_transform_full(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    upload_id: UUID,
):
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = InvestmentService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            cashflows=InvestmentCashflowRepository(session),
        )
        return await service.transform_upload_to_investments(
            upload_id,
            user_id=user_id,
            asset_class_repository=AssetClassRepository(session),
            data_upload_repository=DataUploadRepository(session),
            extractor=InvestmentExtractor(),
            region_repository=RegionRepository(session),
            sector_repository=SectorRepository(session),
            region_weights_repository=(InvestmentRegionWeightsRepository(session)),
            sector_weights_repository=(InvestmentSectorWeightsRepository(session)),
        )


# ---------------------------------------------------------------------------
# W-01: round-trip — region + sector splits land in their tables
# ---------------------------------------------------------------------------


async def test_w01_roundtrip_region_and_sector_splits(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    (
        actor,
        sector_tech,
        sector_health,
        region_dach,
        _region_uk,
        region_usa,
    ) = await _seed_full(app_engine, tenant_id, email="w01@example.com")

    investments = {
        "Fund Alpha": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        }
    }
    upload = await _create_upload_with_weights(
        app_engine,
        tenant_id,
        actor.id,
        investments=investments,
        sector_rows=[
            ("Technology — Software", [0.70]),
            ("Healthcare", [0.30]),
        ],
        region_rows=[
            ("DACH", [0.60]),
            ("North America — USA", [0.40]),
        ],
        # Two actual-NAV dates — the composition must anchor to the
        # *latest* of them (2024-12-31), per ADR-0080 §2.
        nav_actual=[
            ("2023-12-31", [90.0]),
            ("2024-12-31", [100.0]),
        ],
        file_hash="w01",
    )
    result = await _run_transform_full(app_engine, tenant_id, actor.id, upload.id)
    assert result.investments_created == 1
    assert result.region_weights_replaced == 2
    assert result.sector_weights_replaced == 2
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund Alpha")
        rws = await InvestmentRegionWeightsRepository(session).list_for_investment(inv.id)
        sws = await InvestmentSectorWeightsRepository(session).list_for_investment(inv.id)

    assert {w.region_id: w.weight_pct for w in rws} == {
        region_dach.id: Decimal("60.0000"),
        region_usa.id: Decimal("40.0000"),
    }
    assert {w.sector_id for w in sws} == {sector_tech.id, sector_health.id}
    # ADR-0080: every composition row of the import is a single snapshot
    # anchored to the latest actual-NAV date, basis='reported'.
    assert {w.as_of_date for w in rws} == {date(2024, 12, 31)}
    assert {w.as_of_date for w in sws} == {date(2024, 12, 31)}
    assert {w.basis for w in rws} == {"reported"}
    assert {w.basis for w in sws} == {"reported"}


# ---------------------------------------------------------------------------
# W-02: re-import replaces weights atomically
# ---------------------------------------------------------------------------


async def test_w02_reimport_replaces_weights(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    (
        actor,
        _sector_tech,
        sector_health,
        _region_dach,
        region_uk,
        _region_usa,
    ) = await _seed_full(app_engine, tenant_id, email="w02@example.com")

    investments = {
        "Fund Beta": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        }
    }
    upload_1 = await _create_upload_with_weights(
        app_engine,
        tenant_id,
        actor.id,
        investments=investments,
        sector_rows=[
            ("Technology — Software", [0.70]),
            ("Healthcare", [0.30]),
        ],
        region_rows=[
            ("DACH", [0.60]),
            ("North America — USA", [0.40]),
        ],
        nav_actual=[("2024-12-31", [100.0])],
        file_hash="w02-1",
    )
    await _run_transform_full(app_engine, tenant_id, actor.id, upload_1.id)

    upload_2 = await _create_upload_with_weights(
        app_engine,
        tenant_id,
        actor.id,
        investments=investments,
        sector_rows=[("Healthcare", [1.0])],
        region_rows=[("UK & Ireland", [1.0])],
        nav_actual=[("2024-12-31", [110.0])],
        file_hash="w02-2",
    )
    await _run_transform_full(app_engine, tenant_id, actor.id, upload_2.id)

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund Beta")
        rws = await InvestmentRegionWeightsRepository(session).list_for_investment(inv.id)
        sws = await InvestmentSectorWeightsRepository(session).list_for_investment(inv.id)

    assert {w.region_id for w in rws} == {region_uk.id}
    assert {w.sector_id for w in sws} == {sector_health.id}


# ---------------------------------------------------------------------------
# W-03: soft-delete then reactivation preserves weights
# ---------------------------------------------------------------------------


async def test_w03_soft_delete_then_reactivation_keeps_weights(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    (
        actor,
        sector_tech,
        _sector_health,
        region_dach,
        _region_uk,
        region_usa,
    ) = await _seed_full(app_engine, tenant_id, email="w03@example.com")

    investments = {
        "Fund Gamma": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        }
    }
    upload_1 = await _create_upload_with_weights(
        app_engine,
        tenant_id,
        actor.id,
        investments=investments,
        sector_rows=[("Technology — Software", [1.0])],
        region_rows=[("DACH", [1.0])],
        nav_actual=[("2024-12-31", [100.0])],
        file_hash="w03-1",
    )
    await _run_transform_full(app_engine, tenant_id, actor.id, upload_1.id)

    upload_2 = await _create_upload_with_weights(
        app_engine,
        tenant_id,
        actor.id,
        investments={
            "Fund Other": {
                "Investment Type": "private_equity",
                "Asset Class": "private_equity",
                "Währung": "EUR",
            }
        },
        region_rows=None,
        sector_rows=None,
        nav_actual=[("2024-12-31", [80.0])],
        file_hash="w03-2",
    )
    await _run_transform_full(app_engine, tenant_id, actor.id, upload_2.id)

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund Gamma")
        assert inv.is_active is False
        rws = await InvestmentRegionWeightsRepository(session).list_for_investment(inv.id)
        sws = await InvestmentSectorWeightsRepository(session).list_for_investment(inv.id)
    assert {w.region_id for w in rws} == {region_dach.id}
    assert {w.sector_id for w in sws} == {sector_tech.id}

    upload_3 = await _create_upload_with_weights(
        app_engine,
        tenant_id,
        actor.id,
        investments=investments,
        sector_rows=[("Technology — Software", [1.0])],
        region_rows=[("North America — USA", [1.0])],
        nav_actual=[("2024-12-31", [120.0])],
        file_hash="w03-3",
    )
    await _run_transform_full(app_engine, tenant_id, actor.id, upload_3.id)

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund Gamma")
        assert inv.is_active is True
        rws = await InvestmentRegionWeightsRepository(session).list_for_investment(inv.id)
    assert {w.region_id for w in rws} == {region_usa.id}


# ---------------------------------------------------------------------------
# W-04: auto-create — unknown sector labels are created from Excel values
# ---------------------------------------------------------------------------


async def test_w04_auto_creates_unknown_sector_from_excel(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Sector labels that are not yet in the tenant catalogue are
    auto-created with a normalised code and the Excel label as
    display name. Mirrors the asset-class auto-create path so the
    Excel sheet drives the sector vocabulary additively. Region
    labels are *not* auto-created — ADR-0046 mandates strict
    resolution against the bootstrap-seeded catalogue.
    """
    tenant_id = await seed_tenant()
    (
        actor,
        _sector_tech,
        _sector_health,
        region_dach,
        _region_uk,
        _region_usa,
    ) = await _seed_full(app_engine, tenant_id, email="w04@example.com")

    investments = {
        "Fund Delta": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        }
    }
    upload = await _create_upload_with_weights(
        app_engine,
        tenant_id,
        actor.id,
        investments=investments,
        sector_rows=[
            ("Industrials", [1.0]),
        ],
        region_rows=[("DACH", [1.0])],
        nav_actual=[("2024-12-31", [100.0])],
        file_hash="w04",
    )
    result = await _run_transform_full(app_engine, tenant_id, actor.id, upload.id)
    assert result.investments_created == 1
    assert result.sector_weights_replaced == 1
    assert result.region_weights_replaced == 1
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund Delta")
        sws = await InvestmentSectorWeightsRepository(session).list_for_investment(inv.id)
        rws = await InvestmentRegionWeightsRepository(session).list_for_investment(inv.id)
        new_sector = await SectorRepository(session).get_by_code("industrials")

    assert new_sector is not None
    assert new_sector.display_name == "Industrials"
    assert {w.sector_id for w in sws} == {new_sector.id}
    assert {w.region_id for w in rws} == {region_dach.id}


# ---------------------------------------------------------------------------
# W-05: unknown region label raises a hard import error and drops the row
# ---------------------------------------------------------------------------


async def test_w05_unknown_region_label_is_hard_error(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    (
        actor,
        _sector_tech,
        _sector_health,
        region_dach,
        _region_uk,
        _region_usa,
    ) = await _seed_full(app_engine, tenant_id, email="w05@example.com")
    investments = {
        "Fund Epsilon": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        }
    }
    upload = await _create_upload_with_weights(
        app_engine,
        tenant_id,
        actor.id,
        investments=investments,
        sector_rows=[("Technology — Software", [1.0])],
        region_rows=[
            ("DACH", [0.7]),
            ("Atlantis", [0.3]),
        ],
        nav_actual=[("2024-12-31", [100.0])],
        file_hash="w05",
    )
    result = await _run_transform_full(app_engine, tenant_id, actor.id, upload.id)
    # The DACH row succeeds; the Atlantis row is dropped with an error.
    assert result.region_weights_replaced == 1
    assert len(result.errors) == 1
    assert "atlantis" in result.errors[0].message.lower()

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund Epsilon")
        rws = await InvestmentRegionWeightsRepository(session).list_for_investment(inv.id)
    assert {w.region_id for w in rws} == {region_dach.id}


# ---------------------------------------------------------------------------
# W-06: an investment with no *actual* NAV is skipped + warned (ADR-0080 §2)
# ---------------------------------------------------------------------------


async def test_w06_no_actual_nav_skips_composition_with_warning(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """ADR-0080 Test 6: composition needs an actual-NAV date to anchor.

    The investment carries a *plan* NAV only — there is no actual NAV
    to honestly anchor the snapshot's ``as_of_date`` on. The import
    must skip its composition (write zero region/sector rows), surface
    an :class:`ExtractionWarning`, and not abort.
    """
    tenant_id = await seed_tenant()
    (
        actor,
        _sector_tech,
        _sector_health,
        _region_dach,
        _region_uk,
        _region_usa,
    ) = await _seed_full(app_engine, tenant_id, email="w06@example.com")
    investments = {
        "Fund NoNav": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        }
    }
    upload = await _create_upload_with_weights(
        app_engine,
        tenant_id,
        actor.id,
        investments=investments,
        sector_rows=[("Technology — Software", [1.0])],
        region_rows=[("DACH", [1.0])],
        # Plan NAV only — proves the anchor considers *actual* NAVs.
        nav_plan=[("2024-12-31", [100.0])],
        file_hash="w06",
    )
    result = await _run_transform_full(app_engine, tenant_id, actor.id, upload.id)

    assert result.investments_created == 1
    assert result.region_weights_replaced == 0
    assert result.sector_weights_replaced == 0
    assert result.errors == ()
    composition_warnings = [
        w
        for w in result.warnings
        if w.investment_name == "Fund NoNav" and "composition skipped" in w.message
    ]
    assert len(composition_warnings) == 1

    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund NoNav")
        rws = await InvestmentRegionWeightsRepository(session).list_for_investment(inv.id)
        sws = await InvestmentSectorWeightsRepository(session).list_for_investment(inv.id)
    assert rws == []
    assert sws == []


# ---------------------------------------------------------------------------
# W-07: the Excel transform stamps ingest_origin='excel' (ADR-0092)
# ---------------------------------------------------------------------------


async def test_w07_excel_transform_writes_ingest_origin_excel(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Every row the Excel producer writes carries ingest_origin='excel'.

    Covers three of the seven ingested families in one pass — NAV, region
    weights, sector weights — so a later live fetch can recognise them as
    book-of-record and never overwrite them (ADR-0092).
    """
    tenant_id = await seed_tenant()
    (actor, *_rest) = await _seed_full(app_engine, tenant_id, email="w07@example.com")
    investments = {
        "Fund Origin": {
            "Investment Type": "private_equity",
            "Asset Class": "private_equity",
            "Währung": "EUR",
        }
    }
    upload = await _create_upload_with_weights(
        app_engine,
        tenant_id,
        actor.id,
        investments=investments,
        sector_rows=[("Technology — Software", [1.0])],
        region_rows=[("DACH", [1.0])],
        nav_actual=[("2024-12-31", [100.0])],
        file_hash="w07",
    )
    result = await _run_transform_full(app_engine, tenant_id, actor.id, upload.id)
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        for table in (
            "investment_navs",
            "investment_region_weights",
            "investment_sector_weights",
        ):
            origins = (
                (await session.execute(text(f"SELECT DISTINCT ingest_origin FROM {table}")))
                .scalars()
                .all()
            )
            assert origins == ["excel"], f"{table}: {origins}"
