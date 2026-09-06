# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the R-SEC-BUY composer (ADR-0128, S4c / P-4b).

ASGI-level tests over a live Postgres, in the fixture style of
``tests/web/test_transactions_secondary_sale.py`` — the same seeding helpers,
the same login-and-CSRF handshake, the same "nothing was written" counting.

What is pinned here:

* **MD-12, with no picker at all.** A ``secondary``/``buy`` that named an
  investment is ``_unroutable``; the composer therefore offers no picker, and
  the stake's row is created by the booking and nowhere else.
* **MD-20 is context.** "Price vs. acquired NAV" carries a sign and a word —
  discount below the acquired NAV, premium at or above it — and never a
  warning: a secondary that changed hands below its last statement is
  ordinary economics.
* **The four emission rows, before and after.** The created row, its opening
  NAV at the *acquired* value rather than at what was paid, the unfunded
  commitment assumed with the stake (D-U), and the cash leg — shown on the
  composer, then read back out of ``trade_ticket_effects``.
* **No fees, no taxes.** M-3 states the purchase price as the net cash out, so
  neither control exists and a body that posts them is ignored.
* **MD-21 / MD-11**, as on every creating flow: the AnlV gate withholds
  Propose and Book now, never Save as draft.

The expected amounts are M-3's own and are written out longhand rather than
recomputed from the service, so a change of sign convention fails here loudly.

Not here: R-COMMIT (``test_transactions_commitment.py``), the non-creating
secondary purchase — a top-up, which v1 cannot route and a successor ADR
names — or the blotter and history surfaces (S5).
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from datetime import date as _date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import web.routes.transactions as routes
from core.repositories._session import tenant_context
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.position_transaction_repository import PositionTransactionRepository
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

#: A second tenant, for the RLS assertion on the settlement candidates.
_OTHER_TENANT_ID = UUID("11111111-2222-3333-4444-555555555555")

#: A settled past date, so the future-trade-date warning never fires by
#: accident and every rendered date is stable.
_TRADE_DATE = _date(2026, 1, 15)
_OPENING_DATE = _date(2026, 1, 2)

#: M-3's own transfer, verbatim.
_NAME = "Halstenbek Infrastructure Partners II"
_PRICE = Decimal("2208000")
_ACQUIRED_NAV = Decimal("2400000")
_UNFUNDED = Decimal("600000")
_VINTAGE = 2022
_ANLV = "anlv_13"
_CASH_BALANCE = Decimal("2750000")
#: ``balance − price``, the one derivation, stated longhand.
_AFTER = Decimal("542000")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; skipping "
            "live-DB secondary-purchase tests.",
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
    "investment_identifiers, "
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
    email = "secbuy-owner@example.com"
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
                    (:id, :tid, :email, :hash, 'S. Behrens',
                     ARRAY['owner']::text[], TRUE)
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
    email = "secbuy-member@example.com"
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


async def _seed_asset_class(user_id: UUID) -> UUID:
    """One asset class, for the master-data select and the created row's FK."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            created = await AssetClassRepository(session).create(
                code="real-assets", display_name="Real Assets"
            )
            return created.id
    finally:
        await engine.dispose()


async def _seed_investment(
    user_id: UUID,
    *,
    name: str,
    investment_type: str = "cash",
    currency: str = "EUR",
    tenant_id: UUID = SENTINEL_TENANT_ID,
) -> UUID:
    """Create one investment (with its own neutrally named asset class)."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code=f"ac-{uuid4().hex[:8]}", display_name=f"Catalogue {uuid4().hex[:6]}"
            )
            investment = await InvestmentRepository(session).create(
                name=name,
                investment_type=investment_type,
                asset_class_id=asset_class.id,
                currency=currency,
                created_by=user_id,
                valuation_mode="unitised" if investment_type == "cash" else "reported",
            )
            return investment.id
    finally:
        await engine.dispose()


async def _seed_cash(
    user_id: UUID,
    *,
    name: str = "EUR Cash — Commerzbank",
    currency: str = "EUR",
    balance: Decimal = _CASH_BALANCE,
    tenant_id: UUID = SENTINEL_TENANT_ID,
) -> UUID:
    """One cash position with an opening balance — the settlement candidate."""
    cash_id = await _seed_investment(user_id, name=name, currency=currency, tenant_id=tenant_id)
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as session:
            await PositionTransactionRepository(session).add(
                investment_id=cash_id,
                txn_type="opening",
                trade_date=_OPENING_DATE,
                units=balance,
                currency=currency,
                ingest_origin="manual",
                created_by=user_id,
            )
    finally:
        await engine.dispose()
    return cash_id


