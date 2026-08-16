# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the embedded Portfolio Analysis section.

Live-DB tests against the compose Postgres, mirroring the
``test_statistics_section_routes.py`` shape. The fixtures seed the
sentinel tenant plus a sentinel-tenant user; the per-test client is
bound via ``ASGITransport``; investments and NAVs are seeded inline
via the Phase-4 repositories.

Coverage targets — sub-stream 6F-3c:

* ``GET /api/portfolio-analysis/section`` requires authentication.
* The GET response carries the Compute form and the empty-state
  copy (no chart on initial render).
* ``POST /api/portfolio-analysis/section/compute`` renders the
  frontier chart partial when the universe is sufficient.
* CSRF is required on the POST.
* Out-of-range form inputs are rejected with 422.
* An empty universe yields the empty-state copy on POST.
* ``GET /front-office`` returns the lazy-load shell — the Compute
  form is absent until HTMX fetches the section endpoint.
* No German tokens leak into the rendered fragments.
"""

from __future__ import annotations

import html
import json
import os
import pathlib
import re
from collections.abc import AsyncGenerator
from datetime import date
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
            "skipping live-DB portfolio-analysis section tests.",
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
                "investment_region_weights, region_country_memberships, "
                "regions, "
                "investment_country_weights, investment_sector_weights, "
                "fx_rates, "
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
    email = "pa-reader@example.com"
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


async def _login_and_get_session_csrf(client: AsyncClient, email: str, password: str) -> str:
    """Log in then scrape the session CSRF from the Front Office page.

    The hidden ``csrf_token`` input lives inside the Data Import
    section's upload form which is rendered synchronously on the
    page — so the value is visible before the Portfolio Analysis
    lazy shell fetches the section.
    """
    await _login(client, email, password)
    page = await client.get("/front-office", follow_redirects=False)
    assert page.status_code == 200
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


# ---------------------------------------------------------------------------
# Inline universe seeding
# ---------------------------------------------------------------------------


_TWO_INVESTMENTS_NAVS: dict[str, list[tuple[date, Decimal]]] = {
    "Alpha Fund": [
        (date(2024, 3, 31), Decimal("100")),
        (date(2024, 6, 30), Decimal("104")),
        (date(2024, 9, 30), Decimal("110")),
        (date(2024, 12, 31), Decimal("112")),
        (date(2025, 3, 31), Decimal("118")),
        (date(2025, 6, 30), Decimal("121")),
    ],
    "Beta Fund": [
        (date(2024, 3, 31), Decimal("200")),
        (date(2024, 6, 30), Decimal("198")),
        (date(2024, 9, 30), Decimal("210")),
        (date(2024, 12, 31), Decimal("215")),
        (date(2025, 3, 31), Decimal("220")),
        (date(2025, 6, 30), Decimal("225")),
    ],
    "Gamma Fund": [
        (date(2024, 3, 31), Decimal("50")),
        (date(2024, 6, 30), Decimal("53")),
        (date(2024, 9, 30), Decimal("55")),
        (date(2024, 12, 31), Decimal("60")),
        (date(2025, 3, 31), Decimal("62")),
        (date(2025, 6, 30), Decimal("65")),
    ],
}


async def _seed_universe(
    engine: AsyncEngine,
    actor_id: UUID,
    nav_map: dict[str, list[tuple[date, Decimal]]],
) -> None:
    """Seed an asset class plus the requested investments + NAVs."""
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="pa_section_class",
            display_name="PA Section Class",
        )
        inv_repo = InvestmentRepository(session)
        nav_repo = InvestmentNavRepository(session)
        for name, nav_values in nav_map.items():
            inv = await inv_repo.create(
                name=name,
                investment_type="private_equity",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=actor_id,
            )
            for as_of, value in nav_values:
                await nav_repo.upsert(
                    investment_id=inv.id,
                    as_of_date=as_of,
                    nav_kind="actual",
                    nav_value=value,
                    currency="EUR",
                    source=None,
                    created_by=actor_id,
                )


async def _seed_uncovered_usd_investment(
    engine: AsyncEngine,
    actor_id: UUID,
) -> None:
    """Seed a USD investment and **no** USD rate — the missing-rate path.

    Its NAV history spans the same six dates as the EUR universe, so it
    survives the frontier's overlapping-history filter and certainly reaches
    the conversion boundary. Since ADR-0102 the frontier's inputs — the
    cashflow-adjusted return series and the current-portfolio NAV weights —
    are derived from the *converted* history; with no ``fx_rates`` row to
    convert with, the ADR-0099 §4 boundary raises
    :class:`MissingFxRateError` rather than weighting USD NAV against EUR
    NAV as if 1 = 1.
    """
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="pa_usd_class",
            display_name="PA USD Class",
        )
        inv = await InvestmentRepository(session).create(
            name="Uncovered USD Fund",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="USD",
            created_by=actor_id,
        )
        nav_repo = InvestmentNavRepository(session)
        for as_of, value in (
            (date(2024, 3, 31), Decimal("300")),
            (date(2024, 6, 30), Decimal("310")),
            (date(2024, 9, 30), Decimal("305")),
            (date(2024, 12, 31), Decimal("330")),
            (date(2025, 3, 31), Decimal("340")),
            (date(2025, 6, 30), Decimal("350")),
        ):
            await nav_repo.upsert(
                investment_id=inv.id,
                as_of_date=as_of,
                nav_kind="actual",
                nav_value=value,
                currency="USD",
                source=None,
                created_by=actor_id,
            )


def _parse_data_spec(body: str, anchor: str) -> list[dict]:
    """Return parsed ``data-spec`` JSON dicts on tags whose markup
    contains ``anchor`` (e.g. ``pa-frontier-chart``)."""
    pattern = re.compile(
        r"(<[^>]*" + re.escape(anchor) + r"[^>]*data-spec='([^']*)'[^>]*>)",
        re.DOTALL,
    )
    return [json.loads(html.unescape(m.group(2))) for m in pattern.finditer(body)]


def _parse_weights_data(body: str) -> list[dict]:
    """Return the parsed weights rows embedded in the Tabulator data block.

    The compute partial emits the per-asset weights as a
    ``<script type="application/json" id="pa-weights-data">`` block,
    which the section script feeds into Tabulator. ``tojson`` escapes
    ``<>&`` as unicode sequences, so the content is valid JSON as-is.
    """
    m = re.search(
        r'<script[^>]*id="pa-weights-data"[^>]*>(.*?)</script>',
        body,
        re.DOTALL,
    )
    assert m is not None, "pa-weights-data block missing from body"
    return json.loads(m.group(1))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_portfolio_analysis_section_requires_session(
    web_client: AsyncClient,
) -> None:
    """``require_session`` redirects unauthenticated callers to /login.

    Plain-GET requests get 303; HTMX requests get 401 + ``HX-Redirect``.
    """
    response = await web_client.get("/api/portfolio-analysis/section", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    htmx_response = await web_client.get(
        "/api/portfolio-analysis/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert htmx_response.status_code == 401
    assert htmx_response.headers.get("HX-Redirect") == "/login"


async def test_get_portfolio_analysis_section_renders_form_with_empty_state(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The initial GET renders the Compute form + empty-state copy.

    No chart container should carry a ``data-spec`` attribute on
    first reveal — the operator has not yet clicked Compute.
    """
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/portfolio-analysis/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # Compute form is rendered.
    assert 'name="frontier_points"' in body
    assert 'name="risk_free_rate"' in body
    assert ">Compute<" in body
    # CSRF token is embedded as a hidden input.
    assert 'name="csrf_token"' in body
    # Empty-state copy is present, no frontier chart yet.
    assert "Click <strong>Compute</strong>" in body
    assert 'id="pa-frontier-chart"' not in body


