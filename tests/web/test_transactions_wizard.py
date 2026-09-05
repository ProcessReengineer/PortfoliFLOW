# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the M-2 new-instrument wizard (ADR-0128, S4b).

ASGI-level tests over a live Postgres, in the fixture style of
``tests/web/test_transactions_composer.py`` — its sibling, not its
continuation: the wizard's book is a *different* book (an empty one, plus a
cash position), because U-NEW is the flow whose instrument does not exist
yet, and seeding the composer's holding here would only invite an assertion
to lean on a row this flow must not need.

What is pinned here:

* **The chooser's second live tile.** S4b arms "Buy a new instrument"; the
  three reported-flow tiles keep their pills.
* **Both Identify paths.** A resolved identifier pre-fills FIGI, name and
  currency as *editable* values (operator decision W-4′); a resolver that
  states no currency leaves the field empty and is not an error (P-3a flag
  F-A); no match and a provider failure each get their own sentence.
* **The trio rule.** ``_ensure_draft``'s creating branch asks for a
  direction, a currency and a trade date — and the direction is the flow's
  constant, so a tampered ``sell`` is still stored as ``buy`` (MD-14).
* **MD-2, on the wizard's own gesture.** The first Continue allocates the
  ticket and the number; the second updates the row it allocated.
* **Resume (MD-10).** A mid-wizard draft reopens at the step its own content
  puts it at — no stored step, no column — and a foreign one is a 404.
* **The MD-21 finish gate.** No AnlV category: Propose and Book now are
  withheld while Keep as draft stays (MD-11). Set it: they arm.
* **Booking end to end.** The ``investments`` row exists *only after* the
  booking (MD-12), both identifier rows are stored (MD-13), the ledger legs
  land on the created id and the MD-16 confirmation panel names it.
* **Gating and purity.** Members are refused the writes; a keystroke on the
  creating path writes nothing.

Nothing here calls OpenFIGI. The resolver is monkeypatched at the name the
route imported, which is also the pin that the route calls
``resolve_instrument`` rather than reaching for a client of its own.

Not here: the reported flows (S4c), or the blotter and history surfaces
(S5) — the resume ``GET`` this module exercises is the URL S5 will link to.
"""

from __future__ import annotations

import os
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
from core.models.investment import INVESTMENT_TYPES
from core.repositories._session import tenant_context
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.position_transaction_repository import PositionTransactionRepository
from core.tenant_constants import SENTINEL_TENANT_ID
from services.market_data import (
    IdentifierNotResolvableError,
    ProviderFetchError,
    ResolvedInstrument,
)
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

#: A second tenant, for the RLS assertion on the resume GET.
_OTHER_TENANT_ID = UUID("11111111-2222-3333-4444-555555555555")

#: A settled past date, so the future-trade-date warning never fires by
#: accident and every rendered date is stable.
_TRADE_DATE = _date(2026, 1, 15)
_OPENING_DATE = _date(2026, 1, 2)

#: M-2's own instrument, so the values these tests assert on are the mockup's.
_ISIN = "LU0847246898"
_FIGI = "BBG013T5K5M8"
_NAME = "Meridian European Mid-Cap Equity Fund"


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; skipping live-DB wizard tests.",
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
    email = "wizard-owner@example.com"
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
# Seeding and reading helpers
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


async def _seed_asset_class(user_id: UUID) -> UUID:
    """One asset class, for the Classify select and the created row's FK."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            created = await AssetClassRepository(session).create(
                code="eu-equity", display_name="European Equity"
            )
            return created.id
    finally:
        await engine.dispose()


