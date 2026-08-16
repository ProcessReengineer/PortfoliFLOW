# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Structural validation of HTML-embedded JSON in ``data-spec`` attributes.

Phase-5 acceptance tests assert ``"data-spec" in body`` and
``"Plotly.newPlot" in body``. Both substring checks remain green
even when the embedded JSON is syntactically broken — for example
if an attribute is wrapped in double quotes and the ``tojson`` filter
emits the (correct) double-quoted JSON ``{"data": ...}``, the browser
parses the value as ``{`` and the rest of the line as malformed HTML.
The client-side ``Plotly.newPlot`` script catches the resulting
``JSON.parse`` failure inside a ``try/catch`` and the chart silently
stays blank — no console error, no failing assertion.

This test loads each chart-bearing surface, extracts every
``data-spec`` attribute (single- or double-quoted) from the response
body, and ``json.loads()`` the value. A regression to double-quoted
wrapping would truncate the payload at the first inner ``"`` and the
parse would fail loudly here. The structural ``"data" in spec`` /
``"layout" in spec`` assertions further catch any future template
that embeds something other than a Plotly figure dict in a
``data-spec`` attribute.

P6-K — see ``docs/phase-5-followups.md``. Sub-stream 6F-1 of Phase 6
Block 1 retired ``/statistics``, ``/portfolio-analysis`` and
``/portfolio-review`` (ADR-0046); the structural-JSON test for the
surviving ``/investments/{id}/charts`` surface keeps the invariant
alive until 6F-3 lifts the chart-bearing sections into the area
shell, at which point per-area tests pick the slack back up.
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


_DATA_SPEC_PATTERN = re.compile(r"data-spec=(['\"])(.*?)\1", re.DOTALL)


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB data-spec parse tests.",
            allow_module_level=False,
        )


def _extract_specs(body: str) -> list[dict | list | None]:
    """Pull every ``data-spec`` attribute out of ``body`` and parse it.

    Fails the calling test if any extracted attribute does not parse
    as JSON. Returns the parsed values for caller-side structural
    assertions.
    """
    matches = _DATA_SPEC_PATTERN.findall(body)
    assert matches, "no data-spec attributes found in response body"
    parsed: list[dict | list | None] = []
    for _quote, raw in matches:
        decoded = html.unescape(raw)
        parsed.append(json.loads(decoded))
    return parsed


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
    email = "data-spec-route@example.com"
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


async def _seed_universe(user_id: UUID) -> UUID:
    """Seed two investments with overlapping NAVs and one cashflow each.

    Returns the id of the first investment so the per-investment
    surfaces (``/investments/<id>/charts``,
    ``/portfolio-review/investments/<id>``) can address it.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            ac_repo = AssetClassRepository(session)
            existing = await ac_repo.list_all()
            ac_code = "ac-data-spec"
            existing_match = next((a for a in existing if a.code == ac_code), None)
            ac = existing_match or await ac_repo.create(code=ac_code, display_name="AC Data Spec")
            inv_repo = InvestmentRepository(session)
            nav_repo = InvestmentNavRepository(session)
            cf_repo = InvestmentCashflowRepository(session)

            alpha = await inv_repo.create(
                name="Alpha Fund",
                investment_type="private_equity",
                asset_class_id=ac.id,
                currency="EUR",
                created_by=user_id,
                vintage_year=2020,
                is_active=True,
            )
            beta = await inv_repo.create(
                name="Beta Fund",
                investment_type="private_equity",
                asset_class_id=ac.id,
                currency="EUR",
                created_by=user_id,
                vintage_year=2021,
                is_active=True,
            )
            for as_of, alpha_v, beta_v in [
                (date(2024, 3, 31), Decimal("100"), Decimal("200")),
                (date(2024, 6, 30), Decimal("104"), Decimal("198")),
                (date(2024, 9, 30), Decimal("110"), Decimal("210")),
                (date(2024, 12, 31), Decimal("112"), Decimal("215")),
                (date(2025, 3, 31), Decimal("118"), Decimal("220")),
                (date(2025, 6, 30), Decimal("121"), Decimal("225")),
            ]:
                await nav_repo.upsert(
                    investment_id=alpha.id,
                    as_of_date=as_of,
                    nav_kind="actual",
                    nav_value=alpha_v,
                    currency="EUR",
                    source=None,
                    created_by=user_id,
                )
                await nav_repo.upsert(
                    investment_id=beta.id,
                    as_of_date=as_of,
                    nav_kind="actual",
                    nav_value=beta_v,
                    currency="EUR",
                    source=None,
                    created_by=user_id,
                )
            await cf_repo.create(
                investment_id=alpha.id,
                flow_timestamp=datetime(2024, 1, 31, 12, 0, tzinfo=timezone.utc),
                flow_type="capital_call",
                flow_kind="actual",
                amount=Decimal("-100"),
                currency="EUR",
                description=None,
                created_by=user_id,
            )
            await cf_repo.create(
                investment_id=alpha.id,
                flow_timestamp=datetime(2025, 6, 30, 12, 0, tzinfo=timezone.utc),
                flow_type="distribution",
                flow_kind="actual",
                amount=Decimal("30"),
                currency="EUR",
                description=None,
                created_by=user_id,
            )
            await cf_repo.create(
                investment_id=beta.id,
                flow_timestamp=datetime(2024, 1, 31, 12, 0, tzinfo=timezone.utc),
                flow_type="capital_call",
                flow_kind="actual",
                amount=Decimal("-200"),
                currency="EUR",
                description=None,
                created_by=user_id,
            )
            return alpha.id
    finally:
        await engine.dispose()


def _assert_plotly_figure(spec: object) -> None:
    """Assert that ``spec`` looks like a Plotly figure dict.

    Tolerates ``None`` (e.g. an empty sparkline) — the structural
    aim is that whatever ends up in the attribute is *parseable* JSON
    and, when not null, has the Plotly figure shape consumed by
    ``Plotly.newPlot(target, spec.data, spec.layout, spec.config)``.
    """
    if spec is None:
        return
    assert isinstance(spec, dict), f"expected dict, got {type(spec).__name__}"
    assert "data" in spec, "Plotly spec missing 'data' key"
    assert "layout" in spec, "Plotly spec missing 'layout' key"


# ---------------------------------------------------------------------------
# Tests — one per chart-bearing surface
# ---------------------------------------------------------------------------


async def test_investment_charts_data_spec_attributes_are_valid_json(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    inv_id = await _seed_universe(user_id)
    await _login(web_client, email, password)

    response = await web_client.get(f"/investments/{inv_id}/charts", follow_redirects=False)
    assert response.status_code == 200

    specs = _extract_specs(response.text)
    # Three per-investment charts: total return, cashflows/NAV, multiples.
    assert len(specs) == 3
    for spec in specs:
        _assert_plotly_figure(spec)


# NOTE: the legacy /statistics, /portfolio-analysis, /portfolio-review
# and /portfolio-review/investments/{id} structural-JSON tests were
# retired by sub-stream 6F-1 of Phase 6 Block 1 (ADR-0046) — those
# URLs now return 404, and the area-page sections that replace them
# are placeholder shells until 6F-3 lifts the actual content into
# them. The P6-K invariant (chart-spec ``data-spec`` attributes are
# valid JSON) is still exercised by the surviving
# ``test_investment_charts_data_spec_attributes_are_valid_json``
# above, which hits ``/investments/{id}/charts`` — the same template
# pattern the area sections will pick up once they ship in 6F-3.