async def test_post_portfolio_analysis_compute_renders_chart(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A valid POST returns the chart partial with summary cards
    and a Plotly spec embedded in ``data-spec``."""
    user_id, email, password = seeded_user
    csrf = await _login_and_get_session_csrf(web_client, email, password)
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)

    response = await web_client.post(
        "/api/portfolio-analysis/section/compute",
        data={
            "csrf_token": csrf,
            "frontier_points": "50",
            "risk_free_rate": "2.50",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # Frontier chart container present with a parseable Plotly spec.
    assert 'id="pa-frontier-chart"' in body
    specs = _parse_data_spec(body, "pa-frontier-chart")
    assert len(specs) == 1
    spec = specs[0]
    assert isinstance(spec, dict)
    assert "data" in spec
    assert "layout" in spec

    # Summary cards: at least tangency + min-variance (and current
    # when computable). The seeded universe yields a finite current
    # NAV-weighted portfolio.
    assert "Tangency Portfolio" in body
    assert "Min-Variance Portfolio" in body
    assert "Current Portfolio" in body
    assert body.count('class="pa-summary-card"') == 3


async def test_post_portfolio_analysis_compute_renders_weights_table(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The compute partial renders the per-asset weights table.

    For the populated fixture (which also yields a Current Portfolio
    card), the table compares Current vs Tangency vs Min-Variance
    per-asset weights under the frontier chart. The table is a
    Tabulator instance (Statistics convention) fed from a JSON data
    block; the column headers live in the section script, so this
    test asserts on the container, heading, the Current-column flag
    and the embedded weights payload.
    """
    user_id, email, password = seeded_user
    csrf = await _login_and_get_session_csrf(web_client, email, password)
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)

    response = await web_client.post(
        "/api/portfolio-analysis/section/compute",
        data={
            "csrf_token": csrf,
            "frontier_points": "50",
            "risk_free_rate": "2.50",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # The collapsible weights sub-block, its heading and the Tabulator
    # mount + JSON data block are all present.
    assert "Portfolio Weights" in body
    assert 'id="pa-weights-block"' in body
    assert 'id="pa-weights-table"' in body
    assert 'id="pa-weights-data"' in body

    # The Current column is enabled because the seeded universe yields
    # a definable current portfolio (mirrors the Current Portfolio card).
    assert 'data-has-current="true"' in body

    # The embedded weights payload parses and carries the three
    # comparison fields per asset, including a finite Current weight.
    rows = _parse_weights_data(body)
    assert len(rows) >= 1
    for row in rows:
        assert {"name", "tangency_pct", "min_var_pct", "current_pct"} <= set(row)
    assert any(row["name"] == "Alpha Fund" for row in rows)
    assert any(isinstance(row["current_pct"], (int, float)) for row in rows)


async def test_post_portfolio_analysis_compute_requires_csrf(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """POST without a CSRF token returns 403."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)

    response = await web_client.post(
        "/api/portfolio-analysis/section/compute",
        data={
            "frontier_points": "50",
            "risk_free_rate": "2.50",
        },
        follow_redirects=False,
    )
    assert response.status_code == 403


async def test_post_portfolio_analysis_compute_rejects_out_of_range_inputs(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """FastAPI Form() bounds reject out-of-range inputs with 422."""
    _id, email, password = seeded_user
    csrf = await _login_and_get_session_csrf(web_client, email, password)

    too_few_points = await web_client.post(
        "/api/portfolio-analysis/section/compute",
        data={
            "csrf_token": csrf,
            "frontier_points": "1",  # below _MIN_FRONTIER_POINTS = 20
            "risk_free_rate": "2.50",
        },
        follow_redirects=False,
    )
    assert too_few_points.status_code == 422

    rate_too_high = await web_client.post(
        "/api/portfolio-analysis/section/compute",
        data={
            "csrf_token": csrf,
            "frontier_points": "50",
            "risk_free_rate": "25.0",  # above _MAX_RISK_FREE_RATE_PCT = 20
        },
        follow_redirects=False,
    )
    assert rate_too_high.status_code == 422


async def test_post_portfolio_analysis_compute_empty_universe(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An empty universe yields the empty-state copy, not a chart."""
    _id, email, password = seeded_user
    csrf = await _login_and_get_session_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/portfolio-analysis/section/compute",
        data={
            "csrf_token": csrf,
            "frontier_points": "50",
            "risk_free_rate": "2.50",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "Click <strong>Compute</strong>" in body
    assert 'id="pa-frontier-chart"' not in body


async def test_front_office_renders_portfolio_analysis_lazy_shell(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The initial ``/front-office`` render must not run the
    Portfolio Analysis service — the section ships as a lazy shell
    that HTMX fetches on first visibility.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    # Seed a universe so a synchronous render would carry the
    # Compute form — its absence proves the section is lazy.
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)

    response = await web_client.get("/front-office", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    assert 'hx-get="/api/portfolio-analysis/section"' in body
    assert 'hx-trigger="revealed"' in body
    assert "Loading portfolio analysis" in body
    # The Compute form must not appear in the initial area render.
    assert 'name="frontier_points"' not in body
    assert 'id="pa-frontier-chart"' not in body


async def test_portfolio_analysis_section_no_german_strings(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Structural guard: no German tokens leak into the rendered fragments."""
    user_id, email, password = seeded_user
    csrf = await _login_and_get_session_csrf(web_client, email, password)
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)

    section_response = await web_client.get(
        "/api/portfolio-analysis/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert section_response.status_code == 200

    compute_response = await web_client.post(
        "/api/portfolio-analysis/section/compute",
        data={
            "csrf_token": csrf,
            "frontier_points": "30",
            "risk_free_rate": "2.50",
        },
        follow_redirects=False,
    )
    assert compute_response.status_code == 200

    forbidden = (
        "Berechnen",
        "Optimierung",
        "Effizienz",
        "Risikofrei",
        "Stichtag",
    )
    for body in (section_response.text, compute_response.text):
        for token in forbidden:
            assert token not in body, (
                f"German token {token!r} present in Portfolio Analysis section render"
            )


# ---------------------------------------------------------------------------
# Multi-currency — ADR-0102
# ---------------------------------------------------------------------------


async def test_single_currency_tenant_portfolio_analysis_unchanged(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The ADR-0102 §5 invisibility invariant.

    A tenant whose universe is entirely in the functional currency computes
    the **pre-change frontier**: the conversion boundary short-circuits to an
    identity without reading a single FX row (ADR-0099 §3).

    The key figures are pinned via the current portfolio, which is where the
    conversion would show up first — its weights are the latest NAVs
    normalised (121 + 225 + 65 = 411):

        Alpha  = 121 / 411 = 29.44 %
        Beta   = 225 / 411 = 54.74 %
        Gamma  =  65 / 411 = 15.82 %   (sums to 100)
    """
    user_id, email, password = seeded_user
    csrf = await _login_and_get_session_csrf(web_client, email, password)
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)

    response = await web_client.post(
        "/api/portfolio-analysis/section/compute",
        data={
            "csrf_token": csrf,
            "frontier_points": "50",
            "risk_free_rate": "2.50",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # The chart partial, not the error state.
    assert "pa-error" not in body
    assert "MissingFxRateError" not in body

    # The three summary cards and the frontier chart are all still there.
    assert 'id="pa-frontier-chart"' in body
    assert "Tangency Portfolio" in body
    assert "Min-Variance Portfolio" in body
    assert "Current Portfolio" in body
    assert body.count('class="pa-summary-card"') == 3

    # The current-portfolio weights are the unconverted NAV shares — the
    # identity conversion leaves every one of them untouched.
    weights = {row["name"]: row["current_pct"] for row in _parse_weights_data(body)}
    assert weights["Alpha Fund"] == pytest.approx(29.44, abs=0.01)
    assert weights["Beta Fund"] == pytest.approx(54.74, abs=0.01)
    assert weights["Gamma Fund"] == pytest.approx(15.82, abs=0.01)
    assert sum(weights.values()) == pytest.approx(100.0)


async def test_portfolio_analysis_returns_error_partial_on_missing_fx_rate(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """An uncovered USD position degrades to the FX-error state, not a 500.

    HTTP 200 is deliberate: the body is an HTMX swap into
    ``#pa-chart-container``, and an error status would leave the previous
    chart (or the empty state) in place with no explanation. The message must
    name the currency so the operator knows which row to add to the Excel
    ``FX rates`` sheet.
    """
    user_id, email, password = seeded_user
    csrf = await _login_and_get_session_csrf(web_client, email, password)
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)
    await _seed_uncovered_usd_investment(fresh_superuser_engine, user_id)

    response = await web_client.post(
        "/api/portfolio-analysis/section/compute",
        data={
            "csrf_token": csrf,
            "frontier_points": "50",
            "risk_free_rate": "2.50",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # The section's own error partial, named error, uncovered currency.
    assert 'id="pa-section-error"' in body
    assert "pa-error" in body
    assert "MissingFxRateError" in body
    assert "USD" in body
    # The remedy is actionable and points at the import.
    assert "FX rates" in body
    assert "Data Import" in body
    # No internals leak, and no half-computed frontier is emitted.
    assert "Traceback" not in body
    assert 'id="pa-frontier-chart"' not in body