async def _seed_cash(user_id: UUID, *, currency: str = "EUR", balance: str = "412500") -> UUID:
    """The settlement position M-2's Order step settles against."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code=f"cash-{uuid4().hex[:8]}", display_name=f"{currency} Cash"
            )
            cash = await InvestmentRepository(session).create(
                name=f"{currency} Cash — Commerzbank",
                investment_type="cash",
                asset_class_id=asset_class.id,
                currency=currency,
                created_by=user_id,
                valuation_mode="unitised",
            )
            await PositionTransactionRepository(session).add(
                investment_id=cash.id,
                txn_type="opening",
                trade_date=_OPENING_DATE,
                units=Decimal(balance),
                currency=currency,
                ingest_origin="manual",
                created_by=user_id,
            )
            return cash.id
    finally:
        await engine.dispose()


async def _seed_member(superuser_engine: AsyncEngine) -> tuple[str, str]:
    """Seed a member of the primary tenant, for the owner-gating assertions."""
    plaintext = "correct-horse-battery-staple"
    email = "wizard-member@example.com"
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
                "master_data, set_inactive FROM trade_tickets ORDER BY ticket_number"
            )
        )
        found = rows.mappings().all()
    assert len(found) == 1, f"expected exactly one ticket, found {len(found)}"
    return dict(found[0])


async def _identifiers(engine: AsyncEngine) -> list[tuple[str, str, bool]]:
    """Every identifier row, as (scheme, value, is_primary)."""
    async with engine.begin() as conn:
        rows = await conn.execute(
            text("SELECT scheme, value, is_primary FROM investment_identifiers ORDER BY scheme")
        )
        return [(row[0], row[1], row[2]) for row in rows]


def _flat(markup: str) -> str:
    """Collapse whitespace, so a copy assertion is not a line-wrap assertion."""
    return " ".join(markup.split())


def _new_section(body: str) -> str:
    """Slice the New-transaction Section out of the area page."""
    start = body.index('<section class="pf-section" id="new"')
    return body[start : body.index("</section>", start) + len("</section>")]


def _disabled(markup: str, label: str) -> bool:
    """Is the button carrying ``label`` rendered disabled?"""
    idx = markup.index(f">{label}</button>")
    return "disabled" in markup[markup.rindex("<button", 0, idx) : idx]


def _step(markup: str) -> int:
    """Which step the rendered wizard is on, read from the stepper."""
    for number in (1, 2, 3, 4):
        marker = f'<span class="tx-step__dot">{number}</span>'
        active = f'<div class="tx-step is-active">\n            {marker}'
        if _flat(active) in _flat(markup):
            return number
    raise AssertionError("no active step in the rendered stepper")


def _identify_form(**overrides: str) -> dict[str, str]:
    """Step 1's body: the resolve path, filled in as M-2 shows it."""
    form = {
        "flow": routes.FLOW_NEW_INSTRUMENT,
        "md_identifier_scheme": "isin",
        "md_identifier_value": _ISIN,
        "md_figi": _FIGI,
        "md_name": _NAME,
        "currency": "EUR",
        "trade_date": _TRADE_DATE.isoformat(),
        "step": "2",
    }
    form.update(overrides)
    return form


def _classify_form(asset_class_id: UUID, **overrides: str) -> dict[str, str]:
    """Step 2's body, carrying step 1's facts as the hidden inputs do."""
    form = _identify_form(step="3")
    form.update(
        {
            "md_investment_type": "listed_equity",
            "md_asset_class_id": str(asset_class_id),
            "md_anlv_code": "anlv_15",
            "md_manager": "Meridian Asset Management",
            "md_region": "Europe",
        }
    )
    form.update(overrides)
    return form


def _order_form(asset_class_id: UUID, cash_id: UUID, **overrides: str) -> dict[str, str]:
    """Step 3's body — M-2's own units, price and fees."""
    form = _classify_form(asset_class_id, step="4")
    form.update(
        {
            "units": "950",
            "price_per_unit": "42.18",
            "fees": "95",
            "taxes": "0",
            "cash_investment_id": str(cash_id),
            "settle_confirm": "1",
        }
    )
    form.update(overrides)
    return form


def _stub_resolver(
    monkeypatch: pytest.MonkeyPatch,
    outcome: ResolvedInstrument | Exception,
) -> list[tuple[str, str, str | None]]:
    """Replace the resolver at the name the route imported. Records its calls."""
    calls: list[tuple[str, str, str | None]] = []

    async def _fake(
        scheme: str, value: str, *, api_key: str | None = None, timeout: float = 10.0
    ) -> ResolvedInstrument:
        calls.append((scheme, value, api_key))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(routes, "resolve_instrument", _fake)
    return calls


# ---------------------------------------------------------------------------
# Chooser — S4b arms the second tile
# ---------------------------------------------------------------------------


