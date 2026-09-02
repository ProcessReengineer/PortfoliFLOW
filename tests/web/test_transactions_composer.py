# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the order composer's read surface (ADR-0128, S4a).

ASGI-level tests over a live Postgres, in the fixture style of
``tests/web/test_positions_routes.py`` (superuser-seeded tenant and owner,
repository helpers for domain rows, a login helper that returns the
session-bound CSRF token).

What is pinned here:

* **The chooser** (MD-1) — five tiles with the mockup's copy, exactly one of
  them wired, four inert. This is the sharper successor to the "no controls"
  pin the New-transaction section left behind when S4a filled it.
* **The picker** — unitised, active, non-cash, and tenant-scoped.
* **That nothing is written.** The recalculation endpoint is a read: a
  warning-heavy round trip leaves the ``trade_tickets`` and ``investments``
  row counts exactly as it found them (MD-2 — the ticket row arrives with
  P-2's first gesture, not with a keystroke).
* **The derived numbers** — the legs are ``order_legs``'s, the amounts are
  ``derive_cash_effect``'s, and the settlement projection is the cash leg's.
  The expected values are written out longhand rather than recomputed from
  the service, so a change of sign convention fails here loudly.
* **The three MD-3 settlement states plus the D-F fourth**: one match,
  several, none (which offers creation), and every row retired (which
  deliberately does not).
* **The gating** — Book now and Propose stay disabled until the ticket is
  structurally complete, the settlement position is confirmed and no block
  stands. Save as draft asks only for an investment and survives both
  (operator decision W-3); Discard is never gated.

* **The gestures** — Save as draft, Propose and Book now, the MD-16
  confirmation panel and the MD-3 inline cash-position mini-form. They share
  this module's fixtures rather than a sibling's copy of them: the book they
  act on is the same book the read surface derives from, and two seedings of
  "the M-1 book" would be two books.

Not here: the wizard and reported flows (S4b / S4c), or the blotter and
history surfaces (S5).
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from datetime import date as _date, datetime, timedelta, timezone
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
from core.repositories.case_repository import CaseRepository
from core.repositories.instrument_price_repository import InstrumentPriceRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.position_transaction_repository import PositionTransactionRepository
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

#: A second tenant, for the RLS assertions.
_OTHER_TENANT_ID = UUID("11111111-2222-3333-4444-555555555555")

#: A settled past date, so the future-trade-date warning never fires by
#: accident and every rendered date is stable.
_TRADE_DATE = _date(2026, 1, 15)
_OPENING_DATE = _date(2026, 1, 2)


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; skipping live-DB composer tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
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
    """Seed both tenants and the primary tenant's owner."""
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "composer-owner@example.com"
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, :sub)"),
            [
                {
                    "id": str(SENTINEL_TENANT_ID),
                    "name": "Sentinel Tenant",
                    "sub": "minathena-capital",
                },
                {"id": str(_OTHER_TENANT_ID), "name": "Other Tenant", "sub": "other"},
            ],
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
                "dn": "S. Behrens",
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
# Seeding helpers
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


async def _seed_investment(
    user_id: UUID,
    *,
    name: str,
    investment_type: str = "listed_equity",
    currency: str = "EUR",
    valuation_mode: str = "unitised",
    is_active: bool = True,
    tenant_id: UUID = SENTINEL_TENANT_ID,
) -> UUID:
    """Create one investment (with its own asset class) in a tenant."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as session:
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
                is_active=is_active,
                valuation_mode=valuation_mode,
            )
            return investment.id
    finally:
        await engine.dispose()


async def _seed_opening(
    user_id: UUID,
    investment_id: UUID,
    units: Decimal,
    *,
    currency: str = "EUR",
    on: _date = _OPENING_DATE,
) -> None:
    """Give a position its opening balance."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            await PositionTransactionRepository(session).add(
                investment_id=investment_id,
                txn_type="opening",
                trade_date=on,
                units=units,
                currency=currency,
                ingest_origin="manual",
                created_by=user_id,
            )
    finally:
        await engine.dispose()


async def _seed_price(user_id: UUID, investment_id: UUID, price: Decimal) -> None:
    """Store one instrument price, the deviation warning's reference."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
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


async def _seed_case(user_id: UUID, title: str) -> None:
    """Open one case, for the Provenance block's select."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            await CaseRepository(session).create(
                title=title,
                opened_by=user_id,
                description="Seeded for the composer's Provenance select.",
                now=datetime.now(timezone.utc),
            )
    finally:
        await engine.dispose()


async def _count(engine: AsyncEngine, table: str) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
        return int(result.scalar_one())


