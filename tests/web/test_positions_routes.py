# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""HTTP-level tests for the positions surface (ADR-0097 §6 / ADR-0098 §3).

Coverage targets:

* Route permission matrix — ``owner`` writes; ``member`` and ``auditor`` are
  refused (403); reads stay open to every tenant role.
* CSRF enforcement on all four mutating routes.
* Transaction CRUD: happy path, and every invalid path surfacing as a
  structured form error rather than a 500 — sign rules, price rules,
  currency mismatch (ADR-0097 §5), negative derived holdings (§4), and the
  one-``opening``-per-investment index (409).
* Mode-flip precondition matrix, one-way enforcement, ``'live'``-row
  deletion with ``'excel'``/``'manual'`` rows surviving, and the initial
  computed-NAV materialisation.
* Regression: a ``reported``-mode private-markets investment renders no
  positions panel and its NAV rows are never touched (ADR-0098 §5).
* Cross-tenant / cross-investment ids resolve to 404, never 403.

Live-DB tests against the compose Postgres so RLS, the b024/b025 CHECK
constraints, and the partial unique index evaluate exactly as in production.
"""

from __future__ import annotations

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
    InvestmentRepository,
    tenant_context,
)
from core.repositories.instrument_price_repository import (
    InstrumentPriceRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(pathlib.Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

OTHER_TENANT_ID = UUID("22222222-2222-2222-2222-222222222222")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB positions-route tests.",
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


@pytest_asyncio.fixture
async def reset_schema(
    superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    truncate_sql = text(
        "TRUNCATE TABLE position_transactions, instrument_prices, "
        "investment_region_weights, region_country_memberships, regions, "
        "investment_country_weights, "
        "investment_sector_weights, sectors, "
        "investment_cashflows, investment_navs, investments, "
        "asset_classes, "
        "login_audit, sessions, audit_log, users, tenants "
        "RESTART IDENTITY CASCADE"
    )
    async with superuser_engine.begin() as conn:
        await conn.execute(truncate_sql)
    try:
        yield
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(truncate_sql)


@pytest_asyncio.fixture
async def seeded_users(
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> dict[str, tuple[UUID, str, str]]:
    """Seed one user per tenant role, plus a second tenant for isolation."""
    plaintext = "correct-horse-battery-staple"
    users: dict[str, tuple[UUID, str, str]] = {}
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, 'other-tenant') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(OTHER_TENANT_ID), "name": "Other Tenant"},
        )
        for role in ("owner", "member", "auditor"):
            user_id = uuid4()
            email = f"positions-{role}@example.com"
            await conn.execute(
                text(
                    """
                    INSERT INTO users
                        (id, tenant_id, email, password_hash, roles, is_active)
                    VALUES
                        (:id, :tid, :email, :hash,
                         ARRAY[:role]::text[], TRUE)
                    """
                ),
                {
                    "id": str(user_id),
                    "tid": str(SENTINEL_TENANT_ID),
                    "email": email,
                    "hash": hash_password(plaintext),
                    "role": role,
                },
            )
            users[role] = (user_id, email, plaintext)

        other_id = uuid4()
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(other_id),
                "tid": str(OTHER_TENANT_ID),
                "email": "positions-other@example.com",
                "hash": hash_password(plaintext),
            },
        )
        users["other_owner"] = (
            other_id,
            "positions-other@example.com",
            plaintext,
        )
    return users


@pytest_asyncio.fixture
async def web_client(
    seeded_users: dict[str, tuple[UUID, str, str]],
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
    """Log in and return the session-bound CSRF token."""
    get_response = await client.get("/login")
    pre_csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert pre_csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": pre_csrf},
        follow_redirects=False,
    )
    page = await client.get("/investments", follow_redirects=False)
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
    tenant_id: UUID = SENTINEL_TENANT_ID,
) -> UUID:
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, tenant_id, user_id=user_id) as session:
            ac = await AssetClassRepository(session).create(
                code=f"ac-{name.lower().replace(' ', '-')}",
                display_name=f"AC {name}",
            )
            inv = await InvestmentRepository(session).create(
                name=name,
                investment_type=investment_type,
                asset_class_id=ac.id,
                currency=currency,
                created_by=user_id,
            )
            return inv.id
    finally:
        await engine.dispose()


async def _seed_price(
    user_id: UUID,
    investment_id: UUID,
    as_of: date,
    price: Decimal,
    *,
    currency: str = "EUR",
    ingest_origin: str = "manual",
) -> None:
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            await InstrumentPriceRepository(session).upsert(
                investment_id=investment_id,
                as_of_date=as_of,
                price=price,
                currency=currency,
                source="test",
                created_by=user_id,
                ingest_origin=ingest_origin,
            )
    finally:
        await engine.dispose()


async def _seed_nav(
    user_id: UUID,
    investment_id: UUID,
    as_of: date,
    value: Decimal,
    *,
    ingest_origin: str,
    nav_kind: str = "actual",
) -> None:
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            await InvestmentNavRepository(session).upsert(
                investment_id=investment_id,
                as_of_date=as_of,
                nav_kind=nav_kind,
                nav_value=value,
                currency="EUR",
                source="test",
                created_by=user_id,
                ingest_origin=ingest_origin,
            )
    finally:
        await engine.dispose()


async def _read_navs(investment_id: UUID) -> list[tuple[str, str, str | None]]:
    """Return ``(as_of_date, ingest_origin, basis)`` for the actual NAV rows."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID) as session:
            rows = await InvestmentNavRepository(session).list_by_investment_and_kind(
                investment_id, "actual"
            )
            return [(r.as_of_date.isoformat(), r.ingest_origin, r.basis) for r in rows]
    finally:
        await engine.dispose()