async def test_chooser_arms_the_new_instrument_tile(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The U-NEW tile is live; the flows S4c has not armed keep their pills.

    The counts move with each arming — S4c/P-4a made R-SEC-SELL the third
    live tile — and this is the second of the two places they are stated;
    the other is ``test_transactions_composer.py``'s chooser test, which owns
    the copy on all five. What this test is *for* is unchanged: the tile S4b
    armed is a control and points at the wizard.
    """
    _id, email, password = seeded_user
    await _login_and_csrf(web_client, email, password)

    section = _new_section((await web_client.get("/transactions")).text)

    assert 'hx-get="/api/transactions/wizard"' in section
    assert section.count("<button") == 3, "the three live tiles are the only controls"
    assert "Arrives with S4b" not in section
    assert section.count("Arrives with S4c") == 2
    assert "Create the investment, then buy it." in section


# ---------------------------------------------------------------------------
# Step 1 — Identify, and the two paths through it
# ---------------------------------------------------------------------------


async def test_wizard_opens_unsaved_at_step_one(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """MD-2: opening the wizard allocates nothing and burns no number."""
    _id, email, password = seeded_user
    await _login_and_csrf(web_client, email, password)

    response = await web_client.get("/api/transactions/wizard")
    assert response.status_code == 200
    body = response.text

    assert _step(body) == 1
    assert "New ticket" in body
    assert "Unsaved" in body
    assert "Buy a new instrument" in body
    # M-2's two cards, both open, and the W-4 currency control beneath them.
    assert "Public identifier" in body
    assert "No public identifier" in body
    assert 'name="currency"' in body
    # MD-14: no direction control anywhere in this wizard.
    assert 'name="direction"' not in body
    assert await _count(superuser_engine, "trade_tickets") == 0


async def test_resolve_prefills_editable_figi_name_and_currency(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W-4′: what the resolver learned arrives as editable values, not as text."""
    _id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)
    calls = _stub_resolver(monkeypatch, ResolvedInstrument(figi=_FIGI, name=_NAME, currency="EUR"))

    response = await web_client.post(
        "/api/transactions/resolve-identifier",
        data={
            "flow": routes.FLOW_NEW_INSTRUMENT,
            "md_identifier_scheme": "isin",
            "md_identifier_value": _ISIN,
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200
    body = response.text

    assert calls == [("isin", _ISIN, None)], "the route calls resolve_instrument, keyless here"
    assert _step(body) == 1
    assert "Resolved" in body
    # Editable inputs, not the mockup's read-only text (registered deviation).
    assert f'name="md_figi" value="{_FIGI}"' in body.replace("\n", " ").replace("  ", " ") or (
        'name="md_figi"' in body and _FIGI in body
    )
    assert 'name="md_name"' in body and _NAME in body
    assert 'name="currency"' in body and 'value="EUR"' in body
    assert "Both identifiers (isin, figi) will be stored on the new investment." in _flat(body)
    # A read: nothing is written anywhere by a resolution (MD-13 stores the
    # identifiers at booking, through the emission).
    assert await _count(superuser_engine, "trade_tickets") == 0
    assert await _count(superuser_engine, "investment_identifiers") == 0


async def test_resolve_without_a_currency_leaves_the_field_empty(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-A: OpenFIGI stating no currency is the normal case, never an error."""
    _id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)
    _stub_resolver(monkeypatch, ResolvedInstrument(figi=_FIGI, name=_NAME, currency=None))

    body = (
        await web_client.post(
            "/api/transactions/resolve-identifier",
            data={
                "flow": routes.FLOW_NEW_INSTRUMENT,
                "md_identifier_scheme": "isin",
                "md_identifier_value": _ISIN,
                "csrf_token": csrf,
            },
        )
    ).text

    assert "Resolved" in body
    assert _FIGI in body
    assert 'name="currency" class="tx-mono" maxlength="3" value=""' in _flat(body)
    assert "tx-msg--block" not in body, "an absent currency is not a refusal"


async def test_resolve_reports_no_match_and_provider_failure(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two resolver failures each get their own sentence, on step 1."""
    _id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)
    payload = {
        "flow": routes.FLOW_NEW_INSTRUMENT,
        "md_identifier_scheme": "isin",
        "md_identifier_value": _ISIN,
        "csrf_token": csrf,
    }

    _stub_resolver(monkeypatch, IdentifierNotResolvableError("nothing here"))
    body = (await web_client.post("/api/transactions/resolve-identifier", data=payload)).text
    assert _step(body) == 1
    assert f"No instrument matched isin {_ISIN}." in _flat(body)
    assert "use the second card and name the instrument yourself" in _flat(body)

    _stub_resolver(monkeypatch, ProviderFetchError("OpenFIGI timed out after 10.0s."))
    body = (await web_client.post("/api/transactions/resolve-identifier", data=payload)).text
    assert "OpenFIGI timed out after 10.0s." in _flat(body)


# ---------------------------------------------------------------------------
# MD-2 and the trio rule — the first Continue allocates the ticket
# ---------------------------------------------------------------------------


async def test_continue_without_a_currency_refuses_and_names_the_field(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """W-4: the currency is what the creating path has instead of an investment."""
    _id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)

    for bad in ("", "EU", "EURO", "12X"):
        response = await web_client.post(
            "/api/transactions/draft",
            data=_identify_form(currency=bad, csrf_token=csrf),
        )
        assert response.status_code == 200, bad
        assert "A new instrument needs a currency" in _flat(response.text), bad
        assert _step(response.text) == 1, bad
    assert await _count(superuser_engine, "trade_tickets") == 0


async def test_first_continue_allocates_the_draft_with_the_identify_facts(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """MD-2 on the wizard's own gesture, and MD-12's absent investment_id."""
    _user_id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/draft", data=_identify_form(csrf_token=csrf)
    )
    assert response.status_code == 200
    assert _step(response.text) == 2

    row = await _ticket_row(superuser_engine)
    assert row["kind"] == "order"
    assert row["direction"] == "buy"
    assert row["status"] == "draft"
    assert row["investment_id"] is None
    assert row["currency"] == "EUR"
    assert row["trade_date"] == _TRADE_DATE
    assert row["ticket_number"] == 1
    assert row["master_data"] == {
        "currency": "EUR",
        "figi": _FIGI,
        "identifier_scheme": "isin",
        "identifier_value": _ISIN,
        "name": _NAME,
    }
    assert f"Ticket #{row['ticket_number']}" in response.text
    assert "Draft" in response.text


async def test_the_no_identifier_path_saves_a_draft_too(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The second card is a choice, not a dead end: currency alone is enough."""
    _id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)

    response = await web_client.post(
        "/api/transactions/draft",
        data={
            "flow": routes.FLOW_NEW_INSTRUMENT,
            "currency": "chf",
            "trade_date": _TRADE_DATE.isoformat(),
            "step": "2",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200
    assert _step(response.text) == 2

    row = await _ticket_row(superuser_engine)
    # F-B: the shape rule normalises as well as checks.
    assert row["currency"] == "CHF"
    assert row["master_data"] == {"currency": "CHF"}


async def test_a_tampered_sell_is_still_stored_as_buy(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """MD-14: the direction is the flow's constant, not a form value."""
    _id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)

    await web_client.post(
        "/api/transactions/draft",
        data=_identify_form(direction="sell", set_inactive="1", csrf_token=csrf),
    )

    row = await _ticket_row(superuser_engine)
    assert row["direction"] == "buy"
    # The disposal choice is a sell concept and is forced off with it.
    assert row["set_inactive"] is False


async def test_a_second_continue_updates_the_row_it_allocated(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """MD-2's other half: one composer, one ticket number."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    first = await web_client.post("/api/transactions/draft", data=_identify_form(csrf_token=csrf))
    ticket_id = (await _ticket_row(superuser_engine))["id"]

    assert f'name="ticket_id" value="{ticket_id}"' in first.text
    second = await web_client.post(
        "/api/transactions/draft",
        data=_classify_form(asset_class_id, ticket_id=str(ticket_id), csrf_token=csrf),
    )
    assert second.status_code == 200
    assert _step(second.text) == 3

    row = await _ticket_row(superuser_engine)
    assert row["id"] == ticket_id
    assert row["ticket_number"] == 1
    assert row["master_data"]["name"] == _NAME
    assert row["master_data"]["investment_type"] == "listed_equity"
    assert row["master_data"]["asset_class_id"] == str(asset_class_id)
    assert row["master_data"]["anlv_code"] == "anlv_15"
    assert row["master_data"]["manager"] == "Meridian Asset Management"


# ---------------------------------------------------------------------------
# Resume (MD-10) — the step is derived, never stored
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        ("identify", 2),
        ("classify", 2),
        ("anlv", 2),
        ("order", 3),
        ("complete", 4),
    ],
)
async def test_a_saved_draft_reopens_at_its_own_step(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
    gap: str,
    expected: int,
) -> None:
    """Each gap in the draft's content puts the resume on the step that fills it."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    if gap == "identify":
        form = _identify_form(csrf_token=csrf)
    elif gap == "classify":
        form = _classify_form(asset_class_id, md_name="", csrf_token=csrf)
    elif gap == "anlv":
        form = _classify_form(asset_class_id, md_anlv_code="", csrf_token=csrf)
    elif gap == "order":
        form = _classify_form(asset_class_id, csrf_token=csrf)
    else:
        form = _order_form(asset_class_id, cash_id, csrf_token=csrf)
    await web_client.post("/api/transactions/draft", data=form)
    ticket_id = (await _ticket_row(superuser_engine))["id"]

    response = await web_client.get(f"/api/transactions/wizard?ticket_id={ticket_id}")
    assert response.status_code == 200
    assert _step(response.text) == expected
    assert "Ticket #1" in response.text


async def test_resume_carries_the_saved_values_back_onto_the_form(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """A resumed wizard shows the ticket, not a blank form."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)
    await web_client.post(
        "/api/transactions/draft", data=_order_form(asset_class_id, cash_id, csrf_token=csrf)
    )
    ticket_id = (await _ticket_row(superuser_engine))["id"]

    # Back from the derived step 4 to step 3, without a write.
    before = await _count(superuser_engine, "trade_tickets")
    body = (await web_client.get(f"/api/transactions/wizard?ticket_id={ticket_id}&step=3")).text
    assert await _count(superuser_engine, "trade_tickets") == before

    assert _step(body) == 3
    # The column's fixed scale is normalised away: a resume shows 950, not
    # 950.00000000, which is the same number in the form it was typed in.
    assert 'value="950"' in body
    assert 'value="42.18"' in body
    assert _NAME in body
    assert "EUR Cash — Commerzbank" in body


async def test_resume_refuses_a_ticket_that_is_not_this_wizards(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Cross-tenant, unknown and wrong-shaped all answer 404 (D-AF)."""
    _id, email, password = seeded_user
    await _login_and_csrf(web_client, email, password)

    response = await web_client.get(f"/api/transactions/wizard?ticket_id={uuid4()}")
    assert response.status_code == 404


async def test_resume_is_tenant_scoped(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """A draft in another tenant is not visible, and not distinguishable."""
    _user_id, email, password = seeded_user
    csrf = await _login_and_csrf(web_client, email, password)
    await web_client.post("/api/transactions/draft", data=_identify_form(csrf_token=csrf))
    ticket_id = (await _ticket_row(superuser_engine))["id"]

    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE trade_tickets SET tenant_id = :other WHERE id = :id"),
            {"other": str(_OTHER_TENANT_ID), "id": str(ticket_id)},
        )

    assert (
        await web_client.get(f"/api/transactions/wizard?ticket_id={ticket_id}")
    ).status_code == 404


# ---------------------------------------------------------------------------
# Step 2 — Classify, the catalogues and the MD-21 finish gate
# ---------------------------------------------------------------------------


async def test_classify_offers_the_tenant_catalogues(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Asset classes, AnlV categories and the seven classifiable types."""
    user_id, email, password = seeded_user
    await _seed_asset_class(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    body = (
        await web_client.post("/api/transactions/draft", data=_identify_form(csrf_token=csrf))
    ).text

    assert _step(body) == 2
    assert "European Equity" in body
    assert "§ 2 Abs. 1 Nr. 15 AnlV" in body
    assert "— not set —" in body
    for option in routes._CLASSIFIABLE_TYPES:
        assert f'<option value="{option}"' in body
    assert set(routes._CLASSIFIABLE_TYPES) < INVESTMENT_TYPES
    assert "cash" not in routes._CLASSIFIABLE_TYPES
    # M-2's own hint and the fixed valuation mode.
    assert "Required before the wizard can finish." in _flat(body)
    assert 'value="unitised" readonly' in body


async def test_the_anlv_gate_withholds_propose_and_book_but_never_the_draft(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """MD-11 / MD-21: the draft may dangle; `proposed` may not."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    ungated = await web_client.post(
        "/api/transactions/draft",
        data=_order_form(asset_class_id, cash_id, md_anlv_code="", csrf_token=csrf),
    )
    body = ungated.text
    assert _step(body) == 4
    assert "This wizard cannot finish without an AnlV category." in _flat(body)
    assert _disabled(body, "Book now")
    assert _disabled(body, "Propose")
    assert not _disabled(body, "Keep as draft")
    assert "Keep as draft is always available." in _flat(body)
    # The draft it refuses to finish is nonetheless saved (MD-11).
    assert (await _ticket_row(superuser_engine))["master_data"].get("anlv_code") is None

    ticket_id = (await _ticket_row(superuser_engine))["id"]
    armed = await web_client.post(
        "/api/transactions/draft",
        data=_order_form(asset_class_id, cash_id, ticket_id=str(ticket_id), csrf_token=csrf),
    )
    assert not _disabled(armed.text, "Book now")
    assert not _disabled(armed.text, "Propose")
    assert "Booking creates the investment and both ledger legs in one step." in _flat(armed.text)


# ---------------------------------------------------------------------------
# Step 3 — the Order step derives on M-1's machinery
# ---------------------------------------------------------------------------


async def test_the_order_step_derives_amounts_legs_and_settlement(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """M-2's own numbers, from the same service the booking runs."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)
    await web_client.post(
        "/api/transactions/draft", data=_classify_form(asset_class_id, csrf_token=csrf)
    )

    body = (
        await web_client.post(
            "/api/transactions/recalc",
            data=_order_form(asset_class_id, cash_id, step="3", csrf_token=csrf),
        )
    ).text
    flat = _flat(body)

    # A new row holds nothing, and there is no price to have known.
    assert "0.0000 units" in flat
    assert "— none yet —" in flat
    # 950 × 42.18 = 40,071.00; +95 fees = 40,166.00 to pay.
    #
    # The total is the magnitude with the *label* carrying its direction —
    # M-1's convention, reached unchanged, where M-2 draws a leading minus
    # instead (a registered fidelity deviation). The signed movement is in
    # the cash leg below, where the ledger will record it.
    assert "40,071.00 EUR" in flat
    assert "Net cost" in flat
    assert "+40,166.00 EUR" in flat
    # Both legs: the instrument at the flow's constant buy, the cash leg from
    # `cash_leg` — the pure function the booking itself calls.
    assert f"{_NAME} <em>@ 42.1800</em>" in flat
    assert "+950.0000 units" in flat
    assert "−40,166.0000 units" in flat
    assert "412,500.00" in flat and "372,334.00" in flat


async def test_recalc_on_the_creating_path_writes_nothing(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """A keystroke derives; it never allocates a row (MD-2)."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    before = {
        table: await _count(superuser_engine, table)
        for table in ("trade_tickets", "investments", "position_transactions")
    }
    response = await web_client.post(
        "/api/transactions/recalc",
        data=_order_form(asset_class_id, cash_id, step="3", csrf_token=csrf),
    )
    assert response.status_code == 200
    for table, count in before.items():
        assert await _count(superuser_engine, table) == count, table


# ---------------------------------------------------------------------------
# Booking — MD-12's investment row is an emission effect
# ---------------------------------------------------------------------------


async def test_book_creates_the_investment_its_identifiers_and_both_legs(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """The whole flow: the row exists only after the booking (MD-12, MD-13)."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    await web_client.post(
        "/api/transactions/draft", data=_order_form(asset_class_id, cash_id, csrf_token=csrf)
    )
    ticket_id = (await _ticket_row(superuser_engine))["id"]
    # Before the booking: only the cash position exists.
    async with superuser_engine.begin() as conn:
        names = await conn.execute(text("SELECT name FROM investments ORDER BY name"))
        assert [row[0] for row in names] == ["EUR Cash — Commerzbank"]
    assert await _count(superuser_engine, "investment_identifiers") == 0

    response = await web_client.post(
        "/api/transactions/book",
        data=_order_form(asset_class_id, cash_id, ticket_id=str(ticket_id), csrf_token=csrf),
    )
    assert response.status_code == 200
    panel = response.text

    async with superuser_engine.begin() as conn:
        created = (
            (
                await conn.execute(
                    text(
                        "SELECT id, investment_type, currency, valuation_mode, anlv_code, "
                        "manager_name FROM investments WHERE name = :name"
                    ),
                    {"name": _NAME},
                )
            )
            .mappings()
            .one()
        )
    assert created["investment_type"] == "listed_equity"
    assert created["currency"] == "EUR"
    assert created["valuation_mode"] == "unitised"
    assert created["anlv_code"] == "anlv_15"
    assert created["manager_name"] == "Meridian Asset Management"

    # MD-13: both identifiers, the chosen scheme primary and the FIGI beside it.
    assert sorted(await _identifiers(superuser_engine)) == sorted(
        [("figi", _FIGI, False), ("isin", _ISIN, True)]
    )

    # Both legs, on the created id and on the cash position.
    async with superuser_engine.begin() as conn:
        legs = await conn.execute(
            text(
                "SELECT investment_id, txn_type, units FROM position_transactions "
                "WHERE trade_date = :d ORDER BY txn_type"
            ),
            {"d": _TRADE_DATE},
        )
        rows = [(r[0], r[1], r[2]) for r in legs]
    assert (created["id"], "buy", Decimal("950.0000")) in rows
    assert (cash_id, "sell", Decimal("-40166.0000")) in rows

    # The MD-16 confirmation panel, naming the row the booking made.
    assert "Ticket #1" in panel
    assert _NAME in panel
    assert await _count(superuser_engine, "trade_ticket_effects") >= 3


async def test_a_duplicate_name_refuses_at_book_and_keeps_the_draft(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """D-5: the service's own sentence, and nothing is written."""
    user_id, email, password = seeded_user
    asset_class_id = await _seed_asset_class(user_id)
    cash_id = await _seed_cash(user_id)
    csrf = await _login_and_csrf(web_client, email, password)

    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            await InvestmentRepository(session).create(
                name=_NAME,
                investment_type="listed_equity",
                asset_class_id=asset_class_id,
                currency="EUR",
                created_by=user_id,
                valuation_mode="unitised",
            )
    finally:
        await engine.dispose()

    response = await web_client.post(
        "/api/transactions/book", data=_order_form(asset_class_id, cash_id, csrf_token=csrf)
    )
    assert response.status_code == 200
    body = response.text

    assert "tx-msg--block" in body
    # The service's own sentence, quoted with `!r` and therefore autoescaped
    # by Jinja — the assertion reads around the escaped apostrophes rather
    # than pinning the escaping.
    assert "An investment named" in _flat(body)
    assert _NAME in _flat(body)
    assert "already exists in this tenant." in _flat(body)
    assert _step(body) == 4
    row = await _ticket_row(superuser_engine)
    assert row["status"] == "draft", "a refused booking leaves the work as a draft"
    assert await _count(superuser_engine, "trade_ticket_effects") == 0


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


async def test_a_member_may_not_write_on_the_creating_path(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """Owner writes domain data (ADR-0063 §2); the reads stay open."""
    _id, _email, _password = seeded_user
    member_email, member_password = await _seed_member(superuser_engine)
    csrf = await _login_and_csrf(web_client, member_email, member_password)
    form = _identify_form(csrf_token=csrf)

    for url in (
        "/api/transactions/draft",
        "/api/transactions/propose",
        "/api/transactions/book",
        "/api/transactions/cash-position",
    ):
        assert (await web_client.post(url, data=form)).status_code == 403, url

    # The two reads this strand adds are session-gated, not role-gated.
    assert (await web_client.get("/api/transactions/wizard")).status_code == 200
    assert (
        await web_client.post("/api/transactions/resolve-identifier", data=form)
    ).status_code == 200


async def test_the_wizard_endpoints_require_a_session_and_a_csrf_token(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """No session at all, then a session without the token."""
    _id, email, password = seeded_user

    assert (await web_client.get("/api/transactions/wizard")).status_code in (302, 303, 401, 403)

    await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        "/api/transactions/resolve-identifier",
        data={"flow": routes.FLOW_NEW_INSTRUMENT, "md_identifier_scheme": "isin"},
    )
    assert response.status_code == 403
