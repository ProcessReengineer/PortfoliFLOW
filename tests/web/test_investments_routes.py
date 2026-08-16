# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the Phase-4b investment read surface.

Live-DB tests against the compose Postgres. The fixtures seed the
sentinel tenant and a sentinel-tenant user, the per-test client is
bound to the FastAPI app via :class:`ASGITransport`, and each test
seeds investments / NAVs / cashflows directly via the repository
layer so the assertions are independent of the write-route code that
``test_investments_write_routes.py`` exercises separately.

Coverage targets:

* ``GET /investments`` requires authentication.
* ``GET /investments`` renders the catalogue.
* Filter query params (``type``, ``asset_class_id``, ``active_only``).
* ``GET /investments/new`` renders an empty form.
* ``GET /investments/{id}`` 404 for unknown id.
* ``GET /investments/{id}`` renders detail with NAV chart + cashflow
  table.
* ``GET /investments/{id}/edit`` renders the edit form.
* Cross-tenant isolation: foreign-tenant ids surface as 404.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import AsyncGenerator
from datetime import date, datetime, timezone
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
    InvestmentCashflowRepository,
    InvestmentNavRepository,
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
            "skipping live-DB investment route tests.",
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
    email = "investments-read@example.com"
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


async def _seed_investment(
    user_id: UUID,
    *,
    name: str = "Seed Fund",
    investment_type: str = "private_equity",
    tenant_id: UUID = SENTINEL_TENANT_ID,
    is_active: bool = True,
    nav_rows: list[tuple[date, str, str]] | None = None,
    cashflow_rows: list[tuple[datetime, str, str, str]] | None = None,
) -> tuple[UUID, UUID]:
    """Seed an investment + asset class; returns (investment_id, asset_class_id)."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as session:
            ac_repo = AssetClassRepository(session)
            existing_acs = await ac_repo.list_all()
            existing_codes = {ac.code for ac in existing_acs}
            ac_code = f"ac-{name.lower().replace(' ', '-')}"
            if ac_code in existing_codes:
                ac = next(a for a in existing_acs if a.code == ac_code)
            else:
                ac = await ac_repo.create(
                    code=ac_code,
                    display_name=f"AC {name}",
                )
            inv = await InvestmentRepository(session).create(
                name=name,
                investment_type=investment_type,
                asset_class_id=ac.id,
                currency="EUR",
                created_by=user_id,
                is_active=is_active,
            )
            if nav_rows:
                navs = InvestmentNavRepository(session)
                for as_of, kind, value in nav_rows:
                    await navs.upsert(
                        investment_id=inv.id,
                        as_of_date=as_of,
                        nav_kind=kind,
                        nav_value=Decimal(value),
                        currency="EUR",
                        source=None,
                        created_by=user_id,
                    )
            if cashflow_rows:
                cf = InvestmentCashflowRepository(session)
                for ts, ftype, fkind, amount in cashflow_rows:
                    await cf.create(
                        investment_id=inv.id,
                        flow_timestamp=ts,
                        flow_type=ftype,
                        flow_kind=fkind,
                        amount=Decimal(amount),
                        currency="EUR",
                        description=None,
                        created_by=user_id,
                    )
            return inv.id, ac.id
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# GET /investments
# ---------------------------------------------------------------------------


async def test_get_investments_unauthenticated_redirects_to_login(
    web_client: AsyncClient,
) -> None:
    response = await web_client.get("/investments", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_get_investments_empty_list_renders(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/investments", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert "Investments" in body
    assert "No investments match the current filters" in body


async def test_get_investments_renders_seeded_rows(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _seed_investment(user_id, name="Seed Fund A")
    await _seed_investment(user_id, name="Seed Fund B", investment_type="real_estate")
    await _login(web_client, email, password)

    response = await web_client.get("/investments", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert "Seed Fund A" in body
    assert "Seed Fund B" in body
    assert "investments-table" in body


async def test_get_investments_filters_by_type(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _seed_investment(user_id, name="PE Fund", investment_type="private_equity")
    await _seed_investment(user_id, name="RE Fund", investment_type="real_estate")
    await _login(web_client, email, password)

    response = await web_client.get("/investments?type=real_estate", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    # The Tabulator data payload only contains the matching row.
    assert '"name": "RE Fund"' in body
    assert '"name": "PE Fund"' not in body


async def test_get_investments_filters_by_asset_class(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    _inv_a, ac_a = await _seed_investment(user_id, name="Fund Alpha")
    _inv_b, _ac_b = await _seed_investment(user_id, name="Fund Beta")
    await _login(web_client, email, password)

    response = await web_client.get(f"/investments?asset_class_id={ac_a}", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert '"name": "Fund Alpha"' in body
    assert '"name": "Fund Beta"' not in body


async def test_get_investments_active_only_filter(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _seed_investment(user_id, name="Live Fund", is_active=True)
    await _seed_investment(user_id, name="Soft Deleted", is_active=False)
    await _login(web_client, email, password)

    full = await web_client.get("/investments", follow_redirects=False)
    assert '"name": "Live Fund"' in full.text
    assert '"name": "Soft Deleted"' in full.text

    active_only = await web_client.get("/investments?active_only=true", follow_redirects=False)
    assert '"name": "Live Fund"' in active_only.text
    assert '"name": "Soft Deleted"' not in active_only.text


async def test_get_investments_rejects_unknown_type_filter(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/investments?type=NOT_A_TYPE", follow_redirects=False)
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# GET /investments/new
# ---------------------------------------------------------------------------


async def test_get_investments_new_renders_empty_form(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/investments/new", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert "New Investment" in body
    # The form posts to /investments.
    assert 'action="/investments"' in body
    # The CSRF token appears as a hidden input.
    assert 'name="csrf_token"' in body


# ---------------------------------------------------------------------------
# GET /investments/{id}
# ---------------------------------------------------------------------------


async def test_get_investment_detail_unknown_returns_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(f"/investments/{uuid4()}", follow_redirects=False)
    assert response.status_code == 404


async def test_get_investment_detail_renders_with_navs_and_cashflows(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac_id = await _seed_investment(
        user_id,
        name="Detail Fund",
        nav_rows=[
            (date(2025, 6, 30), "actual", "1000.00"),
            (date(2025, 12, 31), "actual", "1100.00"),
            (date(2025, 12, 31), "plan", "1200.00"),
        ],
        cashflow_rows=[
            (
                datetime(2025, 3, 15, 12, 0, tzinfo=timezone.utc),
                "capital_call",
                "actual",
                "-500.00",
            ),
            (
                datetime(2025, 9, 30, 12, 0, tzinfo=timezone.utc),
                "distribution",
                "actual",
                "200.00",
            ),
        ],
    )
    await _login(web_client, email, password)

    response = await web_client.get(f"/investments/{inv_id}", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert "Detail Fund" in body
    # NAV chart container is wired to Plotly.
    assert "nav-chart" in body
    assert "Plotly.newPlot" in body
    # Cashflow Tabulator hook is rendered.
    assert "cashflows-table" in body
    # NAV values appear in the embedded JSON payload.
    assert "1000" in body
    assert "1100" in body
    # The two cashflow types appear too.
    assert "capital_call" in body
    assert "distribution" in body


async def test_get_investment_edit_unknown_returns_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(f"/investments/{uuid4()}/edit", follow_redirects=False)
    assert response.status_code == 404


async def test_get_investment_edit_renders_form_with_values(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    inv_id, _ac_id = await _seed_investment(user_id, name="Editable Fund")
    await _login(web_client, email, password)

    response = await web_client.get(f"/investments/{inv_id}/edit", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert "Editable Fund" in body
    assert f'action="/investments/{inv_id}"' in body
    assert 'data-mode="PUT"' in body


# ---------------------------------------------------------------------------
# Cross-tenant isolation on reads
# ---------------------------------------------------------------------------


async def test_get_foreign_tenant_investment_returns_404(
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
            {"id": str(other_tenant_id), "name": "Foreign Tenant Read"},
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
                "email": "foreign-read@example.com",
                "hash": hash_password("xxx"),
            },
        )
    foreign_inv_id, _ac_id = await _seed_investment(
        other_user_id,
        name="Foreign Fund",
        tenant_id=other_tenant_id,
    )

    await _login(web_client, email, password)
    # The sentinel-tenant user cannot read the foreign-tenant
    # investment — RLS hides it, the route maps absence to 404.
    response = await web_client.get(f"/investments/{foreign_inv_id}", follow_redirects=False)
    assert response.status_code == 404

    # Same-tenant list view never shows the foreign row.
    list_response = await web_client.get("/investments", follow_redirects=False)
    assert '"name": "Foreign Fund"' not in list_response.text
