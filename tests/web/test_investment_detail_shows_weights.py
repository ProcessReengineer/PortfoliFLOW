# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level test for the allocation sections on investment detail.

Verifies that ``GET /investments/{id}`` renders the read-only Region
and Sector allocation sections with correct values, and that an
investment without weights renders empty sections (no 500, no layout
break). Region semantics per ADR-0046 — the legacy country-allocation
section has been retired from this surface.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories import (
    AssetClassRepository,
    InvestmentRegionWeightsRepository,
    InvestmentRepository,
    InvestmentSectorWeightsRepository,
    RegionRepository,
    RegionWeightInput,
    SectorRepository,
    SectorWeightInput,
    tenant_context,
)
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(pathlib.Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB investment-detail allocation tests.",
            allow_module_level=False,
        )


@pytest_asyncio.fixture
async def superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_schema(
    superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    truncate_sql = text(
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
    async with superuser_engine.begin() as conn:
        await conn.execute(truncate_sql)
    try:
        yield
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(truncate_sql)


@pytest_asyncio.fixture
async def seeded_user(
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "investments-allocation@example.com"
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
            },
        )
    return user_id, email, plaintext


@pytest_asyncio.fixture
async def web_client(
    seeded_user: tuple[UUID, str, str],
) -> AsyncGenerator[AsyncClient, None]:
    settings = WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        csrf_cookie_name="portfoliflow_csrf_pre_session",
        database_url=DATABASE_URL,
        database_url_superuser=DATABASE_URL_SUPERUSER,
        session_cookie_secure=False,
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://testserver") as client,
        app.router.lifespan_context(app),
    ):
        yield client


async def _login(client: AsyncClient, email: str, password: str) -> None:
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )


async def _seed_investment_with_weights(
    user_id: UUID,
    *,
    with_weights: bool,
) -> UUID:
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            ac = await AssetClassRepository(session).create(
                code="ac_alloc", display_name="AC Alloc"
            )
            inv = await InvestmentRepository(session).create(
                name="Allocation Fund",
                investment_type="private_equity",
                asset_class_id=ac.id,
                currency="EUR",
                created_by=user_id,
            )
            if with_weights:
                sector_tech = await SectorRepository(session).create(
                    code="tech_software",
                    display_name="Technology — Software",
                    created_by=user_id,
                )
                region_repo = RegionRepository(session)
                region_dach = await region_repo.create(
                    code="dach",
                    display_name="DACH",
                    sort_order=10,
                )
                region_usa = await region_repo.create(
                    code="north_america_usa",
                    display_name="North America — USA",
                    sort_order=60,
                )
                # ADR-0080: one historised snapshot, anchored to a
                # canonical reporting date and basis='reported'.
                await InvestmentRegionWeightsRepository(session).replace_snapshot_for_investment(
                    inv.id,
                    date(2024, 12, 31),
                    [
                        RegionWeightInput(
                            region_id=region_dach.id,
                            weight_pct=Decimal("60"),
                        ),
                        RegionWeightInput(
                            region_id=region_usa.id,
                            weight_pct=Decimal("40"),
                        ),
                    ],
                    basis="reported",
                    created_by=user_id,
                )
                await InvestmentSectorWeightsRepository(session).replace_snapshot_for_investment(
                    inv.id,
                    date(2024, 12, 31),
                    [
                        SectorWeightInput(
                            sector_id=sector_tech.id,
                            weight_pct=Decimal("100"),
                        )
                    ],
                    basis="reported",
                    created_by=user_id,
                )
        return inv.id
    finally:
        await engine.dispose()


async def _seed_investment_with_two_region_snapshots(
    user_id: UUID,
) -> UUID:
    """Seed one investment with two region snapshots (D1 → D2).

    D1 (2023-12-31): 100 % DACH. D2 (2024-12-31): 100 % North America
    — USA. The detail view must render only the later (D2) generation
    per ADR-0080 §4.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            ac = await AssetClassRepository(session).create(
                code="ac_drift", display_name="AC Drift"
            )
            inv = await InvestmentRepository(session).create(
                name="Drift Fund",
                investment_type="private_equity",
                asset_class_id=ac.id,
                currency="EUR",
                created_by=user_id,
            )
            region_repo = RegionRepository(session)
            region_dach = await region_repo.create(code="dach", display_name="DACH", sort_order=10)
            region_usa = await region_repo.create(
                code="north_america_usa",
                display_name="North America — USA",
                sort_order=60,
            )
            weights_repo = InvestmentRegionWeightsRepository(session)
            await weights_repo.replace_snapshot_for_investment(
                inv.id,
                date(2023, 12, 31),
                [
                    RegionWeightInput(
                        region_id=region_dach.id,
                        weight_pct=Decimal("100"),
                    )
                ],
                basis="reported",
                created_by=user_id,
            )
            await weights_repo.replace_snapshot_for_investment(
                inv.id,
                date(2024, 12, 31),
                [
                    RegionWeightInput(
                        region_id=region_usa.id,
                        weight_pct=Decimal("100"),
                    )
                ],
                basis="reported",
                created_by=user_id,
            )
        return inv.id
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# DA-01: detail page renders allocation sections with correct values
# ---------------------------------------------------------------------------


async def test_da01_detail_renders_region_and_sector_allocation(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    inv_id = await _seed_investment_with_weights(user_id, with_weights=True)
    await _login(web_client, email, password)

    response = await web_client.get(f"/investments/{inv_id}", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    assert "Region Allocation" in body
    assert "Sector Allocation" in body

    # Region values rendered: codes, display names, weights.
    assert "dach" in body
    assert "DACH" in body
    assert "North America — USA" in body
    assert "60.00" in body
    assert "40.00" in body

    # Sector values rendered.
    assert "tech_software" in body
    assert "Technology" in body
    assert "100.00" in body


# ---------------------------------------------------------------------------
# DA-02: detail page renders empty sections when no weights present
# ---------------------------------------------------------------------------


async def test_da02_detail_renders_empty_sections_without_weights(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    inv_id = await _seed_investment_with_weights(user_id, with_weights=False)
    await _login(web_client, email, password)

    response = await web_client.get(f"/investments/{inv_id}", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    assert "Region Allocation" in body
    assert "Sector Allocation" in body
    assert "No region allocation recorded." in body
    assert "No sector allocation recorded." in body


# ---------------------------------------------------------------------------
# DA-03: detail page shows the latest snapshot when two exist (ADR-0080 §4)
# ---------------------------------------------------------------------------


async def test_da03_detail_renders_latest_region_snapshot(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    inv_id = await _seed_investment_with_two_region_snapshots(user_id)
    await _login(web_client, email, password)

    response = await web_client.get(f"/investments/{inv_id}", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    # The later (D2) snapshot is 100 % North America — USA.
    assert "north_america_usa" in body
    assert "North America — USA" in body
    # The earlier (D1) DACH generation is not "the" composition and must
    # not leak into the rendered allocation table.
    assert "DACH" not in body
