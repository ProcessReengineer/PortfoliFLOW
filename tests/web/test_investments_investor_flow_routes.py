# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for investor-flow booking (ADR-0103 §5).

The route's own job is small and this suite pins exactly that much: it
accepts ``'investor_flow'`` as an eighth ``flow_type`` (it is in
``_VALID_FLOW_TYPES``), and it renders the service's typed
:class:`core.exceptions.InvestorFlowScopeError` as a structured **400**
rather than letting it escape as a 500.

The cash-only rule itself is *not* re-tested here — it lives at the service
seam (``tests/services/test_investment_service_investor_flow.py``) and a
second formulation in the route would be exactly the drift ADR-0103 §5
warns against. What is tested here is that the rule *reaches the operator*
correctly through HTTP.

Live-DB tests against the compose Postgres so RLS, the audit triggers, and
the b028 CHECK constraint evaluate exactly as in production.
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
            "skipping live-DB investor-flow route tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fixtures (the house idiom of tests/web/test_investments_write_routes.py)
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
    email = "investor-flow-routes@example.com"
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, 'minathena-capital') "
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


async def _seed_investment(
    user_id: UUID,
    *,
    name: str,
    investment_type: str,
) -> UUID:
    """Create one investment of the given type; returns its id."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            ac = await AssetClassRepository(session).create(
                code=f"ac-{name.lower().replace(' ', '-')}",
                display_name=f"AC {name}",
            )
            inv = await InvestmentRepository(session).create(
                name=name,
                investment_type=investment_type,
                asset_class_id=ac.id,
                currency="EUR",
                created_by=user_id,
            )
            return inv.id
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# POST /investments/{id}/cashflows
# ---------------------------------------------------------------------------


async def test_post_investor_flow_on_cash_succeeds(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """The eighth flow type is accepted by the route on a cash position."""
    user_id, email, password = seeded_user
    cash_id = await _seed_investment(user_id, name="Cash EUR", investment_type="cash")
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        f"/investments/{cash_id}/cashflows",
        json={
            "flow_date": "2025-09-30",
            "flow_type": "investor_flow",
            "flow_kind": "plan",
            "amount": "500000.00",
            "currency": "EUR",
            "description": "Planned investor contribution",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["flow_type"] == "investor_flow"
    assert body["flow_kind"] == "plan"
    assert body["amount"] == 500000.0


async def test_post_investor_flow_on_non_cash_returns_400(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """The service's typed error reaches the operator as a 400, not a 500."""
    user_id, email, password = seeded_user
    fund_id = await _seed_investment(user_id, name="PE Fund", investment_type="private_equity")
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        f"/investments/{fund_id}/cashflows",
        json={
            "flow_date": "2025-09-30",
            "flow_type": "investor_flow",
            "flow_kind": "actual",
            "amount": "500000.00",
            "currency": "EUR",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["field"] == "flow_type"
    # The message names the remedy, not just the refusal (ADR-0008 English).
    assert "cash position" in body["error"]


# ---------------------------------------------------------------------------
# PUT /investments/{id}/cashflows/{cashflow_id}
# ---------------------------------------------------------------------------


async def test_put_cashflow_to_investor_flow_on_non_cash_returns_400(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """Re-typing a fund's cashflow to ``investor_flow`` is a 400, not a 500."""
    user_id, email, password = seeded_user
    fund_id = await _seed_investment(user_id, name="PE Fund", investment_type="private_equity")
    csrf = await _login_and_csrf(web_client, email, password)

    add = await web_client.post(
        f"/investments/{fund_id}/cashflows",
        json={
            "flow_date": "2025-09-30",
            "flow_type": "capital_call",
            "flow_kind": "actual",
            "amount": "-1000.00",
            "currency": "EUR",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert add.status_code == 200
    cashflow_id = add.json()["id"]

    response = await web_client.put(
        f"/investments/{fund_id}/cashflows/{cashflow_id}",
        json={"flow_type": "investor_flow"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert response.json()["field"] == "flow_type"


async def test_put_cashflow_to_investor_flow_on_cash_succeeds(
    web_client: AsyncClient, seeded_user: tuple[UUID, str, str]
) -> None:
    """The same re-type on a cash position is permitted."""
    user_id, email, password = seeded_user
    cash_id = await _seed_investment(user_id, name="Cash EUR", investment_type="cash")
    csrf = await _login_and_csrf(web_client, email, password)

    add = await web_client.post(
        f"/investments/{cash_id}/cashflows",
        json={
            "flow_date": "2025-09-30",
            "flow_type": "other",
            "flow_kind": "actual",
            "amount": "-42.00",
            "currency": "EUR",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert add.status_code == 200
    cashflow_id = add.json()["id"]

    response = await web_client.put(
        f"/investments/{cash_id}/cashflows/{cashflow_id}",
        json={"flow_type": "investor_flow", "amount": "-80000.00"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["flow_type"] == "investor_flow"
    # Withdrawals are negative; no sign constraint exists for this type.
    assert body["amount"] == -80000.0
