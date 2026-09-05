# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the R-SEC-SELL composer (ADR-0128, S4c / P-4a).

ASGI-level tests over a live Postgres, in the fixture style of
``tests/web/test_transactions_composer.py`` — the same seeding helpers, the
same login-and-CSRF handshake, the same "nothing was written" counting.

What is pinned here:

* **The picker's other half.** M-1's composer offers the ``unitised`` rows;
  this one offers the ``reported`` ones, and neither offers cash. The two
  eligibilities are the surface's statement of D-Q, one layer above the
  service's refusal.
* **The reported stake's own facts.** Last reported NAV, unfunded commitment
  (``commitment − called``, through the same public helper the Planning Desk
  pacing rows use), vintage and valuation mode — the four M-3 shows where
  M-1 shows a holding and a last price.
* **MD-20 is context.** The ``vs. last reported NAV`` row carries a sign and
  no judgement: it is an info row and never a warning, because a secondary
  discount is ordinary economics.
* **MD-17: consequences are not options.** The four on-booking rows are
  shown before the gesture and written by it, and ``set_inactive`` is
  ``false`` on the row whatever the body said.
* **MD-18: the one surface-owned block.** Selecting "Sell part of the stake"
  disables Book now, Propose *and* Save as draft — the only block on this
  surface that reaches the draft gesture — and a tampered body that posts it
  anyway still writes no row.

The expected amounts are written out longhand rather than recomputed from the
service, so a change of sign convention fails here loudly.

Not here: R-COMMIT and R-SEC-BUY (P-4b), or the blotter and history surfaces
(S5).
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
from core.repositories.investment_cashflow_repository import InvestmentCashflowRepository
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.position_transaction_repository import PositionTransactionRepository
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

#: A second tenant, for the RLS assertion.
_OTHER_TENANT_ID = UUID("11111111-2222-3333-4444-555555555555")

#: A settled past date, so the future-trade-date warning never fires by
#: accident and every rendered date is stable.
_TRADE_DATE = _date(2026, 1, 15)
_OPENING_DATE = _date(2026, 1, 2)
#: The statement day the stake was last valued on — before the trade date, so
#: the D-N collision guard is not tripped by the fixture itself.
_NAV_DATE = _date(2025, 12, 31)

#: M-3's own figures, scaled to this module's book.
_LAST_NAV = Decimal("1920000")
_COMMITMENT = Decimal("2500000")
_CALLED = Decimal("2050000")
_CASH_BALANCE = Decimal("2750000")
_GROSS = Decimal("1850000")
_FEES = Decimal("12500")
#: ``gross − fees − taxes``, the one derivation, stated longhand.
_NET = Decimal("1837500")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; skipping "
            "live-DB secondary-sale tests.",
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
    email = "secsell-owner@example.com"
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


async def _seed_member(superuser_engine: AsyncEngine) -> tuple[str, str]:
    """Seed a member of the primary tenant, for the owner-gating assertions."""
    plaintext = "correct-horse-battery-staple"
    email = "secsell-member@example.com"
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


async def _seed_investment(
    user_id: UUID,
    *,
    name: str,
    investment_type: str = "listed_equity",
    currency: str = "EUR",
    valuation_mode: str = "unitised",
    is_active: bool = True,
    vintage_year: int | None = None,
    commitment_amount: Decimal | None = None,
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
                vintage_year=vintage_year,
                commitment_amount=commitment_amount,
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


async def _seed_nav(
    user_id: UUID,
    investment_id: UUID,
    value: Decimal,
    *,
    on: _date = _NAV_DATE,
) -> None:
    """Store one actual NAV — the stake's last statement."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            await InvestmentNavRepository(session).upsert(
                investment_id=investment_id,
                as_of_date=on,
                nav_kind="actual",
                nav_value=value,
                currency="EUR",
                source="statement",
                created_by=user_id,
                ingest_origin="excel",
            )
    finally:
        await engine.dispose()


async def _seed_capital_call(user_id: UUID, investment_id: UUID, amount: Decimal) -> None:
    """Book one realised capital call, negative per ADR-0043."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            await InvestmentCashflowRepository(session).create(
                investment_id=investment_id,
                flow_timestamp=datetime(2025, 6, 30, 12, 0, tzinfo=timezone.utc),
                flow_type="capital_call",
                flow_kind="actual",
                amount=-amount,
                currency="EUR",
                description="Seeded call",
                created_by=user_id,
            )
    finally:
        await engine.dispose()


