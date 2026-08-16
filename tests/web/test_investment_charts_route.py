# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the per-investment charts routes.

Live-DB tests against the compose Postgres. Same fixture pattern as
``tests/web/test_investments_routes.py`` (the sub-stream 4b read
surface).

Two routes are covered:

* ``GET /investments/{id}/charts`` — the single-investment detail
  charts page (three fixed Plotly targets + a "Back to detail" handoff).
* ``GET /api/charts/investment/{id}`` — the archetype-aware
  Front-Office universe-charts tile fragment (ADR-0082). This fragment
  dispatches on the investment's presentation archetype and emits the
  matching slots plus a KPI caption.

Coverage targets:

* Authentication is required (unauthenticated → 303 to /login).
* Same-tenant investment renders the detail page with the three Plotly
  chart containers and an inline ``Plotly.newPlot`` script.
* Unknown / foreign-tenant id on the detail page → 404.
* The archetype fragment dispatches each ``investment_type`` to the
  right ``data-archetype`` and the right ``data-slot`` set, renders a
  KPI caption for the three rich archetypes, falls back to a single
  full-width NAV tile for ``other``, and renders a neutral empty state
  (HTTP 200, not 404) for an unknown id.
* The two NAV-space tiles carry the ADR-0113 §2 plan tail through to
  the embedded Plotly spec — anchored on the last actual, cut at the
  unified axis end — and show an empty trace where no plan rows exist.
