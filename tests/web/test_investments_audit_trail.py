# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Audit-trail verification for the Phase-4b investment write surface.

Per ADR-0035 §6 and ADR-0036 §1d, every domain-table write must
produce an ``audit_log`` row with:

* ``tenant_id`` correctly populated (not NULL),
* ``user_id`` correctly populated (not NULL — read from the
  ``app.user_id`` GUC set by ``tenant_context`` in the route handler).

This file exercises every Phase-4b investment write path through the
authenticated web surface and asserts the audit trail captures the
acting user. Coverage spans all 11 mutating routes:

* ``POST   /investments``                                 — INSERT on investments.
* ``PUT    /investments/{id}``                            — UPDATE on investments.
* ``DELETE /investments/{id}``                            — DELETE on investments.
* ``PATCH  /investments/{id}/active``                     — UPDATE on investments.
* ``POST   /investments/{id}/navs``                       — INSERT on investment_navs.
* ``PUT    /investments/{id}/navs/{nav_id}``              — UPDATE on investment_navs.
* ``DELETE /investments/{id}/navs/{nav_id}``              — DELETE on investment_navs.
* ``POST   /investments/{id}/cashflows``                  — INSERT on investment_cashflows.
* ``PUT    /investments/{id}/cashflows/{cashflow_id}``    — UPDATE on investment_cashflows.
* ``DELETE /investments/{id}/cashflows/{cashflow_id}``    — DELETE on investment_cashflows.

Plus an explicit pair-test for soft-delete vs reactivate via PATCH
to confirm both directions write audit rows.
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
    InvestmentRepository,
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
            "skipping live-DB investment audit-trail tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fixtures (parallel to test_investments_write_routes.py)
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
    email = "investments-audit@example.com"
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
    get_response = await client.get("/login")
    pre_csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert pre_csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": pre_csrf},
        follow_redirects=False,
    )
    page = await client.get("/investments", follow_redirects=False)
    assert page.status_code == 200
    body = page.text
    marker = 'name="csrf-token" content="'
    idx = body.find(marker)
    assert idx != -1
    start = idx + len(marker)
    end = body.find('"', start)
    return body[start:end]


async def _seed_asset_class(
    user_id: UUID,
    code: str = "ac-default",
    display_name: str = "Default AC",
) -> UUID:
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            ac = await AssetClassRepository(session).create(
                code=code,
                display_name=display_name,
            )
            return ac.id
    finally:
        await engine.dispose()


async def _seed_investment(
    user_id: UUID,
    asset_class_id: UUID,
    *,
    name: str = "Audit Fund",
) -> UUID:
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            inv = await InvestmentRepository(session).create(
                name=name,
                investment_type="private_equity",
                asset_class_id=asset_class_id,
                currency="EUR",
                created_by=user_id,
            )
            return inv.id
    finally:
        await engine.dispose()


async def _latest_audit_row(
    superuser_engine: AsyncEngine,
    table_name: str,
    record_id: UUID | None = None,
    operation: str | None = None,
) -> dict[str, object] | None:
    sql = (
        "SELECT tenant_id, user_id, table_name, operation, record_id, "
        "created_at FROM audit_log WHERE table_name = :tn"
    )
    params: dict[str, object] = {"tn": table_name}
    if record_id is not None:
        sql += " AND record_id = :rid"
        params["rid"] = str(record_id)
    if operation is not None:
        sql += " AND operation = :op"
        params["op"] = operation
    sql += " ORDER BY created_at DESC LIMIT 1"
    async with superuser_engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        row = result.mappings().first()
        return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# investments — POST / PUT / DELETE / PATCH active
# ---------------------------------------------------------------------------


