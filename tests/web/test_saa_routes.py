# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the SAA web section surface.

Live-DB tests against the compose Postgres. The fixtures seed the
sentinel tenant and a sentinel-tenant user, the per-test client is
bound to the FastAPI app via ``ASGITransport``, and each test seeds
exactly the SAA configurations / inputs / correlations it needs
through :class:`SAAService` so the assertions are independent of
shipped seed data.

Coverage targets:

* ``GET /api/saa/section`` requires authentication (303 redirect when not).
* ``GET /api/saa/section`` renders the picker + active configuration
  body after login.
* ``GET /api/saa/section?config_id={uuid}`` lands on the specified
  configuration (URL-fragment deep-link entry point).
* ``GET /api/saa/section`` renders the empty-state when no
  configurations exist.
* ``GET /api/saa/configuration/{id}`` renders only the configuration
  body partial (picker switch), not the section wrapper.
* ``GET /api/saa/configuration/{id}`` returns 404 for an id absent
  from the active tenant.
* ``GET /api/saa/configuration/{id}/optimization`` returns 200 with
  a Plotly spec when the configuration has ≥ 2 inputs.
* ``GET /api/saa/configuration/{id}/optimization`` returns 400 with
  the inline error partial when the configuration has < 2 inputs.
* ``GET /api/saa/asset-classes`` returns the modal partial.