"""

from __future__ import annotations

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
    InvestmentBondAnalyticsRepository,
    InvestmentCashflowRepository,
    InvestmentMaturityWeightsRepository,
    InvestmentNavRepository,
    InvestmentRatingWeightsRepository,
    InvestmentRegionWeightsRepository,
    InvestmentRepository,
    InvestmentSectorWeightsRepository,
    RegionRepository,
    RegionWeightInput,
    SectorRepository,
    SectorWeightInput,
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
            "skipping live-DB charts route tests.",
            allow_module_level=False,
        )


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
    email = "charts-route@example.com"
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


async def _seed_investment_with_data(
    user_id: UUID,
    *,
    name: str = "Charts Fund",
    tenant_id: UUID = SENTINEL_TENANT_ID,
) -> UUID:
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as session:
            ac_repo = AssetClassRepository(session)
            existing = await ac_repo.list_all()
            ac_code = f"ac-{name.lower().replace(' ', '-')}"
            existing_match = next((a for a in existing if a.code == ac_code), None)
            ac = existing_match or await ac_repo.create(code=ac_code, display_name=f"AC {name}")
            inv = await InvestmentRepository(session).create(
                name=name,
                investment_type="private_equity",
                asset_class_id=ac.id,
                currency="EUR",
                created_by=user_id,
                is_active=True,
            )
            navs = InvestmentNavRepository(session)
            await navs.upsert(
                investment_id=inv.id,
                as_of_date=date(2024, 12, 31),
                nav_kind="actual",
                nav_value=Decimal("100"),
                currency="EUR",
                source=None,
                created_by=user_id,
            )
            await navs.upsert(
                investment_id=inv.id,
                as_of_date=date(2025, 6, 30),
                nav_kind="actual",
                nav_value=Decimal("160"),
                currency="EUR",
                source=None,
                created_by=user_id,
            )
            cf = InvestmentCashflowRepository(session)
            await cf.create(
                investment_id=inv.id,
                flow_timestamp=datetime(2024, 1, 31, 12, 0, tzinfo=timezone.utc),
                flow_type="capital_call",
                flow_kind="actual",
                amount=Decimal("-100"),
                currency="EUR",
                description=None,
                created_by=user_id,
            )
            await cf.create(
                investment_id=inv.id,
                flow_timestamp=datetime(2025, 6, 30, 12, 0, tzinfo=timezone.utc),
                flow_type="distribution",
                flow_kind="actual",
                amount=Decimal("30"),
                currency="EUR",
                description=None,
                created_by=user_id,
            )
            return inv.id
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_charts_route_unauthenticated_redirects_to_login(
    web_client: AsyncClient,
) -> None:
    response = await web_client.get(f"/investments/{uuid4()}/charts", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_charts_route_unknown_investment_returns_404(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(f"/investments/{uuid4()}/charts", follow_redirects=False)
    assert response.status_code == 404


async def test_charts_route_renders_three_chart_targets(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    inv_id = await _seed_investment_with_data(user_id)
    await _login(web_client, email, password)

    response = await web_client.get(f"/investments/{inv_id}/charts", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    # Three Plotly target divs by id, in the right order.
    assert "inv-chart-total-return" in body
    assert "inv-chart-cashflows-nav" in body
    assert "inv-chart-multiples" in body
    # The detail-page handoff: navigation back to the detail view.
    assert "Back to detail" in body
    # The inline bootstrap script must call Plotly.newPlot.
    assert "Plotly.newPlot" in body


async def test_charts_route_foreign_tenant_returns_404(
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
            {"id": str(other_tenant_id), "name": "Foreign Charts Tenant"},
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
                "email": "foreign-charts@example.com",
                "hash": hash_password("xxx"),
            },
        )

    foreign_inv_id = await _seed_investment_with_data(
        other_user_id,
        name="Foreign Charts Fund",
        tenant_id=other_tenant_id,
    )
    await _login(web_client, email, password)
    response = await web_client.get(f"/investments/{foreign_inv_id}/charts", follow_redirects=False)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Archetype-aware fragment (GET /api/charts/investment/{id}, ADR-0082)
# ---------------------------------------------------------------------------


async def _seed_archetype_investment(
    user_id: UUID,
    *,
    name: str,
    investment_type: str,
    tenant_id: UUID = SENTINEL_TENANT_ID,
    seed_fixed_income: bool = False,
    seed_composition: bool = False,
    actual_navs: tuple[tuple[date, Decimal], ...] = (
        (date(2025, 1, 31), Decimal("100")),
        (date(2025, 3, 31), Decimal("105")),
        (date(2025, 6, 30), Decimal("110")),
    ),
    plan_navs: tuple[tuple[date, Decimal], ...] = (),
) -> UUID:
    """Seed one investment of a given type with the data its archetype needs.

    Every investment gets an asset class, three month-end actual NAVs,
    a capital call, and one dividend (so the income-aware total-return
    series is non-empty). Fixed-Income investments additionally get a
    bond-analytics row plus rating / maturity weight snapshots; equity
    investments get sector / region weight snapshots. All statement
    dates are in the past so the default ``as_of`` (today) includes them.

    ``actual_navs`` / ``plan_navs`` override the NAV history — the
    ADR-0113 tests need one investment that carries the tenant's newest
    actual NAV (it sets the unified axis end) and a second one that lags
    behind it with a plan continuation.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as session:
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
                commitment_amount=Decimal("200"),
                is_active=True,
            )
            navs = InvestmentNavRepository(session)
            for nav_kind, rows in (("actual", actual_navs), ("plan", plan_navs)):
                for as_of, value in rows:
                    await navs.upsert(
                        investment_id=inv.id,
                        as_of_date=as_of,
                        nav_kind=nav_kind,
                        nav_value=value,
                        currency="EUR",
                        source=None,
                        created_by=user_id,
                    )
            cf = InvestmentCashflowRepository(session)
            await cf.create(
                investment_id=inv.id,
                flow_timestamp=datetime(2025, 1, 31, 12, 0, tzinfo=timezone.utc),
                flow_type="capital_call",
                flow_kind="actual",
                amount=Decimal("-100"),
                currency="EUR",
                description=None,
                created_by=user_id,
            )
            await cf.create(
                investment_id=inv.id,
                flow_timestamp=datetime(2025, 4, 15, 12, 0, tzinfo=timezone.utc),
                flow_type="dividend",
                flow_kind="actual",
                amount=Decimal("3"),
                currency="EUR",
                description=None,
                created_by=user_id,
            )

            if seed_fixed_income:
                await InvestmentBondAnalyticsRepository(session).upsert(
                    inv.id,
                    date(2025, 6, 30),
                    ytm=Decimal("0.045"),
                    eff_duration=Decimal("6.2"),
                    oas=Decimal("0.015"),
                    convexity=None,
                    basis="reported",
                    created_by=user_id,
                )
                ratings = InvestmentRatingWeightsRepository(session)
                await ratings.upsert(
                    inv.id,
                    date(2025, 6, 30),
                    "AAA",
                    weight_pct=Decimal("60"),
                    basis="reported",
                    created_by=user_id,
                )
                await ratings.upsert(
                    inv.id,
                    date(2025, 6, 30),
                    "A",
                    weight_pct=Decimal("40"),
                    basis="reported",
                    created_by=user_id,
                )
                await InvestmentMaturityWeightsRepository(session).upsert(
                    inv.id,
                    date(2025, 6, 30),
                    "5-7y",
                    weight_pct=Decimal("100"),
                    basis="reported",
                    created_by=user_id,
                )

            if seed_composition:
                tech = await SectorRepository(session).create(
                    code="tech",
                    display_name="Technology",
                    created_by=user_id,
                )
                dach = await RegionRepository(session).create(code="dach", display_name="DACH")
                await InvestmentSectorWeightsRepository(session).replace_snapshot_for_investment(
                    inv.id,
                    date(2025, 6, 30),
                    [SectorWeightInput(sector_id=tech.id, weight_pct=Decimal("100"))],
                    basis="reported",
                    created_by=user_id,
                )
                await InvestmentRegionWeightsRepository(session).replace_snapshot_for_investment(
                    inv.id,
                    date(2025, 6, 30),
                    [RegionWeightInput(region_id=dach.id, weight_pct=Decimal("100"))],
                    basis="reported",
                    created_by=user_id,
                )
            return inv.id
    finally:
        await engine.dispose()


