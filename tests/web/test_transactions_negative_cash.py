# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The negative-cash indicator — the Area banner (A-11, ADR-0130, S5 P-5c).

ASGI-level tests over a live Postgres, on the fixture pattern of
``tests/web/test_transactions_composer.py``. The indicator's *derivation* is
pinned at the service level
(``tests/services/test_investment_service_negative_cash.py``); this module
pins what the surface does with it:

* **It appears** when a booking takes a cash position below zero, naming the
  position, the balance and the date the run began.
* **It is absent at ≥ 0** — and absent means an *empty wrapper*, because the
  wrapper is what re-fetches itself. This is also where self-clearing is
  pinned: no acknowledgement was ever needed, and none exists.
* **Multi-currency** — one line per position, the counted lead, and no
  figure summed across currencies anywhere.
* **It never blocks** (ADR-0130). With an overdraft standing, the full
  draft → propose → book path succeeds and the ticket is ``booked``; the
  indicator's own body carries no ``hx-post``, no button and no form.
* **The Area shell** carries the empty self-fetching wrapper between the
  header and the first section, with the exact refresh contract (T-5 D-X /
  D-AA), and ``/transactions`` stays a no-DB render.
* **A-18** — the hint sentence appears once in the banner and once above the
  blotter table.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import date as _date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories._session import tenant_context
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.instrument_price_repository import InstrumentPriceRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.position_transaction_repository import (
    PositionTransactionRepository,
)
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_OPENING_DATE = _date(2026, 1, 15)
_INDICATOR_URL = "/api/transactions/negative-cash"

#: The refresh contract, verbatim (T-5 D-X, D-AA). Asserted as one string so
#: a drift in either trigger is a failure rather than a silent stop.
_TRIGGER = "load, htmx:afterRequest from:#tx-composer-host, htmx:afterRequest from:#history"

_HINT = (
    "A booking's cash effect is in the book at once; the cash position's "
    "balance shows it from the next price date on."
)

_MD9 = "The position stays flagged until the balance is back at zero or above."