async def test_post_investment_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id, "ac-create")
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/investments",
        data={
            "name": "AuditCreate",
            "investment_type": "private_equity",
            "asset_class_id": str(ac_id),
            "currency": "EUR",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    row = await _latest_audit_row(superuser_engine, "investments", operation="INSERT")
    assert row is not None
    assert row["tenant_id"] == SENTINEL_TENANT_ID
    assert row["user_id"] == user_id


async def test_put_investment_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id, "ac-update")
    inv_id = await _seed_investment(user_id, ac_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.put(
        f"/investments/{inv_id}",
        json={"manager_name": "Renamed Manager"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200

    row = await _latest_audit_row(
        superuser_engine, "investments", record_id=inv_id, operation="UPDATE"
    )
    assert row is not None
    assert row["tenant_id"] == SENTINEL_TENANT_ID
    assert row["user_id"] == user_id


async def test_delete_investment_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id, "ac-delete")
    inv_id = await _seed_investment(user_id, ac_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.request(
        "DELETE", f"/investments/{inv_id}", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200

    row = await _latest_audit_row(
        superuser_engine, "investments", record_id=inv_id, operation="DELETE"
    )
    assert row is not None
    assert row["tenant_id"] == SENTINEL_TENANT_ID
    assert row["user_id"] == user_id


async def test_patch_active_soft_delete_and_reactivate_audit(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id, "ac-patch")
    inv_id = await _seed_investment(user_id, ac_id)
    csrf = await _login_and_csrf(web_client, email, password)

    deactivate = await web_client.patch(
        f"/investments/{inv_id}/active",
        json={"is_active": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert deactivate.status_code == 200

    soft_delete_row = await _latest_audit_row(
        superuser_engine, "investments", record_id=inv_id, operation="UPDATE"
    )
    assert soft_delete_row is not None
    assert soft_delete_row["user_id"] == user_id

    reactivate = await web_client.patch(
        f"/investments/{inv_id}/active",
        json={"is_active": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert reactivate.status_code == 200

    reactivate_row = await _latest_audit_row(
        superuser_engine, "investments", record_id=inv_id, operation="UPDATE"
    )
    assert reactivate_row is not None
    assert reactivate_row["user_id"] == user_id
    # The reactivate write is the most recent UPDATE.
    assert reactivate_row["created_at"] >= soft_delete_row["created_at"]


# ---------------------------------------------------------------------------
# investment_navs — POST / PUT / DELETE
# ---------------------------------------------------------------------------


async def test_post_nav_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id, "ac-nav-create")
    inv_id = await _seed_investment(user_id, ac_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        f"/investments/{inv_id}/navs",
        json={
            "as_of_date": "2025-12-31",
            "nav_value": "1000.00",
            "currency": "EUR",
            "nav_kind": "actual",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200

    row = await _latest_audit_row(superuser_engine, "investment_navs", operation="INSERT")
    assert row is not None
    assert row["tenant_id"] == SENTINEL_TENANT_ID
    assert row["user_id"] == user_id


async def test_put_nav_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id, "ac-nav-update")
    inv_id = await _seed_investment(user_id, ac_id)
    csrf = await _login_and_csrf(web_client, email, password)

    add = await web_client.post(
        f"/investments/{inv_id}/navs",
        json={
            "as_of_date": "2025-12-31",
            "nav_value": "1000.00",
            "currency": "EUR",
            "nav_kind": "actual",
        },
        headers={"X-CSRF-Token": csrf},
    )
    nav_id = UUID(add.json()["id"])

    update = await web_client.put(
        f"/investments/{inv_id}/navs/{nav_id}",
        json={"nav_value": "1100.00", "currency": "EUR"},
        headers={"X-CSRF-Token": csrf},
    )
    assert update.status_code == 200

    row = await _latest_audit_row(
        superuser_engine, "investment_navs", record_id=nav_id, operation="UPDATE"
    )
    assert row is not None
    assert row["user_id"] == user_id


async def test_delete_nav_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id, "ac-nav-delete")
    inv_id = await _seed_investment(user_id, ac_id)
    csrf = await _login_and_csrf(web_client, email, password)

    add = await web_client.post(
        f"/investments/{inv_id}/navs",
        json={
            "as_of_date": "2025-12-31",
            "nav_value": "1000.00",
            "currency": "EUR",
            "nav_kind": "actual",
        },
        headers={"X-CSRF-Token": csrf},
    )
    nav_id = UUID(add.json()["id"])

    response = await web_client.request(
        "DELETE",
        f"/investments/{inv_id}/navs/{nav_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200

    row = await _latest_audit_row(
        superuser_engine, "investment_navs", record_id=nav_id, operation="DELETE"
    )
    assert row is not None
    assert row["user_id"] == user_id


# ---------------------------------------------------------------------------
# investment_cashflows — POST / PUT / DELETE
# ---------------------------------------------------------------------------


async def test_post_cashflow_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id, "ac-cf-create")
    inv_id = await _seed_investment(user_id, ac_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        f"/investments/{inv_id}/cashflows",
        json={
            "flow_date": "2025-09-30",
            "flow_type": "distribution",
            "flow_kind": "actual",
            "amount": "200.00",
            "currency": "EUR",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200

    row = await _latest_audit_row(superuser_engine, "investment_cashflows", operation="INSERT")
    assert row is not None
    assert row["tenant_id"] == SENTINEL_TENANT_ID
    assert row["user_id"] == user_id


async def test_put_cashflow_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id, "ac-cf-update")
    inv_id = await _seed_investment(user_id, ac_id)
    csrf = await _login_and_csrf(web_client, email, password)

    add = await web_client.post(
        f"/investments/{inv_id}/cashflows",
        json={
            "flow_date": "2025-09-30",
            "flow_type": "distribution",
            "flow_kind": "actual",
            "amount": "200.00",
            "currency": "EUR",
        },
        headers={"X-CSRF-Token": csrf},
    )
    cf_id = UUID(add.json()["id"])

    update = await web_client.put(
        f"/investments/{inv_id}/cashflows/{cf_id}",
        json={"amount": "250.00"},
        headers={"X-CSRF-Token": csrf},
    )
    assert update.status_code == 200

    row = await _latest_audit_row(
        superuser_engine,
        "investment_cashflows",
        record_id=cf_id,
        operation="UPDATE",
    )
    assert row is not None
    assert row["user_id"] == user_id


async def test_delete_cashflow_writes_audit_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id, "ac-cf-delete")
    inv_id = await _seed_investment(user_id, ac_id)
    csrf = await _login_and_csrf(web_client, email, password)

    add = await web_client.post(
        f"/investments/{inv_id}/cashflows",
        json={
            "flow_date": "2025-09-30",
            "flow_type": "distribution",
            "flow_kind": "actual",
            "amount": "200.00",
            "currency": "EUR",
        },
        headers={"X-CSRF-Token": csrf},
    )
    cf_id = UUID(add.json()["id"])

    response = await web_client.request(
        "DELETE",
        f"/investments/{inv_id}/cashflows/{cf_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200

    row = await _latest_audit_row(
        superuser_engine,
        "investment_cashflows",
        record_id=cf_id,
        operation="DELETE",
    )
    assert row is not None
    assert row["user_id"] == user_id
