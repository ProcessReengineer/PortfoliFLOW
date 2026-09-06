# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the R-COMMIT composer (ADR-0128, S4c / P-4b).

ASGI-level tests over a live Postgres, in the fixture style of
``tests/web/test_transactions_secondary_sale.py`` — the same seeding helpers,
the same login-and-CSRF handshake, the same "nothing was written" counting.

What is pinned here:

* **MD-19, the flow that moves no cash.** No settlement block is rendered, no
  cash candidate is offered, the ``tx-nocash`` panel says why in M-3's words,
  and a body that posts a ``cash_investment_id`` anyway still writes NULL —
  the surface never offered the choice, so the value is dropped rather than
  forwarded to a service that would refuse it.
* **MD-12, the investment as an emission effect.** No picker; the row does not
  exist until the booking creates it, and it is created ``reported`` with the
  commitment and the vintage on it.
* **D-U's mirror.** One posted ``commitment_amount`` becomes the column *and*
  ``master_data['commitment_amount']``, which is what makes
  ``reconcile_commitment`` compare a value with itself.
* **MD-21 / MD-11, the AnlV gate.** Propose and Book now wait for the
  category; Save as draft never does, and the draft it leaves behind keeps
  everything that was typed.
* **The whole emission, and nothing more.** One ``investment_update`` effect;
  no NAV, no cashflow, no ledger row, and every cash balance where it was.

The expected values are written out longhand rather than recomputed from the
service, so a change of convention fails here loudly.

Not here: R-SEC-BUY (``test_transactions_secondary_buy.py``), or the blotter
and history surfaces (S5).
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

#: A second tenant, for the RLS assertion on the duplicate-name block.
_OTHER_TENANT_ID = UUID("11111111-2222-3333-4444-555555555555")

#: A settled past date, so the future-trade-date warning never fires by
#: accident and every rendered date is stable.
_TRADE_DATE = _date(2026, 1, 15)
_OPENING_DATE = _date(2026, 1, 2)

#: M-3's own commitment, verbatim.
_NAME = "Nordwind Private Credit Fund II"
_COMMITMENT = Decimal("5000000")
_VINTAGE = 2026
_ANLV = "anlv_17"
_CASH_BALANCE = Decimal("2750000")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; skipping "
            "live-DB commitment tests.",
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
    email = "commit-owner@example.com"
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
    email = "commit-member@example.com"
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
                code="private-credit", display_name="Private Credit"
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
    """Create one investment (with its own asset class) in a tenant."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as session:
            # A neutral display name: the asset-class select renders every
            # catalogue row, and naming one after a cash position would put
            # that position's name on a form that must never mention it.
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


async def _seed_cash(user_id: UUID) -> UUID:
    """One EUR cash position — the one this flow must leave entirely alone."""
    cash_id = await _seed_investment(user_id, name="EUR Cash — Commerzbank")
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            await PositionTransactionRepository(session).add(
                investment_id=cash_id,
                txn_type="opening",
                trade_date=_OPENING_DATE,
                units=_CASH_BALANCE,
                currency="EUR",
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
    """The row the booking made — the one that is not the cash position."""
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT name, investment_type, currency, valuation_mode, anlv_code, "
                "vintage_year, commitment_amount, is_active FROM investments "
                "WHERE investment_type <> 'cash'"
            )
        )
        found = rows.mappings().all()
    assert len(found) == 1, f"expected exactly one created investment, found {len(found)}"
    return dict(found[0])


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


_HINT_OPEN = '<p class="tx-actions__hint">'


def _hint(markup: str) -> str:
    """The action row's hint sentence, without its tag."""
    start = markup.index(_HINT_OPEN) + len(_HINT_OPEN)
    return _flat(markup[start : markup.index("</p>", start)])


def _form(**overrides: str) -> dict[str, str]:
    """The composer's posted body, with M-3's own values by default."""
    body: dict[str, str] = {
        "flow": routes.FLOW_COMMITMENT,
        "md_name": _NAME,
        "md_investment_type": "private_debt",
        "currency": "EUR",
        "md_anlv_code": _ANLV,
        "commitment_amount": str(_COMMITMENT),
        "md_vintage_year": str(_VINTAGE),
        "trade_date": _TRADE_DATE.isoformat(),
    }
    body.update(overrides)
    return body


