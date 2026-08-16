# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""End-to-end test for the Phase-5a sector-bootstrap step.

Verifies that ``portfoliflow bootstrap`` installs the per-tenant
``unclassified`` sector idempotently. The test runs against the
live compose Postgres because the bootstrap path uses
:func:`tenant_context` which needs a real engine.

Coverage:

* BSS-01: First ``bootstrap`` run installs the ``unclassified``
  sector for the sentinel tenant.
* BSS-02: A second ``bootstrap`` does not duplicate the row
  (idempotent).
* BSS-03: Each tenant has its own ``unclassified`` row (cross-tenant
  isolation).
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
from cli.bootstrap import install_unclassified_sector
from core.repositories import (
    SectorRepository,
    UserRepository,
    tenant_context,
)
from core.tenant_constants import SENTINEL_TENANT_ID

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

runner = CliRunner()

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
            "cannot run bootstrap-sector tests.",
            allow_module_level=False,
        )


async def _truncate_async() -> None:
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_TRUNCATE_SQL))
    finally:
        await engine.dispose()


async def _count_unclassified_sectors_async() -> int:
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(SENTINEL_TENANT_ID)},
            )
            result = await conn.execute(
                text(
                    "SELECT COUNT(*) FROM sectors WHERE tenant_id = :tid AND code = 'unclassified'"
                ),
                {"tid": str(SENTINEL_TENANT_ID)},
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


def _truncate() -> None:
    asyncio.run(_truncate_async())


def _count_unclassified_sectors() -> int:
    return asyncio.run(_count_unclassified_sectors_async())


@pytest.fixture
def clean_db() -> Iterator[None]:
    _require_db()
    _truncate()
    try:
        yield
    finally:
        _truncate()


# ---------------------------------------------------------------------------
# BSS-01: first bootstrap installs the unclassified sector
# ---------------------------------------------------------------------------


def test_bss01_first_bootstrap_installs_sector(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "test-only-password")

    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code == 0, result.output

    assert _count_unclassified_sectors() == 1


# ---------------------------------------------------------------------------
# BSS-02: second bootstrap is a no-op
# ---------------------------------------------------------------------------


def test_bss02_second_bootstrap_is_idempotent(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "test-only-password")

    first = runner.invoke(app, ["bootstrap"])
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, ["bootstrap"])
    assert second.exit_code == 0, second.output

    assert _count_unclassified_sectors() == 1


# ---------------------------------------------------------------------------
# BSS-03: per-tenant unclassified — install_unclassified_sector directly
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
        # Idempotently obtain an actor user. Re-runs of this helper for
        # the same tenant must not collide on the unique email constraint.
        async with tenant_context(engine, tenant_id) as session:
            user_repo = UserRepository(session)
            email = f"bss03-{tenant_id}@example.com"
            existing = await user_repo.get_by_email(email)
            actor = existing or await user_repo.create(email=email, password_hash="x" * 8)
        async with tenant_context(engine, tenant_id, user_id=actor.id) as session:
            await install_unclassified_sector(SectorRepository(session), actor.id)
    finally:
        await engine.dispose()


async def _count_for_tenant_async(tenant_id: UUID) -> int:
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
            result = await conn.execute(
                text(
                    "SELECT COUNT(*) FROM sectors WHERE tenant_id = :tid AND code = 'unclassified'"
                ),
                {"tid": str(tenant_id)},
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


def test_bss03_per_tenant_unclassified_isolation(clean_db: None) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()

    asyncio.run(_seed_tenant_async(tenant_a))
    asyncio.run(_seed_tenant_async(tenant_b))

    asyncio.run(_install_for_tenant_async(tenant_a))
    asyncio.run(_install_for_tenant_async(tenant_b))
    # Re-running for tenant A must remain a no-op.
    asyncio.run(_install_for_tenant_async(tenant_a))

    assert asyncio.run(_count_for_tenant_async(tenant_a)) == 1
    assert asyncio.run(_count_for_tenant_async(tenant_b)) == 1