The two-asset minimum and the 404 case together exercise the full
read surface; tenant-isolation is covered separately in
``test_saa_rls.py``. The surface lives under ``/api/saa/*`` per
ADR-0054 (roadmap A5); the standalone ``/saa`` pages were retired
in the same change.
"""

from __future__ import annotations

import os
import pathlib
import re
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
            "skipping live-DB SAA route tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
async def seeded_user(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "saa@example.com"
    async with fresh_superuser_engine.begin() as conn:
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
) -> UUID:
    """Insert a 2-asset SAA configuration into ``tenant_id`` and return its id."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as session:
            svc = _build_service(session)
            ac1 = await svc.create_asset_class("equities", "Equities")
            ac2 = await svc.create_asset_class("bonds", "Bonds")
            config = await svc.create_configuration("Web Test Config", 0.025, 30, user_id)
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
            return config.id
    finally:
        await engine.dispose()


async def _seed_one_asset_configuration(user_id: UUID) -> UUID:
    """Insert a 1-asset SAA configuration (insufficient for optimisation)."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            svc = _build_service(session)
            ac = await svc.create_asset_class("only", "Only One")
            config = await svc.create_configuration("Single Asset", 0.02, 30, user_id)
            await svc.save_inputs_and_correlations(
                config.id,
                [
                    SAAAssetClassInputSpec(
                        asset_class_id=ac.id,
                        expected_return=0.05,
                        volatility=0.10,
                        min_weight=0.0,
                        max_weight=1.0,
                    ),
                ],
                [],
            )
            return config.id
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# GET /api/saa/section
# ---------------------------------------------------------------------------


async def test_get_section_unauthenticated_redirects_to_login(
    web_client: AsyncClient,
) -> None:
    response = await web_client.get("/api/saa/section", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_get_section_renders_picker_and_active_config(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _seed_two_asset_configuration(user_id)
    await _login(web_client, email, password)

    response = await web_client.get("/api/saa/section", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    # Section root + picker control are present.
    assert 'id="pf-saa-root"' in body
    assert 'id="saa-config-switcher"' in body
    # The seeded configuration name surfaces in the picker option list
    # and inside the configuration body header input.
    assert "Web Test Config" in body
    # Configuration body markers — inputs + correlation tables.
    assert "saa-asset-class-inputs-table" in body
    assert "saa-correlation-matrix-table" in body
    # The "Run Optimization" button targets the new API path.
    assert "/api/saa/configuration/" in body
    assert "/optimization" in body
    # Asset-class display names surface via the tojson lookup.
    assert "Equities" in body
    assert "Bonds" in body


async def test_get_section_empty_renders_empty_state(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/api/saa/section", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    # No configurations → no picker, empty-state copy instead.
    assert 'id="saa-config-switcher"' not in body
    assert "No SAA configurations yet" in body
    assert "portfoliflow bootstrap" in body


async def test_get_section_with_config_id_pins_specific_configuration(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Deep-link entry: ``?config_id=…`` overrides the active config pick."""
    user_id, email, password = seeded_user
    first_id = await _seed_two_asset_configuration(user_id)

    # Seed a second, named configuration in the same tenant.
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            svc = _build_service(session)
            second = await svc.create_configuration("Deep Link Target", 0.03, 30, user_id)
            second_id = second.id
    finally:
        await engine.dispose()

    await _login(web_client, email, password)

    # Without the query param the section lands on the first config
    # (most-recently-updated fallback wins since neither is active).
    default_resp = await web_client.get("/api/saa/section", follow_redirects=False)
    assert default_resp.status_code == 200

    pinned_resp = await web_client.get(
        f"/api/saa/section?config_id={second_id}", follow_redirects=False
    )
    assert pinned_resp.status_code == 200
    body = pinned_resp.text
    # The pinned configuration is the one whose body is rendered.
    assert "Deep Link Target" in body
    # The header input carries the pinned name.
    assert 'value="Deep Link Target"' in body
    # The optimisation button targets the pinned id; the other config
    # only appears as a (non-selected) <option> in the picker.
    assert f"/api/saa/configuration/{second_id}/optimization" in body
    assert f"/api/saa/configuration/{first_id}/optimization" not in body


# ---------------------------------------------------------------------------
# GET /api/saa/configuration/{config_id}
# ---------------------------------------------------------------------------


async def test_get_configuration_partial_unknown_returns_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(f"/api/saa/configuration/{uuid4()}", follow_redirects=False)
    assert response.status_code == 404


async def test_get_configuration_partial_renders_body_without_section_wrapper(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    config_id = await _seed_two_asset_configuration(user_id)
    await _login(web_client, email, password)

    response = await web_client.get(f"/api/saa/configuration/{config_id}", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    # The configuration body markers are present.
    assert "Web Test Config" in body
    assert "saa-asset-class-inputs-table" in body
    assert "saa-correlation-matrix-table" in body
    assert f"/api/saa/configuration/{config_id}/optimization" in body
    # The section wrapper / picker control belongs to the section
    # endpoint — picker-switch responses must NOT re-emit it, or HTMX
    # would nest a wrapper inside the existing one.
    assert 'id="pf-saa-root"' not in body
    assert 'id="saa-config-switcher"' not in body


# ---------------------------------------------------------------------------
# GET /api/saa/configuration/{config_id}/optimization
# ---------------------------------------------------------------------------


async def test_get_optimization_unknown_returns_404_partial(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(
        f"/api/saa/configuration/{uuid4()}/optimization",
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert "Configuration not found" in response.text


async def test_get_optimization_returns_plotly_spec(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    config_id = await _seed_two_asset_configuration(user_id)
    await _login(web_client, email, password)

    response = await web_client.get(
        f"/api/saa/configuration/{config_id}/optimization",
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # The HTMX partial wires Plotly + Tabulator.
    assert "Plotly.newPlot" in body
    assert "saa-frontier-chart" in body
    assert "saa-weights-table" in body
    # Sanity-check that the embedded JSON spec carries a couple of
    # well-known keys from the Plotly schema.
    assert '"data":' in body
    assert '"layout":' in body
    # Trace names from build_efficient_frontier_spec must surface in
    # the rendered JSON so the chart legend reads sensibly.
    assert "Tangency Portfolio" in body
    assert "Efficient Frontier" in body


async def test_get_optimization_with_one_asset_returns_400_partial(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    config_id = await _seed_one_asset_configuration(user_id)
    await _login(web_client, email, password)

    response = await web_client.get(
        f"/api/saa/configuration/{config_id}/optimization",
        follow_redirects=False,
    )
    assert response.status_code == 400
    body = response.text
    # The inline error partial — not a Plotly chart — is rendered.
    assert "Optimization could not run" in body
    assert "Plotly.newPlot" not in body
    # The validation message references the minimum input count.
    assert re.search(r"At least 2", body) is not None


# ---------------------------------------------------------------------------
# GET /api/saa/asset-classes — modal partial
# ---------------------------------------------------------------------------


async def test_get_asset_classes_modal_renders(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _seed_two_asset_configuration(user_id)
    await _login(web_client, email, password)

    response = await web_client.get("/api/saa/asset-classes", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    # Modal-content markers — table mount, create-form, bootstrap JSON.
    assert "saa-asset-classes-table" in body
    assert "saa-new-ac-form" in body
    assert "saa-asset-classes-bootstrap" in body
    # The seeded asset-class display names are projected into the JSON
    # bootstrap that the modal's Tabulator consumes.
    assert "Equities" in body
    assert "Bonds" in body
