# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the SAA write surface.

The section's read surface is covered in ``test_saa_routes.py``;
this file focuses exclusively on the write paths:

* ``POST   /api/saa/configuration``              — create configuration.
* ``PUT    /api/saa/configuration/{id}``         — atomic save workflow.
* ``POST   /api/saa/configuration/{id}/activate``— activation toggle.
* ``DELETE /api/saa/configuration/{id}``         — configuration deletion.
* ``GET    /api/saa/asset-classes``              — modal partial.
* ``POST   /api/saa/asset-classes``              — asset-class creation.
* ``PUT    /api/saa/asset-classes/{id}``         — asset-class update.
* ``DELETE /api/saa/asset-classes/{id}``         — asset-class deletion
                                                   (with 409 on use).

Mutations signal frontend state changes via ``HX-Trigger`` headers
(``pf:saa-config-created`` / ``-activated`` / ``-deleted``) rather
than ``HX-Redirect``, per ADR-0054.

Each test uses the live compose Postgres so RLS, audit triggers, and
the partial unique index on ``saa_configurations`` evaluate exactly
as they will in production.
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
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB SAA write-route tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
        "TRUNCATE TABLE saa_correlations, saa_asset_class_inputs, "
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
    email = "saa-write@example.com"
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


async def _login_and_csrf(client: AsyncClient, email: str, password: str) -> str:
    """Log in and return the session-bound CSRF token.

    The CSRF meta tag is rendered into the back-office area page by
    base.html; we visit /back-office to scrape it after login.
    """
    get_response = await client.get("/login")
    pre_csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert pre_csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": pre_csrf},
        follow_redirects=False,
    )
    page = await client.get("/back-office", follow_redirects=False)
    assert page.status_code == 200
    body = page.text
    marker = 'name="csrf-token" content="'
    idx = body.find(marker)
    assert idx != -1
    start = idx + len(marker)
    end = body.find('"', start)
    return body[start:end]


def _build_service(session) -> SAAService:
    return SAAService(
        configurations=SAAConfigurationRepository(session),
        asset_classes=AssetClassRepository(session),
        inputs=SAAAssetClassInputRepository(session),
        correlations=SAACorrelationRepository(session),
    )