_WRITE_ENDPOINTS = (
    "/api/transactions/draft",
    "/api/transactions/propose",
    "/api/transactions/book",
)


# ---------------------------------------------------------------------------
# 1 · The chooser's third tile
# ---------------------------------------------------------------------------


async def test_the_third_chooser_tile_opens_this_composer(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """R-COMMIT is live, and opening it writes nothing (MD-2)."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    await _seed_cash(user_id)
    await _login_and_csrf(web_client, email, password)

    section = _new_section((await web_client.get("/transactions")).text)
    assert section.count('hx-get="/api/transactions/commitment-form"') == 1
    assert "New commitment" in section
    assert "Private markets. No cash moves yet." in section

    response = await web_client.get("/api/transactions/commitment-form")
    assert response.status_code == 200
    body = response.text

    # The wizard's seven types, and the tenant's catalogues.
    for option in routes._CLASSIFIABLE_TYPES:
        assert f'<option value="{option}"' in body
    assert "cash" not in routes._CLASSIFIABLE_TYPES
    assert "Private Credit" in body
    assert str(asset_class_id) in body
    assert "§ 2 Abs. 1 Nr. 17 AnlV" in body

    # MD-12: no picker at all — a commitment always records a new position.
    assert 'name="investment_id"' not in body
    # MD-19: no settlement block, and the panel that says why instead.
    assert "Settlement position" not in body
    assert 'name="cash_investment_id"' not in body
    assert "EUR Cash — Commerzbank" not in body
    assert "No cash moves with this ticket." in _flat(body)
    assert (
        "Money leaves the portfolio with the capital calls, which stay ordinary "
        "cashflows on the investment — entered as they arrive, outside this ticket."
    ) in _flat(body)
    assert (
        "The ticket books once, when the commitment is recorded, and remains the "
        "provenance anchor a Case can point at. Pacing and drawdown views stay "
        "with the Planning Desk."
    ) in _flat(body)
    # M-3's own read-only valuation mode and AnlV hint.
    assert 'value="reported" readonly' in body
    assert "Fixed for this flow." in _flat(body)
    assert "Required before this ticket can be proposed or booked." in _flat(body)
    assert "Record a commitment" in body

    assert await _count(superuser_engine, "trade_tickets") == 0


# ---------------------------------------------------------------------------
# 2 · Recalculation — the two rows, the gate, the absent cash
# ---------------------------------------------------------------------------


async def test_recalc_states_the_two_rows_and_gates_on_the_anlv_category(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """MD-19's two emission rows, and MD-21's gate in front of them."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    ungated = await web_client.post(
        "/api/transactions/recalc",
        data=_form(md_asset_class_id=str(asset_class_id), md_anlv_code="", csrf_token=csrf),
    )
    assert ungated.status_code == 200
    body = _flat(ungated.text)

    # Both rows stand, because the name and the amount are what they need.
    assert f"Investment · {_NAME} <em>reported</em>" in body
    assert "Commitment recorded <em>vintage 2026</em>" in body
    assert "5,000,000.00 EUR" in body
    # No cash anywhere: no candidate, no projected balance, no leg.
    assert "EUR Cash — Commerzbank" not in body
    assert "Settlement position" not in body

    # MD-21: the gate holds the two forward gestures, never the draft (MD-11).
    assert "This ticket cannot be proposed or booked without an AnlV category." in body
    assert (
        "The regulatory classification is required. Everything you entered stays in the draft."
    ) in body
    gated = _actions(ungated.text)
    assert gated["Book now"] is True
    assert gated["Propose"] is True
    assert gated["Save as draft"] is False
    assert _hint(ungated.text) == (
        "Book now and Propose are unavailable until the AnlV category is set."
    )

    armed = await web_client.post(
        "/api/transactions/recalc",
        data=_form(md_asset_class_id=str(asset_class_id), csrf_token=csrf),
    )
    assert "without an AnlV category" not in _flat(armed.text)
    armed_actions = _actions(armed.text)
    assert armed_actions["Book now"] is False
    assert armed_actions["Propose"] is False
    assert armed_actions["Save as draft"] is False
    assert _hint(armed.text) == (
        "Booking creates the investment and records the commitment. No cash moves."
    )

    assert await _count(superuser_engine, "trade_tickets") == 0


