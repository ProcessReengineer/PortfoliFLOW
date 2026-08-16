# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the Phase-4b investment write surface.

Coverage targets:

* CSRF enforcement on every mutating route (POST / PUT / DELETE / PATCH).
* Validation: investment_type, currency, asset_class_id existence,
  flow_kind, nav_kind, flow_type.
* Round-trip: create → detail, update → fetch.
* Cross-tenant write isolation (404, never 403).
* Cascade on hard-delete and soft-delete-with-reactivation behaviour.
* NAV UPSERT semantics on add path.
* Cashflow add with ``flow_date`` fallback to 12:00 UTC.

Live-DB tests against the compose Postgres so RLS, audit triggers,
and the b006 CHECK constraints evaluate exactly as in production.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import AsyncGenerator
from datetime import date, datetime, timezone
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
            "skipping live-DB investment write-route tests.",
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
    email = "investments-write@example.com"
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
    """Log in and return the session-bound CSRF token."""
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
    *,
    code: str = "ac-default",
    display_name: str = "Default AC",
    tenant_id: UUID = SENTINEL_TENANT_ID,
) -> UUID:
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as session:
            ac = await AssetClassRepository(session).create(
                code=code,
                display_name=display_name,
            )
            return ac.id
    finally:
        await engine.dispose()