async def _seed_two_asset_configuration(
    user_id: UUID,
    tenant_id: UUID = SENTINEL_TENANT_ID,
    name: str = "Write Test Config",
) -> tuple[UUID, UUID, UUID]:
    """Create a 2-asset configuration; returns (config_id, ac1_id, ac2_id)."""
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
                        expected_return=0.075,
                        volatility=0.15,
                        min_weight=0.0,
                        max_weight=1.0,
                    ),
                    SAAAssetClassInputSpec(
                        asset_class_id=ac2.id,
                        expected_return=0.035,
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
            return config.id, ac1.id, ac2.id
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# CSRF-required surface
# ---------------------------------------------------------------------------


async def test_post_configuration_without_csrf_is_403(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    _user_id, email, password = seeded_user
    await _login_and_csrf(web_client, email, password)
    # Submit the form without the csrf_token field.
    response = await web_client.post(
        "/api/saa/configuration",
        data={
            "name": "No CSRF",
            "risk_free_rate_pct": "2.5",
            "n_frontier_points": "100",
        },
        follow_redirects=False,
    )
    assert response.status_code == 403


async def test_put_save_without_csrf_is_403(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    config_id, _ac1, _ac2 = await _seed_two_asset_configuration(user_id)
    await _login_and_csrf(web_client, email, password)
    response = await web_client.put(
        f"/api/saa/configuration/{config_id}",
        json={"metadata": {}, "inputs": [], "correlations": []},
    )
    assert response.status_code == 403


async def test_delete_without_csrf_is_403(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    config_id, _ac1, _ac2 = await _seed_two_asset_configuration(user_id)
    await _login_and_csrf(web_client, email, password)
    response = await web_client.request("DELETE", f"/api/saa/configuration/{config_id}")
    assert response.status_code == 403


async def test_activate_without_csrf_is_403(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    config_id, _ac1, _ac2 = await _seed_two_asset_configuration(user_id)
    await _login_and_csrf(web_client, email, password)
    response = await web_client.post(f"/api/saa/configuration/{config_id}/activate")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/saa/configuration — create configuration
# ---------------------------------------------------------------------------


async def test_post_configuration_creates_and_signals_hx_trigger(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """The new surface returns JSON + HX-Trigger, not a 303 redirect.

    The frontend listens for ``pf:saa-config-created`` and re-fetches
    the section pinned to the new id; the redirect-based flow used by
    the standalone surface was retired with ADR-0054.
    """
    _user_id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/saa/configuration",
        data={
            "name": "Created via web",
            "risk_free_rate_pct": "3.50",
            "n_frontier_points": "120",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Created via web"
    # The new id is a parseable UUID and is_active defaults to False.
    new_id = UUID(body["id"])
    assert body["is_active"] is False
    assert response.headers.get("HX-Trigger") == "pf:saa-config-created"

    # The new configuration is reachable via the picker-switch endpoint.
    detail = await web_client.get(f"/api/saa/configuration/{new_id}", follow_redirects=False)
    assert detail.status_code == 200
    assert "Created via web" in detail.text


# ---------------------------------------------------------------------------
# PUT /api/saa/configuration/{id} — atomic save
# ---------------------------------------------------------------------------


async def test_put_save_persists_metadata_inputs_and_correlations(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    config_id, ac1_id, ac2_id = await _seed_two_asset_configuration(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    payload = {
        "metadata": {
            "name": "Renamed via Save",
            "risk_free_rate": 0.04,
            "n_frontier_points": 75,
        },
        "inputs": [
            {
                "asset_class_id": str(ac1_id),
                "expected_return": 0.09,
                "volatility": 0.18,
                "min_weight": 0.05,
                "max_weight": 0.65,
            },
            {
                "asset_class_id": str(ac2_id),
                "expected_return": 0.04,
                "volatility": 0.06,
                "min_weight": 0.0,
                "max_weight": 0.5,
            },
        ],
        "correlations": [
            {
                "asset_class_a_id": str(ac1_id),
                "asset_class_b_id": str(ac2_id),
                "correlation": 0.25,
            },
        ],
    }
    response = await web_client.put(
        f"/api/saa/configuration/{config_id}",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["configuration"]["name"] == "Renamed via Save"
    assert body["configuration"]["risk_free_rate"] == pytest.approx(0.04)
    assert body["configuration"]["n_frontier_points"] == 75
    assert len(body["inputs"]) == 2
    assert len(body["correlations"]) == 1
    assert body["correlations"][0]["correlation"] == pytest.approx(0.25)


async def test_put_save_rejects_min_greater_than_max_with_400(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    config_id, ac1_id, ac2_id = await _seed_two_asset_configuration(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    payload = {
        "metadata": {},
        "inputs": [
            {
                "asset_class_id": str(ac1_id),
                "expected_return": 0.07,
                "volatility": 0.15,
                "min_weight": 0.5,
                "max_weight": 0.2,
            },
            {
                "asset_class_id": str(ac2_id),
                "expected_return": 0.03,
                "volatility": 0.05,
                "min_weight": 0.0,
                "max_weight": 1.0,
            },
        ],
        "correlations": [],
    }
    response = await web_client.put(
        f"/api/saa/configuration/{config_id}",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    body = response.json()
    assert "min_weight" in body["error"]
    assert body["row_index"] == 0


async def test_put_save_404_for_unknown_configuration(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    _user_id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.put(
        f"/api/saa/configuration/{uuid4()}",
        json={"metadata": {}, "inputs": [], "correlations": []},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/saa/configuration/{id}/activate
# ---------------------------------------------------------------------------


async def test_post_activate_sets_active_and_clears_old_active(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    config_a_id, _ac1, _ac2 = await _seed_two_asset_configuration(user_id, name="Config A")
    # Seed a second configuration in the same tenant, active.
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            svc = _build_service(session)
            config_b = await svc.create_configuration("Config B", 0.02, 30, user_id)
            await svc.activate_configuration(config_b.id)
            config_b_id = config_b.id
    finally:
        await engine.dispose()

    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        f"/api/saa/configuration/{config_a_id}/activate",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["is_active"] is True
    # ADR-0054: section stays in the back-office shell; we signal via
    # HX-Trigger and the frontend reloads the section in place.
    assert response.headers.get("HX-Trigger") == "pf:saa-config-activated"
    assert "HX-Redirect" not in response.headers

    # B should no longer be active.
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            svc = _build_service(session)
            config_b = await svc.get_configuration(config_b_id)
            assert config_b is not None
            assert config_b.is_active is False
    finally:
        await engine.dispose()


async def test_activate_unknown_returns_404(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    _user_id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        f"/api/saa/configuration/{uuid4()}/activate",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/saa/configuration/{id}
# ---------------------------------------------------------------------------


async def test_delete_configuration_cascade_removes_children(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    config_id, _ac1, _ac2 = await _seed_two_asset_configuration(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.request(
        "DELETE",
        f"/api/saa/configuration/{config_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "pf:saa-config-deleted"
    assert "HX-Redirect" not in response.headers

    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            svc = _build_service(session)
            assert await svc.get_configuration(config_id) is None
            inputs = await SAAAssetClassInputRepository(session).list_by_configuration(config_id)
            correlations = await SAACorrelationRepository(session).list_by_configuration(config_id)
            assert inputs == []
            assert correlations == []
    finally:
        await engine.dispose()


async def test_delete_unknown_configuration_returns_404(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    _user_id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.request(
        "DELETE",
        f"/api/saa/configuration/{uuid4()}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Asset-class CRUD
# ---------------------------------------------------------------------------


async def test_get_asset_classes_modal_renders(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    await _seed_two_asset_configuration(user_id)
    await _login_and_csrf(web_client, email, password)

    response = await web_client.get("/api/saa/asset-classes", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert "Asset Class Catalogue" in body
    assert "Equities" in body
    assert "saa-asset-classes-table" in body


async def test_post_asset_class_returns_json_and_hx_trigger(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    _user_id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/saa/asset-classes",
        data={
            "code": "fixed_income_em",
            "display_name": "Fixed Income EM",
            "description": "Emerging-market sovereign and corporate bonds.",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "fixed_income_em"
    assert body["display_name"] == "Fixed Income EM"
    assert body["usage_count"] == 0
    assert response.headers.get("HX-Trigger") == "pf:saa-asset-class-created"

    # The modal re-fetch picks up the new asset class.
    page = await web_client.get("/api/saa/asset-classes", follow_redirects=False)
    assert "Fixed Income EM" in page.text


async def test_post_asset_class_duplicate_code_returns_409(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """Duplicate codes surface as 409 JSON, not as a redirect with ?error.

    The standalone surface returned a 303 with a query-string error
    so the form page could surface it; the modal-driven flow uses an
    inline error banner driven by the JSON response body.
    """
    user_id, email, password = seeded_user
    await _seed_two_asset_configuration(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/saa/asset-classes",
        data={
            "code": "equities",  # already created in seed helper
            "display_name": "Duplicate Test",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 409
    body = response.json()
    assert "already exists" in body["error"]
    assert body["field"] == "code"


async def test_put_asset_class_updates_display_name(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    _config_id, ac1_id, _ac2_id = await _seed_two_asset_configuration(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.put(
        f"/api/saa/asset-classes/{ac1_id}",
        json={"display_name": "Equities (Renamed)"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Equities (Renamed)"


async def test_delete_unreferenced_asset_class_succeeds(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)
    # Create an unreferenced AC via repository.
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            svc = _build_service(session)
            ac = await svc.create_asset_class("orphan-ac", "Orphan AC")
    finally:
        await engine.dispose()

    response = await web_client.request(
        "DELETE",
        f"/api/saa/asset-classes/{ac.id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert response.headers.get("HX-Trigger") == "pf:saa-asset-class-deleted"


async def test_delete_referenced_asset_class_returns_409(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    _config_id, ac1_id, _ac2_id = await _seed_two_asset_configuration(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.request(
        "DELETE",
        f"/api/saa/asset-classes/{ac1_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 409
    body = response.json()
    assert "in use" in body["error"]
    assert body["usage_count"] == 1


# ---------------------------------------------------------------------------
# Cross-tenant isolation on writes
# ---------------------------------------------------------------------------


async def test_save_against_foreign_tenant_returns_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """A logged-in sentinel user cannot save a configuration owned by another tenant."""
    _user_id, email, password = seeded_user
    other_tenant_id = uuid4()
    other_user_id = uuid4()
    # Create the other tenant + its owner.
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, gen_random_uuid()::text)"
            ),
            {"id": str(other_tenant_id), "name": "Foreign Tenant"},
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
                "id": str(other_user_id),
                "tid": str(other_tenant_id),
                "email": "foreign@example.com",
                "hash": hash_password("xxx"),
            },
        )
    foreign_config_id, _ac1, _ac2 = await _seed_two_asset_configuration(
        other_user_id, tenant_id=other_tenant_id, name="Foreign Config"
    )

    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.put(
        f"/api/saa/configuration/{foreign_config_id}",
        json={"metadata": {}, "inputs": [], "correlations": []},
        headers={"X-CSRF-Token": csrf},
    )
    # RLS hides the foreign row from the active tenant; the route maps
    # absence to 404.
    assert response.status_code == 404


async def test_delete_against_foreign_tenant_returns_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    _user_id, email, password = seeded_user
    other_tenant_id = uuid4()
    other_user_id = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, gen_random_uuid()::text)"
            ),
            {"id": str(other_tenant_id), "name": "Foreign Tenant 2"},
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
                "id": str(other_user_id),
                "tid": str(other_tenant_id),
                "email": "foreign2@example.com",
                "hash": hash_password("xxx"),
            },
        )
    foreign_config_id, _ac1, _ac2 = await _seed_two_asset_configuration(
        other_user_id, tenant_id=other_tenant_id, name="Foreign Config 2"
    )

    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.request(
        "DELETE",
        f"/api/saa/configuration/{foreign_config_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 404
