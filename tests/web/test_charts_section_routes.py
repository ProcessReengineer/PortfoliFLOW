# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the embedded Charts section endpoints.

Live-DB tests against the compose Postgres, mirroring the existing
``test_statistics_section_routes.py`` shape. The fixtures seed the
sentinel tenant plus a sentinel-tenant user; the per-test client is
bound via ``ASGITransport``; investments and NAVs / cashflows are
seeded inline via the Phase-4 repositories.

Coverage targets — sub-stream 6F-4:

* ``GET /api/charts/section`` requires authentication.
* The empty-universe path returns the empty-state copy.
* Two seeded investments render two articles, each with a
  per-investment lazy-loader pointing at
  ``/api/charts/investment/{id}``.
* ``GET /api/charts/investment/{id}`` returns the three-chart
  fragment with parseable ``data-spec`` JSON.
* Unknown ``investment_id`` resolves to 404.
* ``GET /front-office`` carries the charts lazy shell instead of
  the old placeholder string.
* No German tokens leak into the rendered fragment.
"""

from __future__ import annotations

import html
import json
import os
import pathlib
import re
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
            "skipping live-DB charts section tests.",
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
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE investment_navs, investment_cashflows, "
                "investment_country_weights, investment_sector_weights, "
                "investments, asset_classes, "
                "data_upload_sheets, data_uploads, "
                "login_audit, sessions, audit_log, "
                "data_store_entries, users, tenants "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest_asyncio.fixture
async def seeded_user(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "charts-section@example.com"
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
    """Drive ``GET /login`` + ``POST /login`` to seat the session cookie."""
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Inline universe seeding
# ---------------------------------------------------------------------------


_TWO_INVESTMENTS: tuple[str, ...] = ("Investment A", "Investment B")


async def _seed_two_investments(
    actor_id: UUID,
) -> None:
    """Seed two investments with NAVs and at least one cashflow each.

    The Cashflows tile requires both NAV history and a capital call to
    produce non-trivial output; without cashflows the chart bundle
    still renders but the spec is essentially empty.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            ac_repo = AssetClassRepository(session)
            asset_class = await ac_repo.create(
                code="charts_section_class",
                display_name="Charts Section Class",
            )
            inv_repo = InvestmentRepository(session)
            nav_repo = InvestmentNavRepository(session)
            cf_repo = InvestmentCashflowRepository(session)
            for name in _TWO_INVESTMENTS:
                inv = await inv_repo.create(
                    name=name,
                    investment_type="private_equity",
                    asset_class_id=asset_class.id,
                    currency="EUR",
                    created_by=actor_id,
                )
                for as_of, value in (
                    (date(2024, 12, 31), Decimal("100")),
                    (date(2025, 3, 31), Decimal("110")),
                    (date(2025, 6, 30), Decimal("130")),
                ):
                    await nav_repo.upsert(
                        investment_id=inv.id,
                        as_of_date=as_of,
                        nav_kind="actual",
                        nav_value=value,
                        currency="EUR",
                        source=None,
                        created_by=actor_id,
                    )
                await cf_repo.create(
                    investment_id=inv.id,
                    flow_timestamp=datetime(2024, 1, 31, 12, 0, tzinfo=timezone.utc),
                    flow_type="capital_call",
                    flow_kind="actual",
                    amount=Decimal("-100"),
                    currency="EUR",
                    description=None,
                    created_by=actor_id,
                )
                await cf_repo.create(
                    investment_id=inv.id,
                    flow_timestamp=datetime(2025, 6, 30, 12, 0, tzinfo=timezone.utc),
                    flow_type="distribution",
                    flow_kind="actual",
                    amount=Decimal("30"),
                    currency="EUR",
                    description=None,
                    created_by=actor_id,
                )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_section_endpoint_requires_auth(
    web_client: AsyncClient,
) -> None:
    """``require_session`` redirects unauthenticated callers.

    Plain GET requests get 303; HTMX requests get 401 + ``HX-Redirect``.
    """
    response = await web_client.get("/api/charts/section", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_section_empty_universe_returns_empty_copy(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/charts/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "No active investments to chart" in response.text


async def test_section_renders_one_article_per_active_investment(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)

    response = await web_client.get(
        "/api/charts/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # Both names appear as article titles.
    assert "Investment A" in body
    assert "Investment B" in body
    # Two per-investment lazy-loaders pointing at the right URL.
    assert body.count('hx-get="/api/charts/investment/') == 2
    # Both use hx-trigger="revealed".
    assert body.count('hx-trigger="revealed"') >= 2


async def test_investment_triplet_returns_three_plotly_targets(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)

    # First fetch the section to discover the investment IDs.
    section_response = await web_client.get(
        "/api/charts/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    ids = re.findall(
        r'hx-get="/api/charts/investment/([0-9a-f-]+)"',
        section_response.text,
    )
    assert len(ids) >= 1
    investment_id = ids[0]

    response = await web_client.get(
        f"/api/charts/investment/{investment_id}",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # Three plotly-target containers, each with a data-spec attribute.
    assert body.count('class="ch-chart plotly-target"') == 3
    assert body.count("data-spec=") == 3
    # Each container has a unique id rooted in the investment id.
    assert f'id="ch-tr-{investment_id}"' in body
    assert f'id="ch-cn-{investment_id}"' in body
    assert f'id="ch-mp-{investment_id}"' in body


async def test_investment_triplet_unknown_id_renders_neutral_empty_state(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Unknown / cross-tenant id → neutral empty state with HTTP 200.

    A 404 would leak whether the row exists in another tenant; per
    ADR-0082 (ADR-0073 precedent) the route returns a neutral
    empty-state fragment with HTTP 200 instead and renders no Plotly
    targets.
    """
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)
    bogus_id = "00000000-0000-0000-0000-000000000bad"
    response = await web_client.get(
        f"/api/charts/investment/{bogus_id}",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert 'data-tile-count="0"' in body
    assert "plotly-target" not in body


async def test_investment_triplet_data_spec_parses_as_json(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)

    section_response = await web_client.get(
        "/api/charts/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    ids = re.findall(
        r'hx-get="/api/charts/investment/([0-9a-f-]+)"',
        section_response.text,
    )
    assert ids, "section endpoint did not expose any investment ids"
    investment_id = ids[0]
    triplet_response = await web_client.get(
        f"/api/charts/investment/{investment_id}",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert triplet_response.status_code == 200
    # Extract the three data-spec values (HTML-attr-escaped JSON).
    specs = re.findall(
        r"data-spec='([^']+)'",
        triplet_response.text,
    )
    assert len(specs) == 3
    for spec_attr in specs:
        unescaped = html.unescape(spec_attr)
        parsed = json.loads(unescaped)
        assert "data" in parsed
        assert "layout" in parsed


async def test_front_office_charts_section_now_carries_lazy_shell(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/front-office", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    # The lazy shell is now in place.
    assert 'hx-get="/api/charts/section"' in body
    # Old placeholder text is gone.
    assert "6F-4 re-renders this" not in body


async def test_section_article_header_includes_asset_class_name(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Each article header pairs the investment name with its asset class.

    The shared seed fixture installs both investments against an
    asset class whose ``display_name`` is ``"Charts Section Class"``
    (see :func:`_seed_two_investments`). The rendered header should
    read ``"Investment A, Charts Section Class"`` (and the analogue
    for Investment B) — covering the A6a contract.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)

    response = await web_client.get(
        "/api/charts/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # Whitespace-tolerant match: header content sits inside an <h3>
    # and the template uses block-level indentation between the
    # investment name and the asset-class label.
    assert re.search(r"Investment A,\s+Charts Section Class", body), (
        "Investment A header is missing the asset-class label"
    )
    assert re.search(r"Investment B,\s+Charts Section Class", body), (
        "Investment B header is missing the asset-class label"
    )


async def test_no_german_strings_in_charts_section(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)

    response = await web_client.get(
        "/api/charts/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    text_lower = response.text.lower()
    for forbidden in ("bereich", "investmentliste", "übersicht", "ansicht"):
        assert forbidden not in text_lower, (
            f"German token leaked into Charts section: {forbidden!r}"
        )