# ---------------------------------------------------------------------------
# Reading helpers
# ---------------------------------------------------------------------------


async def _count(engine: AsyncEngine, table: str) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
        return int(result.scalar_one())


async def _ticket_row(engine: AsyncEngine) -> dict[str, Any]:
    """The one trade ticket, as a plain mapping."""
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, ticket_number, kind, direction, status, investment_id, "
                "cash_investment_id, currency, trade_date, units, price_per_unit, "
                "gross_amount, fees, taxes, commitment_amount, master_data, set_inactive "
                "FROM trade_tickets ORDER BY ticket_number"
            )
        )
        found = rows.mappings().all()
    assert len(found) == 1, f"expected exactly one ticket, found {len(found)}"
    return dict(found[0])


async def _effects(engine: AsyncEngine) -> list[str]:
    async with engine.begin() as conn:
        rows = await conn.execute(text("SELECT effect_type FROM trade_ticket_effects"))
        return sorted(str(row[0]) for row in rows)


async def _created_investment(engine: AsyncEngine) -> dict[str, Any]:
    """The row the booking made — the one that is not a cash position."""
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, name, investment_type, currency, valuation_mode, anlv_code, "
                "vintage_year, commitment_amount, is_active FROM investments "
                "WHERE investment_type <> 'cash'"
            )
        )
        found = rows.mappings().all()
    assert len(found) == 1, f"expected exactly one created investment, found {len(found)}"
    return dict(found[0])


async def _navs(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT as_of_date, nav_kind, nav_value, currency, ingest_origin "
                "FROM investment_navs"
            )
        )
        return [dict(row) for row in rows.mappings()]


async def _cash_units(engine: AsyncEngine) -> Decimal:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT COALESCE(SUM(pt.units), 0) FROM position_transactions pt "
                "JOIN investments i ON i.id = pt.investment_id "
                "WHERE i.investment_type = 'cash'"
            )
        )
        return Decimal(str(result.scalar_one()))


# ---------------------------------------------------------------------------
# Markup helpers
# ---------------------------------------------------------------------------

_BUTTON = re.compile(r"<button\b(?P<attrs>[^>]*)>\s*(?P<label>[^<]*?)\s*</button>", re.S)
_HINT_OPEN = '<p class="tx-actions__hint">'


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


def _hint(markup: str) -> str:
    """The action row's hint sentence, without its tag."""
    start = markup.index(_HINT_OPEN) + len(_HINT_OPEN)
    return _flat(markup[start : markup.index("</p>", start)])


def _form(**overrides: str) -> dict[str, str]:
    """The composer's posted body, with M-3's own values by default."""
    body: dict[str, str] = {
        "flow": routes.FLOW_SECONDARY_BUY,
        "md_name": _NAME,
        "md_investment_type": "infra_equity",
        "currency": "EUR",
        "md_anlv_code": _ANLV,
        "md_vintage_year": str(_VINTAGE),
        "trade_date": _TRADE_DATE.isoformat(),
        "gross_amount": str(_PRICE),
        "md_acquired_nav": str(_ACQUIRED_NAV),
        "md_assumed_unfunded": str(_UNFUNDED),
    }
    body.update(overrides)
    return body


def _confirmed(**overrides: str) -> dict[str, str]:
    """The same body with the MD-3 settlement confirmation ticked."""
    return _form(settle_confirm="1", **overrides)


_WRITE_ENDPOINTS = (
    "/api/transactions/draft",
    "/api/transactions/propose",
    "/api/transactions/book",
)


# ---------------------------------------------------------------------------
# 1 · The chooser's fourth tile
# ---------------------------------------------------------------------------