async def _count(engine: AsyncEngine, table: str) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
        return int(result.scalar_one())


async def _tickets(engine: AsyncEngine) -> list[dict[str, object]]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, ticket_number, kind, direction, status, units, "
                "gross_amount, set_inactive, investment_id "
                "FROM trade_tickets ORDER BY ticket_number"
            )
        )
        return [dict(row) for row in rows.mappings()]


async def _effects(engine: AsyncEngine) -> list[str]:
    async with engine.begin() as conn:
        rows = await conn.execute(text("SELECT effect_type FROM trade_ticket_effects"))
        return sorted(str(row[0]) for row in rows)


async def _standard_book(user_id: UUID) -> tuple[UUID, UUID]:
    """The M-3 book: one reported stake with a statement and a call, one cash row.

    The stake carries a commitment and one realised call, so the unfunded
    figure the context strip states is a real ``commitment − called`` rather
    than a constant the fixture happens to hold.
    """
    stake_id = await _seed_investment(
        user_id,
        name="Cinder Ridge Buyout Fund III",
        investment_type="private_equity",
        valuation_mode="reported",
        vintage_year=2021,
        commitment_amount=_COMMITMENT,
    )
    await _seed_nav(user_id, stake_id, _LAST_NAV)
    await _seed_capital_call(user_id, stake_id, _CALLED)
    cash_id = await _seed_investment(user_id, name="EUR Cash — Commerzbank", investment_type="cash")
    await _seed_opening(user_id, cash_id, _CASH_BALANCE)
    return stake_id, cash_id


# ---------------------------------------------------------------------------
# Markup helpers
# ---------------------------------------------------------------------------

_BUTTON = re.compile(r"<button\b(?P<attrs>[^>]*)>\s*(?P<label>[^<]*?)\s*</button>", re.S)


def _flat(markup: str) -> str:
    """Collapse whitespace, so a copy assertion is not a line-wrap assertion."""
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


def _form(**overrides: str) -> dict[str, str]:
    """The composer's posted body, with M-3's own values by default."""
    body: dict[str, str] = {
        "flow": "secondary_sale",
        "trade_date": _TRADE_DATE.isoformat(),
        "gross_amount": str(_GROSS),
        "fees": str(_FEES),
        "fraction": "full",
    }
    body.update(overrides)
    return body


def _confirmed(**overrides: str) -> dict[str, str]:
    """The same body with the MD-3 settlement confirmation ticked."""
    return _form(settle_confirm="1", **overrides)


# ---------------------------------------------------------------------------
# 1 · The chooser's fifth tile
# ---------------------------------------------------------------------------