async def _seed_investment_via_repo(
    user_id: UUID,
    *,
    name: str = "Repo Seed Fund",
    tenant_id: UUID = SENTINEL_TENANT_ID,
    asset_class_id: UUID | None = None,
) -> tuple[UUID, UUID]:
    """Create an investment via the repo, returns (investment_id, ac_id)."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as session:
            ac_id = asset_class_id
            if ac_id is None:
                ac = await AssetClassRepository(session).create(
                    code=f"ac-{name.lower().replace(' ', '-')}",
                    display_name=f"AC {name}",
                )
                ac_id = ac.id
            inv = await InvestmentRepository(session).create(
                name=name,
                investment_type="private_equity",
                asset_class_id=ac_id,
                currency="EUR",
                created_by=user_id,
            )
            return inv.id, ac_id
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# CSRF-required surface — 4 baseline tests (POST, PUT, DELETE, PATCH)
# ---------------------------------------------------------------------------


async def test_post_investment_without_csrf_is_403(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id)
    await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        "/investments",
        data={
            "name": "No CSRF",
            "investment_type": "private_equity",
            "asset_class_id": str(ac_id),
            "currency": "EUR",
        },
        follow_redirects=False,
    )
    assert response.status_code == 403


async def test_put_investment_without_csrf_is_403(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    await _login_and_csrf(web_client, email, password)
    response = await web_client.put(
        f"/investments/{inv_id}",
        json={"name": "Renamed"},
    )
    assert response.status_code == 403


async def test_delete_investment_without_csrf_is_403(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    await _login_and_csrf(web_client, email, password)
    response = await web_client.request("DELETE", f"/investments/{inv_id}")
    assert response.status_code == 403


async def test_patch_active_without_csrf_is_403(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    await _login_and_csrf(web_client, email, password)
    response = await web_client.patch(
        f"/investments/{inv_id}/active",
        json={"is_active": False},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# POST /investments — create
# ---------------------------------------------------------------------------


async def test_post_investment_creates_and_redirects(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id, code="ac-pe", display_name="Private Equity")
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/investments",
        data={
            "name": "Created via web",
            "investment_type": "private_equity",
            "asset_class_id": str(ac_id),
            "currency": "EUR",
            "manager_name": "Acme Capital",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/investments/")

    # Detail page shows the new investment.
    location = response.headers["location"]
    detail = await web_client.get(location, follow_redirects=False)
    assert detail.status_code == 200
    assert "Created via web" in detail.text


async def test_post_investment_rejects_invalid_type(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/investments",
        data={
            "name": "Bad Type",
            "investment_type": "NOT_A_TYPE",
            "asset_class_id": str(ac_id),
            "currency": "EUR",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


async def test_post_investment_rejects_invalid_currency(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/investments",
        data={
            "name": "Bad Currency",
            "investment_type": "private_equity",
            "asset_class_id": str(ac_id),
            "currency": "EU",  # too short
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


async def test_post_investment_rejects_unknown_asset_class(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    _user_id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/investments",
        data={
            "name": "Unknown AC",
            "investment_type": "private_equity",
            "asset_class_id": str(uuid4()),
            "currency": "EUR",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


async def test_post_investment_duplicate_name_returns_409(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    ac_id = await _seed_asset_class(user_id)
    # First creation via repo establishes the name.
    await _seed_investment_via_repo(user_id, name="Duplicate Fund", asset_class_id=ac_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/investments",
        data={
            "name": "Duplicate Fund",
            "investment_type": "private_equity",
            "asset_class_id": str(ac_id),
            "currency": "EUR",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# PUT /investments/{id} — update
# ---------------------------------------------------------------------------


async def test_put_investment_updates_fields(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id, name="Editable Fund")
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.put(
        f"/investments/{inv_id}",
        json={
            "name": "Renamed Fund",
            "manager_name": "New Manager",
            "vintage_year": 2022,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed Fund"
    assert body["manager_name"] == "New Manager"
    assert body["vintage_year"] == 2022


async def test_put_investment_unknown_returns_404(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    _user_id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.put(
        f"/investments/{uuid4()}",
        json={"name": "Renamed"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 404


async def test_put_investment_invalid_type_returns_400(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.put(
        f"/investments/{inv_id}",
        json={"investment_type": "NOT_A_TYPE"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["field"] == "investment_type"


# ---------------------------------------------------------------------------
# DELETE /investments/{id}
# ---------------------------------------------------------------------------


async def test_delete_investment_cascade(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    # Add a NAV and a cashflow first so we can confirm cascade.
    add_nav = await web_client.post(
        f"/investments/{inv_id}/navs",
        json={
            "as_of_date": "2025-12-31",
            "nav_value": "1000.00",
            "currency": "EUR",
            "nav_kind": "actual",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert add_nav.status_code == 200
    add_cf = await web_client.post(
        f"/investments/{inv_id}/cashflows",
        json={
            "flow_date": "2025-12-31",
            "flow_type": "capital_call",
            "flow_kind": "actual",
            "amount": "-100.00",
            "currency": "EUR",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert add_cf.status_code == 200

    # Hard-delete.
    response = await web_client.request(
        "DELETE", f"/investments/{inv_id}", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200
    assert response.headers.get("HX-Redirect") == "/investments"

    # Verify cascade — NAVs and cashflows are gone.
    async with superuser_engine.connect() as conn:
        nav_count = (
            await conn.execute(
                text("SELECT COUNT(*) FROM investment_navs WHERE investment_id = :id"),
                {"id": str(inv_id)},
            )
        ).scalar_one()
        cf_count = (
            await conn.execute(
                text("SELECT COUNT(*) FROM investment_cashflows WHERE investment_id = :id"),
                {"id": str(inv_id)},
            )
        ).scalar_one()
    assert nav_count == 0
    assert cf_count == 0


async def test_delete_unknown_investment_returns_404(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    _user_id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.request(
        "DELETE", f"/investments/{uuid4()}", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /investments/{id}/active
# ---------------------------------------------------------------------------


async def test_patch_active_soft_delete_then_reactivate(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id, name="Toggleable")
    csrf = await _login_and_csrf(web_client, email, password)

    deactivate = await web_client.patch(
        f"/investments/{inv_id}/active",
        json={"is_active": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert deactivate.status_code == 200
    assert deactivate.json()["is_active"] is False

    detail = await web_client.get(f"/investments/{inv_id}", follow_redirects=False)
    assert "Inactive" in detail.text

    reactivate = await web_client.patch(
        f"/investments/{inv_id}/active",
        json={"is_active": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert reactivate.status_code == 200
    assert reactivate.json()["is_active"] is True


async def test_patch_active_invalid_body_returns_400(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.patch(
        f"/investments/{inv_id}/active",
        json={"is_active": "yes"},  # not a boolean
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /investments/{id}/navs
# ---------------------------------------------------------------------------


async def test_post_nav_adds_and_upserts_on_conflict(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    first = await web_client.post(
        f"/investments/{inv_id}/navs",
        json={
            "as_of_date": "2025-12-31",
            "nav_value": "1000.00",
            "currency": "EUR",
            "nav_kind": "actual",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert first.status_code == 200
    assert first.json()["nav_value"] == 1000.0

    # Re-post the same triple — UPSERT updates the value, returns same id.
    second = await web_client.post(
        f"/investments/{inv_id}/navs",
        json={
            "as_of_date": "2025-12-31",
            "nav_value": "1100.00",
            "currency": "EUR",
            "nav_kind": "actual",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["nav_value"] == 1100.0


async def test_post_nav_invalid_kind_returns_400(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        f"/investments/{inv_id}/navs",
        json={
            "as_of_date": "2025-12-31",
            "nav_value": "1000.00",
            "currency": "EUR",
            "nav_kind": "forecast",  # not allowed
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert response.json()["field"] == "nav_kind"


async def test_post_nav_against_unknown_investment_returns_404(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    _user_id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        f"/investments/{uuid4()}/navs",
        json={
            "as_of_date": "2025-12-31",
            "nav_value": "1000.00",
            "currency": "EUR",
            "nav_kind": "actual",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /investments/{id}/navs/{nav_id}
# ---------------------------------------------------------------------------


async def test_delete_nav_succeeds(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
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
    nav_id = add.json()["id"]

    response = await web_client.request(
        "DELETE",
        f"/investments/{inv_id}/navs/{nav_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json() == {"deleted": True}


# ---------------------------------------------------------------------------
# POST /investments/{id}/cashflows
# ---------------------------------------------------------------------------


async def test_post_cashflow_with_flow_date_synthesises_noon_utc(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
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
    body = response.json()
    # The route synthesised flow_timestamp at 12:00 UTC on the date.
    parsed = datetime.fromisoformat(body["flow_timestamp"])
    assert parsed.date() == date(2025, 9, 30)
    assert parsed.hour == 12
    assert parsed.minute == 0
    # tzinfo present and UTC
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)


async def test_post_cashflow_invalid_type_returns_400(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        f"/investments/{inv_id}/cashflows",
        json={
            "flow_date": "2025-09-30",
            "flow_type": "NOT_A_TYPE",
            "flow_kind": "actual",
            "amount": "200.00",
            "currency": "EUR",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert response.json()["field"] == "flow_type"


async def test_put_cashflow_updates_amount(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
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
    cf_id = add.json()["id"]

    response = await web_client.put(
        f"/investments/{inv_id}/cashflows/{cf_id}",
        json={"amount": "250.00"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json()["amount"] == 250.0


async def test_delete_cashflow_succeeds(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
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
    cf_id = add.json()["id"]

    response = await web_client.request(
        "DELETE",
        f"/investments/{inv_id}/cashflows/{cf_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json() == {"deleted": True}


# ---------------------------------------------------------------------------
# Cross-tenant isolation on writes
# ---------------------------------------------------------------------------


async def test_write_against_foreign_tenant_investment_returns_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """A logged-in sentinel user cannot write to a foreign tenant's investment."""
    _user_id, email, password = seeded_user
    other_tenant_id = uuid4()
    other_user_id = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, gen_random_uuid()::text)"
            ),
            {"id": str(other_tenant_id), "name": "Foreign Tenant Write"},
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
                "email": "foreign-write@example.com",
                "hash": hash_password("xxx"),
            },
        )
    foreign_inv_id, _ac = await _seed_investment_via_repo(
        other_user_id,
        name="Foreign Fund Write",
        tenant_id=other_tenant_id,
    )

    csrf = await _login_and_csrf(web_client, email, password)

    # PUT
    put_response = await web_client.put(
        f"/investments/{foreign_inv_id}",
        json={"name": "Renamed by Foreign User"},
        headers={"X-CSRF-Token": csrf},
    )
    assert put_response.status_code == 404

    # DELETE
    del_response = await web_client.request(
        "DELETE",
        f"/investments/{foreign_inv_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert del_response.status_code == 404

    # PATCH /active
    patch_response = await web_client.patch(
        f"/investments/{foreign_inv_id}/active",
        json={"is_active": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert patch_response.status_code == 404

    # POST /navs (a write that references the foreign investment)
    nav_response = await web_client.post(
        f"/investments/{foreign_inv_id}/navs",
        json={
            "as_of_date": "2025-12-31",
            "nav_value": "1.00",
            "currency": "EUR",
            "nav_kind": "actual",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert nav_response.status_code == 404


async def test_two_tenants_with_same_investment_name_no_conflict(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The UNIQUE constraint is per-tenant, not global."""
    user_id, email, password = seeded_user
    other_tenant_id = uuid4()
    other_user_id = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, gen_random_uuid()::text)"
            ),
            {"id": str(other_tenant_id), "name": "Same-Name Tenant"},
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
                "email": "same-name@example.com",
                "hash": hash_password("xxx"),
            },
        )
    # Foreign tenant has a fund called "Shared Name".
    await _seed_investment_via_repo(other_user_id, name="Shared Name", tenant_id=other_tenant_id)

    # Sentinel user can also create an investment called "Shared Name".
    ac_id = await _seed_asset_class(user_id, code="ac-shared")
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/investments",
        data={
            "name": "Shared Name",
            "investment_type": "private_equity",
            "asset_class_id": str(ac_id),
            "currency": "EUR",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


# ---------------------------------------------------------------------------
# Security identifiers (ADR-0096) — nested-resource routes on the detail page
# ---------------------------------------------------------------------------


async def test_post_identifier_adds_and_appears_in_panel(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    add = await web_client.post(
        f"/investments/{inv_id}/identifiers",
        json={"scheme": "preqin", "value": "pq-42"},
        headers={"X-CSRF-Token": csrf},
    )
    assert add.status_code == 200
    rows = add.json()["identifiers"]
    assert [(r["scheme"], r["value"]) for r in rows] == [("preqin", "PQ-42")]
    assert rows[0]["source"] == "manual"
    assert rows[0]["is_primary"] is False

    # The row is embedded in the detail-page panel data.
    detail = await web_client.get(f"/investments/{inv_id}")
    assert detail.status_code == 200
    assert "PQ-42" in detail.text


async def test_post_identifier_invalid_scheme_returns_400(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        f"/investments/{inv_id}/identifiers",
        json={"scheme": "sedol", "value": "0263494"},  # not in the closed set
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert response.json()["field"] == "scheme"


async def test_post_identifier_duplicate_returns_409(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    first = await web_client.post(
        f"/investments/{inv_id}/identifiers",
        json={"scheme": "isin", "value": "US0378331005"},
        headers={"X-CSRF-Token": csrf},
    )
    assert first.status_code == 200
    dup = await web_client.post(
        f"/investments/{inv_id}/identifiers",
        json={"scheme": "isin", "value": "US0378331005"},
        headers={"X-CSRF-Token": csrf},
    )
    assert dup.status_code == 409


async def test_set_primary_identifier_promotes(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    add = await web_client.post(
        f"/investments/{inv_id}/identifiers",
        json={"scheme": "ticker", "value": "ACME"},
        headers={"X-CSRF-Token": csrf},
    )
    identifier_id = add.json()["identifiers"][0]["id"]

    promote = await web_client.post(
        f"/investments/{inv_id}/identifiers/{identifier_id}/primary",
        headers={"X-CSRF-Token": csrf},
    )
    assert promote.status_code == 200
    rows = promote.json()["identifiers"]
    assert [r["is_primary"] for r in rows if r["id"] == identifier_id] == [True]


async def test_delete_identifier_succeeds(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    add = await web_client.post(
        f"/investments/{inv_id}/identifiers",
        json={"scheme": "ticker", "value": "ACME"},
        headers={"X-CSRF-Token": csrf},
    )
    identifier_id = add.json()["identifiers"][0]["id"]

    deleted = await web_client.request(
        "DELETE",
        f"/investments/{inv_id}/identifiers/{identifier_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert deleted.status_code == 200
    assert deleted.json()["identifiers"] == []


async def test_identifier_write_against_foreign_tenant_returns_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """Cross-tenant identifier writes resolve to 404 (never 403)."""
    _user_id, email, password = seeded_user
    other_tenant_id = uuid4()
    other_user_id = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, gen_random_uuid()::text)"
            ),
            {"id": str(other_tenant_id), "name": "Foreign Tenant Identifiers"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(other_user_id),
                "tid": str(other_tenant_id),
                "email": "foreign-ident@example.com",
                "hash": hash_password("xxx"),
            },
        )
    foreign_inv_id, _ac = await _seed_investment_via_repo(
        other_user_id, name="Foreign Ident Fund", tenant_id=other_tenant_id
    )

    csrf = await _login_and_csrf(web_client, email, password)

    # POST /identifiers against the foreign investment → 404.
    add = await web_client.post(
        f"/investments/{foreign_inv_id}/identifiers",
        json={"scheme": "isin", "value": "US0378331005"},
        headers={"X-CSRF-Token": csrf},
    )
    assert add.status_code == 404

    # DELETE against a random identifier id under the foreign investment → 404.
    deleted = await web_client.request(
        "DELETE",
        f"/investments/{foreign_inv_id}/identifiers/{uuid4()}",
        headers={"X-CSRF-Token": csrf},
    )
    assert deleted.status_code == 404


async def test_post_identifier_without_csrf_is_403(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac = await _seed_investment_via_repo(user_id)
    await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        f"/investments/{inv_id}/identifiers",
        json={"scheme": "isin", "value": "US0378331005"},
    )
    assert response.status_code == 403