async def test_the_fourth_chooser_tile_opens_this_composer(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """R-SEC-BUY is live; it has a settlement block and no picker."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    await _seed_cash(user_id)
    await _login_and_csrf(web_client, email, password)

    section = _new_section((await web_client.get("/transactions")).text)
    assert section.count('hx-get="/api/transactions/secondary-buy-form"') == 1
    assert "Buy a stake (secondary)" in section
    assert "Acquire an existing fund interest." in section

    response = await web_client.get("/api/transactions/secondary-buy-form")
    assert response.status_code == 200
    body = response.text
    flat = _flat(body)

    for option in routes._CLASSIFIABLE_TYPES:
        assert f'<option value="{option}"' in body
    assert "Real Assets" in body
    assert str(asset_class_id) in body
    assert "§ 2 Abs. 1 Nr. 13 AnlV" in body

    # MD-12: no picker. A `secondary`/`buy` naming an investment is
    # `_unroutable`, so offering one would offer a ticket no emission takes.
    assert 'name="investment_id"' not in body
    # M-3's transfer terms, and the two controls it does *not* draw.
    # The currency rides in the label once it is known; the opening render
    # has no form to know it from (W-4), so the base labels stand alone.
    assert "Purchase price, net cash out" in flat
    assert "Acquired NAV at transfer" in flat
    assert "Books as the stake's opening NAV." in flat
    assert "Assumed unfunded commitment" in flat
    assert 'name="fees"' not in body
    assert 'name="taxes"' not in body
    # The shared settlement panel is present, and waiting: the currency it
    # follows is a *field* on this flow (W-4), so the empty opening render has
    # nothing to look candidates up by and names that as the remedy.
    assert "Settlement position" in body
    assert "Set the currency first — the settlement position follows it." in flat
    assert "EUR Cash — Commerzbank" not in body
    assert 'value="reported" readonly' in body
    assert "Buy a stake (secondary)" in body

    assert await _count(superuser_engine, "trade_tickets") == 0


async def test_the_settlement_panel_waits_for_a_currency(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """W-4: the currency is a field here, so that is the remedy the panel names."""
    user_id, email, password = seeded_user
    await _seed_asset_class(user_id)
    await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/recalc", data=_form(currency="", csrf_token=csrf)
    )
    flat = _flat(response.text)
    assert "Set the currency first — the settlement position follows it." in flat
    assert "Pick an investment first" not in flat


# ---------------------------------------------------------------------------
# 2 · Recalculation — the derived rows, the discount, the four effects
# ---------------------------------------------------------------------------


async def test_recalc_derives_the_terms_the_discount_and_the_four_effects(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """M-3's own figures: 2,208,000 against an acquired NAV of 2,400,000."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/recalc",
        data=_confirmed(
            md_asset_class_id=str(asset_class_id),
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200
    flat = _flat(response.text)

    # The derived rows. The cash out is `preview.cash_effect`, signed.
    assert "Purchase price (cash out)</span> <span>−2,208,000.00 EUR</span>" in flat
    assert "Acquired NAV</span> <span>2,400,000.00 EUR</span>" in flat
    # MD-20: an info row with a sign and a word, never a warning — it carries
    # the info modifier and no message class anywhere near it.
    assert (
        'tx-derived__row tx-derived__row--info"> <span>Price vs. acquired NAV</span> '
        "<span>−8.0 % (discount)</span>"
    ) in flat

    # The four On-booking rows, in emission order.
    assert f"Investment · {_NAME} <em>reported</em>" in flat
    assert "Opening NAV at trade date <em>manual origin</em>" in flat
    assert "2,400,000.00 EUR" in flat
    assert "Unfunded commitment assumed <em>vintage 2022</em>" in flat
    assert "600,000.00 EUR" in flat
    assert "−2,208,000.0000 units" in flat

    # The projected balance, from the one shared projection.
    assert "2,750,000.00" in flat
    assert "542,000.00" in flat

    actions = _actions(response.text)
    assert actions["Book now"] is False
    assert _hint(response.text) == (
        "Booking creates the investment, opening NAV, commitment and cash leg in one step."
    )

    assert await _count(superuser_engine, "trade_tickets") == 0


async def test_a_premium_reads_as_a_premium(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The sign carries the whole meaning; the word is a reading of it."""
    user_id, email, password = seeded_user
    await _seed_asset_class(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/recalc",
        data=_form(gross_amount="2640000", csrf_token=csrf),
    )
    assert ("<span>Price vs. acquired NAV</span> <span>+10.0 % (premium)</span>") in _flat(
        response.text
    )


async def test_a_purchase_that_overdraws_the_position_warns_and_still_books(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """MD-5, OP-06 struck: negative cash is a warning and never a refusal."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id, balance=Decimal("1000000"))
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/recalc",
        data=_confirmed(
            md_asset_class_id=str(asset_class_id),
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    flat = _flat(response.text)
    assert "EUR Cash — Commerzbank goes to" in flat
    assert "−1,208,000.00 EUR" in flat
    assert "Booking is allowed — the trade is your call." in flat
    # It warns; it does not block.
    assert _actions(response.text)["Book now"] is False
    # And the two warnings this flow cannot raise stay silent.
    assert "Net proceeds are" not in flat
    assert "the last known price" not in flat


# ---------------------------------------------------------------------------
# 3 · Save as draft — the kind-map's fifth column
# ---------------------------------------------------------------------------


async def test_save_as_draft_writes_a_creating_secondary_buy_ticket(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The column map, and D-U's mirror written from one field."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/draft",
        data=_confirmed(
            md_asset_class_id=str(asset_class_id),
            cash_investment_id=str(cash_id),
            # M-3 offers neither control, so a hand-made body carrying them
            # must not reach the ticket: the price is stated as the net cash
            # out and adding costs to it would change what was shown.
            fees="9999",
            taxes="8888",
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200

    row = await _ticket_row(superuser_engine)
    assert row["kind"] == "secondary"
    assert row["direction"] == "buy"
    assert row["status"] == "draft"
    assert row["investment_id"] is None
    assert row["cash_investment_id"] == cash_id
    assert row["currency"] == "EUR"
    assert row["units"] is None
    assert row["price_per_unit"] is None
    assert row["fees"] is None
    assert row["taxes"] is None
    assert row["set_inactive"] is False
    assert Decimal(str(row["gross_amount"])) == _PRICE
    assert Decimal(str(row["commitment_amount"])) == _UNFUNDED

    payload = row["master_data"]
    assert payload["name"] == _NAME
    assert payload["investment_type"] == "infra_equity"
    assert payload["currency"] == "EUR"
    assert payload["anlv_code"] == _ANLV
    assert payload["vintage_year"] == str(_VINTAGE)
    assert Decimal(payload["acquired_nav"]) == _ACQUIRED_NAV
    # D-U, both mirrors: the assumed unfunded is the column, and the purchase
    # price is `gross_amount` — each written from the one field it came off.
    assert Decimal(payload["assumed_unfunded"]) == Decimal(str(row["commitment_amount"]))
    assert Decimal(payload["purchase_price"]) == Decimal(str(row["gross_amount"]))

    assert await _count(superuser_engine, "investments") == 1, "only the seeded cash row"
    assert await _count(superuser_engine, "trade_ticket_effects") == 0


# ---------------------------------------------------------------------------
# 4 · Propose — the service's own sentences
# ---------------------------------------------------------------------------


async def test_propose_without_the_acquired_nav_is_refused(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The stake's value at transfer is what the flow is *about* (D-5)."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/propose",
        data=_confirmed(
            md_asset_class_id=str(asset_class_id),
            cash_investment_id=str(cash_id),
            md_acquired_nav="",
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200
    flat = _flat(response.text)
    assert "tx-msg--block" in flat
    # The service's own sentence, which quotes the payload key and is
    # therefore autoescaped by Jinja (the S4b precedent).
    assert "A secondary purchase needs the acquired NAV" in flat
    assert "the stake" in flat
    assert "value at transfer" in flat
    assert (await _ticket_row(superuser_engine))["status"] == "draft"
    assert await _count(superuser_engine, "trade_ticket_effects") == 0


async def test_the_anlv_gate_withholds_propose_and_book_but_never_the_draft(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """MD-11 / MD-21, on the third creating flow."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    ungated = await web_client.post(
        "/api/transactions/recalc",
        data=_confirmed(
            md_asset_class_id=str(asset_class_id),
            cash_investment_id=str(cash_id),
            md_anlv_code="",
            csrf_token=csrf,
        ),
    )
    flat = _flat(ungated.text)
    assert "This ticket cannot be proposed or booked without an AnlV category." in flat
    assert (
        "The regulatory classification is required. Everything you entered stays in the draft."
    ) in flat
    gated = _actions(ungated.text)
    assert gated["Book now"] is True
    assert gated["Propose"] is True
    assert gated["Save as draft"] is False
    # M-3's script tests the gate before the settlement confirmation, and so
    # does the hint chain.
    assert _hint(ungated.text) == (
        "Book now and Propose are unavailable until the AnlV category is set."
    )

    saved = await web_client.post(
        "/api/transactions/draft",
        data=_confirmed(
            md_asset_class_id=str(asset_class_id),
            cash_investment_id=str(cash_id),
            md_anlv_code="",
            csrf_token=csrf,
        ),
    )
    assert saved.status_code == 200
    assert (await _ticket_row(superuser_engine))["master_data"].get("anlv_code") is None


async def test_the_hint_asks_for_the_settlement_confirmation_once_the_gate_is_clear(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The chain beneath the gate is M-1's, unchanged."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/recalc",
        data=_form(
            md_asset_class_id=str(asset_class_id),
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    assert _hint(response.text) == "Confirm the settlement position before booking."
    assert _actions(response.text)["Book now"] is True


# ---------------------------------------------------------------------------
# 5 · Book now — three effects, and the panel that lists them
# ---------------------------------------------------------------------------


async def test_book_now_emits_the_row_its_nav_and_the_cash_leg(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The stake opens at what it was worth, not at what was paid for it."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/book",
        data=_confirmed(
            md_asset_class_id=str(asset_class_id),
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200

    ticket = await _ticket_row(superuser_engine)
    assert ticket["status"] == "booked"
    assert await _effects(superuser_engine) == ["investment_update", "nav", "position_txn"]

    created = await _created_investment(superuser_engine)
    assert created["name"] == _NAME
    assert created["valuation_mode"] == "reported"
    assert created["anlv_code"] == _ANLV
    assert created["vintage_year"] == _VINTAGE
    assert Decimal(str(created["commitment_amount"])) == _UNFUNDED

    navs = await _navs(superuser_engine)
    assert len(navs) == 1
    assert navs[0]["as_of_date"] == _TRADE_DATE
    assert navs[0]["nav_kind"] == "actual"
    assert Decimal(str(navs[0]["nav_value"])) == _ACQUIRED_NAV
    assert navs[0]["ingest_origin"] == "manual"

    # The cash leg, and no cashflow row anywhere: a purchase is not a flow on
    # the acquired investment.
    assert await _count(superuser_engine, "investment_cashflows") == 0
    assert await _cash_units(superuser_engine) == _AFTER

    panel = _flat(response.text)
    assert f"Ticket #{ticket['ticket_number']}" in panel
    assert f"{_NAME} <em>created</em>" in panel
    assert "Commitment 600,000.00 EUR · vintage 2022" in panel
    assert "2,400,000.00 EUR" in panel
    assert "−2,208,000.0000 units" in panel
    assert f'href="/investments/{created["id"]}"' in panel


# ---------------------------------------------------------------------------
# 6 · Gating
# ---------------------------------------------------------------------------


async def test_a_member_may_not_write_but_may_recalculate(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """Owner writes domain data (ADR-0063 §2); a member reads and derives."""
    user_id, _email, _password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    member_email, member_password = await _seed_member(superuser_engine)
    csrf = await _login_and_csrf(web_client, member_email, member_password)

    body = _confirmed(
        md_asset_class_id=str(asset_class_id),
        cash_investment_id=str(cash_id),
        csrf_token=csrf,
    )
    for url in _WRITE_ENDPOINTS:
        assert (await web_client.post(url, data=body)).status_code == 403, url

    assert (await web_client.post("/api/transactions/recalc", data=body)).status_code == 200
    assert (await web_client.get("/api/transactions/secondary-buy-form")).status_code == 200
    assert await _count(superuser_engine, "trade_tickets") == 0


async def test_writes_require_the_csrf_token(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The uniform CSRF posture of every POST on this surface."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    await _login_and_csrf(web_client, email, password)

    body = _confirmed(md_asset_class_id=str(asset_class_id), cash_investment_id=str(cash_id))
    for url in _WRITE_ENDPOINTS:
        assert (await web_client.post(url, data=body)).status_code == 403, url
    assert await _count(superuser_engine, "trade_tickets") == 0


async def test_a_cash_position_from_another_tenant_reads_as_unconfirmed(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """A foreign id is a field that says nothing — never an error that leaks."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    foreign_cash = await _seed_cash(user_id, name="Foreign EUR Cash", tenant_id=_OTHER_TENANT_ID)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/recalc",
        data=_confirmed(
            md_asset_class_id=str(asset_class_id),
            cash_investment_id=str(foreign_cash),
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200
    assert "Foreign EUR Cash" not in response.text
    assert _actions(response.text)["Book now"] is True, "nothing is settled, so nothing is bookable"

    refused = await web_client.post(
        "/api/transactions/book",
        data=_confirmed(
            md_asset_class_id=str(asset_class_id),
            cash_investment_id=str(foreign_cash),
            csrf_token=csrf,
        ),
    )
    assert (
        "This flow settles against a cash position but none is confirmed on the ticket"
    ) in _flat(refused.text)
    assert await _count(superuser_engine, "trade_ticket_effects") == 0