async def _valuation_mode(investment_id: UUID) -> str:
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID) as session:
            inv = await InvestmentRepository(session).get_by_id(investment_id)
            assert inv is not None
            return inv.valuation_mode
    finally:
        await engine.dispose()


def _is_flip_button_disabled(html: str) -> bool:
    """Whether the flip button renders with the ``disabled`` attribute.

    Matches the opening tag only, so an unrelated ``disabled`` elsewhere on
    the page cannot make this read ``True``.
    """
    match = re.search(r"<button id=\"flip-mode-btn\"[^>]*>", html)
    assert match is not None, "flip button is absent from the page"
    return "disabled" in match.group(0)


def _is_flip_reason_hidden(html: str) -> bool:
    """Whether the blocked-reason paragraph renders hidden."""
    match = re.search(r"<p class=\"inv-flip-blocked\"[^>]*>", html)
    assert match is not None, "flip-blocked paragraph is absent from the page"
    return "hidden" in match.group(0)


def _txn(**overrides) -> dict:
    body = {
        "txn_type": "opening",
        "trade_date": "2026-01-05",
        "units": "100",
        "currency": "EUR",
    }
    body.update(overrides)
    return body


async def _add_opening(client: AsyncClient, csrf: str, investment_id: UUID, **overrides):
    return await client.post(
        f"/investments/{investment_id}/positions",
        json=_txn(**overrides),
        headers={"X-CSRF-Token": csrf},
    )


# ---------------------------------------------------------------------------
# Permission matrix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_may_add_transaction(web_client: AsyncClient, seeded_users) -> None:
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed A")

    response = await _add_opening(web_client, csrf, inv_id)

    assert response.status_code == 200
    panel = response.json()["positions"]
    assert len(panel["transactions"]) == 1
    assert panel["transactions"][0]["txn_type"] == "opening"
    assert panel["transactions"][0]["ingest_origin"] == "manual"
    assert panel["holdings_units"] == 100.0
    assert panel["holdings_as_of_date"] == "2026-01-05"
    # The opening now satisfies the flip preconditions.
    assert panel["can_flip"] is True
    assert panel["flip_blocked_reason"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "auditor"])
