# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the embedded Portfolio Review section endpoint.

Live-DB tests against the compose Postgres, mirroring the shape of
``test_charts_section_routes.py``. The fixtures seed the sentinel
tenant plus a sentinel-tenant user; the per-test client is bound via
``ASGITransport``; investments, NAVs and cashflows are seeded inline
via the Phase-4 repositories.

Coverage targets — sub-stream 6F-3d:

* The Investor Communication area page carries the lazy shell that
  targets ``/api/portfolio-review/section``.
* ``GET /api/portfolio-review/section`` requires authentication.
* The seeded-universe path renders the KPI strip with at least one
  numeric value.
* Six tiles are rendered when data is present.
* The empty-universe path returns the empty-state copy.
* The ``as_of_date`` query parameter shifts the resolved as-of date
  (and the rendered meta line) when a valid value is supplied.
* Invalid ``as_of_date`` values are silently ignored (the route falls
  back to the latest activity date).
* A valid ``as_of_date`` is echoed back into the form input value.

Coverage targets — A7 / ADR-0073 (per-investment stack):

* The section body carries one lazy placeholder per active investment,
  each with the shared resolved as-of date and ``hx-trigger="revealed"``.
* ``GET /api/portfolio-review/investment/{id}/section`` (valid id)
  renders the six-tile fragment (including the Total Return tile) with
  investment-suffixed DOM ids and a KPI strip.
* An unknown id returns HTTP 200 with the neutral "unavailable"
  fragment (not a 404).
* A cross-tenant id is indistinguishable from an unknown id — the
  neutral fragment renders and the row's existence does not leak.
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
            "skipping live-DB portfolio-review section tests.",
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
    email = "portfolio-review-section@example.com"
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


_TWO_INVESTMENTS: tuple[str, ...] = (
    "PR Section Investment A",
    "PR Section Investment B",
)


async def _seed_two_investments(actor_id: UUID) -> None:
    """Seed two investments with NAV history and cashflows.

    Investment A activity spans 2024-01..2025-06.
    Investment B activity spans 2024-01..2026-03 — supplying a later
    NAV row that lets the ``as_of_date`` shift test observe a
    different aggregated NAV between an early cutoff and the default
    (latest-activity) resolution.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            ac_repo = AssetClassRepository(session)
            asset_class = await ac_repo.create(
                code="pr_section_class",
                display_name="PR Section Class",
            )
            inv_repo = InvestmentRepository(session)
            nav_repo = InvestmentNavRepository(session)
            cf_repo = InvestmentCashflowRepository(session)

            for name, navs in (
                (
                    _TWO_INVESTMENTS[0],
                    [
                        (date(2024, 12, 31), Decimal("100")),
                        (date(2025, 3, 31), Decimal("110")),
                        (date(2025, 6, 30), Decimal("130")),
                    ],
                ),
                (
                    _TWO_INVESTMENTS[1],
                    [
                        (date(2024, 12, 31), Decimal("200")),
                        (date(2025, 6, 30), Decimal("210")),
                        (date(2025, 12, 31), Decimal("250")),
                        (date(2026, 3, 31), Decimal("500")),
                    ],
                ),
            ):
                inv = await inv_repo.create(
                    name=name,
                    investment_type="private_equity",
                    asset_class_id=asset_class.id,
                    currency="EUR",
                    created_by=actor_id,
                )
                for as_of, value in navs:
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


async def _list_sentinel_investment_ids(
    actor_id: UUID,
) -> list[tuple[UUID, str]]:
    """Return ``(id, name)`` for the sentinel tenant's active investments."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            invs = await InvestmentRepository(session).list_active()
            return [(inv.id, inv.name) for inv in invs]
    finally:
        await engine.dispose()