async def _standard_book(user_id: UUID) -> tuple[UUID, UUID]:
    """The M-1 book: one unitised holding of 1,250 and one EUR cash position.

    Prices, units and balances are the mockup's own, so the numbers these
    tests assert on are the numbers the mockup shows.
    """
    investment_id = await _seed_investment(user_id, name="Alpha Global Equity Fund")
    await _seed_opening(user_id, investment_id, Decimal("1250"))
    await _seed_price(user_id, investment_id, Decimal("104.30"))
    cash_id = await _seed_investment(user_id, name="EUR Cash — Commerzbank", investment_type="cash")
    await _seed_opening(user_id, cash_id, Decimal("412500"))
    return investment_id, cash_id


# ---------------------------------------------------------------------------
# Markup helpers
# ---------------------------------------------------------------------------

_BUTTON = re.compile(r"<button\b(?P<attrs>[^>]*)>\s*(?P<label>[^<]*?)\s*</button>", re.S)


def _assert_signs_are_single(markup: str) -> None:
    """No number carries its sign twice.

    A doubled sign is invisible to a substring assertion — "−−108,900.00"
    contains "−108,900.00" — so the shape of every formatted number is
    asserted separately from its value.
    """
    assert "−−" not in markup, "a number is rendered with a doubled minus"
    assert "++" not in markup, "a number is rendered with a doubled plus"
    assert "+−" not in markup and "−+" not in markup, "a number carries two signs"


def _flat(markup: str) -> str:
    """Collapse whitespace, so a copy assertion is not a line-wrap assertion.

    The templates wrap MD-9's sentences to stay readable; HTML collapses the
    runs when it renders them, and so does this. Assertions on markup tokens
    (class names, attributes) use the raw body instead.
    """
    return " ".join(markup.split())


def _new_section(body: str) -> str:
    """Slice the New-transaction Section out of the area page."""
    start = body.index('<section class="pf-section" id="new"')
    return body[start : body.index("</section>", start) + len("</section>")]


def _actions(markup: str) -> dict[str, bool]:
    """Map each primary action's label to whether it is disabled."""
    start = markup.index('<div class="tx-actions">')
    region = markup[start : markup.index("</div>", markup.index("</p>", start))]
    return {m.group("label"): "disabled" in m.group("attrs") for m in _BUTTON.finditer(region)}


def _recalc_form(**overrides: str) -> dict[str, str]:
    """The composer's posted body, with M-1's clean-sell values by default."""
    form: dict[str, str] = {
        "direction": "sell",
        "trade_date": _TRADE_DATE.isoformat(),
        "units": "400",
        "price_per_unit": "104.10",
        "fees": "180",
        "taxes": "45",
    }
    form.update(overrides)
    return form


# ---------------------------------------------------------------------------
# Chooser (MD-1)
# ---------------------------------------------------------------------------


