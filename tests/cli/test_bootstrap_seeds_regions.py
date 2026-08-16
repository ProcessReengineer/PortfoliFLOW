# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""End-to-end test for the Phase-6 region-bootstrap step (ADR-0046).

Verifies that ``portfoliflow bootstrap`` installs the canonical
region catalogue and country memberships idempotently. Runs against
the live compose Postgres because the bootstrap path uses
:func:`tenant_context` which needs a real engine.

Coverage:

* BSR-01: First ``bootstrap`` installs all twelve default regions
  for the sentinel tenant, with the expected total number of
  country memberships.
* BSR-02: A second ``bootstrap`` run is a no-op.
* BSR-03: Per-tenant isolation — installing into one tenant does not
  affect another.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from cli import app
from cli.bootstrap import install_default_regions
from core.repositories import RegionRepository, tenant_context
from core.repositories.country_repository import CountryRepository
from core.tenant_constants import SENTINEL_TENANT_ID

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

runner = CliRunner()

_EXPECTED_REGION_COUNT = 12

_TRUNCATE_SQL = (
    "TRUNCATE TABLE investment_region_weights, "
    "region_country_memberships, regions, "
    "investment_country_weights, "
    "investment_sector_weights, sectors, "
    "investment_cashflows, investment_navs, investments, "
    "saa_correlations, saa_asset_class_inputs, "
    "saa_configurations, asset_classes, "
    "data_upload_sheets, data_uploads, "
    "login_audit, sessions, audit_log, "
    "data_store_entries, users, tenants "
    "RESTART IDENTITY CASCADE"
)


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "cannot run bootstrap-regions tests.",
            allow_module_level=False,
        )


async def _truncate_async() -> None:
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_TRUNCATE_SQL))
    finally:
        await engine.dispose()


def _truncate() -> None:
    asyncio.run(_truncate_async())


@pytest.fixture
def clean_db() -> Iterator[None]:
    _require_db()
    _truncate()
    try:
        yield
    finally:
        _truncate()


async def _count_regions_async(tenant_id: UUID) -> int:
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            result = await conn.execute(
                text("SELECT COUNT(*) FROM regions WHERE tenant_id = :tid"),
                {"tid": str(tenant_id)},
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


async def _count_memberships_async(tenant_id: UUID) -> int:
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            result = await conn.execute(
                text("SELECT COUNT(*) FROM region_country_memberships WHERE tenant_id = :tid"),
                {"tid": str(tenant_id)},
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


def _count_regions(tenant_id: UUID = SENTINEL_TENANT_ID) -> int:
    return asyncio.run(_count_regions_async(tenant_id))


def _count_memberships(tenant_id: UUID = SENTINEL_TENANT_ID) -> int:
    return asyncio.run(_count_memberships_async(tenant_id))


# ---------------------------------------------------------------------------
# BSR-01: first bootstrap installs the region catalogue
# ---------------------------------------------------------------------------


def test_bsr01_first_bootstrap_installs_regions(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "test-only-password")

    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code == 0, result.output

    assert _count_regions() == _EXPECTED_REGION_COUNT
    # A non-zero membership count proves the (region, country)
    # attachment loop ran successfully.
    assert _count_memberships() > 100


# ---------------------------------------------------------------------------
# BSR-02: second bootstrap is a no-op
# ---------------------------------------------------------------------------


def test_bsr02_second_bootstrap_is_idempotent(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "test-only-password")

    first = runner.invoke(app, ["bootstrap"])
    assert first.exit_code == 0, first.output
    first_regions = _count_regions()
    first_memberships = _count_memberships()

    second = runner.invoke(app, ["bootstrap"])
    assert second.exit_code == 0, second.output

    assert _count_regions() == first_regions
    assert _count_memberships() == first_memberships


# ---------------------------------------------------------------------------
# BSR-03: per-tenant isolation via install_default_regions directly
# ---------------------------------------------------------------------------


async def _seed_tenant_async(tenant_id: UUID) -> None:
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, gen_random_uuid()::text)"
                ),
                {"id": str(tenant_id), "name": f"Tenant {tenant_id}"},
            )
    finally:
        await engine.dispose()


async def _install_for_tenant_async(tenant_id: UUID) -> None:
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, tenant_id) as session:
            await install_default_regions(
                RegionRepository(session),
                CountryRepository(session),
            )
    finally:
        await engine.dispose()


def test_bsr03_per_tenant_isolation(clean_db: None) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()

    asyncio.run(_seed_tenant_async(tenant_a))
    asyncio.run(_seed_tenant_async(tenant_b))

    asyncio.run(_install_for_tenant_async(tenant_a))
    asyncio.run(_install_for_tenant_async(tenant_b))
    # Re-running for tenant A must remain a no-op.
    asyncio.run(_install_for_tenant_async(tenant_a))

    assert _count_regions(tenant_a) == _EXPECTED_REGION_COUNT
    assert _count_regions(tenant_b) == _EXPECTED_REGION_COUNT
