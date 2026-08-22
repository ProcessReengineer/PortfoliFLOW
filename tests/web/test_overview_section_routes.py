# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the embedded Front-Office Overview section endpoint.

Live-DB tests against the compose Postgres, mirroring the shape of
``test_portfolio_review_section_routes.py``. The fixtures seed the sentinel
tenant plus a sentinel-tenant user; the per-test client is bound via
``ASGITransport``; investments, NAVs, cashflows and AUM rows are seeded
inline via the Phase-4/5 repositories.

Coverage targets — ADR-0067:

* The Front Office area page carries the lazy shell targeting
  ``/api/overview/section``.
* ``GET /api/overview/section`` requires authentication.
* Seeded universe: the response shows the "Assets under management" hero —
  ``Σ nav_functional`` over the book (ADR-0103 §2) — an Invested sub-line,
  and a numeric metric. There is no "no AUM series" fallback state any more:
  a book with NAVs always has an AUM.
* Seeded universe **holding cash**: the sub-line adds the Cash figure, which
  is the Σ NAV of the explicit cash positions, not a residual.
* Empty universe: the empty-state copy is returned (no hero).
* ``_format_money_compact`` covers the k / M / B thresholds and rounding.

Coverage targets — ADR-0101 (multi-currency Block 5):

* **Invisibility (§4):** a functional-currency-only tenant renders exactly
  as before — no exposure tile, no FX-cash card, unchanged ``€`` strings.
* **Mixed currency:** the exposure tile and the FX-cash card appear, and
  their numbers match the fixture rates by hand-computation.
* **Missing rate:** an uncovered foreign-currency position degrades to the
  FX-error state (HTTP 200, currency named, no traceback) instead of
  surfacing an unhandled exception on the landing surface.

Coverage targets — ADR-0125 §6 (the freshness line):

* The ``.ov-meta`` line states when the book's live prices were last
  refreshed, in the schedule's timezone, with distinct copy for "never run"
  and "off".
* **Owners** additionally get one control — the Refresh form while live data
  is on, an "Enable in Admin" link while it is off. **Members** get the
  stamp and nothing clickable. The module's ``seeded_user`` carries
  ``owner``; ``seeded_member`` (added here, not in ``conftest.py``, because
  this is the only Overview concern that needs a non-owner) carries
  ``member``.
* The Overview poll answers the ADR-0120 branches and re-renders the whole
  section body on the terminal 286 (ADR-0125 §6/§7).
"""

from __future__ import annotations

import html
import json
import os
import pathlib
from collections.abc import AsyncGenerator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories import (
    AssetClassRepository,
    FxRateRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    tenant_context,
)
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.routes.areas import _derive_first_name
from web.routes.overview import _format_money_compact
from web.settings import WebSettings

load_dotenv(pathlib.Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB overview section tests.",
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
    email = "overview-section@example.com"
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


_MEMBER_EMAIL = "overview-member@example.com"


@pytest_asyncio.fixture
async def seeded_member(
    fresh_superuser_engine: AsyncEngine,
    seeded_user: tuple[UUID, str, str],
) -> tuple[UUID, str, str]:
    """Seed a second user in the same tenant holding ``member`` only.

    ADR-0125 §6 splits the freshness line by role, so this module needs both
    sides. Seeded here rather than in ``tests/web/conftest.py``: it is the
    only non-owner any Overview test wants. Depends on ``seeded_user`` so
    the tenant row exists first.
    """
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, ARRAY['member']::text[], TRUE)
                """
            ),
            {
                "id": str(user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": _MEMBER_EMAIL,
                "hash": hash_password(plaintext),
            },
        )
    return user_id, _MEMBER_EMAIL, plaintext