async def test_the_fifth_chooser_tile_opens_this_composer(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """R-SEC-SELL is live; R-COMMIT and R-SEC-BUY still say when they arrive."""
    _id, email, password = seeded_user
    await _login_and_csrf(web_client, email, password)

    section = _new_section((await web_client.get("/transactions")).text)

    assert section.count('hx-get="/api/transactions/secondary-sale-form"') == 1
    assert "Sell a stake (secondary)" in section
    assert "Exit a fund interest in full." in section
    # The two flows P-4b arms keep their pills, and only those two.
    assert section.count("Arrives with S4c") == 2
    for still_pending in ("New commitment", "Buy a stake (secondary)"):
        assert still_pending in section


# ---------------------------------------------------------------------------
# 2 · The opening render
# ---------------------------------------------------------------------------


async def test_the_form_opens_on_the_reported_rows_and_writes_nothing(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The picker offers what M-3's hint promises, and MD-2 holds."""
    user_id, email, password = seeded_user
    await _standard_book(user_id)
    await _seed_investment(user_id, name="Listed Tracker")
    await _seed_investment(
        user_id, name="Retired Stake", valuation_mode="reported", is_active=False
    )
    await _seed_investment(
        user_id, name="Foreign Stake", valuation_mode="reported", tenant_id=_OTHER_TENANT_ID
    )
    await _login_and_csrf(web_client, email, password)

    response = await web_client.get("/api/transactions/secondary-sale-form")
    assert response.status_code == 200
    body = response.text

    assert "Reported and active investments only." in _flat(body)
    assert "Cinder Ridge Buyout Fund III — EUR" in _flat(body)
    for excluded in ("Listed Tracker", "Retired Stake", "EUR Cash — Commerzbank", "Foreign Stake"):
        assert excluded not in body, f"{excluded!r} must not be offered in the picker"

    assert "Sell a stake" in body
    assert "New ticket" in body and "Unsaved" in body
    assert await _count(superuser_engine, "trade_tickets") == 0


async def test_the_context_strip_states_the_stakes_own_facts(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """M-3's four items: last NAV, unfunded, vintage, valuation mode.

    The unfunded figure is ``commitment − called`` through
    :func:`~services.investments.pacing_rows.unfunded_commitment` — the same
    public helper the Planning Desk's pacing rows state, so the two surfaces
    cannot quote different remainders for one fund.
    """
    user_id, email, password = seeded_user
    stake_id, _cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    body = (
        await web_client.post(
            "/api/transactions/recalc",
            data=_form(investment_id=str(stake_id), csrf_token=csrf),
        )
    ).text
    flat = _flat(body)

    assert "Last reported NAV" in flat
    assert f"1,920,000.00 EUR · {_NAV_DATE}" in flat
    assert "Unfunded commitment" in flat
    assert "450,000.00 EUR" in flat, "commitment 2,500,000 − called 2,050,000"
    assert "Vintage" in flat and "2021" in flat
    assert "Valuation mode" in flat and "reported" in flat


# ---------------------------------------------------------------------------
# 3 · The derived numbers
# ---------------------------------------------------------------------------


async def test_recalc_derives_the_net_the_info_row_and_the_four_effects(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """Proceeds − fees − taxes, the MD-20 context row, and the emission preview."""
    user_id, email, password = seeded_user
    stake_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/recalc",
        data=_confirmed(
            investment_id=str(stake_id),
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200
    flat = _flat(response.text)

    # The amounts: gross stated, costs subtracted, net from the service.
    assert "1,850,000.00 EUR" in flat
    assert "−12,500.00 EUR" in flat
    assert "+1,837,500.00 EUR" in flat

    # MD-20: proceeds 1,837,500 against a NAV of 1,920,000 is −4.3 %.
    assert "vs. last reported NAV" in flat
    assert "−4.3 %" in flat
    assert "tx-derived__row--info" in response.text

    # The four on-booking rows, in M-3's order.
    assert "Emitted together, or not at all:" in flat
    assert "Cinder Ridge Buyout Fund III <em>distribution · actual</em>" in flat
    assert "NAV set to zero at trade date <em>manual origin</em>" in flat
    assert "Investment set inactive <em>full disposal</em>" in flat
    assert "EUR Cash — Commerzbank <em>@ 1.0000</em>" in flat
    assert "+1,837,500.0000 units" in flat

    # The settlement projection: 2,750,000 + 1,837,500.
    assert "4,587,500.00" in flat

    assert await _count(superuser_engine, "trade_tickets") == 0
    assert await _count(superuser_engine, "position_transactions") == 1


# ---------------------------------------------------------------------------
# 4 · MD-18 — the scope refusal
# ---------------------------------------------------------------------------


async def test_a_partial_sale_is_refused_and_disables_all_three_gestures(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """MD-18: the one block on this surface that also withholds Save as draft."""
    user_id, email, password = seeded_user
    stake_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/recalc",
        data=_confirmed(
            investment_id=str(stake_id),
            cash_investment_id=str(cash_id),
            fraction="partial",
            csrf_token=csrf,
        ),
    )
    body = response.text
    flat = _flat(body)

    assert "Partial secondary sales are not supported yet." in flat
    assert "Record a full sale, or wait for the successor." in flat
    assert "tx-msg--block" in body
    assert "is-refused" in body, "the selected option carries the refusal state"

    assert _actions(body) == {
        "Book now": True,
        "Propose": True,
        "Save as draft": True,
        "Discard": False,
    }
    assert "A partial-sale ticket cannot be created in v1 — not even as a draft." in flat
    # MD-17's consequence is about a sale that is going to happen; a refused
    # one has no consequence to state.
    assert "This closes the stake completely." not in flat


# ---------------------------------------------------------------------------
# 5 · The full-sale states
# ---------------------------------------------------------------------------


async def test_a_full_sale_states_its_consequence_and_its_warnings(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """MD-17's consequence, plus the two amber warnings that never stop anything."""
    user_id, email, password = seeded_user
    stake_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    base = _confirmed(
        investment_id=str(stake_id),
        cash_investment_id=str(cash_id),
        csrf_token=csrf,
    )

    clean = await web_client.post("/api/transactions/recalc", data=base)
    flat = _flat(clean.text)
    assert "This closes the stake completely." in flat
    assert "NAV goes to zero, the unfunded commitment ends" in flat
    assert "tx-msg--consequence" in clean.text
    assert _actions(clean.text)["Book now"] is False

    # Costs above the gross: amber, and the actions stay open (MD-5).
    costly = await web_client.post(
        "/api/transactions/recalc", data={**base, "fees": str(_GROSS + Decimal(1))}
    )
    costly_flat = _flat(costly.text)
    assert "Net proceeds are −1.00 EUR." in costly_flat
    assert "tx-msg--block" not in costly.text
    assert _actions(costly.text)["Book now"] is False

    # A post-dated trade: amber for the same reason.
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    future = await web_client.post(
        "/api/transactions/recalc", data={**base, "trade_date": tomorrow}
    )
    assert f"Trade date {tomorrow} is in the future." in _flat(future.text)
    assert _actions(future.text)["Book now"] is False


# ---------------------------------------------------------------------------
# 6 · Save as draft
# ---------------------------------------------------------------------------


async def test_save_as_draft_writes_a_secondary_sell_ticket(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The third column of the kind-map, as it lands on the row (MD-2, MD-17)."""
    user_id, email, password = seeded_user
    stake_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    body = _confirmed(
        investment_id=str(stake_id),
        cash_investment_id=str(cash_id),
        csrf_token=csrf,
    )
    first = await web_client.post("/api/transactions/draft", data=body)
    assert first.status_code == 200

    rows = await _tickets(superuser_engine)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "secondary"
    assert row["direction"] == "sell"
    assert row["status"] == "draft"
    assert row["gross_amount"] == _GROSS
    assert row["units"] is None, "a reported stake deals in no units"
    assert row["set_inactive"] is False, "MD-17: not this flow's control"
    assert row["investment_id"] == stake_id

    assert f"Ticket #{row['ticket_number']}" in first.text
    assert "Draft" in first.text
    assert f'name="ticket_id" value="{row["id"]}"' in first.text

    # MD-2: a second gesture updates the same row rather than burning a number.
    second = await web_client.post(
        "/api/transactions/draft",
        data={**body, "ticket_id": str(row["id"]), "gross_amount": "1900000"},
    )
    assert second.status_code == 200
    after = await _tickets(superuser_engine)
    assert len(after) == 1
    assert after[0]["id"] == row["id"]
    assert after[0]["gross_amount"] == Decimal("1900000")


async def test_a_tampered_partial_sale_still_writes_no_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """MD-18 holds against a body that never came from the render.

    The surface disabled all three buttons, so this can only arrive from a
    hand-made post. It is answered by re-rendering the blocked state rather
    than by a route-side refusal: the service has no rule to state here, and
    the block the operator would see is the one already on the page.
    """
    user_id, email, password = seeded_user
    stake_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/draft",
        data=_confirmed(
            investment_id=str(stake_id),
            cash_investment_id=str(cash_id),
            fraction="partial",
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200
    assert "Partial secondary sales are not supported yet." in _flat(response.text)
    assert await _count(superuser_engine, "trade_tickets") == 0


# ---------------------------------------------------------------------------
# 7 · Book now
# ---------------------------------------------------------------------------


async def test_book_now_emits_all_four_effects_and_confirms_them(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """MD-2 and MD-17 together: one draft, one booking, four rows."""
    user_id, email, password = seeded_user
    stake_id, cash_id = await _standard_book(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    before_ledger = await _count(superuser_engine, "position_transactions")
    response = await web_client.post(
        "/api/transactions/book",
        data=_confirmed(
            investment_id=str(stake_id),
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200

    rows = await _tickets(superuser_engine)
    assert len(rows) == 1
    assert rows[0]["status"] == "booked"
    assert rows[0]["kind"] == "secondary"
    assert await _effects(superuser_engine) == [
        "cashflow",
        "investment_update",
        "nav",
        "position_txn",
    ]

    # One ledger row added: the cash leg, and nothing on the stake.
    assert await _count(superuser_engine, "position_transactions") == before_ledger + 1

    async with superuser_engine.begin() as conn:
        flow = (
            (
                await conn.execute(
                    text(
                        "SELECT amount, flow_type, flow_kind FROM investment_cashflows "
                        "WHERE investment_id = :id AND flow_type = 'distribution'"
                    ),
                    {"id": str(stake_id)},
                )
            )
            .mappings()
            .one()
        )
        assert flow["amount"] == _NET
        assert flow["flow_kind"] == "actual"

        nav = (
            (
                await conn.execute(
                    text(
                        "SELECT nav_value, as_of_date FROM investment_navs "
                        "WHERE investment_id = :id AND as_of_date = :day"
                    ),
                    {"id": str(stake_id), "day": _TRADE_DATE},
                )
            )
            .mappings()
            .one()
        )
        assert nav["nav_value"] == 0

        assert (
            await conn.execute(
                text("SELECT is_active FROM investments WHERE id = :id"),
                {"id": str(stake_id)},
            )
        ).scalar_one() is False

        prior = (
            await conn.execute(
                text(
                    "SELECT prior_state FROM trade_ticket_effects "
                    "WHERE effect_type = 'investment_update'"
                )
            )
        ).scalar_one()
        assert isinstance(prior, dict), "a restatement carries its before-image (D-I)"

    # The MD-16 panel lists every row the booking wrote.
    flat = _flat(response.text)
    assert "What was written" in flat
    assert "Cinder Ridge Buyout Fund III <em>set inactive</em>" in flat
    assert '<span class="tx-leg__type">distribution</span>' in flat
    assert '<span class="tx-leg__type">nav</span>' in flat


async def test_a_nav_already_standing_on_the_trade_date_is_refused(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """D-N: the service's own sentence, and nothing written (D-5)."""
    user_id, email, password = seeded_user
    stake_id, cash_id = await _standard_book(user_id)
    await _seed_nav(user_id, stake_id, Decimal("1500000"), on=_TRADE_DATE)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/book",
        data=_confirmed(
            investment_id=str(stake_id),
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200
    assert "tx-msg--block" in response.text

    assert await _effects(superuser_engine) == []
    async with superuser_engine.begin() as conn:
        assert (
            await conn.execute(
                text("SELECT is_active FROM investments WHERE id = :id"),
                {"id": str(stake_id)},
            )
        ).scalar_one() is True
        assert (
            await conn.execute(
                text("SELECT COUNT(*) FROM investment_cashflows WHERE flow_type = 'distribution'")
            )
        ).scalar_one() == 0


# ---------------------------------------------------------------------------
# 8 · Gating and tenancy
# ---------------------------------------------------------------------------

_WRITE_ENDPOINTS = (
    "/api/transactions/draft",
    "/api/transactions/propose",
    "/api/transactions/book",
)


async def test_a_member_may_not_write_but_may_recalculate(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """Owner writes domain data (ADR-0063 §2); a member reads and derives."""
    user_id, _email, _password = seeded_user
    stake_id, cash_id = await _standard_book(user_id)
    member_email, member_password = await _seed_member(superuser_engine)
    csrf = await _login_and_csrf(web_client, member_email, member_password)

    body = _confirmed(
        investment_id=str(stake_id),
        cash_investment_id=str(cash_id),
        csrf_token=csrf,
    )
    for url in _WRITE_ENDPOINTS:
        assert (await web_client.post(url, data=body)).status_code == 403, url

    assert (await web_client.post("/api/transactions/recalc", data=body)).status_code == 200
    assert (await web_client.get("/api/transactions/secondary-sale-form")).status_code == 200
    assert await _count(superuser_engine, "trade_tickets") == 0


async def test_writes_require_the_csrf_token(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The uniform CSRF posture of every POST on this surface."""
    user_id, email, password = seeded_user
    stake_id, cash_id = await _standard_book(user_id)
    await _login_and_csrf(web_client, email, password)

    body = _confirmed(investment_id=str(stake_id), cash_investment_id=str(cash_id))
    for url in _WRITE_ENDPOINTS:
        assert (await web_client.post(url, data=body)).status_code == 403, url


async def test_an_investment_from_another_tenant_reads_as_unpicked(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """A foreign id is a field that says nothing — never an error that leaks."""
    user_id, email, password = seeded_user
    await _standard_book(user_id)
    foreign_id = await _seed_investment(
        user_id,
        name="Foreign Stake",
        valuation_mode="reported",
        tenant_id=_OTHER_TENANT_ID,
    )
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/recalc",
        data=_form(investment_id=str(foreign_id), csrf_token=csrf),
    )
    assert response.status_code == 200
    assert "Foreign Stake" not in response.text
    assert _actions(response.text)["Book now"] is True, "nothing is picked, so nothing is bookable"
    assert await _count(superuser_engine, "trade_tickets") == 0