async def _fetch_fragment(web_client: AsyncClient, investment_id: UUID | str) -> str:
    response = await web_client.get(
        f"/api/charts/investment/{investment_id}",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    return response.text


async def test_fragment_listed_bonds_is_fixed_income_archetype(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    inv_id = await _seed_archetype_investment(
        user_id,
        name="Bond Fund",
        investment_type="listed_bonds",
        seed_fixed_income=True,
    )
    await _login(web_client, email, password)
    body = await _fetch_fragment(web_client, inv_id)

    assert 'data-archetype="fixed_income"' in body
    assert 'data-slot="hero"' in body
    assert 'data-slot="yd"' in body
    assert 'data-slot="rm"' in body
    # The Capital-Account multiples slot must not appear.
    assert 'data-slot="mp"' not in body
    # The KPI caption is present for this rich archetype.
    assert 'class="ch-kpi-caption"' in body


async def test_fragment_private_equity_is_capital_account_archetype(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    inv_id = await _seed_archetype_investment(
        user_id, name="PE Fund", investment_type="private_equity"
    )
    await _login(web_client, email, password)
    body = await _fetch_fragment(web_client, inv_id)

    assert 'data-archetype="capital_account"' in body
    assert 'data-slot="tr"' in body
    assert 'data-slot="cn"' in body
    assert 'data-slot="mp"' in body
    # The Fixed-Income YTM/duration slot must not appear.
    assert 'data-slot="yd"' not in body
    assert 'class="ch-kpi-caption"' in body


async def test_fragment_listed_equity_is_total_return_equity_archetype(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    inv_id = await _seed_archetype_investment(
        user_id,
        name="Equity Fund",
        investment_type="listed_equity",
        seed_composition=True,
    )
    await _login(web_client, email, password)
    body = await _fetch_fragment(web_client, inv_id)

    assert 'data-archetype="total_return_equity"' in body
    assert 'data-slot="hero"' in body
    assert 'data-slot="uw"' in body
    assert 'data-slot="comp"' in body
    assert 'class="ch-kpi-caption"' in body


async def test_fragment_other_is_single_nav_only_tile_without_caption(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    inv_id = await _seed_archetype_investment(user_id, name="Misc Holding", investment_type="other")
    await _login(web_client, email, password)
    body = await _fetch_fragment(web_client, inv_id)

    assert 'data-archetype="nav_only"' in body
    assert 'data-slot="nav"' in body
    assert 'data-tile-count="1"' in body
    # Exactly one Plotly target and no KPI caption.
    assert body.count('class="ch-chart plotly-target"') == 1
    assert "ch-kpi-caption" not in body


async def _seed_universe_frontier(user_id: UUID) -> None:
    """Seed a fresher investment so the unified axis end lies in the future.

    ADR-0113 §1: the axis end is the newest actual NAV across the active
    universe. Without a fresher sibling the charted investment *is* the
    frontier and its plan tail is empty by construction.
    """
    await _seed_archetype_investment(
        user_id,
        name="Fresh Listed Fund",
        investment_type="listed_equity",
        actual_navs=(
            (date(2025, 6, 30), Decimal("200")),
            (date(2025, 12, 31), Decimal("220")),
        ),
    )


def _tile_spec(body: str, slot: str) -> dict:
    """Extract and parse one tile's embedded Plotly spec from the fragment."""
    match = re.search(rf'data-slot="{slot}"\s*data-spec=\'(.*?)\'></div>', body, re.DOTALL)
    assert match is not None, f"no {slot} tile in the fragment"
    return json.loads(match.group(1))


async def test_fragment_capital_account_nav_tile_carries_the_plan_tail(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """ADR-0113 §2: the Cashflows & NAV tile gains an anchored plan tail."""
    user_id, email, password = seeded_user
    await _seed_universe_frontier(user_id)
    inv_id = await _seed_archetype_investment(
        user_id,
        name="PE Plan Fund",
        investment_type="private_equity",
        plan_navs=(
            # At / before the last actual — inside the solid line's period.
            (date(2025, 6, 30), Decimal("112")),
            # The tail proper, up to the unified axis end.
            (date(2025, 9, 30), Decimal("120")),
            (date(2025, 12, 31), Decimal("130")),
            # Beyond the axis end.
            (date(2026, 6, 30), Decimal("150")),
        ),
    )
    await _login(web_client, email, password)
    body = await _fetch_fragment(web_client, inv_id)

    spec = _tile_spec(body, "cn")
    plan = next(t for t in spec["data"] if t["name"] == "NAV (Plan)")
    nav = next(t for t in spec["data"] if t["name"] == "NAV")
    # Anchored on the last actual, cut at the unified axis end.
    assert plan["x"][0] == nav["x"][-1]
    assert plan["y"] == [110.0, 120.0, 130.0]
    assert plan["line"]["dash"] == "dash"
    # The bars and the Net Capital Gain line stay actual-only.
    assert [t["name"] for t in spec["data"][:4]] == [
        "Calls",
        "Distributions",
        "Net Capital Gain",
        "NAV",
    ]


async def test_fragment_nav_only_tile_carries_the_plan_tail(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """ADR-0113 §2: the NAV-only tile's plan trace narrows to the tail."""
    user_id, email, password = seeded_user
    await _seed_universe_frontier(user_id)
    inv_id = await _seed_archetype_investment(
        user_id,
        name="Misc Plan Holding",
        investment_type="other",
        plan_navs=(
            (date(2025, 3, 31), Decimal("104")),
            (date(2025, 9, 30), Decimal("118")),
            (date(2025, 12, 31), Decimal("125")),
            (date(2026, 6, 30), Decimal("140")),
        ),
    )
    await _login(web_client, email, password)
    body = await _fetch_fragment(web_client, inv_id)

    spec = _tile_spec(body, "nav")
    plan = next(t for t in spec["data"] if t["name"] == "Plan")
    assert plan["x"] == ["2025-06-30", "2025-09-30", "2025-12-31"]
    assert plan["y"] == [110.0, 118.0, 125.0]
    # The shared axis end and the tail end are the same date here.
    assert spec["layout"]["xaxis"]["autorangeoptions"] == {"include": "2025-12-31"}


async def test_fragment_nav_only_tile_without_plan_rows_ends_early(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The honest gap: no plan rows, no fabricated continuation."""
    user_id, email, password = seeded_user
    await _seed_universe_frontier(user_id)
    inv_id = await _seed_archetype_investment(
        user_id, name="Misc Stale Holding", investment_type="other"
    )
    await _login(web_client, email, password)
    body = await _fetch_fragment(web_client, inv_id)

    spec = _tile_spec(body, "nav")
    plan = next(t for t in spec["data"] if t["name"] == "Plan")
    actual = next(t for t in spec["data"] if t["name"] == "Actual")
    assert plan["x"] == []
    # The solid line stops short of the axis end — visible empty space.
    assert actual["x"][-1] == "2025-06-30"
    assert spec["layout"]["xaxis"]["autorangeoptions"] == {"include": "2025-12-31"}


async def test_fragment_unknown_id_renders_neutral_empty_state(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Unknown id → neutral empty state, HTTP 200 (not 404)."""
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)
    response = await web_client.get(
        f"/api/charts/investment/{uuid4()}",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert 'data-tile-count="0"' in body
    assert "plotly-target" not in body
    assert "ch-kpi-caption" not in body
