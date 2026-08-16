# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression: the system tenant holds no tenant-data rows.

Per ADR-0063 §3 the system tenant
(``00000000-0000-0000-0000-000000000000``) exists to host
super-admin user accounts and nothing else. This regression test
asserts that every tenant-data table contains zero rows scoped to
the system tenant.

The check runs as superuser to bypass RLS for the assertion
itself — a tenant-scoped session would see only its own tenant
and would not detect a leak.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.tenant_constants import SYSTEM_TENANT_ID

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


@pytest_asyncio.fixture
async def superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Live superuser engine, defined locally per the regression-test
    convention (siblings define it the same way) so the package does not
    need a conftest whose autouse ``reset_schema`` would force the
    DB-free static-analysis regression tests to require Postgres.
    """
    if not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL_SUPERUSER not set; cannot run system-tenant guard.",
            allow_module_level=False,
        )
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(
            f"Cannot reach Postgres at {DATABASE_URL_SUPERUSER!r}: {exc}.",
            allow_module_level=False,
        )
    try:
        yield engine
    finally:
        await engine.dispose()


# Each table here has a ``tenant_id`` column. Any row scoped to the
# system tenant indicates a leak.
_TENANT_DATA_TABLES: tuple[str, ...] = (
    "investments",
    "investment_navs",
    "investment_cashflows",
    "investment_country_weights",
    "investment_region_weights",
    "investment_sector_weights",
    "saa_configurations",
    "saa_asset_class_inputs",
    "saa_correlations",
    "asset_classes",
    "sectors",
    "regions",
    "benchmarks",
    "benchmark_observations",
    "asset_class_benchmark_mapping",
    "limits",
    "limit_sets",
    "data_uploads",
)


async def test_system_tenant_holds_no_tenant_data(
    superuser_engine: AsyncEngine,
) -> None:
    """No row in any tenant-data table may reference SYSTEM_TENANT_ID."""
    # Seed the system tenant so the check has a target to query.
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, :subdomain) "
                "ON CONFLICT (id) DO UPDATE "
                "SET subdomain = EXCLUDED.subdomain"
            ),
            {
                "id": str(SYSTEM_TENANT_ID),
                "name": "Platform Administration",
                "subdomain": "admin",
            },
        )

    async with superuser_engine.connect() as conn:
        for table in _TENANT_DATA_TABLES:
            result = await conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = :tid"),
                {"tid": str(SYSTEM_TENANT_ID)},
            )
            count = int(result.scalar_one())
            assert count == 0, (
                f"Tenant-data leak: {table!r} holds {count} rows "
                f"scoped to SYSTEM_TENANT_ID. The system tenant must "
                "only host super-admin users per ADR-0063 §3."
            )