async def test_non_owner_roles_refused_on_every_write(
    web_client: AsyncClient, seeded_users, role: str
) -> None:
    """require_role('owner') is strict — no implicit promotion (ADR-0063 §2)."""
    owner_id, _, _ = seeded_users["owner"]
    inv_id = await _seed_investment(owner_id, name="Listed B")

    _, email, pw = seeded_users[role]
    csrf = await _login_and_csrf(web_client, email, pw)
    headers = {"X-CSRF-Token": csrf}
    txn_id = uuid4()

    assert (
        await web_client.post(f"/investments/{inv_id}/positions", json=_txn(), headers=headers)
    ).status_code == 403
    assert (
        await web_client.put(
            f"/investments/{inv_id}/positions/{txn_id}",
            json=_txn(),
            headers=headers,
        )
    ).status_code == 403
    assert (
        await web_client.delete(f"/investments/{inv_id}/positions/{txn_id}", headers=headers)
    ).status_code == 403
    assert (
        await web_client.post(f"/investments/{inv_id}/valuation-mode/unitised", headers=headers)
    ).status_code == 403


@pytest.mark.asyncio
async def test_reads_stay_open_to_auditor(web_client: AsyncClient, seeded_users) -> None:
    owner_id, _, _ = seeded_users["owner"]
    inv_id = await _seed_investment(owner_id, name="Listed C")

    _, email, pw = seeded_users["auditor"]
    await _login_and_csrf(web_client, email, pw)

    page = await web_client.get(f"/investments/{inv_id}")
    assert page.status_code == 200
    assert "Positions" in page.text


@pytest.mark.asyncio
async def test_writes_require_csrf(web_client: AsyncClient, seeded_users) -> None:
    owner_id, email, pw = seeded_users["owner"]
    await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed D")
    txn_id = uuid4()

    assert (
        await web_client.post(f"/investments/{inv_id}/positions", json=_txn())
    ).status_code == 403
    assert (
        await web_client.put(f"/investments/{inv_id}/positions/{txn_id}", json=_txn())
    ).status_code == 403
    assert (await web_client.delete(f"/investments/{inv_id}/positions/{txn_id}")).status_code == 403
    assert (
        await web_client.post(f"/investments/{inv_id}/valuation-mode/unitised")
    ).status_code == 403


# ---------------------------------------------------------------------------
# Transaction CRUD — invalid paths surface as structured form errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"txn_type": "sell", "units": "5", "price_per_unit": "10"}, "units"),
        ({"txn_type": "buy", "units": "-5", "price_per_unit": "10"}, "units"),
        ({"txn_type": "opening", "units": "-1"}, "units"),
        ({"txn_type": "transfer", "units": "0"}, "units"),
        ({"txn_type": "buy", "units": "5"}, "price_per_unit"),
        (
            {"txn_type": "buy", "units": "5", "price_per_unit": "0"},
            "price_per_unit",
        ),
        ({"txn_type": "gift", "units": "5"}, "txn_type"),
    ],
)
async def test_sign_and_price_rules_surface_as_form_errors(
    web_client: AsyncClient, seeded_users, overrides: dict, field: str
) -> None:
    """ADR-0097 §2 rules are caught before the CHECK fires — 400, not 500."""
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed E")

    response = await _add_opening(web_client, csrf, inv_id, **overrides)

    assert response.status_code == 400
    assert response.json()["field"] == field


@pytest.mark.asyncio
async def test_currency_mismatch_is_rejected_not_converted(
    web_client: AsyncClient, seeded_users
) -> None:
    """ADR-0097 §5: reject rather than convert."""
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed F", currency="EUR")

    response = await _add_opening(web_client, csrf, inv_id, currency="USD")

    assert response.status_code == 400
    body = response.json()
    assert body["field"] == "currency"
    assert "USD" in body["error"]