async def test_a_sparse_form_states_neither_row(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """D-3: showing one of two rows would be a promise the booking does not make."""
    user_id, email, password = seeded_user
    await _seed_asset_class(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/recalc",
        data=_form(md_name="", commitment_amount="", csrf_token=csrf),
    )
    body = _flat(response.text)
    assert (
        "Both rows appear here once the investment is named and the commitment amount is set."
    ) in body
    assert "Commitment recorded" not in body
    assert _hint(response.text) == (
        "Name the investment and state the commitment amount before booking."
    )


# ---------------------------------------------------------------------------
# 3 · Save as draft — the column, the mirror, and the dropped cash id
# ---------------------------------------------------------------------------


async def test_save_as_draft_writes_a_commitment_ticket(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The kind-map's fourth column, and D-U's mirror written from one field."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/draft",
        data=_form(
            md_asset_class_id=str(asset_class_id),
            # MD-19: a hand-made body offering a settlement position. The
            # surface never rendered the control, so the value is dropped
            # rather than forwarded to a service that would refuse it.
            cash_investment_id=str(cash_id),
            csrf_token=csrf,
        ),
    )
    assert response.status_code == 200

    row = await _ticket_row(superuser_engine)
    assert row["kind"] == "commitment"
    assert row["direction"] == "buy"
    assert row["status"] == "draft"
    assert row["investment_id"] is None
    assert row["cash_investment_id"] is None
    assert row["currency"] == "EUR"
    assert row["trade_date"] == _TRADE_DATE
    assert row["units"] is None
    assert row["price_per_unit"] is None
    assert row["gross_amount"] is None
    assert row["fees"] is None
    assert row["taxes"] is None
    assert row["set_inactive"] is False
    assert Decimal(str(row["commitment_amount"])) == _COMMITMENT

    payload = row["master_data"]
    assert payload["name"] == _NAME
    assert payload["investment_type"] == "private_debt"
    assert payload["asset_class_id"] == str(asset_class_id)
    assert payload["currency"] == "EUR"
    assert payload["anlv_code"] == _ANLV
    assert payload["vintage_year"] == str(_VINTAGE)
    # D-U: the mirror equals the column, because both came off one field.
    assert Decimal(payload["commitment_amount"]) == Decimal(str(row["commitment_amount"]))

    # The composer comes back carrying its ticket, and nothing else moved.
    assert f"Ticket #{row['ticket_number']}" in response.text
    assert await _count(superuser_engine, "investments") == 1, "only the seeded cash row"
    assert await _count(superuser_engine, "trade_ticket_effects") == 0


async def test_a_draft_needs_a_currency_and_nothing_else(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """W-3: a draft may dangle, but not without the fact the flow derives from."""
    user_id, email, password = seeded_user
    await _seed_asset_class(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    refused = await web_client.post(
        "/api/transactions/draft", data=_form(currency="EU", csrf_token=csrf)
    )
    assert routes._WIZARD_CURRENCY_REQUIRED in _flat(refused.text)
    assert await _count(superuser_engine, "trade_tickets") == 0

    # With a currency and nothing else, the draft saves — no name, no amount,
    # no category. `proposed` is what means complete (ADR-0128 §3).
    saved = await web_client.post(
        "/api/transactions/draft",
        data=_form(md_name="", commitment_amount="", md_anlv_code="", csrf_token=csrf),
    )
    assert saved.status_code == 200
    row = await _ticket_row(superuser_engine)
    assert row["kind"] == "commitment"
    assert row["commitment_amount"] is None


# ---------------------------------------------------------------------------
# 4 · Propose — the service's own sentences
# ---------------------------------------------------------------------------


async def test_propose_without_an_anlv_category_is_refused_in_the_services_words(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """D-5: the block copy is the service's, and the draft survives it."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/propose",
        data=_form(md_asset_class_id=str(asset_class_id), md_anlv_code="", csrf_token=csrf),
    )
    assert response.status_code == 200
    body = _flat(response.text)
    assert "tx-msg--block" in body
    # The service's own sentence, which quotes `'draft'` and is therefore
    # autoescaped by Jinja — the assertion reads around the escaped
    # apostrophes rather than pinning the escaping (the S4b precedent).
    assert "This flow creates an investment, so its AnlV category must be set" in body
    assert "before the ticket leaves" in body
    assert "(MD-11, MD-21)." in body
    assert (await _ticket_row(superuser_engine))["status"] == "draft"
    assert await _count(superuser_engine, "trade_ticket_effects") == 0


async def test_a_duplicate_name_is_refused_and_a_foreign_one_is_not(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """Names are the natural key the Excel re-import resolves on — per tenant."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    # A namesake in *another* tenant is not a clash: the block reads through
    # the tenant-scoped repository, so RLS answers the question.
    await _seed_investment(
        user_id, name=_NAME, investment_type="private_debt", tenant_id=_OTHER_TENANT_ID
    )
    clean = await web_client.post(
        "/api/transactions/propose",
        data=_form(md_asset_class_id=str(asset_class_id), csrf_token=csrf),
    )
    assert "already exists in this tenant" not in clean.text
    assert (await _ticket_row(superuser_engine))["status"] == "proposed"

    # The same name in *this* tenant is.
    await _seed_investment(user_id, name="Halstenbek Partners II", investment_type="private_debt")
    clash = await web_client.post(
        "/api/transactions/propose",
        data=_form(
            md_name="Halstenbek Partners II",
            md_asset_class_id=str(asset_class_id),
            csrf_token=csrf,
        ),
    )
    clashed = _flat(clash.text)
    assert "An investment named" in clashed
    assert "Halstenbek Partners II" in clashed
    assert "already exists in this tenant." in clashed


# ---------------------------------------------------------------------------
# 5 · Book now — one effect, and no cash anywhere
# ---------------------------------------------------------------------------


async def test_book_now_emits_the_investment_and_moves_no_cash(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """MD-19: the smallest emission in the system, and the panel that states it."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    before = await _cash_units(superuser_engine)
    response = await web_client.post(
        "/api/transactions/book",
        data=_form(md_asset_class_id=str(asset_class_id), csrf_token=csrf),
    )
    assert response.status_code == 200

    ticket = await _ticket_row(superuser_engine)
    assert ticket["status"] == "booked"
    assert await _effects(superuser_engine) == ["investment_update"]

    created = await _created_investment(superuser_engine)
    assert created["name"] == _NAME
    assert created["investment_type"] == "private_debt"
    assert created["currency"] == "EUR"
    assert created["valuation_mode"] == "reported"
    assert created["anlv_code"] == _ANLV
    assert created["vintage_year"] == _VINTAGE
    assert Decimal(str(created["commitment_amount"])) == _COMMITMENT
    assert created["is_active"] is True

    # Nothing else was written, and no money moved.
    assert await _count(superuser_engine, "investment_navs") == 0
    assert await _count(superuser_engine, "investment_cashflows") == 0
    assert await _cash_units(superuser_engine) == before

    # The MD-16 panel, with the line operator fork 5 asked for.
    panel = _flat(response.text)
    assert f"Ticket #{ticket['ticket_number']}" in panel
    assert f"{_NAME} <em>created</em>" in panel
    assert "Commitment 5,000,000.00 EUR · vintage 2026" in panel


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
    member_email, member_password = await _seed_member(superuser_engine)
    csrf = await _login_and_csrf(web_client, member_email, member_password)

    body = _form(md_asset_class_id=str(asset_class_id), csrf_token=csrf)
    for url in _WRITE_ENDPOINTS:
        assert (await web_client.post(url, data=body)).status_code == 403, url

    assert (await web_client.post("/api/transactions/recalc", data=body)).status_code == 200
    assert (await web_client.get("/api/transactions/commitment-form")).status_code == 200
    assert await _count(superuser_engine, "trade_tickets") == 0


async def test_writes_require_the_csrf_token(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The uniform CSRF posture of every POST on this surface."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    await _login_and_csrf(web_client, email, password)

    body = _form(md_asset_class_id=str(asset_class_id))
    for url in _WRITE_ENDPOINTS:
        assert (await web_client.post(url, data=body)).status_code == 403, url
    assert await _count(superuser_engine, "trade_tickets") == 0
