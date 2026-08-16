# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the embedded Statistics section endpoint.

Live-DB tests against the compose Postgres, mirroring the existing
``test_data_import_section_routes.py`` shape. The fixtures seed the
sentinel tenant plus a sentinel-tenant user; the per-test client is
bound via ``ASGITransport``; investments and NAVs are seeded inline
via the Phase-4 repositories.

Coverage targets — sub-stream 6F-3b:

* ``GET /api/statistics/section`` requires authentication.
* The empty-universe path returns the empty-state copy.
* Two seeded investments render two KPI cards plus a correlation
  heatmap target whose ``data-spec`` attribute parses as JSON.
* A single seeded investment renders the "need at least two
  investments" copy and omits the correlation target.
* ``GET /front-office`` returns the lazy-load shell — the KPI
  markers are absent until HTMX fetches the section endpoint.
* No German tokens leak into the rendered fragment.
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
            "skipping live-DB statistics section tests.",
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
    email = "stats-reader@example.com"
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
    """Drive ``GET /login`` + ``POST /login`` so the session cookie is set."""
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


_TWO_INVESTMENTS_NAVS: dict[str, list[tuple[date, Decimal]]] = {
    "Alpha Fund": [
        (date(2024, 12, 31), Decimal("100")),
        (date(2025, 3, 31), Decimal("110")),
        (date(2025, 6, 30), Decimal("121")),
        (date(2025, 9, 30), Decimal("100")),
    ],
    "Beta Fund": [
        (date(2024, 12, 31), Decimal("200")),
        (date(2025, 3, 31), Decimal("180")),
        (date(2025, 6, 30), Decimal("220")),
        (date(2025, 9, 30), Decimal("210")),
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
            code="stats_section_class",
            display_name="Stats Section Class",
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

    Its NAV history spans the same dates as the EUR universe, so the
    Statistics service certainly reaches it: since ADR-0102 every
    investment's NAV history is converted into the functional currency
    before the return series are derived, and with no ``fx_rates`` row
    to convert with, the ADR-0099 §4 boundary raises
    :class:`MissingFxRateError` rather than treating 1 USD as 1 EUR.
    """
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="stats_usd_class",
            display_name="Stats USD Class",
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
            (date(2024, 12, 31), Decimal("300")),
            (date(2025, 3, 31), Decimal("320")),
            (date(2025, 6, 30), Decimal("310")),
            (date(2025, 9, 30), Decimal("340")),
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


def _parse_data_spec_attrs(body: str, anchor: str) -> list[dict]:
    """Return parsed ``data-spec`` JSON dicts on tags whose markup
    contains ``anchor`` (e.g. ``stats-correlation`` or
    ``stats-spark-``). Used to assert spec validity.
    """
    pattern = re.compile(
        r"(<[^>]*" + re.escape(anchor) + r"[^>]*data-spec='([^']*)'[^>]*>)",
        re.DOTALL,
    )
    return [json.loads(html.unescape(m.group(2))) for m in pattern.finditer(body)]


def _json_script_blocks(body: str, element_id: str) -> list:
    """Return parsed JSON payloads from
    ``<script type="application/json" id="...">`` blocks.

    The Statistics section embeds the Tabulator row data via
    ``{{ rows | tojson }}`` script blocks. The ``tojson`` filter
    escapes ``<`` / ``>`` / ``&`` as ``\\uXXXX`` and marks the result
    safe, so the embedded payload is valid JSON and contains no
    literal ``</script>``; a non-greedy match between the tags is
    therefore safe.
    """
    pattern = re.compile(
        r'<script type="application/json" id="' + re.escape(element_id) + r'">(.*?)</script>',
        re.DOTALL,
    )
    return [json.loads(m.group(1).strip()) for m in pattern.finditer(body)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_statistics_section_requires_session(
    web_client: AsyncClient,
) -> None:
    """``require_session`` redirects unauthenticated callers to /login.

    Plain-GET requests get 303; HTMX requests get 401 + ``HX-Redirect``.
    """
    response = await web_client.get("/api/statistics/section", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    htmx_response = await web_client.get(
        "/api/statistics/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert htmx_response.status_code == 401
    assert htmx_response.headers.get("HX-Redirect") == "/login"


async def test_get_statistics_section_renders_empty_universe(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/statistics/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "No statistics to display" in body
    assert "stats-kpi-card" not in body
    assert 'id="stats-correlation"' not in body


async def test_get_statistics_section_renders_kpi_cards(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)

    response = await web_client.get(
        "/api/statistics/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # Two KPI cards rendered, one per investment.
    assert body.count('class="stats-kpi-card"') == 2
    assert "Alpha Fund" in body
    assert "Beta Fund" in body

    # Each sparkline target carries a parseable Plotly spec.
    sparkline_specs = _parse_data_spec_attrs(body, "stats-spark-")
    assert len(sparkline_specs) == 2
    for spec in sparkline_specs:
        assert isinstance(spec, dict)
        assert "data" in spec
        assert "layout" in spec

    # All four sub-blocks are present in the Details block, now rendered
    # as native <details> collapsibles (ADR-0062).
    assert '<span class="stats-subblock__label">Distribution</span>' in body
    assert '<span class="stats-subblock__label">Risk</span>' in body
    assert '<span class="stats-subblock__label">Risk / Return</span>' in body
    assert '<span class="stats-subblock__label">Autocorrelation</span>' in body
    assert body.count('class="stats-block__sub stats-subblock"') == 4
    # Collapsed by default: the Distribution sub-block renders without `open`.
    assert (
        '<details class="stats-block__sub stats-subblock" aria-labelledby="stats-dist-title">'
    ) in body


async def test_get_statistics_section_renders_correlation_when_two_or_more_investments(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)

    response = await web_client.get(
        "/api/statistics/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    assert 'id="stats-correlation"' in body
    assert "Need at least two investments" not in body, (
        "Correlation empty-state must not appear when >=2 investments are seeded."
    )

    corr_specs = _parse_data_spec_attrs(body, "stats-correlation")
    assert len(corr_specs) == 1
    spec = corr_specs[0]
    assert isinstance(spec, dict)
    assert "data" in spec
    assert "layout" in spec


async def test_get_statistics_section_correlation_empty_with_single_investment(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_universe(
        fresh_superuser_engine,
        user_id,
        {"Solo Fund": _TWO_INVESTMENTS_NAVS["Alpha Fund"]},
    )

    response = await web_client.get(
        "/api/statistics/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    assert 'id="stats-correlation"' not in body
    assert "Need at least two investments" in body


async def test_front_office_renders_statistics_lazy_shell(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The initial ``/front-office`` render must not invoke the
    Statistics service — the section ships as a lazy shell that
    HTMX fetches on first visibility.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    # Seed a universe so a synchronous render would carry KPI markers
    # — the absence of those markers proves the section is lazy.
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)

    response = await web_client.get("/front-office", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    assert 'hx-get="/api/statistics/section"' in body
    assert 'hx-trigger="revealed"' in body
    assert "Loading statistics" in body
    # The KPI / correlation markers must be absent on the initial
    # area render — they only appear after HTMX fetches the section.
    assert "stats-kpi-card" not in body
    assert 'id="stats-correlation"' not in body


async def test_statistics_section_no_german_strings(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Structural guard: no German tokens leak into the Statistics
    section render."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)

    response = await web_client.get(
        "/api/statistics/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    forbidden = ("Statistik", "Übersicht", "Risikofreier", "Stichtag")
    for token in forbidden:
        assert token not in body, f"German token {token!r} present in Statistics section render"


# ---------------------------------------------------------------------------
# ADR-0062 migration: investments-as-rows pivot + Tabulator detail tables
# ---------------------------------------------------------------------------


_DISTRIBUTION_FIELDS = (
    "mean_daily",
    "mean_annualised",
    "std_daily",
    "std_annualised",
    "variance_daily",
    "skewness",
    "kurtosis_excess",
    "median",
    "min_return",
    "max_return",
)

_RISK_FIELDS = (
    "var_90_daily",
    "var_95_daily",
    "var_99_daily",
    "cvar_95_daily",
    "max_drawdown",
    "ulcer_index",
    "downside_deviation",
    "sharpe_ratio",
    "sortino_ratio",
    "lag_1_autocorrelation",
    "lag_2_autocorrelation",
    "lag_3_autocorrelation",
    "lag_4_autocorrelation",
)


async def _fetch_section(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> str:
    """Log in, seed the two-investment universe, return the section body."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)
    response = await web_client.get(
        "/api/statistics/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    return response.text


async def test_distribution_rows_are_one_per_investment_with_flat_metric_fields(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Distribution data block is one row per investment, metrics flat."""
    body = await _fetch_section(web_client, seeded_user, fresh_superuser_engine)
    blocks = _json_script_blocks(body, "stats-distribution-data")
    assert len(blocks) == 1
    rows = blocks[0]
    assert isinstance(rows, list)
    assert {row["name"] for row in rows} == {"Alpha Fund", "Beta Fund"}
    for row in rows:
        for field in _DISTRIBUTION_FIELDS:
            assert field in row, f"Distribution row {row['name']!r} missing {field!r}"


async def test_risk_rows_carry_all_thirteen_metric_fields_per_investment(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Shared riskmetrics data block carries the 13 RiskMetrics fields."""
    body = await _fetch_section(web_client, seeded_user, fresh_superuser_engine)
    blocks = _json_script_blocks(body, "stats-riskmetrics-data")
    assert len(blocks) == 1
    rows = blocks[0]
    assert isinstance(rows, list)
    assert {row["name"] for row in rows} == {"Alpha Fund", "Beta Fund"}
    for row in rows:
        for field in _RISK_FIELDS:
            assert field in row, f"Risk row {row['name']!r} missing {field!r}"


async def test_distribution_pills_are_no_longer_emitted(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The pre-pivot pill rows are removed from the rendered section."""
    body = await _fetch_section(web_client, seeded_user, fresh_superuser_engine)
    assert "stats-pill" not in body
    # The μ / MDD / SR / ρ₁ pill prefixes are gone with the pivot.
    assert "μ " not in body
    assert "ρ₁" not in body


async def test_section_emits_tabulator_mount_point_per_detail_table(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """All four detail tables expose a Tabulator mount div."""
    body = await _fetch_section(web_client, seeded_user, fresh_superuser_engine)
    for mount_id in (
        "stats-dist-table",
        "stats-risk-table",
        "stats-rr-table",
        "stats-ac-table",
    ):
        assert f'id="{mount_id}"' in body, f"missing mount div {mount_id!r}"


async def test_section_emits_distribution_data_script_block(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The Distribution data block is present and parses as JSON."""
    body = await _fetch_section(web_client, seeded_user, fresh_superuser_engine)
    blocks = _json_script_blocks(body, "stats-distribution-data")
    assert len(blocks) == 1
    assert isinstance(blocks[0], list)


async def test_section_emits_riskmetrics_data_script_block_once(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The shared riskmetrics JSON block appears exactly once.

    The Risk / Risk-Return / Autocorrelation tables deliberately
    share a single embedded data array to avoid triplication.
    """
    body = await _fetch_section(web_client, seeded_user, fresh_superuser_engine)
    assert body.count('id="stats-riskmetrics-data"') == 1
    blocks = _json_script_blocks(body, "stats-riskmetrics-data")
    assert len(blocks) == 1
    assert isinstance(blocks[0], list)


async def test_section_does_not_emit_legacy_stats_table_markup(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """No pre-pivot ``<table class="stats-table">`` markup remains."""
    body = await _fetch_section(web_client, seeded_user, fresh_superuser_engine)
    assert 'class="stats-table"' not in body
    assert 'class="stats-table-wrapper"' not in body


async def test_section_does_not_emit_stats_pills(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """No ``.stats-pill`` elements remain after the pivot."""
    body = await _fetch_section(web_client, seeded_user, fresh_superuser_engine)
    assert "stats-pill" not in body


async def test_section_does_not_emit_details_collapsibles(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The legacy per-investment ``<details class="stats-details">``
    collapsibles do not return. (The current section uses
    ``stats-subblock`` collapsibles for the four detail tables by design,
    ADR-0062 — a distinct class, so this guard is unaffected.)"""
    body = await _fetch_section(web_client, seeded_user, fresh_superuser_engine)
    assert 'class="stats-details"' not in body


# ---------------------------------------------------------------------------
# Multi-currency — ADR-0102
# ---------------------------------------------------------------------------


async def test_single_currency_tenant_statistics_unchanged(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The ADR-0102 §5 invisibility invariant.

    A tenant whose universe is entirely in the functional currency renders
    the **pre-change section**: the conversion boundary short-circuits to an
    identity without reading a single FX row (ADR-0099 §3), so the figures
    must be bit-for-bit the ones the unconverted service produced.

    The key figures are pinned, not merely the markup:

    * the two latest NAVs (100.00 / 210.00 EUR) — taken from the
      *unconverted* history and therefore stated in the position currency;
    * the sparkline values, which *are* derived from the converted history:
      ``(1 + r).cumprod()`` over Alpha's three quarterly returns
      (+10 %, +10 %, −17.36 % along 100 → 110 → 121 → 100) is
      ``[1.1, 1.21, 1.0]``. An identity conversion must leave it untouched.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)

    response = await web_client.get(
        "/api/statistics/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # The section body, not the error state.
    assert "stats-section--error" not in body
    assert "MissingFxRateError" not in body

    # Both investments render, with their latest NAV in the position currency.
    assert body.count('class="stats-kpi-card"') == 2
    assert "Alpha Fund" in body
    assert "Beta Fund" in body
    assert "100.00 EUR" in body
    assert "210.00 EUR" in body

    # The converted-history figures: the cumulative-return path implied by
    # Alpha's NAVs, unchanged by the identity conversion.
    sparkline_specs = _parse_data_spec_attrs(body, "stats-spark-")
    assert len(sparkline_specs) == 2
    alpha_values = sparkline_specs[0]["data"][0]["y"]
    assert alpha_values == pytest.approx([1.1, 1.21, 1.0])

    # Two investments with overlapping histories still yield the heatmap.
    assert 'id="stats-correlation"' in body


async def test_statistics_returns_error_partial_on_missing_fx_rate(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """An uncovered USD position degrades to the FX-error state, not a 500.

    HTTP 200 is deliberate: the body is an HTMX section swap, and an error
    status would leave the lazy shell in place instead of showing the
    message. The message must name the currency so the operator knows which
    row to add to the Excel ``FX rates`` sheet.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_universe(fresh_superuser_engine, user_id, _TWO_INVESTMENTS_NAVS)
    await _seed_uncovered_usd_investment(fresh_superuser_engine, user_id)

    response = await web_client.get(
        "/api/statistics/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # The section's own error partial, named error, uncovered currency.
    assert "stats-section--error" in body
    assert "MissingFxRateError" in body
    assert "USD" in body
    # The remedy is actionable and points at the import.
    assert "FX rates" in body
    assert "Data Import" in body
    # No internals leak, and no half-rendered section is emitted.
    assert "Traceback" not in body
    assert "stats-kpi-card" not in body