async def test_chooser_renders_five_tiles_one_of_them_live(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The MD-1 chooser: five tiles, one wired, four pilled and inert."""
    _id, email, password = seeded_user
    await _login_and_csrf(web_client, email, password)

    response = await web_client.get("/transactions", follow_redirects=False)
    section = _new_section(response.text)

    for name, hint, code in (
        ("Buy or sell units", "An instrument already on the book.", "U-BUY / U-SELL"),
        ("Buy a new instrument", "Create the investment, then buy it.", "U-NEW"),
        ("New commitment", "Private markets. No cash moves yet.", "R-COMMIT"),
        ("Buy a stake (secondary)", "Acquire an existing fund interest.", "R-SEC-BUY"),
        ("Sell a stake (secondary)", "Exit a fund interest in full.", "R-SEC-SELL"),
    ):
        assert name in section
        assert hint in section
        assert code in section

    assert section.count('hx-get="/api/transactions/order-form"') == 1
    assert section.count("<button") == 1, "only the U-BUY / U-SELL tile is a control"
    assert section.count("Arrives with S4b") == 1
    assert section.count("Arrives with S4c") == 3
    for token in ("<form", "<input", "<a "):
        assert token not in section, f"the chooser carries an unexpected control: {token!r}"


# ---------------------------------------------------------------------------
# Order form
# ---------------------------------------------------------------------------


async def test_order_form_picker_is_unitised_active_and_non_cash(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The picker offers exactly what M-1's hint promises, tenant-scoped."""
    user_id, email, password = seeded_user
    await _seed_investment(user_id, name="Tradable Fund")
    await _seed_investment(user_id, name="Reported Fund", valuation_mode="reported")
    await _seed_investment(user_id, name="Retired Fund", is_active=False)
    await _seed_investment(user_id, name="EUR Cash — Main", investment_type="cash")
    await _seed_investment(user_id, name="Foreign Fund", tenant_id=_OTHER_TENANT_ID)
    await _login_and_csrf(web_client, email, password)

    response = await web_client.get("/api/transactions/order-form")
    assert response.status_code == 200
    body = response.text

    assert "Unitised and active investments only." in _flat(body)
    assert "Tradable Fund — EUR" in _flat(body)
    for excluded in ("Reported Fund", "Retired Fund", "EUR Cash — Main", "Foreign Fund"):
        assert excluded not in body, f"{excluded!r} must not be offered in the picker"


async def test_order_form_lists_open_cases(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The Provenance block offers the tenant's open cases."""
    user_id, email, password = seeded_user
    await _seed_case(user_id, "Equity overweight vs. SAA band")
    await _login_and_csrf(web_client, email, password)

    body = (await web_client.get("/api/transactions/order-form")).text
    assert "No case" in _flat(body)
    assert "Equity overweight vs. SAA band" in _flat(body)


async def test_order_form_starts_gated_and_unsaved(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An empty composer is unsaved (MD-2) and acts on nothing."""
    _id, email, password = seeded_user
    await _login_and_csrf(web_client, email, password)

    body = (await web_client.get("/api/transactions/order-form")).text
    assert "New ticket" in body
    assert "Unsaved" in body
    assert _actions(body) == {
        "Book now": True,
        "Propose": True,
        "Save as draft": True,
        "Discard": False,
    }


# ---------------------------------------------------------------------------
# Recalculation — the read that writes nothing
# ---------------------------------------------------------------------------


async def test_recalc_writes_nothing(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """A warning-heavy recalculation leaves the book exactly as it was.

    MD-2 puts ticket persistence on the first explicit gesture; a keystroke
    is not one. The investment count is checked alongside it because the
    composer's P-2 mini-form is the only thing that may ever create a row
    from this surface — and it is not this endpoint.
    """
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    before = (
        await _count(superuser_engine, "trade_tickets"),
        await _count(superuser_engine, "investments"),
        await _count(superuser_engine, "position_transactions"),
    )
    response = await web_client.post(
        "/api/transactions/recalc",
        data=_recalc_form(
            direction="buy",
            units="5000",
            price_per_unit="104.10",
            fees="900",
            taxes="",
            trade_date=(_date.today() + timedelta(days=5)).isoformat(),
            investment_id=str(investment_id),
            cash_investment_id=str(cash_id),
            settle_confirm="1",
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200
    after = (
        await _count(superuser_engine, "trade_tickets"),
        await _count(superuser_engine, "investments"),
        await _count(superuser_engine, "position_transactions"),
    )
    assert before == after


async def test_recalc_without_csrf_is_refused(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The uniform POST posture holds even though the endpoint only reads."""
    _id, email, password = seeded_user
    await _login_and_csrf(web_client, email, password)

    response = await web_client.post("/api/transactions/recalc", data=_recalc_form())
    assert response.status_code == 403


async def test_sparse_recalc_derives_little_and_gates_everything(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """With no instrument the ledger block is a placeholder (D-3)."""
    _id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/recalc",
        data={"direction": "sell", "csrf_token": csrf},
    )
    assert response.status_code == 200
    body = response.text
    assert "Both legs appear here once the investment, units, price and" in body, (
        "the ledger block must not show half an emission"
    )
    assert "tx-leg__type" not in body
    assert "Fill in the investment, units and price before booking." in _flat(body)
    actions = _actions(body)
    assert actions["Book now"] and actions["Propose"] and actions["Save as draft"]
    assert not actions["Discard"], "Discard is never gated — there is nothing to discard"


# ---------------------------------------------------------------------------
# The complete sell
# ---------------------------------------------------------------------------


async def test_complete_sell_renders_both_legs_and_gates_on_the_tick(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """M-1's clean sell: two legs, the amounts, and the MD-3 confirmation.

    400 units at 104.10 with 180 fees and 45 taxes: gross 41,640.00 and net
    proceeds 41,415.00, an instrument leg of −400.0000 units at 104.1000 and
    a cash leg of +41,415.0000 units at unity.
    """
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    form = _recalc_form(
        investment_id=str(investment_id),
        cash_investment_id=str(cash_id),
        csrf_token=csrf,
    )
    unconfirmed = (await web_client.post("/api/transactions/recalc", data=form)).text

    assert "400.0000 units × 104.1000" in unconfirmed
    assert "41,640.00" in unconfirmed
    assert "+41,415.00" in unconfirmed
    assert "Net proceeds" in unconfirmed
    _assert_signs_are_single(unconfirmed)
    assert "−400.0000 units" in unconfirmed
    assert "+41,415.0000 units" in unconfirmed
    assert "@ 1.0000" in unconfirmed
    assert "Alpha Global Equity Fund" in unconfirmed
    assert "EUR Cash — Commerzbank" in unconfirmed
    assert "1,250.0000 units" in unconfirmed, "the context strip states the holding"

    assert "Confirm the settlement position before booking." in _flat(unconfirmed)
    assert _actions(unconfirmed)["Book now"] is True

    confirmed = (
        await web_client.post("/api/transactions/recalc", data={**form, "settle_confirm": "1"})
    ).text
    assert _actions(confirmed) == {
        "Book now": False,
        "Propose": False,
        "Save as draft": False,
        "Discard": False,
    }
    assert "Booking records the decision and both ledger legs in one step." in _flat(confirmed)


async def test_full_disposal_offers_the_inactivation_choice(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Selling the whole holding is a consequence, not a warning (MD-7)."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    body = (
        await web_client.post(
            "/api/transactions/recalc",
            data=_recalc_form(
                units="1250",
                investment_id=str(investment_id),
                cash_investment_id=str(cash_id),
                settle_confirm="1",
                csrf_token=csrf,
            ),
        )
    ).text
    assert "tx-msg--consequence" in body
    assert "This sells the entire holding." in _flat(body)
    assert "The position closes at zero units." in _flat(body)
    assert "Set Alpha Global Equity Fund inactive after booking." in _flat(body)
    assert _actions(body)["Book now"] is False, "a consequence never gates anything"


# ---------------------------------------------------------------------------
# Blocks and warnings
# ---------------------------------------------------------------------------


async def test_oversell_blocks_with_the_holding_and_the_date(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The oversell block carries M-1's copy and stops the gestures."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    body = (
        await web_client.post(
            "/api/transactions/recalc",
            data=_recalc_form(
                units="1400",
                investment_id=str(investment_id),
                cash_investment_id=str(cash_id),
                settle_confirm="1",
                csrf_token=csrf,
            ),
        )
    ).text
    assert "tx-msg--block" in body
    assert f"1,400.0000 units exceed the holding of 1,250.0000 units on {_TRADE_DATE}." in body
    assert "The instrument leg cannot go negative." in body
    assert _actions(body)["Book now"] is True
    # W-3: a block stops the ticket becoming a proposal, never the draft.
    # The user's work is worth keeping even when the numbers are wrong.
    assert _actions(body)["Save as draft"] is False


async def test_buy_short_of_cash_warns_and_marks_the_projection(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """MD-5(a): the resulting balance, live, and never a refusal.

    5,000 units at 104.10 plus 900 in fees costs 521,400.00 against a
    balance of 412,500.00 — the position lands at −108,900.00.
    """
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    body = (
        await web_client.post(
            "/api/transactions/recalc",
            data=_recalc_form(
                direction="buy",
                units="5000",
                fees="900",
                taxes="",
                investment_id=str(investment_id),
                cash_investment_id=str(cash_id),
                settle_confirm="1",
                csrf_token=csrf,
            ),
        )
    ).text
    _assert_signs_are_single(body)
    assert "EUR Cash — Commerzbank goes to" in _flat(body)
    assert "−108,900.00 EUR" in _flat(body)
    assert "Booking is allowed — the trade is your call." in _flat(body)
    assert "tx-num--neg" in body, "the projected balance is marked negative"
    assert _actions(body)["Book now"] is False, "a warning never disables anything (MD-5)"


async def test_future_trade_date_warns(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A post-dated ticket is unusual, not refused."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    future = _date.today() + timedelta(days=5)
    body = (
        await web_client.post(
            "/api/transactions/recalc",
            data=_recalc_form(
                trade_date=future.isoformat(),
                investment_id=str(investment_id),
                cash_investment_id=str(cash_id),
                settle_confirm="1",
                csrf_token=csrf,
            ),
        )
    ).text
    assert f"Trade date {future} is in the future." in _flat(body)
    assert "The book records facts. Post-dating is allowed but unusual." in _flat(body)
    assert _actions(body)["Book now"] is False


async def test_price_deviation_warns_with_its_reference(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """98.20 against a stored 104.30 is 5.8 % below — over the 5 % threshold."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    body = (
        await web_client.post(
            "/api/transactions/recalc",
            data=_recalc_form(
                price_per_unit="98.20",
                investment_id=str(investment_id),
                cash_investment_id=str(cash_id),
                settle_confirm="1",
                csrf_token=csrf,
            ),
        )
    ).text
    assert "Execution price is 5.8 % below the last known price." in _flat(body)
    assert f"Last price 104.3000 EUR on {_OPENING_DATE}." in _flat(body)
    assert "your execution price is the better fact." in _flat(body)
    assert _actions(body)["Book now"] is False


# ---------------------------------------------------------------------------
# Settlement states (MD-3 and D-F)
# ---------------------------------------------------------------------------


async def test_settlement_one_match_asks_for_the_tick(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """One match is pre-shown but never pre-confirmed (MD-3)."""
    user_id, email, password = seeded_user
    investment_id, _cash = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    body = (
        await web_client.post(
            "/api/transactions/recalc",
            data=_recalc_form(investment_id=str(investment_id), csrf_token=csrf),
        )
    ).text
    assert "One EUR cash position on the book. Confirm it or pick another." in _flat(body)
    assert "Settle against this position." in body
    assert "412,500.00" in body


async def test_settlement_several_matches_offer_no_default(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Several matches: a radio each, no default, and the projection on the pick."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    second = await _seed_investment(user_id, name="EUR Cash — DZ Bank", investment_type="cash")
    await _seed_opening(user_id, second, Decimal("88120.44"))
    csrf = await _login_and_csrf(web_client, email, password)

    body = (
        await web_client.post(
            "/api/transactions/recalc",
            data=_recalc_form(
                investment_id=str(investment_id),
                cash_investment_id=str(cash_id),
                csrf_token=csrf,
            ),
        )
    ).text
    assert (
        "2 EUR cash positions on the book. Pick the one the money actually moved on — "
        "there is no default." in _flat(body)
    )
    assert body.count('name="cash_investment_id"') == 2
    # The projection is shown on the selected row only.
    assert body.count("tx-settle__arrow") == 1
    assert "453,915.00" in body, "412,500.00 + 41,415.00 on the picked row"


async def test_settlement_none_offers_creation(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """No cash row in the currency at all: the MD-3 creation offer (D-F)."""
    user_id, email, password = seeded_user
    investment_id = await _seed_investment(user_id, name="Alpha Global Equity Fund")
    await _seed_opening(user_id, investment_id, Decimal("1250"))
    csrf = await _login_and_csrf(web_client, email, password)

    body = (
        await web_client.post(
            "/api/transactions/recalc",
            data=_recalc_form(investment_id=str(investment_id), csrf_token=csrf),
        )
    ).text
    assert "No EUR cash position exists." in _flat(body)
    assert "PortfoliFLOW never converts on your behalf" in _flat(body)
    assert "Pick or create a settlement position before booking." in _flat(body)


async def test_settlement_all_retired_does_not_offer_creation(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """D-F: a retired position is not a missing one, so nothing is offered."""
    user_id, email, password = seeded_user
    investment_id = await _seed_investment(user_id, name="Alpha Global Equity Fund")
    await _seed_opening(user_id, investment_id, Decimal("1250"))
    await _seed_investment(
        user_id, name="EUR Cash — Retired", investment_type="cash", is_active=False
    )
    csrf = await _login_and_csrf(web_client, email, password)

    body = (
        await web_client.post(
            "/api/transactions/recalc",
            data=_recalc_form(investment_id=str(investment_id), csrf_token=csrf),
        )
    ).text
    assert "Every EUR cash position has been deactivated." in _flat(body)
    assert "No EUR cash position exists." not in _flat(body)
    assert "never converts on your behalf" not in _flat(body)
    assert 'name="cash_investment_id"' not in body


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_cross_tenant_investment_id_reads_as_absent(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An id RLS hides is treated as unsaid — no context, no legs, no error."""
    user_id, email, password = seeded_user
    foreign_id = await _seed_investment(user_id, name="Foreign Fund", tenant_id=_OTHER_TENANT_ID)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/recalc",
        data=_recalc_form(investment_id=str(foreign_id), csrf_token=csrf),
    )
    assert response.status_code == 200
    body = response.text
    assert "Foreign Fund" not in body
    assert "tx-leg__type" not in body
    assert (
        "Both legs appear here once the investment, units, price and settlement position are set."
        in _flat(body)
    )
    assert "Pick an investment first — the settlement position follows its currency." in _flat(body)


# ---------------------------------------------------------------------------
# Gestures — seeding and reading back
# ---------------------------------------------------------------------------


async def _seed_member(superuser_engine: AsyncEngine) -> tuple[str, str]:
    """Seed a member of the primary tenant, for the owner-gating assertions."""
    plaintext = "correct-horse-battery-staple"
    email = "composer-member@example.com"
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, display_name,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, 'M. Ruiz',
                     ARRAY['member']::text[], TRUE)
                """
            ),
            {
                "id": str(uuid4()),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
            },
        )
    return email, plaintext


async def _tickets(engine: AsyncEngine) -> list[tuple[UUID, int, str]]:
    """Every trade ticket in the database, as (id, number, status)."""
    async with engine.begin() as conn:
        rows = await conn.execute(
            text("SELECT id, ticket_number, status FROM trade_tickets ORDER BY ticket_number")
        )
        return [(row[0], row[1], row[2]) for row in rows]


async def _ledger(user_id: UUID, investment_id: UUID) -> list[tuple[str, Decimal, _date]]:
    """One investment's ledger rows, as (txn_type, units, trade_date)."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            rows = await PositionTransactionRepository(session).list_for_investment(investment_id)
            return [(row.txn_type, row.units, row.trade_date) for row in rows]
    finally:
        await engine.dispose()


async def _deactivate(user_id: UUID, investment_id: UUID) -> None:
    """Retire an investment behind the composer's back — the race the D-P block guards."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            await InvestmentRepository(session).set_active(investment_id, is_active=False)
    finally:
        await engine.dispose()


def _gesture_form(**overrides: str) -> dict[str, str]:
    """M-1's clean sell, complete and confirmed — what a gesture posts."""
    form = _recalc_form(settle_confirm="1")
    form.update(overrides)
    return form


# ---------------------------------------------------------------------------
# MD-2 — the first explicit gesture allocates the ticket, and only the first
# ---------------------------------------------------------------------------


async def test_opening_and_typing_allocate_no_ticket(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """MD-2: neither the composer nor a keystroke burns a ticket number."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    await web_client.get("/api/transactions/order-form")
    await web_client.post(
        "/api/transactions/recalc",
        data=_gesture_form(
            investment_id=str(investment_id),
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    assert await _count(superuser_engine, "trade_tickets") == 0


async def test_saving_twice_updates_one_ticket(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The first Save allocates; the second edits the row it allocated (MD-2)."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    form = _gesture_form(
        investment_id=str(investment_id),
        cash_investment_id=str(cash_id),
        csrf_token=csrf,
    )
    first = await web_client.post("/api/transactions/draft", data=form)
    assert first.status_code == 200

    rows = await _tickets(superuser_engine)
    assert len(rows) == 1
    ticket_id, number, status_value = rows[0]
    assert number == 1
    assert status_value == "draft"

    body = first.text
    assert f"Ticket #{number}" in body
    assert "New ticket" not in body
    assert f'name="ticket_id" value="{ticket_id}"' in body
    assert "Draft" in body
    # The saved draft is worth keeping, so the escape hatch stops destroying.
    assert "Close" in _actions(body)
    assert "Discard" not in _actions(body)

    second = await web_client.post(
        "/api/transactions/draft",
        data={**form, "ticket_id": str(ticket_id), "units": "500"},
    )
    assert second.status_code == 200
    assert await _tickets(superuser_engine) == [(ticket_id, number, "draft")]

    async with superuser_engine.begin() as conn:
        units = await conn.execute(
            text("SELECT units FROM trade_tickets WHERE id = :id"), {"id": str(ticket_id)}
        )
        assert units.scalar_one() == Decimal("500")


# ---------------------------------------------------------------------------
# W-3 — a draft may dangle
# ---------------------------------------------------------------------------


async def test_draft_saves_under_a_block_that_stops_propose(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """W-3: `draft` is a private workspace; `proposed` means validated."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    oversold = _gesture_form(
        units="1400",
        investment_id=str(investment_id),
        cash_investment_id=str(cash_id),
        csrf_token=csrf,
    )
    saved = await web_client.post("/api/transactions/draft", data=oversold)
    assert saved.status_code == 200
    rows = await _tickets(superuser_engine)
    assert len(rows) == 1 and rows[0][2] == "draft"
    assert "1,400.0000 units exceed the holding" in saved.text

    refused = await web_client.post(
        "/api/transactions/propose",
        data={**oversold, "ticket_id": str(rows[0][0])},
    )
    assert refused.status_code == 200
    assert "tx-msg--block" in refused.text
    # Nothing moved: the same one ticket, still a draft.
    assert await _tickets(superuser_engine) == [(rows[0][0], rows[0][1], "draft")]


# ---------------------------------------------------------------------------
# Propose
# ---------------------------------------------------------------------------


async def test_propose_flips_the_status_and_shows_its_warnings(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """Propose saves, advances, and renders the warnings it collected."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    future = _date.today() + timedelta(days=5)
    response = await web_client.post(
        "/api/transactions/propose",
        data=_gesture_form(
            trade_date=future.isoformat(),
            investment_id=str(investment_id),
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200
    rows = await _tickets(superuser_engine)
    assert len(rows) == 1 and rows[0][2] == "proposed"

    body = response.text
    assert f"Ticket #{rows[0][1]}" in body
    assert "Proposed" in body
    # The amber strip is the service's own answer, rendered by the one
    # projection the preview uses.
    assert f"Trade date {future} is in the future." in _flat(body)
    assert "tx-msg--block" not in body
    # A record is not a form: the two editing gestures retire, Book does not.
    actions = _actions(body)
    assert actions["Save as draft"] is True
    assert actions["Propose"] is True
    assert actions["Book now"] is False


# ---------------------------------------------------------------------------
# Book — MD-16
# ---------------------------------------------------------------------------


async def test_book_from_scratch_writes_both_legs_and_confirms(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """Draft-if-needed, book, and list exactly what landed (MD-16)."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/book",
        data=_gesture_form(
            investment_id=str(investment_id),
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200

    rows = await _tickets(superuser_engine)
    assert len(rows) == 1 and rows[0][2] == "booked"
    assert await _count(superuser_engine, "trade_ticket_effects") == 2

    assert (_date(2026, 1, 15), Decimal("-400.0000")) in [
        (row[2], row[1]) for row in await _ledger(user_id, investment_id)
    ]
    assert (_date(2026, 1, 15), Decimal("41415.0000")) in [
        (row[2], row[1]) for row in await _ledger(user_id, cash_id)
    ]

    body = response.text
    flat = _flat(body)
    assert f"Ticket #{rows[0][1]}" in body
    assert "Booked" in body
    # Both legs, with their provenance, and the way onward.
    assert "−400.0000 units" in body
    assert "+41,415.0000 units" in body
    assert "Alpha Global Equity Fund" in body
    assert "EUR Cash — Commerzbank" in body
    assert f"ticket #{rows[0][1]}" in flat
    assert f'href="/investments/{investment_id}"' in body
    assert "New transaction" in body
    _assert_signs_are_single(body)


async def test_booking_into_the_red_books_and_flags(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """MD-5 / OP-06 struck: a negative balance is never a refusal.

    5,000 units at 104.10 plus 900 in fees costs 521,400.00 against a
    balance of 412,500.00 — the position lands at −108,900.00.
    """
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/book",
        data=_gesture_form(
            direction="buy",
            units="5000",
            fees="900",
            taxes="",
            investment_id=str(investment_id),
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200
    assert [row[2] for row in await _tickets(superuser_engine)] == ["booked"]

    flat = _flat(response.text)
    # The balance carries its own markup, so the sentence is asserted either
    # side of it — the P-1 negative-cash pin's own shape.
    assert "EUR Cash — Commerzbank now stands at" in flat
    assert "−108,900.00 EUR" in flat
    assert "The position stays flagged until the balance is back at zero or above." in flat
    _assert_signs_are_single(response.text)


# ---------------------------------------------------------------------------
# Refusals — one uniform rule (D-5)
# ---------------------------------------------------------------------------


async def test_tampered_propose_renders_the_service_sentence(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """A form stripped of its units is refused in the service's own words."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/propose",
        data=_gesture_form(
            units="",
            investment_id=str(investment_id),
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200
    assert "An order ticket needs a unit quantity." in _flat(response.text)
    assert "tx-msg--block" in response.text
    # The draft the gesture created on its way is kept — the user's work is
    # in it — and nothing beyond it was written.
    assert [row[2] for row in await _tickets(superuser_engine)] == ["draft"]
    assert await _count(superuser_engine, "trade_ticket_effects") == 0


async def test_book_against_a_deactivated_investment_refuses_and_keeps_the_draft(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The D-P race: retired between the last recalc and the gesture.

    The composer has no copy for this block — it cannot preview it — so the
    surface renders the service's sentence rather than inventing a second one.
    """
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)
    await _deactivate(user_id, investment_id)

    response = await web_client.post(
        "/api/transactions/book",
        data=_gesture_form(
            investment_id=str(investment_id),
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200
    # Jinja escapes the quotes the service's message puts around the name, so
    # the assertion takes the half of the sentence that carries none of them.
    assert "has been deactivated and cannot be traded" in _flat(response.text)
    assert "reactivate it first if this trade is real (D-P)." in _flat(response.text)
    assert [row[2] for row in await _tickets(superuser_engine)] == ["draft"]
    assert await _count(superuser_engine, "trade_ticket_effects") == 0
    assert await _count(superuser_engine, "position_transactions") == 2, "only the two openings"


async def test_a_gesture_without_an_investment_writes_nothing(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """W-3's floor, re-checked server-side: no investment, no currency, no ticket."""
    _id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post("/api/transactions/draft", data=_gesture_form(csrf_token=csrf))
    assert response.status_code == 200
    assert (
        "A ticket needs a direction, an investment and a trade date before it can be saved."
        in _flat(response.text)
    )
    assert await _count(superuser_engine, "trade_tickets") == 0


# ---------------------------------------------------------------------------
# MD-3 — the inline cash-position mini-form
# ---------------------------------------------------------------------------


async def _seed_cash_asset_class(user_id: UUID) -> None:
    """Give the tenant the ``cash`` asset class a cash position is filed against.

    Production tenants get it from the SAA seed catalogue
    (``services/saa/seeds.py``); this module builds its investments one at a
    time with ad-hoc classes, so the one class
    :meth:`InvestmentService.create_cash_position` looks up by code has to be
    seeded deliberately.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            await AssetClassRepository(session).create(code="cash", display_name="Cash")
    finally:
        await engine.dispose()


async def _usd_book(user_id: UUID) -> UUID:
    """A USD holding with no USD cash anywhere — the MD-3 "none" state."""
    investment_id = await _seed_investment(user_id, name="Northstar US Equity", currency="USD")
    await _seed_opening(user_id, investment_id, Decimal("800"), currency="USD")
    await _seed_cash_asset_class(user_id)
    return investment_id


def _cash_form(investment_id: UUID, csrf: str, **overrides: str) -> dict[str, str]:
    form = _gesture_form(
        investment_id=str(investment_id),
        csrf_token=csrf,
        cash_name="USD Cash — main account",
        cash_opening_balance="250000",
    )
    form.pop("settle_confirm", None)
    form.update(overrides)
    return form


async def test_mini_form_creates_the_candidate_on_the_trade_date(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """W-1: the position opens — and is priced — on the composer's trade date."""
    user_id, email, password = seeded_user
    investment_id = await _usd_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/cash-position", data=_cash_form(investment_id, csrf)
    )
    assert response.status_code == 200
    body = response.text
    assert "USD Cash — main account" in body
    assert "No USD cash position exists." not in _flat(body)
    # MD-3: creating is not confirming. The tick is still the user's to give.
    assert 'name="settle_confirm"' in body
    assert "Confirm the settlement position before booking." in _flat(body)

    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            created = await InvestmentRepository(session).get_by_name("USD Cash — main account")
            assert created is not None
            assert created.investment_type == "cash"
            assert created.currency == "USD"
            opening = await PositionTransactionRepository(session).get_opening(created.id)
            assert opening is not None
            assert opening.units == Decimal("250000.0000")
            assert opening.trade_date == _TRADE_DATE
            prices = await InstrumentPriceRepository(session).list_by_investment(created.id)
            assert [(p.as_of_date, p.price) for p in prices] == [
                (_TRADE_DATE, Decimal("1.00000000"))
            ]
    finally:
        await engine.dispose()


async def test_mini_form_duplicate_name_is_a_conflict(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The CRUD precedent: the UNIQUE constraint is the guarantee, 409 the answer."""
    user_id, email, password = seeded_user
    investment_id = await _usd_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/cash-position",
        data=_cash_form(investment_id, csrf, cash_name="Northstar US Equity"),
    )
    assert response.status_code == 409


async def test_mini_form_negative_balance_is_refused(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An opening is an opening: a negative one is a later movement."""
    user_id, email, password = seeded_user
    investment_id = await _usd_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/cash-position",
        data=_cash_form(investment_id, csrf, cash_opening_balance="-5"),
    )
    assert response.status_code == 400
    assert response.json()["field"] == "opening_balance"


async def test_mini_form_refuses_when_a_position_already_exists(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The offer never renders in this state, so a body from it is not honoured."""
    user_id, email, password = seeded_user
    investment_id, _cash = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    before = await _count(superuser_engine, "investments")
    response = await web_client.post(
        "/api/transactions/cash-position",
        data=_cash_form(investment_id, csrf, cash_name="EUR Cash — second"),
    )
    assert response.status_code == 400
    assert "already holds an active EUR cash position" in response.json()["error"]
    assert await _count(superuser_engine, "investments") == before


# ---------------------------------------------------------------------------
# Gating of the write surface
# ---------------------------------------------------------------------------


_WRITE_ENDPOINTS = (
    "/api/transactions/draft",
    "/api/transactions/propose",
    "/api/transactions/book",
    "/api/transactions/cash-position",
)


async def test_a_member_may_not_write(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """Owner writes domain data (ADR-0063 §2); a member reads and derives."""
    user_id, _email, _password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    member_email, member_password = await _seed_member(superuser_engine)
    csrf = await _login_and_csrf(web_client, member_email, member_password)

    form = _gesture_form(
        investment_id=str(investment_id),
        cash_investment_id=str(cash_id),
        csrf_token=csrf,
    )
    for url in _WRITE_ENDPOINTS:
        response = await web_client.post(url, data=form)
        assert response.status_code == 403, url

    # The read surface is deliberately not role-gated.
    assert (await web_client.post("/api/transactions/recalc", data=form)).status_code == 200
    assert await _count(superuser_engine, "trade_tickets") == 0


async def test_writes_require_the_csrf_token(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The uniform CSRF posture of every POST on this surface."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    await _login_and_csrf(web_client, email, password)

    form = _gesture_form(
        investment_id=str(investment_id),
        cash_investment_id=str(cash_id),
    )
    for url in _WRITE_ENDPOINTS:
        assert (await web_client.post(url, data=form)).status_code == 403, url


async def test_a_ticket_id_from_another_tenant_is_not_found(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """RLS makes "not yours" and "does not exist" one case (ADR-0128 §1)."""
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/draft",
        data=_gesture_form(
            investment_id=str(investment_id),
            cash_investment_id=str(cash_id),
            ticket_id=str(uuid4()),
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 404


async def test_proposing_twice_keeps_the_ticket_on_the_composer(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """A refused second Propose must not invite a second row.

    The service refuses — a proposal is not a draft — and the composer comes
    back still identified, so the next gesture updates this ticket rather
    than allocating another number over the top of it.
    """
    user_id, email, password = seeded_user
    investment_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    form = _gesture_form(
        investment_id=str(investment_id),
        cash_investment_id=str(cash_id),
        csrf_token=csrf,
    )
    first = await web_client.post("/api/transactions/propose", data=form)
    rows = await _tickets(superuser_engine)
    assert rows[0][2] == "proposed"
    assert f'name="ticket_id" value="{rows[0][0]}"' in first.text

    second = await web_client.post(
        "/api/transactions/propose", data={**form, "ticket_id": str(rows[0][0])}
    )
    assert second.status_code == 200
    assert "tx-msg--block" in second.text
    assert f"Ticket #{rows[0][1]}" in second.text
    assert f'name="ticket_id" value="{rows[0][0]}"' in second.text
    assert await _tickets(superuser_engine) == [(rows[0][0], rows[0][1], "proposed")]
