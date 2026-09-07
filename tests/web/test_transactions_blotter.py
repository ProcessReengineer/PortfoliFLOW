# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Blotter — the in-flight list and its two gestures (S5, P-5a).

ASGI-level tests over a live Postgres, on the fixture pattern of
``tests/web/test_transactions_composer.py``. They cover the three things
P-5a adds and nothing the composers already pin:

* **The section** — ``/transactions`` ships the lazy shell where the Blotter's
  placeholder sentence used to be, and History's sentence is untouched.
* **The list** — ``GET /api/transactions/blotter`` projects the cancellable
  set, newest number first, with the flow label, the creating name, the
  station line and the amount fallback.
* **The reverse lookup** — :func:`_flow_of` maps each of the five flows back
  onto ``_FLOWS`` and refuses anything else.
* **The resume GET** — one address opens all five composers (A-12), and only
  for a ticket that is still in flight.
* **Cancel** — the reason rule is the service's, the refusal is the service's
  sentence (A-7), and success is answered with the whole list.

Tickets are seeded through the repository rather than through the composer
gestures: the blotter's subject is a ticket in a *status*, and
``set_status`` is the mechanism that puts one there without a surface having
to be able to reach it (an ``approved`` ticket has no gesture in v1).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import date as _date, datetime, timezone
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

from core.repositories._session import tenant_context
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.trade_ticket_repository import (
    TradeTicketDTO,
    TradeTicketRepository,
)
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password
from services.transactions.constants import (
    DIRECTION_BUY,
    DIRECTION_SELL,
    KIND_COMMITMENT,
    KIND_ORDER,
    KIND_SECONDARY,
    MD_ANLV_CODE,
    MD_ASSET_CLASS_ID,
    MD_CURRENCY,
    MD_INVESTMENT_TYPE,
    MD_NAME,
    STATUS_APPROVED,
    STATUS_BOOKED,
    STATUS_CANCELLED,
    STATUS_PROPOSED,
)
from web.main import create_app
from web.routes.transactions import _flow_of
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_TRADE_DATE = _date(2026, 3, 2)
_NOW = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
_OTHER_TENANT_ID = UUID("11111111-2222-3333-4444-555555555555")


def _url(value: str | None) -> str:
    """Narrow a configured URL to ``str``; ``_require_db`` already skipped if unset."""
    assert value is not None
    return value


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; skipping live-DB blotter tests.",
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
    email = "blotter-owner@example.com"
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
                "dn": "A. Weber",
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


async def _seed_investment(user_id: UUID, *, name: str, currency: str = "EUR") -> UUID:
    """Create one investment (with its own asset class)."""
    engine = create_async_engine(_url(DATABASE_URL), future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code=f"ac-{uuid4().hex[:8]}",
                display_name=f"AC {name}",
            )
            investment = await InvestmentRepository(session).create(
                name=name,
                investment_type="listed_equity",
                asset_class_id=asset_class.id,
                currency=currency,
                created_by=user_id,
            )
            return investment.id
    finally:
        await engine.dispose()


def _payload(name: str | None = None, *, complete: bool = False) -> dict[str, Any]:
    """A creating flow's master-data payload, as the composers write it."""
    payload: dict[str, Any] = {MD_CURRENCY: "EUR"}
    if name is not None:
        payload[MD_NAME] = name
    if complete:
        payload |= {
            MD_INVESTMENT_TYPE: "listed_equity",
            MD_ASSET_CLASS_ID: str(uuid4()),
            MD_ANLV_CODE: "1.1",
        }
    return payload


#: The stations a ticket passes on the way to each seedable status.
#:
#: A station's attribution columns stay required for every later status
#: (``ck_trade_tickets_proposed_attribution`` and its two siblings), so a
#: booked ticket cannot lose its proposer — which means a seed cannot skip
#: one either. Cancellation is the exception the schema also makes: it needs
#: only its own timestamp, because a draft is cancelled straight from draft.
_STATION_PATH: dict[str, tuple[str, ...]] = {
    STATUS_PROPOSED: (STATUS_PROPOSED,),
    STATUS_APPROVED: (STATUS_PROPOSED, STATUS_APPROVED),
    STATUS_BOOKED: (STATUS_PROPOSED, STATUS_APPROVED, STATUS_BOOKED),
    STATUS_CANCELLED: (STATUS_CANCELLED,),
}