def _url(value: str | None) -> str:
    assert value is not None
    return value


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB negative-cash tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fixtures (live DB)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    engine = create_async_engine(_url(DATABASE_URL_SUPERUSER), future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


_TRUNCATE = text(
    "TRUNCATE TABLE trade_ticket_effects, trade_tickets, "
    "case_entries, cases, "
    "position_transactions, instrument_prices, "
    "investment_cashflows, investment_navs, investments, asset_classes, "
    "login_audit, sessions, audit_log, data_store_entries, users, tenants "
    "RESTART IDENTITY CASCADE"
)


@pytest_asyncio.fixture
async def reset_schema(superuser_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    async with superuser_engine.begin() as conn:
        await conn.execute(_TRUNCATE)
    try:
        yield
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(_TRUNCATE)


@pytest_asyncio.fixture
async def seeded_user(
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    """Seed the primary tenant and its owner."""
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "negative-cash-owner@example.com"
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, :sub)"),
            {
                "id": str(SENTINEL_TENANT_ID),
                "name": "Sentinel Tenant",
                "sub": "minathena-capital",
            },
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, display_name,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, :dn,
                     ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
                "dn": "R. Falk",
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login_and_csrf(client: AsyncClient, email: str, password: str) -> str:
    """Log in and return the session-bound CSRF token from the area page."""
    get_response = await client.get("/login")
    pre_csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert pre_csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": pre_csrf},
        follow_redirects=False,
    )
    page = await client.get("/transactions", follow_redirects=False)
    assert page.status_code == 200
    marker = 'name="csrf-token" content="'
    idx = page.text.find(marker)
    assert idx != -1
    start = idx + len(marker)
    return page.text[start : page.text.find('"', start)]


def _flat(markup: str) -> str:
    """Collapse whitespace, so a copy assertion is not a line-wrap assertion."""
    return " ".join(markup.split())


async def _seed_investment(
    user_id: UUID,
    *,
    name: str,
    investment_type: str = "listed_equity",
    currency: str = "EUR",
    valuation_mode: str = "unitised",
) -> UUID:
    engine = create_async_engine(_url(DATABASE_URL), future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code=f"ac-{uuid4().hex[:8]}",
                display_name=f"AC {name}",
            )
            investment = await InvestmentRepository(session).create(
                name=name,
                investment_type=investment_type,
                asset_class_id=asset_class.id,
                currency=currency,
                created_by=user_id,
                valuation_mode=valuation_mode,
            )
            return investment.id
    finally:
        await engine.dispose()


async def _seed_ledger(
    user_id: UUID,
    investment_id: UUID,
    rows: tuple[tuple[str, _date, str], ...],
    *,
    currency: str = "EUR",
) -> None:
    engine = create_async_engine(_url(DATABASE_URL), future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            transactions = PositionTransactionRepository(session)
            for txn_type, trade_date, units in rows:
                await transactions.add(
                    investment_id=investment_id,
                    txn_type=txn_type,
                    trade_date=trade_date,
                    units=Decimal(units),
                    currency=currency,
                    ingest_origin="manual",
                    created_by=user_id,
                )
    finally:
        await engine.dispose()


async def _deactivate(user_id: UUID, investment_id: UUID) -> None:
    """Deactivate a position, without touching its ledger (T-5 D-Y)."""
    engine = create_async_engine(_url(DATABASE_URL), future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            await InvestmentRepository(session).set_active(investment_id, False)
    finally:
        await engine.dispose()


async def _seed_price(user_id: UUID, investment_id: UUID, price: Decimal) -> None:
    engine = create_async_engine(_url(DATABASE_URL), future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            await InstrumentPriceRepository(session).upsert(
                investment_id=investment_id,
                as_of_date=_OPENING_DATE,
                price=price,
                currency="EUR",
                source="test",
                created_by=user_id,
                ingest_origin="manual",
            )
    finally:
        await engine.dispose()


async def _standard_book(user_id: UUID) -> tuple[UUID, UUID]:
    """The composer suite's own book: one unitised holding, one EUR cash row."""
    investment_id = await _seed_investment(user_id, name="Alpha Global Equity Fund")
    await _seed_ledger(user_id, investment_id, (("opening", _OPENING_DATE, "1250"),))
    await _seed_price(user_id, investment_id, Decimal("104.30"))
    cash_id = await _seed_investment(user_id, name="EUR Cash — Commerzbank", investment_type="cash")
    await _seed_ledger(user_id, cash_id, (("opening", _OPENING_DATE, "412500"),))
    return investment_id, cash_id


def _overdrawing_form(investment_id: UUID, cash_id: UUID, csrf: str) -> dict[str, str]:
    """The composer suite's overdrawing buy: 521,400.00 against 412,500.00."""
    return {
        "direction": "buy",
        "trade_date": _OPENING_DATE.isoformat(),
        "units": "5000",
        "price_per_unit": "104.10",
        "fees": "900",
        "taxes": "",
        "investment_id": str(investment_id),
        "cash_investment_id": str(cash_id),
        "settle_confirm": "1",
        "csrf_token": csrf,
    }


def _assert_offers_nothing(body: str) -> None:
    """The indicator never blocks and never asks (ADR-0130, A-11).

    No gesture of any kind lives on it: nothing to press, nothing to submit,
    nothing to acknowledge. Asserted on every render — the populated one and
    the empty one — because "self-clearing" means precisely that no
    acknowledgement was ever needed and none exists.
    """
    assert "<button" not in body
    assert "hx-post" not in body
    assert "<form" not in body


# ---------------------------------------------------------------------------
# It appears when a booking takes a position below zero
# ---------------------------------------------------------------------------


async def test_indicator_names_the_position_the_balance_and_the_date(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A booking into the red, then the banner that outlives its notice."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    booked = await web_client.post(
        "/api/transactions/book",
        data=_overdrawing_form(investment_id, cash_id, csrf),
    )
    assert booked.status_code == 200

    response = await web_client.get(_INDICATOR_URL)
    assert response.status_code == 200
    body = response.text
    flat = _flat(body)

    assert "A cash position is below zero." in flat
    assert "EUR Cash — Commerzbank" in flat
    assert "−108,900.00 EUR" in flat
    # `since` is the trade date of the transaction that opened the run.
    assert f"since {_OPENING_DATE.isoformat()}" in flat
    assert f'href="/investments/{cash_id}"' in body
    assert _MD9 in flat
    assert _HINT in flat
    # It is not the traded position that is flagged, only the cash one.
    assert "Alpha Global Equity Fund" not in body
    _assert_offers_nothing(body)


# ---------------------------------------------------------------------------
# Absent at >= 0 — and absent is an empty wrapper
# ---------------------------------------------------------------------------


async def test_indicator_clears_itself_when_the_balance_is_restored(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Restored to zero or above, the wrapper comes back empty.

    Nothing was acknowledged and nothing was cleared: the state is re-derived
    on every call, so a healthy ledger simply has no line to state. The
    wrapper itself stays, because it is the element that asks again.
    """
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    await web_client.post(
        "/api/transactions/book",
        data=_overdrawing_form(investment_id, cash_id, csrf),
    )
    assert "tx-indicator" in (await web_client.get(_INDICATOR_URL)).text

    # An inflow that restores the balance, dated today so it counts today.
    await _seed_ledger(user_id, cash_id, (("transfer", _date.today(), "108900"),))

    response = await web_client.get(_INDICATOR_URL)
    assert response.status_code == 200
    body = response.text
    assert "tx-indicator" not in body
    assert "EUR Cash — Commerzbank" not in body
    assert _MD9 not in _flat(body)
    # The wrapper survives, with its refresh contract intact.
    assert 'id="tx-negative-cash"' in body
    assert f'hx-trigger="{_TRIGGER}"' in body
    _assert_offers_nothing(body)


# ---------------------------------------------------------------------------
# Multi-currency — two facts, never one figure
# ---------------------------------------------------------------------------


async def test_two_currencies_render_two_lines_and_no_total(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    eur_id = await _seed_investment(user_id, name="Cash EUR · Main custody", investment_type="cash")
    await _seed_ledger(
        user_id,
        eur_id,
        (("opening", _OPENING_DATE, "100000"), ("transfer", _date(2026, 9, 5), "-142310")),
    )
    usd_id = await _seed_investment(
        user_id, name="Cash USD", investment_type="cash", currency="USD"
    )
    await _seed_ledger(
        user_id,
        usd_id,
        (("opening", _OPENING_DATE, "5000"), ("transfer", _date(2026, 9, 2), "-6250")),
        currency="USD",
    )
    await _login_and_csrf(web_client, email, password)

    response = await web_client.get(_INDICATOR_URL)
    body = response.text
    flat = _flat(body)

    assert "Two cash positions are below zero." in flat
    assert body.count('class="tx-indicator__line"') == 2
    assert "−42,310.00 EUR" in flat
    assert "−1,250.00 USD" in flat
    assert f'href="/investments/{eur_id}"' in body
    assert f'href="/investments/{usd_id}"' in body
    # No sum across currencies: neither the arithmetic total nor a
    # currency-less figure appears anywhere in the body.
    assert "43,560" not in flat
    assert "−43,560.00" not in flat
    # The MD-9 sentence is stated once for the block, not once per line.
    assert flat.count(_MD9) == 1
    _assert_offers_nothing(body)


# ---------------------------------------------------------------------------
# It never blocks (ADR-0130)
# ---------------------------------------------------------------------------


async def test_a_standing_overdraft_blocks_no_gesture(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """Draft, propose and book all succeed while a position stands below zero.

    The second ticket is booked *through* the overdraft — the cash position is
    already negative when it is proposed and negative again when it books. The
    indicator observes; it has no say.
    """
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    first = await web_client.post(
        "/api/transactions/book",
        data=_overdrawing_form(investment_id, cash_id, csrf),
    )
    assert first.status_code == 200
    assert "tx-indicator" in (await web_client.get(_INDICATOR_URL)).text

    form = {
        "direction": "buy",
        "trade_date": _OPENING_DATE.isoformat(),
        "units": "10",
        "price_per_unit": "104.10",
        "fees": "",
        "taxes": "",
        "investment_id": str(investment_id),
        "cash_investment_id": str(cash_id),
        "settle_confirm": "1",
        "csrf_token": csrf,
    }
    # Each gesture on an unsaved composer allocates its own ticket (MD-2),
    # which is exactly what the composer suite does; none of the three is
    # given the overdraft as a reason to refuse.
    for endpoint in ("draft", "propose", "book"):
        response = await web_client.post(f"/api/transactions/{endpoint}", data=form)
        assert response.status_code == 200, endpoint
        assert "tx-msg--block" not in response.text, endpoint

    async with superuser_engine.begin() as conn:
        rows = await conn.execute(text("SELECT status FROM trade_tickets ORDER BY ticket_number"))
        statuses = [row[0] for row in rows]
    # The overdrawing booking, then a draft, a proposal, and a booking made
    # while the position stood below zero.
    assert statuses == ["booked", "draft", "proposed", "booked"]

    _assert_offers_nothing((await web_client.get(_INDICATOR_URL)).text)


# ---------------------------------------------------------------------------
# The Area shell
# ---------------------------------------------------------------------------


async def test_area_shell_carries_the_empty_self_fetching_wrapper(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The banner's shell sits between the header and the first section.

    The Area render is no-DB, so the element arrives empty and fetches itself.
    The existing area tests prove the route renders without any seeding; this
    one adds that the wrapper is there, in the right place, with the exact
    refresh contract.
    """
    _user_id, email, password = seeded_user
    await _login_and_csrf(web_client, email, password)

    response = await web_client.get(
        "/transactions", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert response.status_code == 200
    body = response.text

    assert 'id="tx-negative-cash"' in body
    assert f'hx-get="{_INDICATOR_URL}"' in body
    assert f'hx-trigger="{_TRIGGER}"' in body
    assert 'hx-swap="outerHTML"' in body
    # Empty: nothing has been derived, because nothing was read.
    assert "tx-indicator" not in body

    header_end = body.index("</header>")
    wrapper = body.index('id="tx-negative-cash"')
    first_section = body.index('<section class="pf-section" id="new"')
    assert header_end < wrapper < first_section


# ---------------------------------------------------------------------------
# A-18 — the hint, once in each of its two places
# ---------------------------------------------------------------------------


async def test_a18_hint_appears_once_in_the_banner_and_once_on_the_blotter(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    await _seed_ledger(user_id, cash_id, (("transfer", _date(2026, 2, 1), "-500000"),))
    csrf = await _login_and_csrf(web_client, email, password)

    banner = await web_client.get(_INDICATOR_URL)
    assert _flat(banner.text).count(_HINT) == 1

    # One draft, so the blotter renders its table and the hint has something
    # to sit above.
    saved = await web_client.post(
        "/api/transactions/draft",
        data={
            "direction": "sell",
            "trade_date": _OPENING_DATE.isoformat(),
            "units": "400",
            "price_per_unit": "104.10",
            "fees": "180",
            "taxes": "45",
            "investment_id": str(investment_id),
            "cash_investment_id": str(cash_id),
            "settle_confirm": "1",
            "csrf_token": csrf,
        },
    )
    assert saved.status_code == 200

    blotter = await web_client.get("/api/transactions/blotter")
    assert blotter.status_code == 200
    flat = _flat(blotter.text)
    assert flat.count(_HINT) == 1
    # Above the table, not below it.
    assert flat.index(_HINT) < flat.index("<table")
    assert 'class="tx-blotter__hint"' in blotter.text


# ---------------------------------------------------------------------------
# `since` end-to-end — the second run, not the first
# ---------------------------------------------------------------------------


async def test_since_names_the_current_run_after_a_recovery(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Negative, recovered, negative again: the banner shows the second start."""
    user_id, email, password = seeded_user
    today = _date.today()
    first_run = today - timedelta(days=120)
    recovery = today - timedelta(days=60)
    second_run = today - timedelta(days=3)
    cash_id = await _seed_investment(user_id, name="Cash EUR · Twice", investment_type="cash")
    await _seed_ledger(
        user_id,
        cash_id,
        (
            ("opening", _OPENING_DATE, "100"),
            ("transfer", first_run, "-300"),
            ("transfer", recovery, "500"),
            ("transfer", second_run, "-800"),
        ),
    )
    await _login_and_csrf(web_client, email, password)

    flat = _flat((await web_client.get(_INDICATOR_URL)).text)
    assert f"since {second_run.isoformat()}" in flat
    assert first_run.isoformat() not in flat
    assert "−500.00 EUR" in flat


# ---------------------------------------------------------------------------
# The two copy branches the cases above do not reach
# ---------------------------------------------------------------------------


async def test_more_than_two_positions_are_counted_and_inactive_ones_labelled(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The ``<n>`` lead, and D-Y's label.

    A deactivated cash position keeps its overdraft — deactivating it did not
    settle anything — so it is listed, with the label that says what it is.
    """
    user_id, email, password = seeded_user
    for index in range(3):
        position_id = await _seed_investment(
            user_id, name=f"Cash EUR · {index}", investment_type="cash"
        )
        await _seed_ledger(
            user_id,
            position_id,
            (("opening", _OPENING_DATE, "1"), ("transfer", _date(2026, 2, 1), "-11")),
        )
    retired_id = await _seed_investment(user_id, name="Cash EUR · Retired", investment_type="cash")
    await _seed_ledger(
        user_id,
        retired_id,
        (("opening", _OPENING_DATE, "1"), ("transfer", _date(2026, 2, 1), "-6")),
    )
    await _deactivate(user_id, retired_id)
    await _login_and_csrf(web_client, email, password)

    body = (await web_client.get(_INDICATOR_URL)).text
    flat = _flat(body)

    assert "4 cash positions are below zero." in flat
    assert body.count('class="tx-indicator__line"') == 4
    assert "Cash EUR · Retired</a> · inactive stands at" in flat
    # Only the retired one is labelled.
    assert flat.count("· inactive stands at") == 1
    _assert_offers_nothing(body)