@pytest.mark.asyncio
async def test_negative_holdings_rejected(web_client: AsyncClient, seeded_users) -> None:
    """ADR-0097 §4: no transaction may drive holdings below zero."""
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed G")

    assert (await _add_opening(web_client, csrf, inv_id)).status_code == 200

    response = await web_client.post(
        f"/investments/{inv_id}/positions",
        json=_txn(
            txn_type="sell",
            units="-150",
            price_per_unit="12",
            trade_date="2026-02-01",
        ),
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert response.json()["field"] == "units"
    assert "below zero" in response.json()["error"]


@pytest.mark.asyncio
async def test_second_opening_is_a_conflict(web_client: AsyncClient, seeded_users) -> None:
    """The partial unique index makes a second opening impossible."""
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed H")

    assert (await _add_opening(web_client, csrf, inv_id)).status_code == 200
    second = await _add_opening(web_client, csrf, inv_id, trade_date="2026-03-01")

    assert second.status_code == 409


@pytest.mark.asyncio
async def test_update_transaction_moves_holdings(web_client: AsyncClient, seeded_users) -> None:
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed I")

    created = await _add_opening(web_client, csrf, inv_id)
    txn_id = created.json()["positions"]["transactions"][0]["id"]

    response = await web_client.put(
        f"/investments/{inv_id}/positions/{txn_id}",
        json={"trade_date": "2026-01-05", "units": "250"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert response.json()["positions"]["holdings_units"] == 250.0


@pytest.mark.asyncio
async def test_update_rejecting_negative_holdings(web_client: AsyncClient, seeded_users) -> None:
    """Lowering an opening beneath a later sell is refused."""
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed J")

    created = await _add_opening(web_client, csrf, inv_id)
    opening_id = created.json()["positions"]["transactions"][0]["id"]
    sell = await web_client.post(
        f"/investments/{inv_id}/positions",
        json=_txn(
            txn_type="sell",
            units="-80",
            price_per_unit="12",
            trade_date="2026-02-01",
        ),
        headers={"X-CSRF-Token": csrf},
    )
    assert sell.status_code == 200

    response = await web_client.put(
        f"/investments/{inv_id}/positions/{opening_id}",
        json={"trade_date": "2026-01-05", "units": "50"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert response.json()["field"] == "units"


@pytest.mark.asyncio
async def test_delete_transaction_rejecting_negative_holdings(
    web_client: AsyncClient, seeded_users
) -> None:
    """Deleting the opening a later sell depends on is refused."""
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed K")

    created = await _add_opening(web_client, csrf, inv_id)
    opening_id = created.json()["positions"]["transactions"][0]["id"]
    await web_client.post(
        f"/investments/{inv_id}/positions",
        json=_txn(
            txn_type="sell",
            units="-80",
            price_per_unit="12",
            trade_date="2026-02-01",
        ),
        headers={"X-CSRF-Token": csrf},
    )

    response = await web_client.delete(
        f"/investments/{inv_id}/positions/{opening_id}",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert response.json()["field"] == "transaction_id"


@pytest.mark.asyncio
async def test_delete_transaction_happy_path(web_client: AsyncClient, seeded_users) -> None:
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed L")

    created = await _add_opening(web_client, csrf, inv_id)
    txn_id = created.json()["positions"]["transactions"][0]["id"]

    response = await web_client.delete(
        f"/investments/{inv_id}/positions/{txn_id}",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    panel = response.json()["positions"]
    assert panel["transactions"] == []
    assert panel["holdings_units"] == 0.0


@pytest.mark.asyncio
async def test_transaction_of_another_investment_is_404(
    web_client: AsyncClient, seeded_users
) -> None:
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_a = await _seed_investment(owner_id, name="Listed M")
    inv_b = await _seed_investment(owner_id, name="Listed N")

    created = await _add_opening(web_client, csrf, inv_a)
    txn_id = created.json()["positions"]["transactions"][0]["id"]

    response = await web_client.delete(
        f"/investments/{inv_b}/positions/{txn_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_foreign_tenant_investment_is_404_never_403(
    web_client: AsyncClient, seeded_users
) -> None:
    other_id, _, _ = seeded_users["other_owner"]
    foreign_inv = await _seed_investment(other_id, name="Foreign Listed", tenant_id=OTHER_TENANT_ID)

    _, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)

    response = await _add_opening(web_client, csrf, foreign_inv)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Mode flip (ADR-0097 §6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flip_refused_for_private_markets_type(web_client: AsyncClient, seeded_users) -> None:
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="PE Fund", investment_type="private_equity")

    response = await web_client.post(
        f"/investments/{inv_id}/valuation-mode/unitised",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    # The exact ADR-0103 §1 copy, so the pre-cash sentence cannot survive.
    assert response.json()["error"] == (
        "Unitised valuation is available for listed equity, listed bonds, and cash only."
    )
    assert await _valuation_mode(inv_id) == "reported"


@pytest.mark.asyncio
async def test_flip_refused_without_opening(web_client: AsyncClient, seeded_users) -> None:
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed O")

    response = await web_client.post(
        f"/investments/{inv_id}/valuation-mode/unitised",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert "opening transaction" in response.json()["error"]
    assert await _valuation_mode(inv_id) == "reported"


@pytest.mark.asyncio
async def test_flip_deletes_live_navs_keeps_excel_and_manual(
    web_client: AsyncClient, seeded_users
) -> None:
    """ADR-0097 §6 + ADR-0098 §1: only 'live' rows go; precedence is honoured."""
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed P")

    # A per-share price wrongly landed in the NAV column by live ingest (F1),
    # alongside a book-of-record row and an operator row.
    await _seed_nav(owner_id, inv_id, date(2026, 1, 5), Decimal("12.50"), ingest_origin="live")
    await _seed_nav(owner_id, inv_id, date(2026, 1, 6), Decimal("9000"), ingest_origin="excel")
    await _seed_nav(owner_id, inv_id, date(2026, 1, 7), Decimal("9100"), ingest_origin="manual")
    # A price on a fourth date, so materialisation has somewhere to write.
    await _seed_price(owner_id, inv_id, date(2026, 1, 8), Decimal("13.00"))

    assert (await _add_opening(web_client, csrf, inv_id)).status_code == 200

    response = await web_client.post(
        f"/investments/{inv_id}/valuation-mode/unitised",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert response.json()["positions"]["valuation_mode"] == "unitised"
    assert await _valuation_mode(inv_id) == "unitised"

    rows = dict((d, (origin, basis)) for d, origin, basis in await _read_navs(inv_id))
    # The live row is gone; excel and manual survive byte-identical.
    assert "2026-01-05" not in rows
    assert rows["2026-01-06"] == ("excel", None)
    assert rows["2026-01-07"] == ("manual", None)
    # The initial materialisation wrote a computed row: 100 units × 13.00.
    assert rows["2026-01-08"] == ("system", "computed")


@pytest.mark.asyncio
async def test_flip_materialises_computed_nav_value(web_client: AsyncClient, seeded_users) -> None:
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed Q")

    await _seed_price(owner_id, inv_id, date(2026, 1, 8), Decimal("13.00"))
    await _add_opening(web_client, csrf, inv_id)

    response = await web_client.post(
        f"/investments/{inv_id}/valuation-mode/unitised",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    nav = response.json()["positions"]["latest_computed_nav"]
    assert nav["nav_value"] == 1300.0
    # basis and ingest_origin stay orthogonal (ADR-0098 §1).
    assert nav["basis"] == "computed"
    assert nav["ingest_origin"] == "system"


@pytest.mark.asyncio
async def test_flip_is_one_way(web_client: AsyncClient, seeded_users) -> None:
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed R")
    await _add_opening(web_client, csrf, inv_id)

    first = await web_client.post(
        f"/investments/{inv_id}/valuation-mode/unitised",
        headers={"X-CSRF-Token": csrf},
    )
    assert first.status_code == 200

    second = await web_client.post(
        f"/investments/{inv_id}/valuation-mode/unitised",
        headers={"X-CSRF-Token": csrf},
    )
    assert second.status_code == 409
    assert "already uses unitised" in second.json()["error"]
    assert await _valuation_mode(inv_id) == "unitised"


# ---------------------------------------------------------------------------
# Cash — the degenerate unitised case (ADR-0103 §1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flip_cash_with_opening_succeeds(web_client: AsyncClient, seeded_users) -> None:
    """A cash position flips end-to-end through the unchanged route.

    With no ``instrument_prices`` rows the initial materialisation legitimately
    inserts **zero** NAV rows: ADR-0098 defines the target set by price date,
    and this position has none yet. That is exactly why S1.3 (Cash-sheet
    import) and S1.4 (ADR-0100-row migration) must supply the unity prices —
    a flipped cash row without them is valued nowhere.
    """
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Cash EUR", investment_type="cash")

    assert (await _add_opening(web_client, csrf, inv_id)).status_code == 200

    response = await web_client.post(
        f"/investments/{inv_id}/valuation-mode/unitised",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert response.json()["positions"]["valuation_mode"] == "unitised"
    assert await _valuation_mode(inv_id) == "unitised"
    # No price rows ⇒ no materialisable dates ⇒ no computed NAVs. Not a bug:
    # the price series is the materialisation's clock (ADR-0098).
    assert await _read_navs(inv_id) == []


@pytest.mark.asyncio
async def test_flip_cash_materialises_the_statement_balance(
    web_client: AsyncClient, seeded_users
) -> None:
    """Once a unity price exists, ``holdings × 1.0000`` *is* the balance."""
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Cash EUR Two", investment_type="cash")

    await _seed_price(owner_id, inv_id, date(2026, 1, 8), Decimal("1.0000"))
    # The opening carries the statement balance as units (ADR-0103 §4).
    await _add_opening(web_client, csrf, inv_id, units="25000")

    response = await web_client.post(
        f"/investments/{inv_id}/valuation-mode/unitised",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    nav = response.json()["positions"]["latest_computed_nav"]
    assert nav["nav_value"] == 25000.0
    assert nav["basis"] == "computed"
    assert nav["ingest_origin"] == "system"


@pytest.mark.asyncio
async def test_flip_cash_without_opening_is_a_conflict(
    web_client: AsyncClient, seeded_users
) -> None:
    """Cash clears the type gate; it does not skip the ledger anchor."""
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Cash EUR Three", investment_type="cash")

    response = await web_client.post(
        f"/investments/{inv_id}/valuation-mode/unitised",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert "opening transaction" in response.json()["error"]
    assert await _valuation_mode(inv_id) == "reported"


@pytest.mark.asyncio
async def test_ledger_write_after_flip_rematerialises(
    web_client: AsyncClient, seeded_users
) -> None:
    """ADR-0098 §3: a ledger edit recomputes the computed NAV in-transaction."""
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed S")

    await _seed_price(owner_id, inv_id, date(2026, 2, 10), Decimal("10.00"))
    await _add_opening(web_client, csrf, inv_id)
    await web_client.post(
        f"/investments/{inv_id}/valuation-mode/unitised",
        headers={"X-CSRF-Token": csrf},
    )

    # A buy on 2026-02-01 lifts holdings to 150 before the priced date.
    response = await web_client.post(
        f"/investments/{inv_id}/positions",
        json=_txn(
            txn_type="buy",
            units="50",
            price_per_unit="11",
            trade_date="2026-02-01",
        ),
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    nav = response.json()["positions"]["latest_computed_nav"]
    assert nav["nav_value"] == 1500.0


@pytest.mark.asyncio
async def test_deleting_last_transaction_strands_no_system_rows(
    web_client: AsyncClient, seeded_users
) -> None:
    """Holdings to zero → the stranded 'system' row is deleted (ADR-0098 §2)."""
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed T")

    await _seed_price(owner_id, inv_id, date(2026, 2, 10), Decimal("10.00"))
    created = await _add_opening(web_client, csrf, inv_id)
    txn_id = created.json()["positions"]["transactions"][0]["id"]
    await web_client.post(
        f"/investments/{inv_id}/valuation-mode/unitised",
        headers={"X-CSRF-Token": csrf},
    )
    assert [r for r in await _read_navs(inv_id) if r[1] == "system"]

    response = await web_client.delete(
        f"/investments/{inv_id}/positions/{txn_id}",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert await _read_navs(inv_id) == []


# ---------------------------------------------------------------------------
# Regression: reported-mode investments are untouched (ADR-0098 §5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_private_markets_detail_page_has_no_positions_panel(
    web_client: AsyncClient, seeded_users
) -> None:
    owner_id, email, pw = seeded_users["owner"]
    await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="PE Fund Two", investment_type="private_equity")

    page = await web_client.get(f"/investments/{inv_id}")

    assert page.status_code == 200
    assert 'id="positions-table"' not in page.text
    assert 'id="position-dialog"' not in page.text
    assert 'id="mode-flip-dialog"' not in page.text


@pytest.mark.asyncio
async def test_listed_detail_page_shows_positions_panel(
    web_client: AsyncClient, seeded_users
) -> None:
    """A unitisable type shows the panel even before it carries a ledger."""
    owner_id, email, pw = seeded_users["owner"]
    await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed U")

    page = await web_client.get(f"/investments/{inv_id}")

    assert page.status_code == 200
    assert 'id="positions-table"' in page.text
    # Preconditions unmet: the flip button is disabled and says why.
    assert "Add an opening transaction" in page.text
    assert _is_flip_button_disabled(page.text) is True
    assert _is_flip_reason_hidden(page.text) is False


@pytest.mark.asyncio
async def test_cash_detail_page_shows_positions_panel(
    web_client: AsyncClient, seeded_users
) -> None:
    """ADR-0103 §1: cash is unitisable, so its page offers the panel too.

    ``shows_positions_panel`` composes over ``UNITISABLE_TYPES``, so this
    follows from the Task-A change with no web-layer edit — which is the
    assertion's point.
    """
    owner_id, email, pw = seeded_users["owner"]
    await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Cash EUR Four", investment_type="cash")

    page = await web_client.get(f"/investments/{inv_id}")

    assert page.status_code == 200
    assert 'id="positions-table"' in page.text
    assert _is_flip_button_disabled(page.text) is True
    assert "Add an opening transaction" in page.text


@pytest.mark.asyncio
async def test_adding_opening_enables_the_flip_button(
    web_client: AsyncClient, seeded_users
) -> None:
    """The operator's path: enter the opening, the flip becomes available."""
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed X")

    assert (await _add_opening(web_client, csrf, inv_id)).status_code == 200
    page = await web_client.get(f"/investments/{inv_id}")

    assert page.status_code == 200
    assert _is_flip_button_disabled(page.text) is False
    # The blocked-reason paragraph stays in the DOM but is hidden, so an
    # in-place panel refresh can re-surface it without a reload.
    assert _is_flip_reason_hidden(page.text) is True
    assert "Add an opening transaction" not in page.text


@pytest.mark.asyncio
async def test_unitised_detail_page_renders_the_populated_panel(
    web_client: AsyncClient, seeded_users
) -> None:
    """Exercise every populated Jinja branch of the panel, not just the JSON.

    The summary's price / computed-NAV / holdings blocks only render once an
    investment is unitised and priced; without this the badge, the
    ``basis='computed'`` pill, and the holdings formatting would be proven
    only through the JSON payload and could still raise in the template.
    """
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed W")

    await _seed_price(owner_id, inv_id, date(2026, 1, 8), Decimal("13.00"))
    await _add_opening(web_client, csrf, inv_id)
    flip = await web_client.post(
        f"/investments/{inv_id}/valuation-mode/unitised",
        headers={"X-CSRF-Token": csrf},
    )
    assert flip.status_code == 200

    page = await web_client.get(f"/investments/{inv_id}")

    assert page.status_code == 200
    body = page.text
    # Mode badge, and no flip button once unitised (the act is one-way).
    assert "inv-pill inv-pill--unitised" in body
    assert 'id="flip-mode-btn"' not in body
    # Holdings trimmed of trailing zeros, matching the JS re-render.
    assert "100 units as of 2026-01-05" in body
    # Latest price and its provenance.
    assert "13.0000 EUR" in body
    assert "origin: manual" in body
    # Computed NAV: 100 × 13.00, badged with basis and origin side by side.
    assert "1,300.00 EUR" in body
    assert "basis: computed" in body
    assert "origin: system" in body


@pytest.mark.asyncio
async def test_reported_investment_navs_untouched_by_ledger_write(
    web_client: AsyncClient, seeded_users
) -> None:
    """A ledger write on a 'reported' investment triggers no materialisation."""
    owner_id, email, pw = seeded_users["owner"]
    csrf = await _login_and_csrf(web_client, email, pw)
    inv_id = await _seed_investment(owner_id, name="Listed V")

    await _seed_nav(owner_id, inv_id, date(2026, 1, 6), Decimal("9000"), ingest_origin="excel")
    await _seed_price(owner_id, inv_id, date(2026, 1, 8), Decimal("13.00"))
    before = await _read_navs(inv_id)

    assert (await _add_opening(web_client, csrf, inv_id)).status_code == 200

    # Still reported: no computed row appeared despite ledger + price existing.
    assert await _valuation_mode(inv_id) == "reported"
    assert await _read_navs(inv_id) == before
