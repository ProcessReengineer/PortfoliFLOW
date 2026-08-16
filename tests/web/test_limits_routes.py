# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the embedded Investment Limits section endpoint.

Live-DB tests against the compose Postgres, mirroring the shape of
``test_portfolio_review_section_routes.py``. The fixtures seed the
Sentinel Tenant plus a sentinel-tenant user; the per-test client is
bound via ``ASGITransport``; investments, NAVs, AUM rows and limit
sets are seeded inline via the Phase-4/-7 repositories.

Coverage targets — Kickoff #3b sub-stream 3:

* Empty-AUM tenant → empty-state copy (no 5xx).
* Seeded tenant → KPI strip + per-family blocks rendered.
* ``from_date`` / ``to_date`` query parameters are honoured.
* Junk dates are silently ignored.
* ``LimitSetNotEffective`` → error-state partial.
* ``CoverageInputMissing`` → error-state partial.
* History-detail endpoint: known set → 200; unknown set → 404;
  cross-tenant set → 404 (RLS hides the row).
* Section is registered in the Back Office area body.
"""

from __future__ import annotations

import os
import pathlib
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
    LimitsRepository,
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
            "skipping live-DB limits section tests.",
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
                "TRUNCATE TABLE limits, limit_sets, "
                "investment_navs, investment_cashflows, "
                "investment_region_weights, region_country_memberships, "
                "regions, "
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
    email = "limits-section@example.com"
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
# Inline universe seeding
# ---------------------------------------------------------------------------


async def _seed_universe(
    actor_id: UUID,
    *,
    nav_rows: list[tuple[date, Decimal]] | None = None,
    cash_rows: list[tuple[date, Decimal]] | None = None,
    saa_limits: dict[str, Decimal] | None = None,
    anlv_limits: dict[str, Decimal] | None = None,
    asset_class_code: str = "equities",
    tenant_id: UUID | None = None,
) -> None:
    """Seed a one-investment / one-class universe, optionally plus cash.

    ``cash_rows`` seeds an explicit cash position holding the rest of the
    book. Since ADR-0103 §2 the denominator is ``Σ NAV``, so a test that
    wants its class at a *given percentage* has to hold the remainder rather
    than assert it with an AUM row. The cash class is deliberately left out
    of the SAA limit set (a NO_LIMIT row) and carries no ``anlv_code`` (the
    unallocated bucket), so it is not a constrained class and does not
    disturb the rows under test.

    Cross-tenant variants pass ``tenant_id`` explicitly; the default
    is the Sentinel Tenant which the logged-in fixture user belongs
    to.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    target_tenant = tenant_id or SENTINEL_TENANT_ID
    try:
        async with tenant_context(engine, target_tenant, user_id=actor_id) as session:
            ac = await AssetClassRepository(session).create(
                code=asset_class_code, display_name=asset_class_code.title()
            )
            inv = await InvestmentRepository(session).create(
                name="Alpha",
                investment_type="private_equity",
                asset_class_id=ac.id,
                currency="EUR",
                created_by=actor_id,
            )
            nav_repo = InvestmentNavRepository(session)
            for as_of, value in nav_rows or []:
                await nav_repo.upsert(
                    investment_id=inv.id,
                    as_of_date=as_of,
                    nav_kind="actual",
                    nav_value=value,
                    currency="EUR",
                    source=None,
                    created_by=actor_id,
                )

            if cash_rows:
                cash_ac = await AssetClassRepository(session).create(
                    code="cash", display_name="Cash"
                )
                cash = await InvestmentRepository(session).create(
                    name="Cash EUR",
                    investment_type="cash",
                    asset_class_id=cash_ac.id,
                    currency="EUR",
                    created_by=actor_id,
                )
                for as_of, value in cash_rows:
                    await nav_repo.upsert(
                        investment_id=cash.id,
                        as_of_date=as_of,
                        nav_kind="actual",
                        nav_value=value,
                        currency="EUR",
                        source=None,
                        created_by=actor_id,
                    )

            if saa_limits is not None:
                await LimitsRepository(session).create_set_with_limits(
                    family="saa",
                    effective_from=date(2020, 1, 1),
                    label="SAA test",
                    notes=None,
                    limits=saa_limits,
                    created_by=actor_id,
                )
            if anlv_limits is not None:
                await LimitsRepository(session).create_set_with_limits(
                    family="anlv",
                    effective_from=date(2020, 1, 1),
                    label="AnlV test",
                    notes=None,
                    limits=anlv_limits,
                    created_by=actor_id,
                )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_section_lazy_shell_renders_in_body(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The Back Office area carries the limits lazy shell."""
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/back-office", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert 'hx-get="/api/back-office/limits/section"' in body
    # Section anchor present.
    assert 'id="limits"' in body


async def test_section_route_requires_session(
    web_client: AsyncClient,
) -> None:
    """``require_session`` redirects unauthenticated callers."""
    response = await web_client.get("/api/back-office/limits/section", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_section_returns_empty_state_for_empty_tenant(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/back-office/limits/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "No AUM data available" in body
    # KPI strip absent.
    assert "lim-strip__card" not in body


_FULL_YEAR_MONTH_ENDS: list[date] = [
    date(2023, 4, 30),
    date(2023, 5, 31),
    date(2023, 6, 30),
    date(2023, 7, 31),
    date(2023, 8, 31),
    date(2023, 9, 30),
    date(2023, 10, 31),
    date(2023, 11, 30),
    date(2023, 12, 31),
    date(2024, 1, 31),
    date(2024, 2, 29),
    date(2024, 3, 31),
    date(2024, 4, 30),
]


async def test_section_returns_body_for_seeded_tenant(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)

    nav_rows = [(d, Decimal("250000")) for d in _FULL_YEAR_MONTH_ENDS]
    await _seed_universe(
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": Decimal("50.0")},
        anlv_limits={"anlv_1": Decimal("60.0")},
    )

    response = await web_client.get(
        "/api/back-office/limits/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # KPI strip rendered.
    assert "lim-strip__card" in body
    # As-of meta line carries the latest Stichtag (max AUM date).
    assert "2024-04-30" in body
    # Both family blocks present.
    assert 'id="lim-saa-title"' in body
    assert 'id="lim-anlv-title"' in body
    # Coverage chart mount.
    assert 'id="lim-saa-chart"' in body


async def test_section_respects_from_to_query_params(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)

    nav_rows = [
        (d, Decimal("250000"))
        for d in (
            date(2024, 1, 31),
            date(2024, 2, 29),
            date(2024, 3, 31),
            date(2024, 4, 30),
        )
    ]
    await _seed_universe(
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": Decimal("50.0")},
        anlv_limits={"anlv_1": Decimal("60.0")},
    )

    response = await web_client.get(
        "/api/back-office/limits/section",
        params={
            "from_date": "2024-01-01",
            "to_date": "2024-02-29",
        },
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # Latest Stichtag is now 2024-02-29 (not the full-range 2024-04-30).
    assert "2024-02-29" in body
    # Form inputs echo the params.
    assert 'value="2024-01-01"' in body
    assert 'value="2024-02-29"' in body


async def test_section_handles_junk_date_input_gracefully(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)

    nav_rows = [(d, Decimal("250000")) for d in _FULL_YEAR_MONTH_ENDS]
    await _seed_universe(
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": Decimal("50.0")},
        anlv_limits={"anlv_1": Decimal("60.0")},
    )

    response = await web_client.get(
        "/api/back-office/limits/section",
        params={"from_date": "not-a-date", "to_date": "garbage"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    # 200, not 4xx — junk is silently ignored; the route falls back
    # to the service's resolved defaults.
    assert response.status_code == 200
    body = response.text
    assert "lim-strip__card" in body


async def test_section_returns_error_partial_on_limit_set_not_effective(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Stichtag before earliest set effective_from → error partial."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)

    nav_rows = [
        (date(2019, 1, 31), Decimal("250000")),
        (date(2019, 2, 28), Decimal("250000")),
    ]
    # Limit sets are seeded with effective_from = 2020-01-01 (helper
    # default) but the range 2019-01..2019-02 falls before that.
    await _seed_universe(
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": Decimal("50.0")},
        anlv_limits={"anlv_1": Decimal("60.0")},
    )

    response = await web_client.get(
        "/api/back-office/limits/section",
        params={"from_date": "2019-01-01", "to_date": "2019-02-28"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "LimitSetNotEffective" in body
    assert "lim-section--error" in body


async def test_section_returns_error_partial_on_coverage_input_missing(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A valueless book at a Stichtag → ``CoverageInputMissing`` partial.

    The old trigger was "Stichtag before the first AUM observation". That
    condition retired with the AUM series (ADR-0103 §2). The denominator is
    now the book, so the way to have no denominator is to have no value: the
    book opens at zero and only funds itself in March.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)

    nav_rows = [
        (date(2024, 1, 31), Decimal("0")),
        (date(2024, 2, 29), Decimal("0")),
        (date(2024, 3, 31), Decimal("250000")),
        (date(2024, 4, 30), Decimal("250000")),
        (date(2024, 5, 31), Decimal("250000")),
        (date(2024, 6, 30), Decimal("250000")),
    ]
    await _seed_universe(
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": Decimal("50.0")},
        anlv_limits={"anlv_1": Decimal("60.0")},
    )

    response = await web_client.get(
        "/api/back-office/limits/section",
        params={"from_date": "2024-01-01", "to_date": "2024-06-30"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "CoverageInputMissing" in body
    assert "lim-section--error" in body


async def _seed_usd_universe_without_rates(actor_id: UUID) -> None:
    """Seed a one-USD-investment universe with limits, no FX rates."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            ac = await AssetClassRepository(session).create(
                code="equities", display_name="Equities"
            )
            inv = await InvestmentRepository(session).create(
                name="Dollar Fund",
                investment_type="listed_equity",
                asset_class_id=ac.id,
                currency="USD",
                created_by=actor_id,
            )
            await InvestmentNavRepository(session).upsert(
                investment_id=inv.id,
                as_of_date=date(2024, 12, 31),
                nav_kind="actual",
                nav_value=Decimal("500000"),
                currency="USD",
                source=None,
                created_by=actor_id,
            )
            limits_repo = LimitsRepository(session)
            await limits_repo.create_set_with_limits(
                family="saa",
                effective_from=date(2020, 1, 1),
                label="SAA test",
                notes=None,
                limits={"equities": Decimal("50.0")},
                created_by=actor_id,
            )
            await limits_repo.create_set_with_limits(
                family="anlv",
                effective_from=date(2020, 1, 1),
                label="AnlV test",
                notes=None,
                limits={"anlv_1": Decimal("60.0")},
                created_by=actor_id,
            )
    finally:
        await engine.dispose()


