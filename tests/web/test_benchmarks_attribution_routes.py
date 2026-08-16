# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the Benchmarks & Attribution section endpoints.

Live-DB tests against the compose Postgres, mirroring the shape of
``test_limits_routes.py``. The fixtures seed the Sentinel Tenant
plus a sentinel-tenant user; the per-test client is bound via
``ASGITransport``; investments, NAVs, benchmarks, and mappings are
seeded inline via the Phase-4 / Phase-7 repositories.

Coverage targets — Roadmap A12 Phase 1a:

* Empty-tenant → empty-state copy (no 5xx).
* Seeded tenant with mappings → Stage a table + Stage b chart.
* Detail endpoint with a valid investment id → chart spec partial.
* Detail endpoint with an unmapped investment → "no mapping" copy.
* Detail endpoint with a non-Sentinel-tenant investment id → empty
  partial (RLS hides the row).
* Stage-c hypothetical endpoint without a configuration → empty-
  state partial.
* Section is registered in the Back Office area body.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories import (
    AssetClassBenchmarkMappingRepository,
    AssetClassRepository,
    BenchmarkObservationRepository,
    BenchmarkRepository,
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
            "skipping live-DB benchmarks-attribution section tests.",
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
                "TRUNCATE TABLE "
                "asset_class_benchmark_mapping, "
                "benchmark_observations, benchmarks, "
                "limits, limit_sets, "
                "investment_navs, investment_cashflows, "
                "investment_region_weights, region_country_memberships, "
                "regions, "
                "investment_country_weights, investment_sector_weights, "
                "fx_rates, "
                "investments, "
                "saa_correlations, saa_asset_class_inputs, "
                "saa_configurations, "
                "asset_classes, "
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
    email = "benchmarks-attribution@example.com"
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


# ---------------------------------------------------------------------------
# Inline seeding helpers
# ---------------------------------------------------------------------------


async def _seed_benchmarks_universe(
    actor_id: UUID,
    *,
    n_days: int = 400,
    tenant_id: UUID | None = None,
) -> dict[str, object]:
    """Seed an investments / benchmarks / mappings universe.

    Builds:
      - one mapped asset class (``equities``) with one investment
        carrying ``n_days`` of daily NAV rows;
      - one unmapped asset class (``cash``) with one investment
        carrying NAV rows but no benchmark mapping;
      - one benchmark with ``n_days`` of daily period-return rows.
    """
    target_tenant = tenant_id or SENTINEL_TENANT_ID
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    ids: dict[str, object] = {}
    try:
        async with tenant_context(engine, target_tenant, user_id=actor_id) as session:
            ac_mapped = await AssetClassRepository(session).create(
                code="equities", display_name="Equities"
            )
            ac_unmapped = await AssetClassRepository(session).create(
                code="cash", display_name="Cash"
            )
            inv_mapped = await InvestmentRepository(session).create(
                name="Alpha",
                investment_type="listed_equity",
                asset_class_id=ac_mapped.id,
                currency="EUR",
                created_by=actor_id,
            )
            inv_unmapped = await InvestmentRepository(session).create(
                name="CashFund",
                investment_type="other",
                asset_class_id=ac_unmapped.id,
                currency="EUR",
                created_by=actor_id,
            )
            ids["ac_mapped_id"] = ac_mapped.id
            ids["ac_unmapped_id"] = ac_unmapped.id
            ids["investment_mapped_id"] = inv_mapped.id
            ids["investment_unmapped_id"] = inv_unmapped.id

            nav_repo = InvestmentNavRepository(session)
            value = 100.0
            start_day = date(2023, 1, 1)
            for i in range(n_days):
                as_of = start_day + timedelta(days=i)
                await nav_repo.upsert(
                    investment_id=inv_mapped.id,
                    as_of_date=as_of,
                    nav_kind="actual",
                    nav_value=Decimal(str(round(value, 4))),
                    currency="EUR",
                    source=None,
                    created_by=actor_id,
                )
                await nav_repo.upsert(
                    investment_id=inv_unmapped.id,
                    as_of_date=as_of,
                    nav_kind="actual",
                    nav_value=Decimal(str(round(value * 0.5, 4))),
                    currency="EUR",
                    source=None,
                    created_by=actor_id,
                )
                value *= 1.0005

            benchmark = await BenchmarkRepository(session).create(
                code="BM_EQ",
                display_name="Equities Benchmark",
                description=None,
                provider_hint=None,
                created_by=actor_id,
            )
            ids["benchmark_id"] = benchmark.id

            obs_pairs = [(start_day + timedelta(days=i), Decimal("0.0004")) for i in range(n_days)]
            await BenchmarkObservationRepository(session).replace_observations_for_benchmark(
                benchmark.id, obs_pairs
            )
            await AssetClassBenchmarkMappingRepository(session).upsert_mapping(
                asset_class_id=ac_mapped.id,
                benchmark_id=benchmark.id,
                weight=Decimal("1.0"),
            )
    finally:
        await engine.dispose()
    return ids


async def _seed_uncovered_usd_investment(
    actor_id: UUID,
    *,
    asset_class_id: UUID,
    n_days: int = 400,
) -> None:
    """Seed a USD investment and **no** USD rate — the missing-rate path.

    It is seeded into the **mapped** asset class on purpose: the comparison
    path only converts what it compares, so a position parked in an unmapped
    class would never reach the conversion boundary and the test would pass
    for the wrong reason. Inside the mapped class it becomes a Stage-a row
    and part of the Stage-b composite, and since ADR-0102 both are derived
    from the *converted* NAV history — with no ``fx_rates`` row to convert
    with, the ADR-0099 §4 boundary raises :class:`MissingFxRateError` rather
    than comparing a USD series against a EUR-stated benchmark.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            inv = await InvestmentRepository(session).create(
                name="Uncovered USD Fund",
                investment_type="listed_equity",
                asset_class_id=asset_class_id,
                currency="USD",
                created_by=actor_id,
            )
            nav_repo = InvestmentNavRepository(session)
            value = 100.0
            start_day = date(2023, 1, 1)
            for i in range(n_days):
                await nav_repo.upsert(
                    investment_id=inv.id,
                    as_of_date=start_day + timedelta(days=i),
                    nav_kind="actual",
                    nav_value=Decimal(str(round(value, 4))),
                    currency="USD",
                    source=None,
                    created_by=actor_id,
                )
                value *= 1.0003
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_section_lazy_shell_renders_in_back_office_body(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The Back Office area carries the benchmarks-attribution lazy shell."""
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/back-office", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert 'hx-get="/api/back-office/benchmarks-attribution/section"' in body
    assert 'id="benchmarks-attribution"' in body


async def test_section_route_requires_session(
    web_client: AsyncClient,
) -> None:
    """``require_session`` redirects unauthenticated callers."""
    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/section",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_section_returns_empty_state_for_empty_tenant(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "No investments yet" in body


async def test_section_returns_stage_a_and_b_for_seeded_tenant(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_benchmarks_universe(user_id)

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # Stage a — table header and the mapped investment.
    assert "Investments vs. Benchmarks" in body
    assert "Alpha" in body
    # The unmapped Cash investment goes to the side list.
    assert "CashFund" in body
    # Stage b — small-multiples chart container.
    assert 'id="benchmarks-stage-b-chart"' in body
    # Cash asset class appears in the without-benchmark sidebar.
    assert "Cash" in body


async def test_stage_a_investment_detail_with_mapped_investment(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    ids = await _seed_benchmarks_universe(user_id)

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/stage-a/investment-detail",
        params={"investment_id": str(ids["investment_mapped_id"])},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # Inline-row partial — per-investment plotly target id encodes
    # the investment UUID, and the .ba-detail-row__inner wrapper is
    # the unambiguous signal that the new partial is in use.
    assert "ba-detail-row__inner" in body
    assert "ba-stage-a-detail-" in body
    assert "Alpha" in body


async def test_stage_a_investment_detail_with_unmapped_investment(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    ids = await _seed_benchmarks_universe(user_id)

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/stage-a/investment-detail",
        params={"investment_id": str(ids["investment_unmapped_id"])},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "No benchmark mapping" in body


async def test_stage_a_investment_detail_cross_tenant_isolation(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Investment-id from a different tenant returns the empty-state partial.

    RLS hides the row from the Sentinel-tenant session, so the
    repository correctly reports absence and the route renders the
    empty-state partial instead of leaking data.
    """
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    other_tenant_id = uuid4()
    other_user_id = uuid4()
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, gen_random_uuid()::text)"
            ),
            {"id": str(other_tenant_id), "name": "Other"},
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
                "email": "other@example.com",
                "hash": hash_password("x" * 32),
            },
        )
    ids = await _seed_benchmarks_universe(other_user_id, tenant_id=other_tenant_id, n_days=60)

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/stage-a/investment-detail",
        params={"investment_id": str(ids["investment_mapped_id"])},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "No benchmark mapping" in body or "Select an investment" in body


async def test_stage_c_hypothetical_without_configuration(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/stage-c/hypothetical",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # The chart partial renders an empty-state when no series is
    # available.
    assert "No data to display" in body


# ---------------------------------------------------------------------------
# Phase 1b — KPI strip / Tabulator / inline detail
# ---------------------------------------------------------------------------


async def test_section_includes_portfolio_kpi_cards(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A seeded tenant renders the four-card KPI strip above Stage a."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_benchmarks_universe(user_id)

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "ba-kpi-strip" in body
    # Labels of all four cards.
    assert "Mapping Coverage" in body
    assert "Median Excess p.a." in body
    assert "Hit Rate" in body
    assert "Median IR" in body


async def test_section_renders_ba_stage_a_data_block(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Stage a server-renders row data as a JSON script block for Tabulator."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_benchmarks_universe(user_id)

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert 'id="ba-stage-a-data"' in body
    assert 'type="application/json"' in body


async def test_section_renders_ba_stage_a_mount_point(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Stage a renders the Tabulator mount div."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_benchmarks_universe(user_id)

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert 'id="ba-stage-a-table"' in body


async def test_stage_a_detail_returns_row_partial(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Detail endpoint renders the inline-row partial, not the legacy one."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    ids = await _seed_benchmarks_universe(user_id)

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/stage-a/investment-detail",
        params={"investment_id": str(ids["investment_mapped_id"])},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "ba-detail-row__inner" in body
    # Legacy id is gone.
    assert 'id="benchmarks-stage-a-detail-chart"' not in body


async def test_stage_a_detail_handles_unknown_investment_id(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An unknown investment id renders the empty-state row variant."""
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    unknown_id = uuid4()
    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/stage-a/investment-detail",
        params={"investment_id": str(unknown_id)},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # The empty branch of the row partial.
    assert "ba-detail-row__inner" in body
    assert "pf-empty-state" in body


def test_stage_a_old_detail_template_is_removed() -> None:
    """The standalone detail partial is removed from the codebase."""
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    legacy = (
        repo_root / "web" / "templates" / "_partials" / "benchmarks_attribution_stage_a_detail.html"
    )
    assert not legacy.exists()


async def test_section_does_not_render_legacy_select(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The legacy dropdown-driven investment selector is gone."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_benchmarks_universe(user_id)

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert 'id="benchmarks-stage-a-investment-selector"' not in body


async def test_kpi_cards_handle_empty_portfolio_gracefully(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Empty portfolio falls through to the section-wide empty state.

    With zero investments the section renders the "No investments
    yet" empty-state copy. The KPI strip is not asserted here — the
    presence/absence of the strip in the empty branch is a render
    concern handled by the template's ``{% if portfolio_kpi_cards %}``
    guard; this test pins the 200/empty-state behaviour.
    """
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "No investments yet" in body


# ---------------------------------------------------------------------------
# Stage-c segmented control (Phase 1b polish round)
#
# These four tests render the Stage-c controls partial directly via
# Jinja2 with a fabricated context, avoiding the heavy SAA-configuration
# seeding required to exercise the populated branch through the HTTP
# stack. The endpoint integration is covered by the existing live-DB
# route tests above.
# ---------------------------------------------------------------------------


def _render_stage_c_controls(context: dict) -> str:
    """Render the Stage-c controls partial with a fabricated context."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    templates_dir = pathlib.Path(__file__).resolve().parents[2] / "web" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("_partials/benchmarks_attribution_stage_c_controls.html")
    return template.render(**context)


def _stage_c_controls_context(
    *,
    selected_weight_set: str = "tangency",
    weight_set_options: list[dict] | None = None,
    saa_configuration_options: list[dict] | None = None,
    selected_configuration_id: str = "cfg-id-1",
) -> dict:
    if weight_set_options is None:
        weight_set_options = [
            {
                "code": "tangency",
                "display_name": "Tangency (max Sharpe)",
                "available": True,
                "unavailable_hint": None,
            },
            {
                "code": "min_var",
                "display_name": "Minimum Variance",
                "available": True,
                "unavailable_hint": None,
            },
        ]
    if saa_configuration_options is None:
        saa_configuration_options = [
            {
                "saa_configuration_id": "cfg-id-1",
                "name": "Standard 2026",
                "is_active": True,
            }
        ]
    return {
        "saa_configuration_options": saa_configuration_options,
        "weight_set_options": weight_set_options,
        "selected_configuration_id": selected_configuration_id,
        "selected_weight_set": selected_weight_set,
        "has_series": False,
        "stage_c_chart_spec": None,
        "stage_c_series_meta": None,
    }


def test_stage_c_renders_segmented_control_not_select_for_weight_set() -> None:
    """Weight Set is rendered as a fieldset/radio segmented control."""
    body = _render_stage_c_controls(_stage_c_controls_context())
    assert 'class="ba-segmented"' in body
    assert 'role="radiogroup"' in body
    assert 'aria-label="Weight Set"' in body
    assert 'type="radio"' in body
    assert 'name="weight_set"' in body
    # Old <select id="benchmarks-stage-c-weight-set"> must be gone.
    assert 'id="benchmarks-stage-c-weight-set"' not in body


def test_stage_c_segmented_control_marks_selected_option_checked() -> None:
    """The radio matching ``selected_weight_set`` carries ``checked``."""
    body = _render_stage_c_controls(_stage_c_controls_context(selected_weight_set="min_var"))
    # The "min_var" radio carries the checked attribute; the
    # "tangency" radio does not.
    assert 'value="min_var"' in body
    min_var_block_start = body.index('value="min_var"')
    min_var_block = body[min_var_block_start : min_var_block_start + 400]
    assert "checked" in min_var_block

    tangency_block_start = body.index('value="tangency"')
    tangency_block = body[tangency_block_start : tangency_block_start + 400]
    assert "checked" not in tangency_block


def test_stage_c_segmented_control_marks_unavailable_options_disabled() -> None:
    """Unavailable options are disabled and carry the hint-icon tooltip."""
    body = _render_stage_c_controls(
        _stage_c_controls_context(
            weight_set_options=[
                {
                    "code": "tangency",
                    "display_name": "Tangency (max Sharpe)",
                    "available": False,
                    "unavailable_hint": "Need at least two asset classes.",
                },
                {
                    "code": "min_var",
                    "display_name": "Minimum Variance",
                    "available": False,
                    "unavailable_hint": "Need at least two asset classes.",
                },
            ]
        )
    )
    assert "ba-segmented__option--disabled" in body
    assert "disabled" in body
    # Hint icon + native tooltip carry the unavailable_hint copy.
    assert 'class="ba-segmented__hint"' in body
    assert "Need at least two asset classes." in body


def test_stage_c_saa_configuration_remains_select() -> None:
    """The SAA Configuration selector stays a <select>; only weight set moves."""
    body = _render_stage_c_controls(_stage_c_controls_context())
    # The SAA Configuration <select> with its id is still present.
    assert 'id="benchmarks-stage-c-config"' in body
    assert "<select" in body
    # The Configuration label is wrapped in the .ba-controls__field
    # container alongside its <select>.
    assert "ba-controls__field" in body


# ---------------------------------------------------------------------------
# Multi-currency — ADR-0102
# ---------------------------------------------------------------------------


def _parse_stage_a_data(body: str) -> list[dict]:
    """Return the Stage-a rows the section server-renders for Tabulator.

    The section emits them as a ``<script type="application/json"
    id="ba-stage-a-data">`` block; ``tojson`` escapes ``<>&``, so the content
    is valid JSON as-is. Reading it back lets the test assert on the metrics
    the table will draw rather than merely on the presence of its mount div.
    """
    match = re.search(
        r'<script[^>]*id="ba-stage-a-data"[^>]*>(.*?)</script>',
        body,
        re.DOTALL,
    )
    assert match is not None, "ba-stage-a-data block missing from body"
    return json.loads(match.group(1))


async def test_single_currency_tenant_benchmarks_unchanged(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The ADR-0102 §5 invisibility invariant.

    A tenant whose universe is entirely in the functional currency renders
    the **pre-change section**: the conversion boundary short-circuits to an
    identity without reading a single FX row (ADR-0099 §3), so the comparison
    metrics must be the ones the unconverted service produced.

    The fixture is deterministic, so the figures can be pinned: the mapped
    investment compounds at 0.0005/day against a benchmark returning
    0.0004/day. The comparison runs on *monthly* observations, so the 400
    daily NAV rows (2023-01-01 onwards, spanning 14 calendar months) align to
    14 of them, and the excess is positive every month. One of the two active
    investments is mapped, so the coverage card reads ``1 / 2``.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_benchmarks_universe(user_id)

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # The section body, not the error state.
    assert "ba-section--error" not in body
    assert "MissingFxRateError" not in body

    # Stage a and Stage b both render.
    assert "Investments vs. Benchmarks" in body
    assert 'id="benchmarks-stage-b-chart"' in body

    # The KPI strip's coverage card: one mapped investment of two active.
    assert "Mapping Coverage" in body
    assert "1 / 2" in body

    # The comparison metrics themselves — derived from the converted history,
    # which for a EUR-only tenant is the unconverted one.
    rows = _parse_stage_a_data(body)
    assert len(rows) == 1
    row = rows[0]
    assert row["investment_name"] == "Alpha"
    assert row["benchmark_display_name"] == "Equities Benchmark"
    assert row["n_observations"] == 14
    # 0.0005/day beats the benchmark's 0.0004/day, every day.
    assert row["excess_return_annualised"] > 0


async def test_benchmarks_returns_error_partial_on_missing_fx_rate(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An uncovered USD position degrades to the FX-error state, not a 500.

    HTTP 200 is deliberate: the body is an HTMX section swap, and an error
    status would leave the lazy shell in place instead of showing the
    message. The message must name the currency so the operator knows which
    row to add to the Excel ``FX rates`` sheet.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    ids = await _seed_benchmarks_universe(user_id)
    await _seed_uncovered_usd_investment(
        user_id,
        asset_class_id=cast(UUID, ids["ac_mapped_id"]),
    )

    response = await web_client.get(
        "/api/back-office/benchmarks-attribution/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # The section's own error partial, named error, uncovered currency.
    assert "ba-section--error" in body
    assert "MissingFxRateError" in body
    assert "USD" in body
    # The remedy is actionable and points at the import.
    assert "FX rates" in body
    assert "Data Import" in body
    # No internals leak, and no half-rendered section is emitted.
    assert "Traceback" not in body
    assert 'id="ba-stage-a-table"' not in body