async def _seed_foreign_investment() -> UUID:
    """Create a second tenant with one investment; return its id.

    Used to prove the per-investment endpoint renders the neutral
    "unavailable" fragment (HTTP 200, not 404) for a cross-tenant id —
    RLS hides the row from the sentinel session, so the route cannot
    tell the id apart from a non-existent one.
    """
    foreign_tenant_id = uuid4()
    foreign_user_id = uuid4()
    su_engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with su_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, :sub)"),
                {
                    "id": str(foreign_tenant_id),
                    "name": "Foreign Tenant",
                    "sub": "foreign-tenant",
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO users
                        (id, tenant_id, email, password_hash,
                         roles, is_active)
                    VALUES
                        (:id, :tid, :email, :hash,
                         ARRAY['owner']::text[], TRUE)
                    """
                ),
                {
                    "id": str(foreign_user_id),
                    "tid": str(foreign_tenant_id),
                    "email": "foreign-owner@example.com",
                    "hash": hash_password("correct-horse-battery-staple"),
                },
            )
    finally:
        await su_engine.dispose()

    app_engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(
            app_engine, foreign_tenant_id, user_id=foreign_user_id
        ) as session:
            asset_class = await AssetClassRepository(session).create(
                code="foreign_class",
                display_name="Foreign Class",
            )
            inv = await InvestmentRepository(session).create(
                name="Foreign Investment",
                investment_type="private_equity",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=foreign_user_id,
            )
            return inv.id
    finally:
        await app_engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_section_lazy_shell_renders_in_body(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The Investor Communication area carries the lazy shell."""
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/investor-communication", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert 'hx-get="/api/portfolio-review/section"' in body
    # Old placeholder string is gone.
    assert "Six-tile portfolio review report" not in body


async def test_section_route_requires_session(
    web_client: AsyncClient,
) -> None:
    """``require_session`` redirects unauthenticated callers."""
    response = await web_client.get("/api/portfolio-review/section", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_section_renders_kpi_strip_when_data(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)

    response = await web_client.get(
        "/api/portfolio-review/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # KPI strip rendered.
    assert "pr-strip__card" in body
    assert "pr-strip__value" in body
    # NAV total = 130 (Inv A latest) + 500 (Inv B latest) = 630.
    assert "630" in body


async def test_section_renders_six_tiles_when_data(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)

    response = await web_client.get(
        "/api/portfolio-review/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    # Six tile articles.
    assert body.count('class="pr-tile"') == 6
    # And six unique tile ids.
    for slug in (
        "pr-tile-1",
        "pr-tile-2",
        "pr-tile-3",
        "pr-tile-region",
        "pr-tile-5",
        "pr-tile-6",
    ):
        assert f'id="{slug}-title"' in body


async def test_section_empty_state_when_no_investments(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Empty universe renders the empty-state copy, not a 5xx."""
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/portfolio-review/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert 'class="pr-empty"' in body
    assert "Data Import" in body
    # KPI strip is absent.
    assert "pr-strip__card" not in body


async def test_section_accepts_as_of_date_param(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A valid as_of_date shifts the resolved as-of-date and the NAV.

    With the default (latest-activity) resolution the NAV is 630
    (Inv A 130 + Inv B 500). With ``as_of_date=2025-12-31`` Inv B's
    cap on that date is 250, so the aggregated NAV is 130 + 250 = 380.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)

    default_response = await web_client.get(
        "/api/portfolio-review/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    capped_response = await web_client.get(
        "/api/portfolio-review/section",
        params={"as_of_date": "2025-12-31"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert default_response.status_code == 200
    assert capped_response.status_code == 200

    # Meta line carries the resolved as-of-date.
    assert "2026-03-31" in default_response.text
    assert "2025-12-31" in capped_response.text
    # NAV value differs between the two cutoffs.
    assert "630" in default_response.text
    assert "380" in capped_response.text


async def test_section_ignores_invalid_as_of_date(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Junk ``as_of_date`` values do not produce a 4xx."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)

    response = await web_client.get(
        "/api/portfolio-review/section",
        params={"as_of_date": "not-a-date"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    # Default resolution lands on the latest activity date.
    assert "2026-03-31" in response.text


async def test_section_form_preserves_as_of_date_input(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A valid ``as_of_date`` is echoed back into the form input."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)

    response = await web_client.get(
        "/api/portfolio-review/section",
        params={"as_of_date": "2025-06-30"},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert 'name="as_of_date"' in body
    assert 'value="2025-06-30"' in body


# ---------------------------------------------------------------------------
# Per-investment stack — ADR-0073
# ---------------------------------------------------------------------------


async def test_section_renders_per_investment_placeholders(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The section body carries one lazy placeholder per active investment.

    Each placeholder fetches its own six-tile fragment via
    ``hx-trigger="revealed"`` and carries the overview's resolved as-of
    date (default resolution lands on 2026-03-31) in its ``hx-get`` URL.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)
    seeded = await _list_sentinel_investment_ids(user_id)
    assert len(seeded) == 2

    response = await web_client.get(
        "/api/portfolio-review/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # One placeholder article per active investment.
    assert body.count('class="pr-investment-stack__item"') == 2
    assert 'hx-trigger="revealed"' in body
    for inv_id, _name in seeded:
        assert (
            'hx-get="/api/portfolio-review/investment/'
            f'{inv_id}/section?as_of_date=2026-03-31"' in body
        )


async def test_investment_section_valid_id_renders_six_tiles(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The per-investment endpoint returns the six-tile fragment.

    Includes the Total Return tile (tile 4 of the single-investment set,
    which the portfolio set does not have), a KPI strip, and tile DOM
    ids suffixed with the investment id.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)
    seeded = await _list_sentinel_investment_ids(user_id)
    inv_id, _name = seeded[0]

    response = await web_client.get(
        f"/api/portfolio-review/investment/{inv_id}/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # KPI strip rendered.
    assert "pr-strip__card" in body
    assert "pr-strip__value" in body

    # All six tile titles, including the single-investment Total Return.
    for title in (
        "Invested Capital &amp; NAV",
        "Cashflows",
        "Multiples (TVPI / DPI / IRR)",
        "Total Return (since inception)",
        "Region split",
        "Sector split",
    ):
        assert title in body

    # Tile DOM ids are suffixed with the investment id (so stacked
    # fragments never collide).
    for base in (
        "pr-inv-tile-1",
        "pr-inv-tile-2",
        "pr-inv-tile-3",
        "pr-inv-tile-4",
        "pr-inv-tile-region",
        "pr-inv-tile-6",
    ):
        assert f'id="{base}-{inv_id}-title"' in body


async def test_investment_section_unknown_id_returns_neutral_fragment(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An unknown id yields HTTP 200 + the neutral fragment, not a 404."""
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        f"/api/portfolio-review/investment/{uuid4()}/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "pr-investment-stack__unavailable" in body
    assert "This review is currently unavailable." in body
    # No tile grid, no investment-scoped root — nothing leaks.
    assert "pr-grid" not in body
    assert "data-investment-id" not in body


async def test_investment_section_cross_tenant_returns_neutral_fragment(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A cross-tenant id is indistinguishable from an unknown id.

    Mirrors
    ``tests/services/test_portfolio_review_service.py::
    test_single_investment_review_cross_tenant_returns_none``: an
    investment seeded in a foreign tenant is hidden from the sentinel
    session by RLS, so the endpoint renders the neutral fragment and
    does not leak that the row exists.
    """
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)
    foreign_id = await _seed_foreign_investment()

    response = await web_client.get(
        f"/api/portfolio-review/investment/{foreign_id}/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "pr-investment-stack__unavailable" in body
    assert "pr-grid" not in body
    assert "data-investment-id" not in body


async def _seed_usd_investment_without_rates(actor_id: UUID) -> None:
    """Seed one USD investment with a NAV but no FX rates (ADR-0099 §4)."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            ac = await AssetClassRepository(session).create(
                code="pr_usd_class", display_name="PR USD Class"
            )
            inv = await InvestmentRepository(session).create(
                name="PR Section USD Fund",
                investment_type="private_equity",
                asset_class_id=ac.id,
                currency="USD",
                created_by=actor_id,
            )
            await InvestmentNavRepository(session).upsert(
                investment_id=inv.id,
                as_of_date=date(2024, 12, 31),
                nav_kind="actual",
                nav_value=Decimal("200"),
                currency="USD",
                source=None,
                created_by=actor_id,
            )
    finally:
        await engine.dispose()


async def test_section_renders_fx_error_partial_when_rate_missing(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A USD position without an FX rate surfaces an actionable error, not 500.

    The ADR-0099 §4 conversion boundary raises ``MissingFxRateError``; the
    route renders the error partial with HTTP 200 (mirrors the limits
    route's engine-error idiom).
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_usd_investment_without_rates(user_id)

    response = await web_client.get(
        "/api/portfolio-review/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "pr-section--error" in body
    assert "MissingFxRateError" in body
    # The operator-actionable hint points at the FX rates supply.
    assert "FX rates" in body