async def _seed_ticket(
    user_id: UUID,
    *,
    status: str | None = None,
    **fields: Any,
) -> TradeTicketDTO:
    """Create one ticket and, optionally, walk it to ``status``.

    ``set_status`` is mechanism rather than policy (ADR-0128 §3), which is
    what lets a test seed an ``approved`` ticket — a station no v1 gesture
    reaches — without pretending a surface could have produced it. The walk
    is not decoration: the attribution CHECKs refuse a row that arrived at a
    station without passing the ones before it.
    """
    engine = create_async_engine(_url(DATABASE_URL), future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            tickets = TradeTicketRepository(session)
            defaults: dict[str, Any] = {
                "kind": KIND_ORDER,
                "direction": DIRECTION_BUY,
                "currency": "EUR",
                "trade_date": _TRADE_DATE,
            }
            ticket = await tickets.create_draft(
                created_by=user_id, now=_NOW, **{**defaults, **fields}
            )
            for station in _STATION_PATH.get(status, ()) if status else ():
                ticket = await tickets.set_status(
                    ticket.id, status=station, actor_user_id=user_id, now=_NOW
                )
            return ticket
    finally:
        await engine.dispose()


async def _ticket_status(ticket_id: UUID) -> tuple[str, str | None]:
    """Read one ticket's status and cancel reason back, outside the app."""
    engine = create_async_engine(_url(DATABASE_URL_SUPERUSER), future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text("SELECT status, cancel_reason FROM trade_tickets WHERE id = :id"),
                {"id": str(ticket_id)},
            )
            row = result.one()
            return row[0], row[1]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# The section shell
# ---------------------------------------------------------------------------


async def test_blotter_section_ships_the_lazy_shell(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The Blotter section carries the lazy shell; History keeps its sentence."""
    _id, email, password = seeded_user
    await _login_and_csrf(web_client, email, password)

    body = (await web_client.get("/transactions", follow_redirects=False)).text

    assert 'hx-get="/api/transactions/blotter"' in body
    assert "Loading blotter" in body
    assert "Draft, proposed and approved tickets arrive with S5." not in body
    # History is untouched by P-5a, verbatim.
    assert "Booked and cancelled tickets, filterable, arrive with S5." in body


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


async def test_blotter_lists_the_cancellable_set_newest_first(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Draft, proposed and approved appear; booked and cancelled do not."""
    user_id, email, password = seeded_user
    investment_id = await _seed_investment(user_id, name="iShares Core MSCI World")

    draft = await _seed_ticket(user_id, investment_id=investment_id)
    proposed = await _seed_ticket(user_id, investment_id=investment_id, status=STATUS_PROPOSED)
    approved = await _seed_ticket(user_id, investment_id=investment_id, status=STATUS_APPROVED)
    booked = await _seed_ticket(user_id, investment_id=investment_id, status=STATUS_BOOKED)
    cancelled = await _seed_ticket(user_id, investment_id=investment_id, status=STATUS_CANCELLED)

    await _login_and_csrf(web_client, email, password)
    response = await web_client.get("/api/transactions/blotter")
    assert response.status_code == 200
    body = response.text

    for ticket in (draft, proposed, approved):
        assert f"#{ticket.ticket_number}" in body
    for ticket in (booked, cancelled):
        assert f"tx-row-{ticket.id}" not in body

    # `list_by_status` orders by ticket_number DESC, and the projection keeps it.
    positions = [body.index(f"tx-row-{t.id}") for t in (approved, proposed, draft)]
    assert positions == sorted(positions)


async def test_blotter_shows_every_flow_label_and_the_creating_name(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Each of the five flows renders its own label; a creating draft its name."""
    user_id, email, password = seeded_user
    investment_id = await _seed_investment(user_id, name="Alpine PE Fund II")

    await _seed_ticket(user_id, investment_id=investment_id, direction=DIRECTION_BUY)
    await _seed_ticket(user_id, investment_id=investment_id, direction=DIRECTION_SELL)
    await _seed_ticket(user_id, master_data=_payload("Vanguard FTSE All-World"))
    await _seed_ticket(
        user_id,
        kind=KIND_COMMITMENT,
        commitment_amount=Decimal("5000000"),
        master_data=_payload("Nordic Infra Fund IV"),
    )
    await _seed_ticket(user_id, kind=KIND_SECONDARY, direction=DIRECTION_BUY)
    await _seed_ticket(
        user_id, kind=KIND_SECONDARY, direction=DIRECTION_SELL, investment_id=investment_id
    )

    await _login_and_csrf(web_client, email, password)
    body = (await web_client.get("/api/transactions/blotter")).text

    for label in (
        "Order · Buy",
        "Order · Sell",
        "New instrument",
        "Commitment",
        "Secondary purchase",
        "Secondary sale",
    ):
        assert label in body, f"missing flow label {label!r}"

    # MD-12: the creating flow has no investments row yet, so the name it
    # carries on the ticket is the only name there is.
    assert "creating: <em>Vanguard FTSE All-World</em>" in body
    assert "Alpine PE Fund II" in body


async def test_blotter_row_details_amount_units_and_station(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The amount, its "—" fallback, the units sub-line and the station line."""
    user_id, email, password = seeded_user
    investment_id = await _seed_investment(user_id, name="iShares Core MSCI World")

    await _seed_ticket(
        user_id,
        investment_id=investment_id,
        units=Decimal("1200"),
        price_per_unit=Decimal("98.40"),
        net_amount=Decimal("118080"),
        status=STATUS_PROPOSED,
    )
    # A commitment states no net amount and moves no cash (MD-19), so the
    # figure that means something is the commitment itself.
    await _seed_ticket(
        user_id,
        kind=KIND_COMMITMENT,
        commitment_amount=Decimal("5000000"),
        master_data=_payload("Nordic Infra Fund IV"),
    )
    # A dangling draft (MD-11) has no figure at all.
    await _seed_ticket(user_id, investment_id=investment_id)

    await _login_and_csrf(web_client, email, password)
    body = (await web_client.get("/api/transactions/blotter")).text

    assert "118,080.00 EUR" in body
    assert "5,000,000.00 EUR" in body
    assert "1,200.0000 units @ 98.4000" in body
    assert "&mdash;" in body
    # A-16: the station line resolves the actor to a display name.
    assert "by A. Weber · 2026-03-02" in body
    assert "2026-03-02" in body


async def test_empty_blotter_says_nothing_in_flight(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """With no in-flight ticket the section states the empty case."""
    user_id, email, password = seeded_user
    await _seed_ticket(user_id, status=STATUS_BOOKED)

    await _login_and_csrf(web_client, email, password)
    body = (await web_client.get("/api/transactions/blotter")).text

    assert "Nothing in flight." in body
    assert "<table" not in body


# ---------------------------------------------------------------------------
# The reverse lookup
# ---------------------------------------------------------------------------


def _dto(**fields: Any) -> TradeTicketDTO:
    """A minimal ticket DTO, for the pure `_flow_of` pins."""
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": SENTINEL_TENANT_ID,
        "ticket_number": 1,
        "kind": KIND_ORDER,
        "direction": DIRECTION_BUY,
        "status": "draft",
        "investment_id": None,
        "cash_investment_id": None,
        "trade_date": _TRADE_DATE,
        "settlement_date": None,
        "units": None,
        "price_per_unit": None,
        "gross_amount": None,
        "fees": None,
        "taxes": None,
        "net_amount": None,
        "currency": "EUR",
        "commitment_amount": None,
        "master_data": None,
        "set_inactive": False,
        "note": None,
        "source": None,
        "cancel_reason": None,
        "case_id": None,
        "proposed_by": None,
        "proposed_at": None,
        "approved_by": None,
        "approved_at": None,
        "booked_by": None,
        "booked_at": None,
        "cancelled_at": None,
        "created_by": uuid4(),
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return TradeTicketDTO(**{**defaults, **fields})


def test_flow_of_maps_each_flow_and_refuses_the_rest() -> None:
    """The five mappings, and a ValueError for a ticket that is no flow."""
    assert _flow_of(_dto(investment_id=uuid4())) == ""
    assert _flow_of(_dto(master_data={MD_CURRENCY: "EUR"})) == "new_instrument"
    assert _flow_of(_dto(kind=KIND_COMMITMENT)) == "commitment"
    assert _flow_of(_dto(kind=KIND_SECONDARY, direction=DIRECTION_BUY)) == "secondary_buy"
    assert (
        _flow_of(_dto(kind=KIND_SECONDARY, direction=DIRECTION_SELL, investment_id=uuid4()))
        == "secondary_sale"
    )

    # A row that fits no flow is a corrupted book, not a case for a fallback.
    with pytest.raises(ValueError, match="no\\s+composer flow"):
        _flow_of(_dto(kind="dividend_reinvestment"))


# ---------------------------------------------------------------------------
# The resume GET (A-12)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "marker"),
    [
        pytest.param({}, 'id="tx-order-form"', id="order"),
        pytest.param(
            {"kind": KIND_SECONDARY, "direction": DIRECTION_SELL},
            'id="tx-secsell-form"',
            id="secondary-sale",
        ),
        pytest.param(
            {"kind": KIND_COMMITMENT, "master_data": _payload("Nordic Infra Fund IV")},
            'id="tx-commit-form"',
            id="commitment",
        ),
        pytest.param(
            {"kind": KIND_SECONDARY, "direction": DIRECTION_BUY},
            'id="tx-secbuy-form"',
            id="secondary-purchase",
        ),
    ],
)
async def test_resume_opens_each_single_page_composer(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fields: dict[str, Any],
    marker: str,
) -> None:
    """One address, four composers — the row does not know which answers it."""
    user_id, email, password = seeded_user
    if fields.get("kind") != KIND_COMMITMENT and fields.get("direction") != DIRECTION_BUY:
        fields = {
            **fields,
            "investment_id": await _seed_investment(user_id, name="Alpine PE Fund II"),
        }
    ticket = await _seed_ticket(user_id, **fields)

    await _login_and_csrf(web_client, email, password)
    response = await web_client.get(f"/api/transactions/ticket/{ticket.id}")

    assert response.status_code == 200
    assert marker in response.text


async def test_resume_opens_the_wizard_at_its_resume_step(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A U-NEW draft reopens as the wizard, where its own content puts it (MD-10)."""
    user_id, email, password = seeded_user
    # Currency and the D-J columns are set, AnlV too: `_resume_step` puts a
    # draft with no units and no settlement position on step 3.
    ticket = await _seed_ticket(
        user_id, master_data=_payload("Vanguard FTSE All-World", complete=True)
    )

    await _login_and_csrf(web_client, email, password)
    response = await web_client.get(f"/api/transactions/ticket/{ticket.id}")

    assert response.status_code == 200
    assert 'id="tx-wizard-form"' in response.text
    # The same body the wizard's own address returns for this ticket.
    direct = await web_client.get(f"/api/transactions/wizard?ticket_id={ticket.id}")
    assert direct.status_code == 200
    assert direct.text == response.text


@pytest.mark.parametrize("status_value", [STATUS_BOOKED, STATUS_CANCELLED])
async def test_resume_refuses_a_terminal_ticket(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    status_value: str,
) -> None:
    """Booked and cancelled tickets are History's; the composer never opens them."""
    user_id, email, password = seeded_user
    ticket = await _seed_ticket(user_id, status=status_value)

    await _login_and_csrf(web_client, email, password)
    response = await web_client.get(f"/api/transactions/ticket/{ticket.id}")

    assert response.status_code == 404


@pytest.mark.parametrize("ticket_id", ["not-a-uuid", str(uuid4())])
async def test_resume_refuses_an_unknown_id(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    ticket_id: str,
) -> None:
    """A malformed id and an absent one are the same answer: 404."""
    _id, email, password = seeded_user
    await _login_and_csrf(web_client, email, password)

    response = await web_client.get(f"/api/transactions/ticket/{ticket_id}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Cancel (A-13)
# ---------------------------------------------------------------------------


async def test_discarding_a_draft_needs_no_reason_and_returns_the_list(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A draft is private workspace: no reason, and the row is gone from the answer."""
    user_id, email, password = seeded_user
    investment_id = await _seed_investment(user_id, name="Alpine PE Fund II")
    draft = await _seed_ticket(user_id, investment_id=investment_id)
    survivor = await _seed_ticket(user_id, investment_id=investment_id)

    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        f"/api/transactions/ticket/{draft.id}/cancel",
        data={"reason": "", "csrf_token": csrf},
    )

    assert response.status_code == 200
    assert 'id="tx-blotter"' in response.text
    assert f"tx-row-{draft.id}" not in response.text
    assert f"tx-row-{survivor.id}" in response.text
    assert await _ticket_status(draft.id) == ("cancelled", None)


async def test_cancelling_a_proposed_ticket_without_a_reason_is_refused(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The service's own sentence comes back in the panel; the ticket stands (A-7)."""
    user_id, email, password = seeded_user
    ticket = await _seed_ticket(user_id, status=STATUS_PROPOSED)

    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        f"/api/transactions/ticket/{ticket.id}/cancel",
        data={"reason": "  ", "csrf_token": csrf},
    )

    assert response.status_code == 200
    assert "requires a reason" in response.text
    assert "tx-msg--block" in response.text
    # The panel came back, not the list.
    assert 'id="tx-blotter"' not in response.text
    assert await _ticket_status(ticket.id) == ("proposed", None)


async def test_cancelling_a_proposed_ticket_with_a_reason_stores_it(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A proposal is a decision others may have seen, so withdrawing it is explained."""
    user_id, email, password = seeded_user
    ticket = await _seed_ticket(user_id, status=STATUS_PROPOSED)

    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        f"/api/transactions/ticket/{ticket.id}/cancel",
        data={"reason": "Manager pulled the allocation.", "csrf_token": csrf},
    )

    assert response.status_code == 200
    assert 'id="tx-blotter"' in response.text
    assert await _ticket_status(ticket.id) == ("cancelled", "Manager pulled the allocation.")


async def test_cancel_panel_states_the_reason_rule_per_status(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The M-5 copy: a draft is discarded, a proposal is cancelled."""
    user_id, email, password = seeded_user
    draft = await _seed_ticket(user_id)
    proposed = await _seed_ticket(user_id, status=STATUS_PROPOSED)

    await _login_and_csrf(web_client, email, password)

    draft_panel = await web_client.get(f"/api/transactions/ticket/{draft.id}/cancel")
    assert draft_panel.status_code == 200
    assert f"Discard draft #{draft.ticket_number}?" in draft_panel.text
    assert "A draft is private workspace. A reason is optional." in draft_panel.text
    assert "required" not in draft_panel.text.split("A draft is private")[0]

    proposed_panel = await web_client.get(f"/api/transactions/ticket/{proposed.id}/cancel")
    assert proposed_panel.status_code == 200
    assert f"Cancel ticket #{proposed.ticket_number}?" in proposed_panel.text
    assert "A proposed" in proposed_panel.text
    assert "A reason is required." in proposed_panel.text


async def test_cancel_refuses_a_non_owner(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    superuser_engine: AsyncEngine,
) -> None:
    """Cancellation is a mutation, and mutations are owner-gated."""
    user_id, email, password = seeded_user
    ticket = await _seed_ticket(user_id)
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET roles = ARRAY['member']::text[] WHERE id = :id"),
            {"id": str(user_id)},
        )

    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        f"/api/transactions/ticket/{ticket.id}/cancel",
        data={"reason": "", "csrf_token": csrf},
    )

    assert response.status_code == 403
    assert (await _ticket_status(ticket.id))[0] == "draft"


async def test_cancel_without_csrf_is_refused(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The existing CSRF refusal covers the new mutation too."""
    user_id, email, password = seeded_user
    ticket = await _seed_ticket(user_id)

    await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        f"/api/transactions/ticket/{ticket.id}/cancel", data={"reason": ""}
    )

    assert response.status_code == 403
    assert (await _ticket_status(ticket.id))[0] == "draft"
