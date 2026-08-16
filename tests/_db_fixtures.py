# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared async-DB fixtures for repository and service tests.

These fixtures used to live in ``tests/repositories/conftest.py``.
Sub-stream 3b adds ``tests/services/`` which exercises the same
compose Postgres, so the fixtures are extracted into a plain module
that both conftests load via ``pytest_plugins``. Putting them
together in ``tests/conftest.py`` was rejected because that would
make every non-DB test pay the autouse ``reset_schema`` truncation
cost.

The contract is unchanged:

* ``app_engine`` — function-scoped engine bound to ``portfoliflow_app``
  (RLS evaluates exactly as in production).
* ``superuser_engine`` — function-scoped engine bound to ``postgres``
  (used only by fixtures that need to bypass RLS to seed tenants
  and truncate domain tables).
* ``reset_schema`` — autouse: TRUNCATE every domain table before AND
  after each test so cross-test pollution cannot accumulate.
* ``seed_tenant`` — superuser helper to create a tenant row (the app
  role cannot do that — see ``tenant_self_visibility`` policy).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# Load .env from the repo root before any os.getenv() call.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db_urls() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set to run "
            "DB-backed tests against the compose DB. See .env.example.",
            allow_module_level=True,
        )


@pytest_asyncio.fixture
async def superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine bound to the Postgres superuser. Fixture-only.

    Function-scoped with NullPool so every test gets its own engine.
    pytest-asyncio's default ``auto`` mode creates a fresh event loop
    per test; a session-scoped asyncpg engine would stay bound to
    the first test's (now-closed) loop and raise "another operation
    is in progress" on the second test. Function-scoped + NullPool
    is the standard fix.
    """
    _require_db_urls()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - intentional, re-skipped
        await engine.dispose()
        pytest.skip(
            f"Cannot reach Postgres at {DATABASE_URL_SUPERUSER!r}: {exc}. "
            "Is the compose container running? `podman compose up -d`.",
            allow_module_level=False,
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine bound to the unprivileged ``portfoliflow_app`` role.

    Function-scoped with NullPool for the same event-loop reason
    documented on ``superuser_engine``.
    """
    _require_db_urls()
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def reset_schema(superuser_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """Truncate domain tables before AND after every test.

    Runs as superuser because TRUNCATE bypasses RLS only for
    BYPASSRLS / superuser; the app role would be filtered to
    whatever its current tenant context allows. RESTART IDENTITY
    resets sequences (none in Phase 1, but cheap insurance for
    later) and CASCADE handles the FK chain
    (audit_log → users → tenants and friends).
    """
    # ``countries`` and ``anlv_categories`` are intentionally OMITTED —
    # they are global stammtabellen (seeded by b007 and b010 respectively),
    # have no tenant_id, and re-seeding them on every test would be
    # expensive and pointless.
    truncate_sql = text(
        "TRUNCATE TABLE "
        "watchpoints, floor_calibration, "
        "irene_finding, irene_watch_state, irene_schedule, "
        "asset_class_benchmark_mapping, benchmark_observations, "
        "benchmarks, "
        "limits, limit_sets, "
        "investment_region_weights, "
        "region_country_memberships, regions, "
        "investment_country_weights, "
        "investment_sector_weights, sectors, "
        "investment_bond_analytics, investment_rating_weight, "
        "investment_maturity_weight, "
        "investment_identifiers, "
        "fx_rates, "
        "instrument_prices, position_transactions, "
        "investment_cashflows, investment_navs, investments, "
        "saa_correlations, saa_asset_class_inputs, "
        "saa_configurations, asset_classes, "
        "data_upload_sheets, data_uploads, "
        "super_admin_audit, "
        "login_audit, sessions, audit_log, "
        "data_store_entries, users, tenants "
        "RESTART IDENTITY CASCADE"
    )
    async with superuser_engine.begin() as conn:
        await conn.execute(truncate_sql)
    try:
        yield
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(truncate_sql)


@pytest_asyncio.fixture
async def seed_tenant(superuser_engine: AsyncEngine):
    """Insert a tenant row and return its id.

    Tenant creation is a superuser path: the
    ``tenant_self_visibility`` policy and the structural constraint
    (Phase 5 owns the workflow) mean the app role cannot insert
    tenants. Tests that need one ask the fixture to mint it.

    Per ADR-0063 §1 ``tenants.subdomain`` is NOT NULL UNIQUE. The
    fixture generates a unique subdomain per call from the tenant
    id so concurrent tests cannot collide on the partial unique
    index.
    """

    async def _seed(
        name: str = "Test Tenant",
        subdomain: str | None = None,
        is_active: bool = True,
    ) -> UUID:
        new_id = uuid4()
        # The default per-call subdomain is derived from the new UUID
        # so back-to-back fixture calls in the same test cannot
        # collide on the unique index.
        resolved_subdomain = subdomain or f"t-{new_id.hex[:12]}"
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, name, subdomain, is_active) "
                    "VALUES (:id, :name, :subdomain, :is_active)"
                ),
                {
                    "id": str(new_id),
                    "name": name,
                    "subdomain": resolved_subdomain,
                    "is_active": is_active,
                },
            )
        return new_id

    return _seed