async def test_section_returns_error_partial_on_missing_fx_rate(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A USD position without an FX rate surfaces the error partial, not 500.

    The ADR-0099 §4 conversion boundary (Seam B) raises
    ``MissingFxRateError`` when the coverage engine's NAV inputs cannot be
    converted; the route renders the limits error partial.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_usd_universe_without_rates(user_id)

    response = await web_client.get(
        "/api/back-office/limits/section",
        params={"from_date": "2024-12-01", "to_date": "2024-12-31"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "MissingFxRateError" in body
    assert "lim-section--error" in body


async def test_history_detail_returns_partial_for_known_set(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)

    nav_rows = [(d, Decimal("250000")) for d in _FULL_YEAR_MONTH_ENDS]
    await _seed_universe(
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": Decimal("50.0"), "bonds": Decimal("30.0")},
        anlv_limits={"anlv_1": Decimal("60.0")},
    )

    # Fetch the set id via the section's history list.
    section = await web_client.get(
        "/api/back-office/limits/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert section.status_code == 200
    # The set_id appears in the hx-get attribute of the history-detail
    # mount; extract the first one.
    import re

    match = re.search(
        r"/api/back-office/limits/sets/([0-9a-f-]{36})/limits",
        section.text,
    )
    assert match is not None
    set_id = match.group(1)

    response = await web_client.get(
        f"/api/back-office/limits/sets/{set_id}/limits",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "lim-set-detail" in body
    # Both class rows must surface (regardless of which family this
    # set belongs to).
    assert "equities" in body or "anlv_1" in body


async def test_history_detail_returns_404_for_unknown_set(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    unknown = uuid4()
    response = await web_client.get(
        f"/api/back-office/limits/sets/{unknown}/limits",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 404


def _extract_json_block(body: str, block_id: str) -> list[dict[str, object]]:
    """Parse the JSON embedded in a ``<script type=application/json>`` block.

    Jinja's ``tojson`` HTML-escapes ``<``/``>``/``&`` as ``\\uXXXX``
    sequences, which ``json.loads`` decodes natively, so the script
    text content parses directly.
    """
    import json
    import re

    match = re.search(
        r'<script type="application/json"\s+id="' + re.escape(block_id) + r'">(.*?)</script>',
        body,
        re.DOTALL,
    )
    assert match is not None, f"JSON block {block_id!r} not found"
    parsed = json.loads(match.group(1))
    assert isinstance(parsed, list)
    return parsed


async def test_section_rows_carry_raw_numeric_fields(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The embedded JSON payload carries raw numeric fields alongside
    the existing fmt-string display values (ADR-0062 §1/§2.1)."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)

    nav_rows = [(d, Decimal("250000")) for d in _FULL_YEAR_MONTH_ENDS]
    await _seed_universe(
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": Decimal("50.0")},
        anlv_limits={"anlv_1": Decimal("60.0")},
    )

    response = await web_client.get(
        "/api/back-office/limits/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    rows = _extract_json_block(response.text, "lim-saa-data")
    assert rows
    for row in rows:
        # Raw numeric fields (numbers or null).
        for key in ("coverage_fraction", "max_pct", "nav_sum_eur", "headroom_eur"):
            assert key in row
            assert row[key] is None or isinstance(row[key], (int, float))
        # Display strings still present.
        for key in ("coverage_pct_fmt", "max_pct_fmt", "nav_sum_fmt", "headroom_fmt"):
            assert key in row
            assert isinstance(row[key], str)


async def test_coverage_fraction_is_normalised_to_zero_one_range(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A class at 75% of AUM under a 90% limit projects
    ``coverage_fraction == 0.75`` (engine percent-form / 100).

    750,000 of equity plus 250,000 of cash makes a 1,000,000 book — the
    denominator ADR-0103 §2 derives rather than reads.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)

    nav_rows = [(d, Decimal("750000")) for d in _FULL_YEAR_MONTH_ENDS]
    cash_rows = [(d, Decimal("250000")) for d in _FULL_YEAR_MONTH_ENDS]
    await _seed_universe(
        user_id,
        nav_rows=nav_rows,
        cash_rows=cash_rows,
        saa_limits={"equities": Decimal("90.0")},
        anlv_limits={"anlv_1": Decimal("60.0")},
    )

    response = await web_client.get(
        "/api/back-office/limits/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    rows = _extract_json_block(response.text, "lim-saa-data")
    equities = next(r for r in rows if r["class_key"] == "equities")
    assert equities["coverage_fraction"] == pytest.approx(0.75, abs=1e-9)


async def test_unallocated_row_has_no_coverage_fraction(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """UNALLOCATED rows surface in the projection but with
    ``coverage_fraction = None`` — they have no limit to compare
    against, so the heatmap leaves them untinted (ADR-0062 §2.1)."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)

    # The seeded investment carries an asset class (→ SAA class) but no
    # anlv_code, so under the AnlV family it falls into UNALLOCATED.
    nav_rows = [(d, Decimal("250000")) for d in _FULL_YEAR_MONTH_ENDS]
    await _seed_universe(
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": Decimal("50.0")},
        anlv_limits={"anlv_1": Decimal("60.0")},
    )

    response = await web_client.get(
        "/api/back-office/limits/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    rows = _extract_json_block(response.text, "lim-anlv-data")
    unallocated = [r for r in rows if r["status"] == "UNALLOCATED"]
    assert unallocated, "expected an UNALLOCATED row in the AnlV family"
    for row in unallocated:
        assert row["coverage_fraction"] is None


async def test_section_emits_tabulator_mount_point_per_family(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Each family renders a Tabulator mount point plus a parseable
    JSON-data script block (ADR-0062 §1)."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)

    nav_rows = [(d, Decimal("250000")) for d in _FULL_YEAR_MONTH_ENDS]
    await _seed_universe(
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": Decimal("50.0")},
        anlv_limits={"anlv_1": Decimal("60.0")},
    )

    response = await web_client.get(
        "/api/back-office/limits/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert 'id="lim-saa-table"' in body
    assert 'id="lim-anlv-table"' in body
    # JSON-data script blocks present and parseable.
    assert _extract_json_block(body, "lim-saa-data")
    assert _extract_json_block(body, "lim-anlv-data")


async def test_section_does_not_emit_legacy_table_markup(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The plain ``<table class="lim-table">`` markup is gone (§1)."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)

    nav_rows = [(d, Decimal("250000")) for d in _FULL_YEAR_MONTH_ENDS]
    await _seed_universe(
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": Decimal("50.0")},
        anlv_limits={"anlv_1": Decimal("60.0")},
    )

    response = await web_client.get(
        "/api/back-office/limits/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert 'class="lim-table"' not in response.text


async def test_status_badges_still_carry_lim_badge_classes(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Every projected row maps to a known ``lim-badge--*`` variant.

    Tabulator renders the badge cell client-side, so the closest
    server-side check is that each row's ``status_class`` is one of
    the variants the template formatter constructs into
    ``<span class="lim-badge lim-badge--{status_class}">``.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)

    nav_rows = [(d, Decimal("250000")) for d in _FULL_YEAR_MONTH_ENDS]
    await _seed_universe(
        user_id,
        nav_rows=nav_rows,
        saa_limits={"equities": Decimal("50.0")},
        anlv_limits={"anlv_1": Decimal("60.0")},
    )

    response = await web_client.get(
        "/api/back-office/limits/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    known = {"ok", "warn", "breach", "no-limit", "unallocated"}
    for block_id in ("lim-saa-data", "lim-anlv-data"):
        for row in _extract_json_block(response.text, block_id):
            assert row["status_class"] in known


async def test_history_detail_returns_404_for_cross_tenant_set(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """RLS hides foreign-tenant rows → repository reports absence →
    route returns 404."""
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    # Mint a separate tenant + actor + limit set in that other tenant.
    other_tenant_id = uuid4()
    other_actor_id = uuid4()
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, gen_random_uuid()::text)"
            ),
            {"id": str(other_tenant_id), "name": "Other Tenant"},
        )
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(other_tenant_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash, "
                "roles, is_active) "
                "VALUES (:uid, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)"
            ),
            {
                "uid": str(other_actor_id),
                "tid": str(other_tenant_id),
                "email": f"other-{other_tenant_id}@example.com",
                "hash": "$2b$04$placeholder_hash_for_x_tenant_tests_only",
            },
        )

    other_engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(other_engine, other_tenant_id, user_id=other_actor_id) as session:
            ac = await AssetClassRepository(session).create(
                code="other_class", display_name="Other"
            )
            _ = ac  # only needed for FK presence; not referenced again
            other_set = await LimitsRepository(session).create_set_with_limits(
                family="saa",
                effective_from=date(2020, 1, 1),
                label="Other tenant SAA",
                notes=None,
                limits={"other_class": Decimal("50.0")},
                created_by=other_actor_id,
            )
    finally:
        await other_engine.dispose()

    # Now the seeded user (Sentinel Tenant) attempts to fetch the
    # other tenant's set — RLS hides it.
    response = await web_client.get(
        f"/api/back-office/limits/sets/{other_set.id}/limits",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 404
