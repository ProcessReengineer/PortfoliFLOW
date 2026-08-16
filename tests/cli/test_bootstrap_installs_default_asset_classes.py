# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Phase-7 default-asset-classes bootstrap step.

Verifies that ``install_default_asset_classes`` is idempotent and
coexists with the pre-Phase-7 ``unclassified`` fallback installed by
:func:`install_unclassified_asset_class`. The two functions write to
the same ``asset_classes`` table but are independent.

Coverage
--------
* BAC-01: First call inserts 12 default asset classes plus the
  pre-installed ``unclassified`` row (13 total).
* BAC-02: A second call is a no-op (count unchanged).
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cli.bootstrap import (
    install_default_asset_classes,
    install_unclassified_asset_class,
)
from core.repositories import AssetClassRepository, tenant_context

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL[_SUPERUSER] not set; cannot run bootstrap tests.",
            allow_module_level=False,
        )


@pytest_asyncio.fixture
async def superuser_engine_bsac():
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_engine_bsac():
    _require_db()
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded_tenant_with_user(superuser_engine_bsac):
    """Create a fresh tenant + user inside the tenant context."""
    tenant_id = uuid4()
    user_id = uuid4()
    async with superuser_engine_bsac.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:tid, :name, :subdomain)"),
            # Distinct param for subdomain: reusing :tid for both the
            # uuid id and the varchar subdomain makes asyncpg fail with
            # AmbiguousParameterError (one positional param deduced as
            # two incompatible types). The value stays unique per tenant.
            {
                "tid": str(tenant_id),
                "name": "BAC tenant",
                "subdomain": f"bac-{tenant_id}",
            },
        )
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO users "
                "(id, tenant_id, email, password_hash, roles, is_active) "
                "VALUES (:uid, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)"
            ),
            {
                "uid": str(user_id),
                "tid": str(tenant_id),
                "email": f"bac-{user_id}@example.com",
                "hash": "$2b$04$placeholder_hash_for_bootstrap_tests_only",
            },
        )
    yield tenant_id, user_id


async def _count_asset_classes(superuser_engine: AsyncEngine, tenant_id: UUID) -> int:
    async with superuser_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        result = await conn.execute(
            text("SELECT COUNT(*) FROM asset_classes WHERE tenant_id = :tid"),
            {"tid": str(tenant_id)},
        )
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# BAC-01: first bootstrap installs unclassified + 12 defaults = 13
# ---------------------------------------------------------------------------


async def test_bac01_first_bootstrap_installs_defaults_alongside_unclassified(
    superuser_engine_bsac: AsyncEngine,
    app_engine_bsac: AsyncEngine,
    seeded_tenant_with_user,
) -> None:
    tenant_id, user_id = seeded_tenant_with_user

    async with tenant_context(app_engine_bsac, tenant_id, user_id=user_id) as session:
        repo = AssetClassRepository(session)
        await install_unclassified_asset_class(repo)
        await install_default_asset_classes(repo)

    count = await _count_asset_classes(superuser_engine_bsac, tenant_id)
    assert count == 13, f"Expected 12 default + 1 unclassified = 13 asset classes, got {count}"


# ---------------------------------------------------------------------------
# BAC-02: re-running is a no-op
# ---------------------------------------------------------------------------


async def test_bac02_second_bootstrap_is_idempotent(
    superuser_engine_bsac: AsyncEngine,
    app_engine_bsac: AsyncEngine,
    seeded_tenant_with_user,
) -> None:
    tenant_id, user_id = seeded_tenant_with_user

    async with tenant_context(app_engine_bsac, tenant_id, user_id=user_id) as session:
        repo = AssetClassRepository(session)
        await install_unclassified_asset_class(repo)
        await install_default_asset_classes(repo)

    first = await _count_asset_classes(superuser_engine_bsac, tenant_id)

    async with tenant_context(app_engine_bsac, tenant_id, user_id=user_id) as session:
        repo = AssetClassRepository(session)
        await install_unclassified_asset_class(repo)
        await install_default_asset_classes(repo)

    second = await _count_asset_classes(superuser_engine_bsac, tenant_id)
    assert second == first == 13
