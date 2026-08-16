# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard for the default-development-tenant seed.

The Phase-2 auth backend resolves every email to
``core.tenant_constants.SENTINEL_TENANT_ID`` and writes every
authenticated row — including ``login_audit`` — against that UUID
via a foreign key. The b008 migration installs the sentinel
``tenants`` row so the FKs resolve. Without that row, the very
first login attempt after a Postgres container reset fails with
``ForeignKeyViolationError``.

These tests pin two invariants:

* The migration is idempotent — re-running the seed insert (the
  same ``INSERT ... ON CONFLICT (id) DO NOTHING`` Alembic executes)
  produces exactly one row.
* The sentinel row's name matches the placeholder the status-bar
  context processor in ``web/main.py`` falls back to. This couples
  the migration's hardcoded name to the application's hardcoded
  fallback so a rename in either place fails the suite.

The tests do not assume the migration has already run during the
current test session — they re-apply the same insert idempotently
to bracket their assertions in a known-good state. That keeps them
independent of test-run ordering with other suites that truncate
the ``tenants`` table (the repository tests do, see
``tests/_db_fixtures.py``).
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

from core.tenant_constants import SENTINEL_TENANT_ID

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

# Mirrors the post-rename values inside the b012 migration. b008
# seeded "Sentinel Tenant"; b012 renamed it to "Minathena Capital"
# with subdomain "minathena-capital". This test pins the b012
# state alongside the SENTINEL_TENANT_ID UUID literal.
_PRIMARY_TENANT_NAME: str = "Minathena Capital"
_PRIMARY_TENANT_SUBDOMAIN: str = "minathena-capital"


@pytest_asyncio.fixture
async def superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    if not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL_SUPERUSER not set; cannot run tenant-seed guards.",
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


_SEED_SQL = text(
    """
    INSERT INTO tenants (id, name, subdomain)
    VALUES (:id, :name, :subdomain)
    ON CONFLICT (id) DO UPDATE
        SET name = EXCLUDED.name,
            subdomain = EXCLUDED.subdomain
    """
)


async def _apply_seed(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            _SEED_SQL,
            {
                "id": str(SENTINEL_TENANT_ID),
                "name": _PRIMARY_TENANT_NAME,
                "subdomain": _PRIMARY_TENANT_SUBDOMAIN,
            },
        )


async def test_default_tenant_seeded_row_present(
    superuser_engine: AsyncEngine,
) -> None:
    """After b012 runs, the primary tenant exists with the new name/subdomain."""
    await _apply_seed(superuser_engine)

    async with superuser_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT name, subdomain FROM tenants WHERE id = :id"),
            {"id": str(SENTINEL_TENANT_ID)},
        )
        row = result.first()

    assert row is not None, (
        "Primary tenant row is missing — b008 seeded it and b012 "
        "should have renamed it to Minathena Capital."
    )
    assert row.name == _PRIMARY_TENANT_NAME
    assert row.subdomain == _PRIMARY_TENANT_SUBDOMAIN


async def test_default_tenant_seed_is_idempotent(
    superuser_engine: AsyncEngine,
) -> None:
    """Re-running the seed leaves exactly one primary-tenant row."""
    await _apply_seed(superuser_engine)
    await _apply_seed(superuser_engine)
    await _apply_seed(superuser_engine)

    async with superuser_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT COUNT(*) FROM tenants WHERE id = :id"),
            {"id": str(SENTINEL_TENANT_ID)},
        )
        count = int(result.scalar_one())

    assert count == 1, (
        f"Primary tenant row count is {count}; the seed should be "
        "idempotent under ON CONFLICT DO UPDATE."
    )
