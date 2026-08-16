# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tenant-isolation tests for the SAA web surface.

A second tenant ("Other Tenant") is seeded alongside the sentinel
tenant. A configuration is created in the other tenant via a
superuser-bypass session (so the seed itself is not blocked by RLS).
The authenticated test user belongs to the sentinel tenant; the test
asserts:

* The SAA section endpoint in the sentinel tenant does NOT show the
  other tenant's configuration name in its picker or body.
* ``GET /api/saa/configuration/{other-tenant-config-id}`` returns
  404 (RLS hides the row from the active tenant; the route maps
  absence to 404).
* ``GET /api/saa/configuration/{other-tenant-config-id}/optimization``
  returns 404 with the inline error partial.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories import (
    AssetClassRepository,
    SAAAssetClassInputRepository,
    SAAConfigurationRepository,
    SAACorrelationRepository,
    tenant_context,
)
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from services.saa import (
    SAAAssetClassInputSpec,
    SAACorrelationSpec,
    SAAService,
)
from web.main import create_app
from web.settings import WebSettings

load_dotenv(pathlib.Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; skipping live-DB SAA RLS tests.",
            allow_module_level=False,
        )


@pytest_asyncio.fixture
async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_schema(
    fresh_superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    truncate_sql = text(
        "TRUNCATE TABLE saa_correlations, saa_asset_class_inputs, "
        "saa_configurations, asset_classes, "
        "data_upload_sheets, data_uploads, "
        "login_audit, sessions, audit_log, "
        "data_store_entries, users, tenants "
        "RESTART IDENTITY CASCADE"
    )
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(truncate_sql)
    try:
        yield
    finally:
        async with fresh_superuser_engine.begin() as conn:
            await conn.execute(truncate_sql)


@pytest_asyncio.fixture
async def two_tenants(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, UUID, UUID, str, str]:
    """Seed sentinel + a second tenant. Return ids and login material.

    Returns:
        (sentinel_user_id, sentinel_user_email, password,
         other_tenant_id, other_user_id)
    """
    plaintext = "correct-horse-battery-staple"
    sentinel_user_id = uuid4()
    other_tenant_id = uuid4()
    other_user_id = uuid4()
    email = "rls@example.com"

    async with fresh_superuser_engine.begin() as conn:
        # Sentinel tenant.
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        # Other tenant.
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, gen_random_uuid()::text)"
            ),
            {"id": str(other_tenant_id), "name": "Other Tenant"},
        )
        # Sentinel user (logs in via the web).
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
                "id": str(sentinel_user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
            },
        )
        # Other tenant's owner — needed for SAAConfiguration.created_by.
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
                "id": str(other_user_id),
                "tid": str(other_tenant_id),
                "email": "other@example.com",
                "hash": hash_password(plaintext),
            },
        )
    return (
        sentinel_user_id,
        email,
        plaintext,
        other_tenant_id,
        other_user_id,
    )


def _build_service(session) -> SAAService:
    return SAAService(
        configurations=SAAConfigurationRepository(session),
        asset_classes=AssetClassRepository(session),
        inputs=SAAAssetClassInputRepository(session),
        correlations=SAACorrelationRepository(session),
    )


async def _seed_in_tenant(tenant_id: UUID, user_id: UUID, name: str) -> UUID:
    """Create a 2-asset SAA configuration inside ``tenant_id``."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as session:
            svc = _build_service(session)
            ac1 = await svc.create_asset_class("equities", "Equities")
            ac2 = await svc.create_asset_class("bonds", "Bonds")
            config = await svc.create_configuration(name, 0.025, 30, user_id)
            await svc.save_inputs_and_correlations(
                config.id,
                [
                    SAAAssetClassInputSpec(
                        asset_class_id=ac1.id,
                        expected_return=0.07,
                        volatility=0.15,
                        min_weight=0.0,
                        max_weight=1.0,
                    ),
                    SAAAssetClassInputSpec(
                        asset_class_id=ac2.id,
                        expected_return=0.03,
                        volatility=0.05,
                        min_weight=0.0,
                        max_weight=1.0,
                    ),
                ],
                [
                    SAACorrelationSpec(
                        asset_class_a_id=ac1.id,
                        asset_class_b_id=ac2.id,
                        correlation=0.1,
                    ),
                ],
            )
            return config.id
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def web_client_with_sentinel_login(
    two_tenants: tuple[UUID, str, str, UUID, UUID],
) -> AsyncGenerator[tuple[AsyncClient, UUID, UUID], None]:
    """Logged-in sentinel-tenant client; yields ``(client, sentinel_uid,
    other_tenant_id)`` so the test can ask the helper to seed a
    foreign configuration."""
    sentinel_user_id, email, password, other_tenant_id, _other_user_id = two_tenants
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
        get_response = await client.get("/login")
        csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
        assert csrf is not None
        await client.post(
            "/login",
            data={
                "email": email,
                "password": password,
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
        yield client, sentinel_user_id, other_tenant_id


# ---------------------------------------------------------------------------
# Cross-tenant read isolation
# ---------------------------------------------------------------------------


async def test_other_tenant_config_not_in_section(
    web_client_with_sentinel_login: tuple[AsyncClient, UUID, UUID],
    two_tenants: tuple[UUID, str, str, UUID, UUID],
) -> None:
    client, _sentinel_uid, other_tenant_id = web_client_with_sentinel_login
    _, _, _, _, other_user_id = two_tenants
    # Seed a configuration in the OTHER tenant.
    await _seed_in_tenant(other_tenant_id, other_user_id, "Other Secret")

    response = await client.get("/api/saa/section", follow_redirects=False)
    assert response.status_code == 200
    # The other tenant's name must not surface in the sentinel section.
    assert "Other Secret" not in response.text


async def test_other_tenant_config_returns_404_on_configuration_partial(
    web_client_with_sentinel_login: tuple[AsyncClient, UUID, UUID],
    two_tenants: tuple[UUID, str, str, UUID, UUID],
) -> None:
    client, _sentinel_uid, other_tenant_id = web_client_with_sentinel_login
    _, _, _, _, other_user_id = two_tenants
    foreign_id = await _seed_in_tenant(other_tenant_id, other_user_id, "Hidden Config")

    response = await client.get(f"/api/saa/configuration/{foreign_id}", follow_redirects=False)
    assert response.status_code == 404


async def test_other_tenant_config_returns_404_on_optimization(
    web_client_with_sentinel_login: tuple[AsyncClient, UUID, UUID],
    two_tenants: tuple[UUID, str, str, UUID, UUID],
) -> None:
    client, _sentinel_uid, other_tenant_id = web_client_with_sentinel_login
    _, _, _, _, other_user_id = two_tenants
    foreign_id = await _seed_in_tenant(other_tenant_id, other_user_id, "Hidden Config Opt")

    response = await client.get(
        f"/api/saa/configuration/{foreign_id}/optimization",
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert "Configuration not found" in response.text
    assert "Plotly.newPlot" not in response.text
