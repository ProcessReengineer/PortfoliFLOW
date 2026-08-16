# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Audit-trail verification for the SAA write surface.

Per ADR-0035 §6 and ADR-0036 §1d, every domain-table write must
produce an ``audit_log`` row with:

* ``tenant_id`` correctly populated (not NULL),
* ``user_id`` correctly populated (not NULL — read from the
  ``app.user_id`` GUC set by ``tenant_context`` in the route handler).

This file exercises every SAA write path through the authenticated
web surface and asserts the audit trail captures the acting user.
It complements ``tests/repositories/test_saa_audit_and_isolation.py``,
which exercises the same invariant at the repository layer rather
than through HTTP.

The fixture set is parallel to ``tests/web/test_saa_routes.py``: a
sentinel tenant, a single seeded user, and an HTTP client bound to
the FastAPI app via ``ASGITransport``. A second engine bound to the
Postgres superuser is used to query ``audit_log`` directly — the
``portfoliflow_app`` role's RLS policy on ``audit_log`` would scope
the query to the active tenant, but using the superuser engine keeps
the read paths obviously decoupled from the surface under test.

Routes live under ``/api/saa/*`` per ADR-0054 (roadmap A5).
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
            "skipping live-DB SAA audit-trail tests.",
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
    email = "saa-audit@example.com"
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


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """Log in and return the session-bound CSRF token."""
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    # Read the session-bound CSRF token by visiting an authed page —
    # the token is rendered into the meta tag of base.html. The SAA
    # section lives inside /back-office now, so we scrape from there.
    page = await client.get("/back-office", follow_redirects=False)
    assert page.status_code == 200
    body = page.text
    marker = 'name="csrf-token" content="'
    idx = body.find(marker)
    assert idx != -1, "csrf-token meta missing from authed page"
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
    user_id: UUID, name: str = "Audit Config"
) -> tuple[UUID, UUID, UUID]:
    """Seed a 2-asset configuration; returns (config_id, ac1_id, ac2_id)."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            svc = _build_service(session)
            ac1 = await svc.create_asset_class("equities-audit", "Equities Audit")
            ac2 = await svc.create_asset_class("bonds-audit", "Bonds Audit")
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
            return config.id, ac1.id, ac2.id
    finally:
        await engine.dispose()


async def _latest_audit_row(
    superuser_engine: AsyncEngine, table_name: str, record_id: UUID | None = None
) -> dict[str, object] | None:
    """Return the most recent ``audit_log`` row for ``table_name``.

    If ``record_id`` is supplied, scopes the lookup to a specific row;
    otherwise returns the latest write of any kind on the table. The
    superuser engine is used so the lookup bypasses RLS — we want to
    see whatever the trigger wrote regardless of the active tenant.
    """
    sql = (
        "SELECT tenant_id, user_id, table_name, operation, record_id, "
        "created_at FROM audit_log WHERE table_name = :tn"
    )
    params: dict[str, object] = {"tn": table_name}
    if record_id is not None:
        sql += " AND record_id = :rid"
        params["rid"] = str(record_id)
    sql += " ORDER BY created_at DESC LIMIT 1"
    async with superuser_engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        row = result.mappings().first()
        return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# IS-W-01 — POST /api/saa/configuration creates an audit row attributed to the user
# ---------------------------------------------------------------------------


async def test_post_configuration_creates_audit_row_with_user_id(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    csrf = await _login(web_client, email, password)

    response = await web_client.post(
        "/api/saa/configuration",
        data={
            "name": "AuditCreated",
            "risk_free_rate_pct": "2.50",
            "n_frontier_points": "100",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200

    row = await _latest_audit_row(superuser_engine, "saa_configurations")
    assert row is not None
    assert row["tenant_id"] == SENTINEL_TENANT_ID
    assert row["user_id"] == user_id
    assert row["operation"] == "INSERT"


# ---------------------------------------------------------------------------
# IS-W-02 — PUT /api/saa/configuration/{id} attributes input + correlation writes
# ---------------------------------------------------------------------------


async def test_put_saa_save_writes_audit_rows_with_user_id(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    config_id, ac1_id, ac2_id = await _seed_two_asset_configuration(user_id)
    csrf = await _login(web_client, email, password)

    payload = {
        "metadata": {
            "name": "Audit Save",
            "risk_free_rate": 0.03,
            "n_frontier_points": 50,
        },
        "inputs": [
            {
                "asset_class_id": str(ac1_id),
                "expected_return": 0.08,
                "volatility": 0.16,
                "min_weight": 0.0,
                "max_weight": 1.0,
            },
            {
                "asset_class_id": str(ac2_id),
                "expected_return": 0.04,
                "volatility": 0.06,
                "min_weight": 0.0,
                "max_weight": 1.0,
            },
        ],
        "correlations": [
            {
                "asset_class_a_id": str(ac1_id),
                "asset_class_b_id": str(ac2_id),
                "correlation": 0.2,
            },
        ],
    }
    response = await web_client.put(
        f"/api/saa/configuration/{config_id}",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200

    # Both child tables produced audit rows attributed to the user.
    for table in ("saa_asset_class_inputs", "saa_correlations"):
        row = await _latest_audit_row(superuser_engine, table)
        assert row is not None, f"no audit row for {table}"
        assert row["tenant_id"] == SENTINEL_TENANT_ID, table
        assert row["user_id"] == user_id, table

    # The metadata UPDATE on saa_configurations is also attributed.
    row = await _latest_audit_row(superuser_engine, "saa_configurations", record_id=config_id)
    assert row is not None
    assert row["user_id"] == user_id
    assert row["operation"] == "UPDATE"


# ---------------------------------------------------------------------------
# IS-W-03 — POST /api/saa/configuration/{id}/activate attributes the activation
# ---------------------------------------------------------------------------


async def test_post_saa_activate_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    config_id, _ac1_id, _ac2_id = await _seed_two_asset_configuration(user_id)
    csrf = await _login(web_client, email, password)

    response = await web_client.post(
        f"/api/saa/configuration/{config_id}/activate",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200

    row = await _latest_audit_row(superuser_engine, "saa_configurations", record_id=config_id)
    assert row is not None
    assert row["tenant_id"] == SENTINEL_TENANT_ID
    assert row["user_id"] == user_id
    assert row["operation"] == "UPDATE"


# ---------------------------------------------------------------------------
# IS-W-04 — DELETE /api/saa/configuration/{id} attributes the deletion
# ---------------------------------------------------------------------------


async def test_delete_saa_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    config_id, _ac1, _ac2 = await _seed_two_asset_configuration(user_id)
    csrf = await _login(web_client, email, password)

    response = await web_client.request(
        "DELETE",
        f"/api/saa/configuration/{config_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200

    row = await _latest_audit_row(superuser_engine, "saa_configurations", record_id=config_id)
    assert row is not None
    assert row["tenant_id"] == SENTINEL_TENANT_ID
    assert row["user_id"] == user_id
    assert row["operation"] == "DELETE"


# ---------------------------------------------------------------------------
# IS-W-05 — POST /api/saa/asset-classes attributes the create
# ---------------------------------------------------------------------------


async def test_post_asset_class_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    csrf = await _login(web_client, email, password)

    response = await web_client.post(
        "/api/saa/asset-classes",
        data={
            "code": "audit-ac-create",
            "display_name": "Audit Asset Class",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 200

    row = await _latest_audit_row(superuser_engine, "asset_classes")
    assert row is not None
    assert row["tenant_id"] == SENTINEL_TENANT_ID
    assert row["user_id"] == user_id
    assert row["operation"] == "INSERT"


# ---------------------------------------------------------------------------
# IS-W-06 — PUT /api/saa/asset-classes/{id} attributes the update
# ---------------------------------------------------------------------------


async def test_put_asset_class_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    _config_id, ac1_id, _ac2_id = await _seed_two_asset_configuration(user_id)
    csrf = await _login(web_client, email, password)

    response = await web_client.put(
        f"/api/saa/asset-classes/{ac1_id}",
        json={"display_name": "Equities Audit (renamed)"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200

    row = await _latest_audit_row(superuser_engine, "asset_classes", record_id=ac1_id)
    assert row is not None
    assert row["tenant_id"] == SENTINEL_TENANT_ID
    assert row["user_id"] == user_id
    assert row["operation"] == "UPDATE"


# ---------------------------------------------------------------------------
# IS-W-07 — DELETE /api/saa/asset-classes/{id} attributes the deletion
# ---------------------------------------------------------------------------


async def test_delete_unreferenced_asset_class_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    csrf = await _login(web_client, email, password)

    # Create an unreferenced asset class via the repository directly so
    # the test's pre-state is independent of the create path.
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            svc = _build_service(session)
            ac = await svc.create_asset_class("audit-ac-delete", "To Delete")
    finally:
        await engine.dispose()

    response = await web_client.request(
        "DELETE",
        f"/api/saa/asset-classes/{ac.id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200

    row = await _latest_audit_row(superuser_engine, "asset_classes", record_id=ac.id)
    assert row is not None
    assert row["tenant_id"] == SENTINEL_TENANT_ID
    assert row["user_id"] == user_id
    assert row["operation"] == "DELETE"