async def _seed_market_data_schedule(
    engine: AsyncEngine,
    *,
    enabled: bool,
    last_run_at: datetime | None = None,
    timezone_name: str = "Europe/Berlin",
) -> None:
    """Write the tenant's market-data schedule row via the superuser engine.

    The freshness line reads ``enabled`` / ``last_run_at`` / ``timezone`` off
    this row (ADR-0125 §6). ``last_run_at`` is written by the tick, never by
    a web route, so seeding it directly is the only way a route test can
    stage a landed run.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM market_data_schedule WHERE tenant_id = :t AND user_id IS NULL"),
            {"t": str(SENTINEL_TENANT_ID)},
        )
        await conn.execute(
            text(
                "INSERT INTO market_data_schedule "
                "(tenant_id, user_id, cadence, preferred_hour, timezone, "
                " enabled, next_due_at, last_run_at) "
                "VALUES (:t, NULL, 'every_15m', 0, :tz, "
                " :enabled, :next_due, :last_run)"
            ),
            {
                "t": str(SENTINEL_TENANT_ID),
                "tz": timezone_name,
                "enabled": enabled,
                "next_due": datetime.now(timezone.utc),
                "last_run": last_run_at,
            },
        )


def _overview_poll_url(since: datetime) -> str:
    """The poll URL the confirmation partial builds, for a given instant."""
    return f"/api/overview/refresh/poll?since={quote(since.isoformat(), safe='')}"


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


async def _seed_two_investments(actor_id: UUID) -> None:
    """Seed two investments with NAV history and cashflows.

    Portfolio NAV total resolves to 130 (Inv A latest) + 500 (Inv B
    latest) = 630 at the default (latest-activity) as-of date 2026-03-31.
    Calls are 100 each (200 total) and distributions 30 each (60 total),
    so TVPI = (60 + 630) / 200 = 3.45 and DPI = 60 / 200 = 0.30.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            ac_repo = AssetClassRepository(session)
            asset_class = await ac_repo.create(
                code="ov_section_class",
                display_name="Overview Section Class",
            )
            inv_repo = InvestmentRepository(session)
            nav_repo = InvestmentNavRepository(session)
            cf_repo = InvestmentCashflowRepository(session)

            for name, navs in (
                (
                    "OV Section Investment A",
                    [
                        (date(2024, 12, 31), Decimal("100")),
                        (date(2025, 6, 30), Decimal("130")),
                    ],
                ),
                (
                    "OV Section Investment B",
                    [
                        (date(2024, 12, 31), Decimal("200")),
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


async def _seed_usd_cash_position(actor_id: UUID) -> None:
    """Seed an explicit USD cash position plus the USD/EUR rate it needs.

    ADR-0100 cash row: ``Cash USD``, 500 USD at 2026-03-31 — the same as-of
    date the EUR universe resolves to. The rate is stored at 2026-03-01 and
    carries forward to the NAV date (ADR-0099 §4), so:

        functional value = 500 USD × 0.90 = 450 EUR

    Against the EUR universe's 630 (see :func:`_seed_two_investments`) the
    full-universe NAV becomes 630 + 450 = 1080, and the exposure splits:

        EUR = 630 / 1080 = 58.33 %
        USD = 450 / 1080 = 41.67 %
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            cash_class = await AssetClassRepository(session).create(
                code="ov_cash_class",
                display_name="Cash",
            )
            inv = await InvestmentRepository(session).create(
                name="Cash USD",
                investment_type="cash",
                asset_class_id=cash_class.id,
                currency="USD",
                created_by=actor_id,
                vintage_year=None,
            )
            await InvestmentNavRepository(session).upsert(
                investment_id=inv.id,
                as_of_date=date(2026, 3, 31),
                nav_kind="actual",
                nav_value=Decimal("500"),
                currency="USD",
                source=None,
                created_by=actor_id,
            )
            await FxRateRepository(session).upsert(
                currency="USD",
                as_of_date=date(2026, 3, 1),
                rate_to_reference=Decimal("0.90"),
                reference_currency="EUR",
                source="test",
                created_by=actor_id,
            )
    finally:
        await engine.dispose()


async def _seed_uncovered_usd_position(actor_id: UUID) -> None:
    """Seed a USD position and **no** USD rate — the missing-rate path.

    The mirror image of :func:`_seed_usd_cash_position`: same 500 USD NAV at
    the universe's as-of date, but the ``fx_rates`` row is deliberately
    omitted. The ADR-0099 §4 boundary then has nothing to convert with, and
    raises :class:`MissingFxRateError` rather than nominally adding USD into
    the EUR total.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            cash_class = await AssetClassRepository(session).create(
                code="ov_uncovered_class",
                display_name="Uncovered",
            )
            inv = await InvestmentRepository(session).create(
                name="Uncovered USD Fund",
                investment_type="private_equity",
                asset_class_id=cash_class.id,
                currency="USD",
                created_by=actor_id,
                vintage_year=2024,
            )
            await InvestmentNavRepository(session).upsert(
                investment_id=inv.id,
                as_of_date=date(2026, 3, 31),
                nav_kind="actual",
                nav_value=Decimal("500"),
                currency="USD",
                source=None,
                created_by=actor_id,
            )
    finally:
        await engine.dispose()


async def _set_display_name(user_id: UUID, display_name: str) -> None:
    """Set a user's ``display_name`` via the RLS-bypassing superuser engine."""
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET display_name = :dn WHERE id = :id"),
                {"dn": display_name, "id": str(user_id)},
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
    """The Front Office area carries the Overview lazy shell."""
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/front-office", follow_redirects=False)
    assert response.status_code == 200
    assert 'hx-get="/api/overview/section"' in response.text


async def test_section_route_requires_session(
    web_client: AsyncClient,
) -> None:
    """``require_session`` redirects unauthenticated callers to /login."""
    response = await web_client.get("/api/overview/section", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def _extract_chart_spec(body: str, chart_id: str) -> dict:
    """Pull one tile's Plotly spec back out of the rendered HTML.

    The template serialises each spec into a ``data-spec='…'`` attribute
    (single-quoted, so the JSON's own double quotes survive) on the tile's
    div. Reading it back lets a route test assert on the *numbers* the tile
    will draw, not merely on the presence of its id.
    """
    marker = f'id="{chart_id}"'
    start = body.index(marker)
    attr = "data-spec='"
    attr_start = body.index(attr, start) + len(attr)
    attr_end = body.index("'", attr_start)
    raw = body[attr_start:attr_end]
    return json.loads(html.unescape(raw))


async def test_section_aum_hero_is_sum_of_navs(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The hero is AUM = Σ NAV; a cash-free book shows no Cash figure.

    ADR-0103 §2: there is no ``portfolio_aum`` row to import and no fallback
    state. The two seeded funds are the whole book (630), so AUM and Invested
    are the same number here and Cash is absent — the strip states only what
    the book holds.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)

    response = await web_client.get(
        "/api/overview/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "Assets under management" in body
    assert "ov-hero__sub" in body
    assert "Invested" in body
    # No cash position in this book → no Cash figure on the sub-line. (Plain
    # "Cash" would also match the "Cashflows" tile title, hence the marker.)
    assert "&middot; Cash" not in body
    assert "Import an AUM series" not in body
    # At least one numeric metric value (TVPI = 3.45).
    assert "ov-card__value" in body
    assert "3.45" in body


async def test_section_cash_figure_reports_the_cash_position(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A book holding cash shows Cash on the sub-line — read, not inferred.

    The USD cash row is 500 USD × 0.90 = 450 EUR against the 630 EUR fund
    book, so AUM = 1080 = Invested 630 + Cash 450 (ADR-0103 §2).
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)
    await _seed_usd_cash_position(user_id)

    response = await web_client.get(
        "/api/overview/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "Assets under management" in body
    assert "ov-hero__sub" in body
    assert "&middot; Cash" in body


async def test_section_renders_three_chart_targets(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A seeded universe renders the three Overview chart tiles (ADR-0072)."""
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)

    response = await web_client.get(
        "/api/overview/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert 'id="ov-chart-invested-nav"' in body
    assert 'id="ov-chart-cashflows"' in body
    assert 'id="ov-chart-composition"' in body
    assert "pf-plotly-target" in body


# ---------------------------------------------------------------------------
# Multi-currency — ADR-0101
# ---------------------------------------------------------------------------


async def test_single_currency_tenant_sees_no_fx_surfaces(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The ADR-0101 §4 invisibility invariant.

    A tenant whose entire universe is in the functional currency renders
    **identically to the pre-block Overview**: no currency-exposure tile, no
    FX-cash card, three chart tiles (not four), and — the functional currency
    being EUR — the same ``€`` money strings as before.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)

    response = await web_client.get(
        "/api/overview/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # No exposure tile: one position currency has nothing to distribute.
    assert 'id="ov-chart-currency"' not in body
    # No FX-cash card, and the chart row keeps its three-tile geometry.
    assert "ov-fx" not in body
    assert "ov-charts--four" not in body
    # The three ADR-0072 tiles are all still there.
    assert 'id="ov-chart-invested-nav"' in body
    assert 'id="ov-chart-cashflows"' in body
    assert 'id="ov-chart-composition"' in body

    # Money strings unchanged: the invested book is 130 + 500 = 630 and still
    # renders through the euro symbol, not an ISO prefix.
    assert "€630" in body
    assert "EUR 630" not in body


async def test_mixed_currency_tenant_renders_exposure_tile_and_fx_card(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Exposure tile + FX-cash card appear, with hand-computed figures.

    Universe: the two EUR funds (Σ latest NAV 630) plus ``Cash USD``
    (500 USD × 0.90 = 450 EUR at the carried-forward 2026-03-01 rate).

        full-universe NAV = 630 + 450          = 1080 EUR
        EUR share         = 630 / 1080 * 100   = 58.33 %
        USD share         = 450 / 1080 * 100   = 41.67 %   (sums to 100)
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)
    await _seed_usd_cash_position(user_id)

    response = await web_client.get(
        "/api/overview/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # --- The fourth tile (ADR-0101 §1) ---------------------------------
    assert 'id="ov-chart-currency"' in body
    assert "ov-charts--four" in body
    # The subtitle that bounds what the donut claims.
    assert "by position currency (unhedged)" in body

    # The spec is embedded as JSON in the data-spec attribute. Pull the tile's
    # slice values back out and check them against the hand-computed shares.
    spec = _extract_chart_spec(body, "ov-chart-currency")
    trace = spec["data"][0]
    assert trace["type"] == "pie"
    assert trace["labels"] == ["EUR", "USD"]
    assert trace["values"] == pytest.approx([630.0, 450.0])
    total = sum(trace["values"])
    shares = [100.0 * v / total for v in trace["values"]]
    assert sum(shares) == pytest.approx(100.0)
    assert shares[1] == pytest.approx(41.666, abs=0.01)  # the USD share

    # --- The FX-cash card (ADR-0101 §2) --------------------------------
    assert "ov-fx" in body
    assert "Foreign-currency cash" in body
    assert "Cash USD" in body
    # Native balance in its own currency; functional equivalent in EUR. The
    # 500 → 450 pair is exactly the 0.90 rate on the NAV date.
    assert "$500" in body
    assert "€450" in body
    assert "2026-03-31" in body


async def test_missing_fx_rate_renders_error_state_not_a_500(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An uncovered position degrades to the FX-error state, not a crash.

    The Overview is the landing surface: an unhandled MissingFxRateError
    here reads as "the app is broken" when the real story is "one FX rate is
    missing". The section must instead return HTTP 200 — it is an HTMX swap,
    and an error status would leave the lazy shell in place — carrying a
    message that names the currency, so the operator knows which row to add
    to the Excel ``FX rates`` sheet.
    """
    user_id, email, password = seeded_user
    await _login(web_client, email, password)
    await _seed_two_investments(user_id)
    await _seed_uncovered_usd_position(user_id)

    response = await web_client.get(
        "/api/overview/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    # The error is named, and the message identifies the uncovered currency
    # (and the date) rather than being masked behind generic copy.
    assert "MissingFxRateError" in body
    assert "USD" in body
    assert "2026-03-31" in body
    # The remedy is actionable and points at the import.
    assert "FX rates" in body
    assert "Data Import" in body
    # No internals leak, and no half-rendered section is emitted.
    assert "Traceback" not in body
    assert "ov-hero" not in body


async def test_section_empty_state_when_no_investments(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Empty universe renders the empty-state copy, not a hero or a 5xx."""
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/overview/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert 'class="ov-empty"' in body
    assert "Data Import" in body
    # No hero rendered.
    assert "ov-hero" not in body


# ---------------------------------------------------------------------------
# Freshness line and owner-gated refresh — ADR-0125 §6
# ---------------------------------------------------------------------------


async def test_overview_meta_member_sees_stamp_without_control(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    seeded_member: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A member reads when the book was refreshed, and can do nothing about it.

    ADR-0125 §6 states it as a split, not a hide: the stamp is for everyone
    because staleness changes how you read the numbers; the control is
    owner-only because a refresh is a tenant-level action. The template half
    of the gate is here; the route half is
    ``tests/web/test_market_data_routes.py::test_refresh_now_member_gets_403``.
    """
    owner_id, _email, _password = seeded_user
    _mid, member_email, member_password = seeded_member
    await _seed_two_investments(owner_id)
    await _seed_market_data_schedule(fresh_superuser_engine, enabled=True)
    await _login(web_client, member_email, member_password)

    response = await web_client.get(
        "/api/overview/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    assert "Live data" in body
    assert "ov-meta__refresh" not in body
    assert "Enable in Admin" not in body


async def test_overview_meta_owner_enabled_sees_refresh_form(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The owner's control posts to the shared enqueue, tagged for this surface.

    ``surface=overview`` is the whole difference between the two
    confirmations (ADR-0125 §6) — one enqueue endpoint, two partials — so
    the hidden field is what this pins.
    """
    owner_id, email, password = seeded_user
    await _seed_two_investments(owner_id)
    await _seed_market_data_schedule(fresh_superuser_engine, enabled=True)
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/overview/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    assert 'hx-post="/api/market-data/refresh-now"' in body
    assert 'name="surface" value="overview"' in body
    # It swaps itself, which is what keeps the poller inside #ov-section-body.
    assert 'hx-target="this"' in body
    assert "Enable in Admin" not in body


async def test_overview_meta_owner_disabled_sees_enable_link(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Live data off: the owner is pointed at Admin, not offered a no-op.

    Enqueueing against a disabled schedule moves nothing (the tick gates on
    ``enabled``), so offering "Refresh" here would be a button that does
    nothing. The line says what is actually wrong instead.
    """
    owner_id, email, password = seeded_user
    await _seed_two_investments(owner_id)
    await _seed_market_data_schedule(fresh_superuser_engine, enabled=False)
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/overview/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    assert "Live data off" in body
    assert 'href="/admin#market-data"' in body
    assert "Enable in Admin" in body
    assert "ov-meta__refresh-btn" not in body


async def test_overview_meta_never_run_copy(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """An enabled-but-never-run schedule says so, rather than showing a blank.

    Three distinct states, three distinct sentences (ADR-0125 §6): "updated
    HH:MM", "not yet refreshed", "off". Rendering an empty time for the
    middle one would read as a broken stamp.
    """
    owner_id, email, password = seeded_user
    await _seed_two_investments(owner_id)
    await _seed_market_data_schedule(fresh_superuser_engine, enabled=True, last_run_at=None)
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/overview/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    assert "Live data not yet refreshed" in body
    assert "Live data updated" not in body
    assert "Live data off" not in body


async def test_overview_meta_updated_time_in_schedule_timezone(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The stamp is local to the *schedule's* timezone, not UTC.

    ADR-0125 §6 says "rendered in the schedule's timezone"; the operator
    reads it against their own wall clock. A mid-January instant is picked
    so Berlin is unambiguously CET (UTC+1) and the assertion cannot turn on
    a DST boundary: 13:32 UTC is 14:32 in Berlin.
    """
    owner_id, email, password = seeded_user
    await _seed_two_investments(owner_id)
    await _seed_market_data_schedule(
        fresh_superuser_engine,
        enabled=True,
        last_run_at=datetime(2026, 1, 15, 13, 32, tzinfo=timezone.utc),
        timezone_name="Europe/Berlin",
    )
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/overview/section",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text

    assert "Live data updated 14:32" in body
    assert "13:32" not in body, "the stamp must not fall back to the UTC instant."


# ---------------------------------------------------------------------------
# Overview refresh poll — ADR-0125 §6/§7 (the ADR-0120 pattern)
# ---------------------------------------------------------------------------


async def test_overview_poll_pending_204(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """No run yet: 204, which HTMX does not swap, so the body stands.

    This is the branch that runs ~4 times a minute per open tab, and §7
    bounds the cost by keeping it to one indexed row read — it must render
    nothing, in particular not the Overview body with its four chart specs.
    """
    owner_id, email, password = seeded_user
    await _seed_two_investments(owner_id)
    await _seed_market_data_schedule(fresh_superuser_engine, enabled=True, last_run_at=None)
    await _login(web_client, email, password)

    response = await web_client.get(
        _overview_poll_url(datetime.now(timezone.utc) - timedelta(seconds=30))
    )

    assert response.status_code == 204
    assert response.text == ""


async def test_overview_poll_landed_286_renders_section_body(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The done condition: 286 carrying the **whole** re-rendered body.

    ADR-0125 §6 rejected a stamp-only update explicitly — the reason for a
    manual refresh is to see the numbers move — so the 286 must carry
    ``#ov-section-body`` with the hero and the tiles, which the poller's
    ``outerHTML`` swap then puts in place of the old body (removing itself
    in the process).
    """
    owner_id, email, password = seeded_user
    now = datetime.now(timezone.utc)
    since = now - timedelta(seconds=30)
    await _seed_two_investments(owner_id)
    await _seed_market_data_schedule(
        fresh_superuser_engine,
        enabled=True,
        last_run_at=since + timedelta(seconds=5),
    )
    await _login(web_client, email, password)

    response = await web_client.get(_overview_poll_url(since))

    assert response.status_code == 286
    body = response.text
    assert 'id="ov-section-body"' in body
    assert "Assets under management" in body
    assert 'id="ov-chart-invested-nav"' in body
    # A settled body starts no poll of its own — only a confirmation does.
    assert "refresh/poll?since=" not in body


@pytest.mark.parametrize(
    "query",
    [
        "",  # no marker at all
        "?since=",
        "?since=not-a-timestamp",
        "?since=2026-08-13T10:00:00",  # naive: no zone to compare against
    ],
    ids=["absent", "empty", "garbage", "naive"],
)
async def test_overview_poll_stops_on_unusable_since(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
    query: str,
) -> None:
    """A hand-edited marker terminates the poll — it never 500s.

    ``HX-Reswap: none`` is the load-bearing half: the poller declares an
    ``outerHTML`` swap of ``#ov-section-body``, so an empty 286 without it
    would delete the whole Overview instead of leaving it alone.
    """
    owner_id, email, password = seeded_user
    await _seed_two_investments(owner_id)
    await _seed_market_data_schedule(fresh_superuser_engine, enabled=True)
    await _login(web_client, email, password)

    response = await web_client.get(f"/api/overview/refresh/poll{query}")

    assert response.status_code == 286
    assert response.text == ""
    assert response.headers["HX-Reswap"] == "none"


# ---------------------------------------------------------------------------
# Welcome header — ADR-0068
# ---------------------------------------------------------------------------


async def test_front_office_welcome_header_with_display_name(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The header greets by the owner's first name with an accented tenant."""
    user_id, email, password = seeded_user
    await _set_display_name(user_id, "Alex Harper")
    await _login(web_client, email, password)

    response = await web_client.get("/front-office", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    # First whitespace token of display_name, not the full name.
    assert "Welcome back, Alex" in body
    # The tenant name is wrapped in the accent span.
    assert 'fo-welcome__tenant">Sentinel Tenant</span>' in body


async def test_front_office_welcome_header_no_name_fallback(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """display_name=NULL + a non-name email local-part → no-name greeting."""
    _user_id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/front-office", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    # No-name fallback greeting; the tenant clause is still present.
    assert "Welcome —" in body
    assert "Welcome back," not in body
    # The hyphenated email local-part ("overview-section") is not a
    # single name and must never be mangled into a greeting.
    assert "Overview-section" not in body
    assert "Overview-Section" not in body


# ---------------------------------------------------------------------------
# Unit tests — first-name derivation (no DB)
# ---------------------------------------------------------------------------


def test_derive_first_name_prefers_display_name_first_token() -> None:
    assert _derive_first_name("Alex Harper", "x@example.com") == "Alex"


def test_derive_first_name_email_local_only_when_a_single_name() -> None:
    # A clean alphabetic local-part is a usable name…
    assert _derive_first_name(None, "soenke@example.com") == "Soenke"
    # …but dotted / hyphenated / digit-bearing locals are not.
    assert _derive_first_name(None, "j.doe@example.com") is None
    assert _derive_first_name(None, "overview-section@example.com") is None
    assert _derive_first_name(None, "user123@example.com") is None


def test_derive_first_name_none_when_no_signal() -> None:
    assert _derive_first_name(None, "") is None
    assert _derive_first_name("   ", "") is None


# ---------------------------------------------------------------------------
# Unit test — compact money formatter (no DB)
#
# ADR-0101 §3 renamed ``_format_eur_compact`` to ``_format_money_compact`` and
# gave it a currency argument. The EUR cases below are unchanged from the
# pre-block test — that is the point: they are the formatter half of the §4
# invisibility guarantee. The symbol / ISO-prefix branches are covered
# exhaustively in ``tests/services/test_money_format.py``.
# ---------------------------------------------------------------------------


def test_format_money_compact_thresholds() -> None:
    """The k / M / B bands round per ADR-0067's worked examples."""
    assert _format_money_compact(342_600_000, "EUR") == "€342.6M"
    assert _format_money_compact(1_240_000_000, "EUR") == "€1.24B"
    assert _format_money_compact(12_345, "EUR") == "€12k"
    assert _format_money_compact(1_000_000, "EUR") == "€1.0M"
    assert _format_money_compact(1_000, "EUR") == "€1k"
    assert _format_money_compact(500, "EUR") == "€500"
    assert _format_money_compact(0, "EUR") == "€0"


def test_format_money_compact_honours_the_currency() -> None:
    """A non-EUR functional currency stops being mislabelled as ``€``."""
    assert _format_money_compact(342_600_000, "USD") == "$342.6M"
    assert _format_money_compact(1_200_000, "CHF") == "CHF 1.2M"
