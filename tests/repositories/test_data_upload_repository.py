# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""DataUploadRepository tests against the live compose Postgres.

Mirrors the structure of ``test_user_repository.py``. Each test runs as
the unprivileged ``portfoliflow_app`` role so RLS evaluates exactly as
it will in production. Tenant creation goes through the ``seed_tenant``
superuser fixture.

Coverage:

* Round-trip a dict of DataFrames through ``create_upload`` →
  ``data_uploads`` row + one ``data_upload_sheets`` row per sheet.
* ``get_by_hash`` returns the upload for a known hash.
* ``list_recent`` orders by ``created_at`` descending.
* RLS isolation between two tenants for the upload tables.
* The audit-log captures the insert with the correct tenant / actor.
"""

from __future__ import annotations

import asyncio

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    DataUploadRepository,
    UserRepository,
    tenant_context,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attributes_df() -> pd.DataFrame:
    return pd.DataFrame(
        [["Aktien", "Private Equity"], ["Large Cap", "Buyout"]],
        index=["Investment Type", "Investment Sub-Class"],
        columns=["Investition A", "Investition B"],
    )


def _navs_df() -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=3, freq="D", name="Date")
    return pd.DataFrame(
        {
            "Investition A": [1_000_000.0, 1_010_000.0, 1_020_100.0],
            "Investition B": [2_000_000.0, 2_020_000.0, 2_040_400.0],
        },
        index=idx,
    )


def _interest_rates_df() -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=3, freq="D", name="Date")
    return pd.DataFrame(
        {"risk free rate": [0.04, 0.04, 0.041]},
        index=idx,
    )


def _sample_sheets() -> dict[str, pd.DataFrame]:
    return {
        "attributes": _attributes_df(),
        "navs_actual": _navs_df(),
        "interest_rates": _interest_rates_df(),
    }


# ---------------------------------------------------------------------------
# D-01: round-trip create + read
# ---------------------------------------------------------------------------


async def test_d01_create_upload_round_trip(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()

    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="uploader@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = DataUploadRepository(session)
        upload = await repo.create_upload(
            uploaded_by=actor.id,
            filename="test.xlsx",
            file_hash="hash-d01-roundtrip",
            size_bytes=1234,
            format_version="v2",
            sheets=_sample_sheets(),
        )

    assert upload.tenant_id == tenant_id
    assert upload.uploaded_by == actor.id
    assert upload.filename == "test.xlsx"
    assert upload.file_hash == "hash-d01-roundtrip"
    assert upload.size_bytes == 1234
    assert upload.format_version == "v2"

    async with tenant_context(app_engine, tenant_id) as session:
        repo = DataUploadRepository(session)
        sheets = await repo.get_sheets(upload.id)
    assert {s.sheet_name for s in sheets} == {
        "attributes",
        "navs_actual",
        "interest_rates",
    }
    by_name = {s.sheet_name: s for s in sheets}

    # NAVs sheet has 3 rows × 2 columns.
    assert by_name["navs_actual"].row_count == 3
    assert by_name["navs_actual"].column_count == 2
    # JSONB payload preserves the to_dict('split') shape.
    payload = by_name["navs_actual"].data
    assert set(payload.keys()) == {"index", "columns", "data"}
    assert payload["columns"] == ["Investition A", "Investition B"]
    assert len(payload["data"]) == 3


# ---------------------------------------------------------------------------
# D-02: get_by_hash returns the matching upload
# ---------------------------------------------------------------------------


async def test_d02_get_by_hash_returns_upload(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="hashlookup@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await DataUploadRepository(session).create_upload(
            uploaded_by=actor.id,
            filename="alpha.xlsx",
            file_hash="hash-d02-alpha",
            size_bytes=42,
            format_version="v2",
            sheets={"attributes": _attributes_df()},
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = DataUploadRepository(session)
        match = await repo.get_by_hash("hash-d02-alpha")
        miss = await repo.get_by_hash("hash-d02-missing")

    assert match is not None
    assert match.filename == "alpha.xlsx"
    assert miss is None


# ---------------------------------------------------------------------------
# D-03: list_recent orders newest first
# ---------------------------------------------------------------------------


async def test_d03_list_recent_orders_newest_first(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="lister@example.com", password_hash="x" * 8
        )

    # Insert three uploads with deliberate ordering.
    for i in range(3):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await DataUploadRepository(session).create_upload(
                uploaded_by=actor.id,
                filename=f"file-{i}.xlsx",
                file_hash=f"hash-d03-{i}",
                size_bytes=10 * (i + 1),
                format_version="v2",
                sheets={"attributes": _attributes_df()},
            )
        # Sleep a small amount so the created_at values are distinct
        # at the millisecond grain, which protects the ORDER BY from
        # ties on fast hosts.
        await asyncio.sleep(0.01)

    async with tenant_context(app_engine, tenant_id) as session:
        recent = await DataUploadRepository(session).list_recent()

    assert [u.filename for u in recent] == [
        "file-2.xlsx",
        "file-1.xlsx",
        "file-0.xlsx",
    ]


# ---------------------------------------------------------------------------
# D-04: RLS isolates uploads between tenants
# ---------------------------------------------------------------------------


async def test_d04_uploads_are_rls_isolated(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="Tenant A")
    tenant_b = await seed_tenant(name="Tenant B")

    async with tenant_context(app_engine, tenant_a) as session:
        actor_a = await UserRepository(session).create(email="a@example.com", password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_b) as session:
        actor_b = await UserRepository(session).create(email="b@example.com", password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
        await DataUploadRepository(session).create_upload(
            uploaded_by=actor_a.id,
            filename="a.xlsx",
            file_hash="hash-d04-a",
            size_bytes=10,
            format_version="v2",
            sheets={"attributes": _attributes_df()},
        )
    async with tenant_context(app_engine, tenant_b, user_id=actor_b.id) as session:
        await DataUploadRepository(session).create_upload(
            uploaded_by=actor_b.id,
            filename="b.xlsx",
            file_hash="hash-d04-b",
            size_bytes=20,
            format_version="v2",
            sheets={"attributes": _attributes_df()},
        )

    # Tenant A sees only its own upload.
    async with tenant_context(app_engine, tenant_a) as session:
        a_recent = await DataUploadRepository(session).list_recent()
        a_can_see_b = await DataUploadRepository(session).get_by_hash("hash-d04-b")
    assert [u.filename for u in a_recent] == ["a.xlsx"]
    assert a_can_see_b is None

    # Tenant B sees only its own upload.
    async with tenant_context(app_engine, tenant_b) as session:
        b_recent = await DataUploadRepository(session).list_recent()
        b_can_see_a = await DataUploadRepository(session).get_by_hash("hash-d04-a")
    assert [u.filename for u in b_recent] == ["b.xlsx"]
    assert b_can_see_a is None


# ---------------------------------------------------------------------------
# D-05: audit_log row is created on INSERT with the correct actor
# ---------------------------------------------------------------------------


async def test_d05_audit_log_records_upload_insert(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="audited-uploader@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        upload = await DataUploadRepository(session).create_upload(
            uploaded_by=actor.id,
            filename="audited.xlsx",
            file_hash="hash-d05",
            size_bytes=99,
            format_version="v2",
            sheets={"attributes": _attributes_df()},
        )

    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(
            text(
                """
                SELECT tenant_id, user_id, table_name, operation,
                       record_id
                FROM audit_log
                WHERE table_name = 'data_uploads'
                  AND record_id  = :rid
                """
            ),
            {"rid": str(upload.id)},
        )
        row = result.mappings().one()

    assert row["tenant_id"] == tenant_id
    assert row["user_id"] == actor.id
    assert row["operation"] == "INSERT"
    assert row["record_id"] == upload.id


# ---------------------------------------------------------------------------
# D-06: get_sheet returns one specific sheet
# ---------------------------------------------------------------------------


async def test_d06_get_sheet_returns_named_sheet(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="sheet-getter@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        upload = await DataUploadRepository(session).create_upload(
            uploaded_by=actor.id,
            filename="sheets.xlsx",
            file_hash="hash-d06",
            size_bytes=10,
            format_version="v2",
            sheets=_sample_sheets(),
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = DataUploadRepository(session)
        navs = await repo.get_sheet(upload.id, "navs_actual")
        missing = await repo.get_sheet(upload.id, "does_not_exist")

    assert navs is not None
    assert navs.row_count == 3
    assert navs.column_count == 2
    assert missing is None
