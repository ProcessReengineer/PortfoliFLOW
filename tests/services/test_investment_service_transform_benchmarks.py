# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Integration tests for ``InvestmentService.transform_benchmarks_from_upload`` (ADR-0061).

Live-DB tests against the compose Postgres. Each test seeds a
tenant + user + asset-class catalogue, builds a Phase-2 upload row
with the two Phase-7 benchmark sheets, and asserts on the DB state
after the service transforms the snapshot.

Coverage:

* IT-BM-01 happy path: benchmarks, observations, mappings persist.
* IT-BM-02 idempotent re-import: counts unchanged on second run.
* IT-BM-03 re-import with changed observations replaces the previous
  generation.
* IT-BM-04 re-import with changed mappings replaces the previous
  generation.
* IT-BM-05 unknown asset-class code raises ``ValidationError``.
"""

from __future__ import annotations

import hashlib
from datetime import date
from uuid import UUID

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import ValidationError
from core.repositories import (
    AssetClassBenchmarkMappingRepository,
    AssetClassRepository,
    BenchmarkObservationRepository,
    BenchmarkRepository,
    DataUploadRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from services.investments import InvestmentService


# ---------------------------------------------------------------------------
# Helpers — DataFrame builders that round-trip through the documented JSONB
# ---------------------------------------------------------------------------


def _benchmarks_actual_df(
    benchmark_codes: list[str],
    rows: list[tuple[str, list[object]]],
) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in rows], name="Date")
    return pd.DataFrame(
        [vals for _, vals in rows],
        index=idx,
        columns=benchmark_codes,
    )


def _benchmark_mapping_df(
    mapping_rows: list[tuple[object, object, object, object]],
) -> pd.DataFrame:
    return pd.DataFrame(
        mapping_rows,
        columns=["asset_class", "benchmark_id", "weight", "comment"],
    )


def _build_service(session) -> InvestmentService:
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
    )


async def _seed_actor(app_engine: AsyncEngine, tenant_id: UUID, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        return await UserRepository(session).create(email=email, password_hash="x" * 8)


async def _seed_asset_classes(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    codes: list[str],
):
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = AssetClassRepository(session)
        for code in codes:
            await repo.create(code=code, display_name=code.title())


async def _create_upload(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    sheets: dict[str, pd.DataFrame],
    filename: str = "benchmarks.xlsx",
    file_hash: str | None = None,
):
    if file_hash is None:
        file_hash = hashlib.sha256(filename.encode()).hexdigest()
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        return await DataUploadRepository(session).create_upload(
            uploaded_by=user_id,
            filename=filename,
            file_hash=file_hash,
            size_bytes=512,
            format_version="v2",
            sheets=sheets,
        )


async def _run_transform(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    upload_id: UUID,
):
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = _build_service(session)
        return await service.transform_benchmarks_from_upload(
            upload_id,
            user_id=user_id,
            data_upload_repository=DataUploadRepository(session),
            asset_class_repository=AssetClassRepository(session),
            benchmark_repository=BenchmarkRepository(session),
            benchmark_observation_repository=(BenchmarkObservationRepository(session)),
            mapping_repository=(AssetClassBenchmarkMappingRepository(session)),
        )


# ---------------------------------------------------------------------------
# IT-BM-01: round-trip — benchmarks, observations, and mappings persist
# ---------------------------------------------------------------------------


async def test_itbm01_roundtrip_persists_all_three_layers(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, "itbm01@example.com")
    await _seed_asset_classes(app_engine, tenant_id, actor.id, ["equities", "bonds"])

    sheets = {
        "benchmarks_actual": _benchmarks_actual_df(
            ["BM_EQ", "BM_BOND"],
            [
                ("2026-01-01", [0.001, -0.0005]),
                ("2026-01-02", [0.002, 0.0007]),
            ],
        ),
        "benchmark_mapping": _benchmark_mapping_df(
            [
                ("equities", "BM_EQ", 1.0, ""),
                ("bonds", "BM_BOND", 1.0, ""),
            ]
        ),
    }
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets,
    )

    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    assert result.n_benchmarks == 2
    assert result.n_observations == 4
    assert result.n_mappings == 2

    # Verify DB rows are visible to the active tenant context.
    async with tenant_context(app_engine, tenant_id) as session:
        bms = await BenchmarkRepository(session).list_all()
        mappings = await AssetClassBenchmarkMappingRepository(session).list_all()
        eq_bm = next(b for b in bms if b.code == "BM_EQ")
        obs = await BenchmarkObservationRepository(session).list_for_benchmark(eq_bm.id)
    assert {b.code for b in bms} == {"BM_EQ", "BM_BOND"}
    assert len(mappings) == 2
    assert [o.as_of_date for o in obs] == [date(2026, 1, 1), date(2026, 1, 2)]


# ---------------------------------------------------------------------------
# IT-BM-02: re-import with identical workbook is idempotent
# ---------------------------------------------------------------------------


async def test_itbm02_reimport_is_idempotent(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, "itbm02@example.com")
    await _seed_asset_classes(app_engine, tenant_id, actor.id, ["equities"])
    sheets = {
        "benchmarks_actual": _benchmarks_actual_df(["BM_EQ"], [("2026-01-01", [0.001])]),
        "benchmark_mapping": _benchmark_mapping_df([("equities", "BM_EQ", 1.0, "")]),
    }

    upload_a = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets,
        filename="a.xlsx",
    )
    first = await _run_transform(app_engine, tenant_id, actor.id, upload_a.id)

    upload_b = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets,
        filename="b.xlsx",
    )
    second = await _run_transform(app_engine, tenant_id, actor.id, upload_b.id)

    assert first.n_benchmarks == second.n_benchmarks == 1
    assert first.n_observations == second.n_observations == 1
    assert first.n_mappings == second.n_mappings == 1

    async with tenant_context(app_engine, tenant_id) as session:
        bms = await BenchmarkRepository(session).list_all()
        mappings = await AssetClassBenchmarkMappingRepository(session).list_all()
    # Idempotent: still exactly one row in each table.
    assert len(bms) == 1
    assert len(mappings) == 1


# ---------------------------------------------------------------------------
# IT-BM-03: re-import with changed observations replaces the previous gen
# ---------------------------------------------------------------------------


async def test_itbm03_reimport_replaces_observations(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, "itbm03@example.com")
    await _seed_asset_classes(app_engine, tenant_id, actor.id, ["equities"])

    sheets_v1 = {
        "benchmarks_actual": _benchmarks_actual_df(
            ["BM_EQ"],
            [
                ("2026-01-01", [0.001]),
                ("2026-01-02", [0.002]),
            ],
        ),
        "benchmark_mapping": _benchmark_mapping_df([("equities", "BM_EQ", 1.0, "")]),
    }
    upload_v1 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets_v1,
        filename="v1.xlsx",
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_v1.id)

    sheets_v2 = {
        "benchmarks_actual": _benchmarks_actual_df(
            ["BM_EQ"],
            [
                ("2026-02-01", [0.005]),
            ],
        ),
        "benchmark_mapping": _benchmark_mapping_df([("equities", "BM_EQ", 1.0, "")]),
    }
    upload_v2 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets_v2,
        filename="v2.xlsx",
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_v2.id)

    async with tenant_context(app_engine, tenant_id) as session:
        bm = await BenchmarkRepository(session).get_by_code("BM_EQ")
        assert bm is not None
        obs = await BenchmarkObservationRepository(session).list_for_benchmark(bm.id)
    assert [o.as_of_date for o in obs] == [date(2026, 2, 1)]


# ---------------------------------------------------------------------------
# IT-BM-04: re-import with different mapping replaces it for the asset class
# ---------------------------------------------------------------------------


async def test_itbm04_reimport_replaces_mapping(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, "itbm04@example.com")
    await _seed_asset_classes(app_engine, tenant_id, actor.id, ["equities"])

    sheets_v1 = {
        "benchmarks_actual": _benchmarks_actual_df(
            ["BM_EQ_A", "BM_EQ_B"],
            [
                ("2026-01-01", [0.001, 0.002]),
            ],
        ),
        "benchmark_mapping": _benchmark_mapping_df([("equities", "BM_EQ_A", 1.0, "")]),
    }
    upload_v1 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets_v1,
        filename="v1.xlsx",
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_v1.id)

    # Repeat the import but switch the mapping to BM_EQ_B.
    sheets_v2 = {
        "benchmarks_actual": _benchmarks_actual_df(
            ["BM_EQ_A", "BM_EQ_B"],
            [
                ("2026-01-01", [0.001, 0.002]),
            ],
        ),
        "benchmark_mapping": _benchmark_mapping_df([("equities", "BM_EQ_B", 1.0, "")]),
    }
    upload_v2 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets_v2,
        filename="v2.xlsx",
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_v2.id)

    async with tenant_context(app_engine, tenant_id) as session:
        ac = await AssetClassRepository(session).get_by_code("equities")
        assert ac is not None
        mappings = await AssetClassBenchmarkMappingRepository(session).list_for_asset_class(ac.id)
        bm_b = await BenchmarkRepository(session).get_by_code("BM_EQ_B")
        assert bm_b is not None
    assert len(mappings) == 1
    assert mappings[0].benchmark_id == bm_b.id


# ---------------------------------------------------------------------------
# IT-BM-05: unknown asset-class code → ValidationError
# ---------------------------------------------------------------------------


async def test_itbm05_unknown_asset_class_raises(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, "itbm05@example.com")
    # Only "bonds" is seeded; the mapping references "equities" which is
    # absent — that must trigger an actionable ValidationError.
    await _seed_asset_classes(app_engine, tenant_id, actor.id, ["bonds"])
    sheets = {
        "benchmarks_actual": _benchmarks_actual_df(["BM_EQ"], [("2026-01-01", [0.001])]),
        "benchmark_mapping": _benchmark_mapping_df([("equities", "BM_EQ", 1.0, "")]),
    }
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets,
    )
    with pytest.raises(ValidationError) as exc_info:
        await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    msg = str(exc_info.value)
    assert "equities" in msg
    assert "Back Office" in msg or "asset class" in msg.lower()
