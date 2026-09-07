# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for History — the terminal list, its detail and the reversal (S5, P-5b).

ASGI-level tests over a live Postgres, on the fixture pattern of
``tests/web/test_transactions_blotter.py``. They cover the four things P-5b
adds and nothing the blotter or the composers already pin:

* **The section** — ``/transactions`` ships the lazy shell where History's
  placeholder sentence used to be, and neither of the Area's two placeholder
  sentences survives anywhere.
* **The list** — ``GET /api/transactions/history`` projects the terminal set,
  newest number first, labelling both endings apart (A-17) and stating the
  booker on a booked row and the reason on a cancelled one (A-16).
* **The filters** — each of the v1 four narrows the list, an unknown value
  behaves as unset, and the investment select offers exactly what can match.
* **The detail and the reversal** — the effects grouped by type, and the
  reversal's three outcomes: the report, the retained shell, and the block.

Unlike the blotter's, most of these tickets are **booked through the
service** rather than seeded through the repository: History's subject is
what a booking *did*, and a hand-seeded ``booked`` row has no effects to
show, undo or refuse to undo.
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
from core.repositories.audit_log_repository import AuditLogRepository
from core.repositories.instrument_price_repository import InstrumentPriceRepository
from core.repositories.investment_cashflow_repository import InvestmentCashflowRepository
from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierRepository,
)
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.position_transaction_repository import (
    PositionTransactionRepository,
)
from core.repositories.trade_ticket_repository import (
    TradeTicketDTO,
    TradeTicketRepository,
)
from core.tenant_constants import SENTINEL_TENANT_ID
from services.investments.investment_service import InvestmentService
from services.password_hashing import hash_password
from services.transactions.constants import (
    DIRECTION_BUY,
    KIND_COMMITMENT,
    KIND_ORDER,
    KIND_SECONDARY,
    MD_CURRENCY,
    STATUS_BOOKED,
    STATUS_CANCELLED,
    STATUS_PROPOSED,
)
from services.transactions.ticket_service import TicketService
from web.main import create_app
from web.routes.transactions import _flow_of
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_TRADE_DATE = _date(2026, 3, 2)
_LATER_DATE = _date(2026, 4, 20)
_OPENING_DATE = _date(2026, 1, 2)
_NOW = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
_TODAY = _date(2026, 3, 2)
_REASON = "booked against the wrong custodian statement"


def _url(value: str | None) -> str:
    """Narrow a configured URL to ``str``; ``_require_db`` already skipped if unset."""
    assert value is not None
    return value


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; skipping live-DB history tests.",
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
    "investment_cashflows, investment_navs, investment_identifiers, "
    "investments, asset_classes, "
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
    email = "history-owner@example.com"
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
async def member_user(seeded_user: tuple[UUID, str, str], superuser_engine: AsyncEngine) -> str:
    """A second user with the ``member`` role — the 403 pin's subject."""
    email = "history-member@example.com"
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, display_name,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, 'M. Ember',
                     ARRAY['member']::text[], TRUE)
                """
            ),
            {
                "id": str(uuid4()),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password("correct-horse-battery-staple"),
            },
        )
    return email


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


def _engine() -> AsyncEngine:
    return create_async_engine(_url(DATABASE_URL), future=True, poolclass=NullPool)


class _World:
    """The seeded world one History test works against.

    The reversal fixture of ``tests/services/transactions`` in miniature: a
    unitised instrument the order flows trade, a cash position they settle
    against, and the asset class a creating flow's payload names.
    """

    def __init__(self, asset_class_id: UUID, instrument_id: UUID, cash_id: UUID) -> None:
        self.asset_class_id = asset_class_id
        self.instrument_id = instrument_id
        self.cash_id = cash_id


async def _seed_world(user_id: UUID) -> _World:
    """Seed an asset class, a unitised instrument with holdings, and cash."""
    engine = _engine()
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code="history_class", display_name="History Class"
            )
            investments = InvestmentRepository(session)
            instrument = await investments.create(
                name="iShares Core MSCI World",
                investment_type="listed_equity",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=user_id,
                anlv_code="anlv_13",
                valuation_mode="unitised",
            )
            cash = await investments.create(
                name="Cash EUR · Main custody",
                investment_type="cash",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=user_id,
                valuation_mode="unitised",
            )
            ledger = PositionTransactionRepository(session)
            for investment_id, units in ((instrument.id, "100"), (cash.id, "1000000")):
                await ledger.add(
                    investment_id=investment_id,
                    txn_type="opening",
                    trade_date=_OPENING_DATE,
                    units=Decimal(units),
                    currency="EUR",
                    ingest_origin="excel",
                    created_by=user_id,
                )
            return _World(asset_class.id, instrument.id, cash.id)
    finally:
        await engine.dispose()


def _service(session: Any) -> TicketService:
    """A fully wired service — the route's ``_build_ticket_service``, in a test."""
    investment_service = InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        identifiers=InvestmentIdentifierRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
    )
    return TicketService(
        tickets=TradeTicketRepository(session),
        investments=InvestmentRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
        investment_service=investment_service,
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        audit_log=AuditLogRepository(session),
    )


def _master_data(world: _World, **overrides: Any) -> dict[str, Any]:
    """A complete creating payload as JSONB carries it."""
    values: dict[str, Any] = {
        "name": "Nordic Infra Fund IV",
        "investment_type": "private_equity",
        "asset_class_id": str(world.asset_class_id),
        "currency": "EUR",
        "anlv_code": "anlv_13",
        "vintage_year": "2024",
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


async def _book(user_id: UUID, **draft_kwargs: Any) -> TradeTicketDTO:
    """Create and book one ticket through the service, as the surface would."""
    engine = _engine()
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            service = _service(session)
            defaults: dict[str, Any] = {
                "kind": KIND_ORDER,
                "direction": DIRECTION_BUY,
                "currency": "EUR",
                "trade_date": _TRADE_DATE,
                "created_by": user_id,
                "now": _NOW,
            }
            draft = await service.create_draft(**{**defaults, **draft_kwargs})
            booked, _warnings = await service.book(
                draft.id, booked_by=user_id, now=_NOW, today=_TODAY
            )
            return booked
    finally:
        await engine.dispose()


async def _order(user_id: UUID, world: _World, **overrides: Any) -> TradeTicketDTO:
    """Book a plain U-BUY against the seeded instrument."""
    values: dict[str, Any] = {
        "investment_id": world.instrument_id,
        "cash_investment_id": world.cash_id,
        "units": Decimal("10"),
        "price_per_unit": Decimal("10.00"),
        "note": "quarterly rebalance",
    }
    values.update(overrides)
    return await _book(user_id, **values)


async def _seed_ticket(
    user_id: UUID, *, status: str | None = None, **fields: Any
) -> TradeTicketDTO:
    """Create one ticket through the repository and walk it to ``status``.

    The blotter test's helper, narrowed: History only ever needs a ticket
    cancelled straight from a station, which is the one walk the attribution
    CHECKs let a seed take without a booking behind it.
    """
    engine = _engine()
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
            for station in (STATUS_PROPOSED,) if status == STATUS_CANCELLED else ():
                ticket = await tickets.set_status(
                    ticket.id, status=station, actor_user_id=user_id, now=_NOW
                )
            if status == STATUS_CANCELLED:
                ticket = await tickets.set_status(
                    ticket.id,
                    status=STATUS_CANCELLED,
                    actor_user_id=user_id,
                    now=_NOW,
                    cancel_reason="IC declined",
                )
            return ticket
    finally:
        await engine.dispose()


async def _ticket_row(ticket_id: UUID) -> tuple[str, str | None, Any]:
    """Read one ticket's status, reason and ``booked_at`` back, outside the app."""
    engine = create_async_engine(_url(DATABASE_URL_SUPERUSER), future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text("SELECT status, cancel_reason, booked_at FROM trade_tickets WHERE id = :id"),
                {"id": str(ticket_id)},
            )
            row = result.one()
            return row[0], row[1], row[2]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# The section shell
# ---------------------------------------------------------------------------


async def test_history_section_ships_the_lazy_shell(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """History carries the lazy shell, and no placeholder sentence survives."""
    _id, email, password = seeded_user
    await _login_and_csrf(web_client, email, password)

    body = (await web_client.get("/transactions", follow_redirects=False)).text

    assert 'hx-get="/api/transactions/history"' in body
    assert "Loading history" in body
    # Both S3 placeholder sentences are gone from the Area — the Blotter's
    # left with P-5a, History's with P-5b.
    assert "Booked and cancelled tickets, filterable, arrive with S5." not in body
    assert "Draft, proposed and approved tickets arrive with S5." not in body


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


async def test_history_lists_the_terminal_set_newest_first(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Booked and cancelled appear, in-flight does not, newest number first."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)

    booked = await _order(user_id, world)
    cancelled = await _seed_ticket(
        user_id, investment_id=world.instrument_id, status=STATUS_CANCELLED
    )
    in_flight = await _seed_ticket(user_id, investment_id=world.instrument_id)

    await _login_and_csrf(web_client, email, password)
    response = await web_client.get("/api/transactions/history")
    assert response.status_code == 200
    body = response.text

    assert f"tx-history-row-{booked.id}" in body
    assert f"tx-history-row-{cancelled.id}" in body
    assert f"tx-history-row-{in_flight.id}" not in body

    positions = [body.index(f"tx-history-row-{t.id}") for t in (cancelled, booked)]
    assert positions == sorted(positions)


async def test_history_labels_both_endings_apart_and_states_the_outcome_line(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A-17's derivation, and A-16's actor rule on the terminal station."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)

    booked = await _order(user_id, world)
    reversed_ticket = await _order(user_id, world, units=Decimal("5"))
    cancelled = await _seed_ticket(
        user_id, investment_id=world.instrument_id, status=STATUS_CANCELLED
    )

    csrf = await _login_and_csrf(web_client, email, password)
    reversal = await web_client.post(
        f"/api/transactions/ticket/{reversed_ticket.id}/reverse",
        data={"reason": "duplicate of #1", "csrf_token": csrf},
    )
    assert reversal.status_code == 200

    body = (await web_client.get("/api/transactions/history")).text

    assert 'class="tx-state tx-state--booked">Booked' in body
    assert 'class="tx-state tx-state--reversed">Reversed' in body
    assert 'class="tx-state tx-state--cancelled">Cancelled' in body

    # A booked row names its booker; a terminal ending names nobody (A-16),
    # because there is no `cancelled_by` column to name them from.
    assert "&#34;duplicate of #1&#34;" in body
    assert "&#34;IC declined&#34;" in body

    def _segment(ticket: TradeTicketDTO) -> str:
        start = body.index(f"tx-history-row-{ticket.id}")
        return body[start : start + 1600]

    assert "by A. Weber · 2026-03-02 09:00" in _segment(booked)
    for ticket in (reversed_ticket, cancelled):
        assert "by A. Weber" not in _segment(ticket), "the terminal station must name no actor"


async def test_empty_history_says_nothing_terminal_yet(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """With nothing terminal the section states the empty case, not the filtered one."""
    user_id, email, password = seeded_user
    await _seed_ticket(user_id)

    await _login_and_csrf(web_client, email, password)
    body = (await web_client.get("/api/transactions/history")).text

    assert "Nothing booked or cancelled yet." in body
    assert "Nothing matches these filters." not in body
    assert "<table" not in body


# ---------------------------------------------------------------------------
# The filter set (A-19)
# ---------------------------------------------------------------------------


async def test_history_status_filter_splits_the_three_outcomes(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """`booked`, `cancelled` and `reversed` each narrow to their own rows."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)

    booked = await _order(user_id, world)
    reversed_ticket = await _order(user_id, world, units=Decimal("5"))
    cancelled = await _seed_ticket(
        user_id, investment_id=world.instrument_id, status=STATUS_CANCELLED
    )

    csrf = await _login_and_csrf(web_client, email, password)
    await web_client.post(
        f"/api/transactions/ticket/{reversed_ticket.id}/reverse",
        data={"reason": "duplicate", "csrf_token": csrf},
    )

    async def _rows(query: str) -> set[UUID]:
        body = (await web_client.get(f"/api/transactions/history{query}")).text
        return {
            t.id for t in (booked, reversed_ticket, cancelled) if f"tx-history-row-{t.id}" in body
        }

    assert await _rows("?status=booked") == {booked.id}
    assert await _rows("?status=cancelled") == {cancelled.id}
    assert await _rows("?status=reversed") == {reversed_ticket.id}
    assert await _rows("?status=all") == {booked.id, reversed_ticket.id, cancelled.id}
    # A filter form never 400s: an unknown value is a question about
    # everything rather than an error worth a page.
    assert await _rows("?status=nonsense") == {booked.id, reversed_ticket.id, cancelled.id}


async def test_history_kind_investment_and_date_filters(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Kind, investment and the inclusive date range each narrow the list."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)

    order = await _order(user_id, world)
    commitment = await _book(
        user_id,
        kind=KIND_COMMITMENT,
        commitment_amount=Decimal("5000000"),
        master_data=_master_data(world),
    )
    later = await _order(user_id, world, units=Decimal("3"), trade_date=_LATER_DATE)

    await _login_and_csrf(web_client, email, password)

    async def _rows(query: str) -> set[UUID]:
        body = (await web_client.get(f"/api/transactions/history{query}")).text
        return {t.id for t in (order, commitment, later) if f"tx-history-row-{t.id}" in body}

    assert await _rows("?kind=order") == {order.id, later.id}
    assert await _rows("?kind=commitment") == {commitment.id}
    assert await _rows("?kind=nonsense") == {order.id, commitment.id, later.id}
    assert await _rows(f"?investment_id={world.instrument_id}") == {order.id, later.id}
    assert await _rows(f"?trade_date_from={_LATER_DATE}") == {later.id}
    assert await _rows(f"?trade_date_to={_TRADE_DATE}") == {order.id, commitment.id}
    assert await _rows(f"?trade_date_from={_TRADE_DATE}&trade_date_to={_TRADE_DATE}") == {
        order.id,
        commitment.id,
    }


async def test_history_investment_select_offers_only_what_can_match(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The select lists investments terminal tickets name, and nothing else."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    engine = _engine()
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            untouched = await InvestmentRepository(session).create(
                name="Never Traded Fund",
                investment_type="listed_equity",
                asset_class_id=world.asset_class_id,
                currency="EUR",
                created_by=user_id,
            )
    finally:
        await engine.dispose()

    await _order(user_id, world)

    await _login_and_csrf(web_client, email, password)
    body = (await web_client.get("/api/transactions/history")).text

    assert f'<option value="{world.instrument_id}"' in body
    assert f'<option value="{untouched.id}"' not in body
    # The cash position settles the booking but is not the traded side, so it
    # is not a history filter (the settlement column is never matched).
    assert f'<option value="{world.cash_id}"' not in body


async def test_history_says_when_filters_match_nothing(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A set filter that matches nothing gets its own sentence."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    await _order(user_id, world)

    await _login_and_csrf(web_client, email, password)
    body = (await web_client.get("/api/transactions/history?kind=secondary")).text

    assert "Nothing matches these filters." in body
    assert "Nothing booked or cancelled yet." not in body


# ---------------------------------------------------------------------------
# `_flow_of` over terminal rows (Phase 2)
# ---------------------------------------------------------------------------


def _dto(**fields: Any) -> TradeTicketDTO:
    """A minimal ticket DTO, for the pure `_flow_of` pins."""
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": SENTINEL_TENANT_ID,
        "ticket_number": 1,
        "kind": KIND_ORDER,
        "direction": DIRECTION_BUY,
        "status": STATUS_BOOKED,
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
        "booked_at": _NOW,
        "cancelled_at": None,
        "created_by": uuid4(),
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return TradeTicketDTO(**{**defaults, **fields})


def test_flow_of_survives_the_booking_that_links_the_investment() -> None:
    """A booked U-NEW is still New instrument; a booked U-BUY is still an order.

    ``link_investment`` writes ``investment_id`` back onto a creating ticket,
    so the live predicate stops answering. The payload is what survives, and
    it is present on an order ticket only when the composer wrote it there.
    """
    assert _flow_of(_dto(investment_id=uuid4(), master_data={MD_CURRENCY: "EUR"})) == (
        "new_instrument"
    )
    assert _flow_of(_dto(investment_id=uuid4(), master_data=None)) == ""


async def test_history_labels_a_booked_new_instrument_as_such(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The Phase-2 pin, end to end: a booked U-NEW is not relabelled an order."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    await _book(
        user_id,
        cash_investment_id=world.cash_id,
        units=Decimal("10"),
        price_per_unit=Decimal("10.00"),
        master_data=_master_data(
            world, name="Vanguard FTSE All-World", investment_type="listed_equity"
        ),
    )

    await _login_and_csrf(web_client, email, password)
    body = (await web_client.get("/api/transactions/history")).text

    assert "New instrument" in body
    assert "Order · Buy" not in body


# ---------------------------------------------------------------------------
# The detail
# ---------------------------------------------------------------------------


async def test_history_detail_states_the_stations_and_groups_the_effects(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Stations with names, the settlement position, and one heading per group."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    booked = await _order(user_id, world)

    await _login_and_csrf(web_client, email, password)
    body = (await web_client.get(f"/api/transactions/history/{booked.id}")).text

    assert "created 2026-03-02 09:00" in body
    assert "proposed by A. Weber 2026-03-02 09:00" in body
    assert "booked by A. Weber 2026-03-02 09:00" in body
    assert "Cash EUR · Main custody" in body
    assert "quarterly rebalance" in body
    assert f"ticket #{booked.ticket_number}" in body
    # An order emits two ledger legs and nothing else, so exactly one group
    # heading appears — the empty three are absent, not empty.
    assert "position transactions" in body
    assert ">cashflows<" not in body
    assert ">NAVs<" not in body
    # A-13 puts both gestures on the list row and neither on the detail.
    assert "Reverse booking" not in body
    assert "csrf_token" not in body


async def test_history_detail_of_a_commitment_says_no_cash_leg(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """MD-19: a commitment moves no cash, and the detail says so rather than "—"."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    commitment = await _book(
        user_id,
        kind=KIND_COMMITMENT,
        commitment_amount=Decimal("5000000"),
        master_data=_master_data(world),
    )

    await _login_and_csrf(web_client, email, password)
    body = (await web_client.get(f"/api/transactions/history/{commitment.id}")).text

    assert "no cash leg" in body
    assert "investment" in body


async def test_history_detail_of_a_reversed_ticket_keeps_its_effect_list(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A-17 / D-AD: the linkage survives, each row reported as gone."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    booked = await _order(user_id, world)

    csrf = await _login_and_csrf(web_client, email, password)
    await web_client.post(
        f"/api/transactions/ticket/{booked.id}/reverse",
        data={"reason": _REASON, "csrf_token": csrf},
    )

    body = (await web_client.get(f"/api/transactions/history/{booked.id}")).text

    assert body.count("This row is no longer in the book.") == 2
    # The stations string ends "reversed", not "cancelled": both endings
    # share the status, and `booked_at` is what tells them apart (A-17). The
    # timestamp is the route's own `_now()`, so only its shape is pinned.
    assert " · reversed 20" in body
    assert " · cancelled 20" not in body


async def test_history_detail_of_a_cancelled_ticket_says_nothing_was_written(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A ticket that never booked emitted nothing, and the detail says so."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    cancelled = await _seed_ticket(
        user_id, investment_id=world.instrument_id, status=STATUS_CANCELLED
    )

    await _login_and_csrf(web_client, email, password)
    body = (await web_client.get(f"/api/transactions/history/{cancelled.id}")).text

    assert "Nothing was written — this ticket was never booked." in body
    assert " · cancelled 2026-03-02 09:00" in body
    assert "tx-leg__type" not in body


async def test_history_detail_refuses_an_in_flight_ticket(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The `_terminal` gate is the mirror of `_in_flight`, and 404s the same way."""
    user_id, email, password = seeded_user
    draft = await _seed_ticket(user_id)

    await _login_and_csrf(web_client, email, password)

    assert (await web_client.get(f"/api/transactions/history/{draft.id}")).status_code == 404
    assert (await web_client.get(f"/api/transactions/history/{uuid4()}")).status_code == 404
    assert (await web_client.get("/api/transactions/history/not-a-uuid")).status_code == 404


# ---------------------------------------------------------------------------
# The reversal
# ---------------------------------------------------------------------------


async def test_reverse_panel_opens_on_a_booked_ticket_only(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A cancelled ticket wrote nothing, so there is no surface for reversing it."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    booked = await _order(user_id, world)
    cancelled = await _seed_ticket(
        user_id, investment_id=world.instrument_id, status=STATUS_CANCELLED
    )

    await _login_and_csrf(web_client, email, password)

    panel = await web_client.get(f"/api/transactions/ticket/{booked.id}/reverse")
    assert panel.status_code == 200
    assert f"Reverse booking #{booked.ticket_number}?" in panel.text
    assert "A reason is always required." in panel.text

    refused = await web_client.get(f"/api/transactions/ticket/{cancelled.id}/reverse")
    assert refused.status_code == 404


async def test_reverse_without_a_reason_is_refused_and_changes_nothing(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The empty reason is the service's refusal (D-X), rendered verbatim."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    booked = await _order(user_id, world)

    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        f"/api/transactions/ticket/{booked.id}/reverse",
        data={"reason": "   ", "csrf_token": csrf},
    )

    assert response.status_code == 200
    assert "requires a" in response.text
    assert "reason" in response.text
    assert f"Reverse booking #{booked.ticket_number}?" in response.text

    status_value, reason, booked_at = await _ticket_row(booked.id)
    assert status_value == STATUS_BOOKED
    assert reason is None
    assert booked_at is not None


async def test_reverse_with_a_reason_reports_and_refreshes_the_list(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A-15: the done block, the per-type tally, and the row now reading Reversed."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    booked = await _order(user_id, world)

    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        f"/api/transactions/ticket/{booked.id}/reverse",
        data={"reason": _REASON, "csrf_token": csrf},
    )

    assert response.status_code == 200
    body = response.text
    assert f"Ticket #{booked.ticket_number} reversed." in body
    assert "2 rows undone; the ticket is" in body
    assert _REASON in body
    # All four types are stated, zeros included.
    assert "position transactions: 2" in body
    assert "cashflows: 0" in body
    assert "NAVs: 0" in body
    assert "investments: 0" in body
    # The report rides on the refreshed list, so the row it was about is in
    # the same response and already reads Reversed.
    assert 'id="tx-history"' in body
    assert f"tx-history-row-{booked.id}" in body
    assert 'class="tx-state tx-state--reversed">Reversed' in body
    # No consequence block: an order creates no shell to retain.
    assert "Investment retained, inactive" not in body

    status_value, reason, booked_at = await _ticket_row(booked.id)
    assert status_value == STATUS_CANCELLED
    assert reason == _REASON
    assert booked_at is not None, "booked_at is what tells reversed from cancelled (A-17)"


async def test_reverse_of_a_commitment_deletes_the_shell_without_a_consequence(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """D-AC: nothing but the shell is there, so the shell goes and nothing is said."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    commitment = await _book(
        user_id,
        kind=KIND_COMMITMENT,
        commitment_amount=Decimal("5000000"),
        master_data=_master_data(world),
    )

    csrf = await _login_and_csrf(web_client, email, password)
    body = (
        await web_client.post(
            f"/api/transactions/ticket/{commitment.id}/reverse",
            data={"reason": _REASON, "csrf_token": csrf},
        )
    ).text

    assert f"Ticket #{commitment.ticket_number} reversed." in body
    assert "1 row undone; the ticket is" in body
    assert "investments: 1" in body
    assert "Investment retained, inactive" not in body

    engine = _engine()
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            assert await InvestmentRepository(session).get_by_name("Nordic Infra Fund IV") is None
    finally:
        await engine.dispose()


async def test_reverse_reports_a_retained_shell_verbatim(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A-4 / A-5: a human's row keeps the shell, and the reason is quoted as given."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    created = await _book(
        user_id,
        kind=KIND_SECONDARY,
        direction=DIRECTION_BUY,
        cash_investment_id=world.cash_id,
        gross_amount=Decimal("750000"),
        fees=Decimal("5000"),
        master_data=_master_data(world, acquired_nav="800000", assumed_unfunded="250000"),
    )

    engine = _engine()
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            effects = await TradeTicketRepository(session).list_effects(created.id)
            shell_id = next(
                effect.effect_id
                for effect in effects
                if effect.effect_type == "investment_update" and effect.prior_state is None
            )
            # An operator adds a statement NAV of their own after the booking.
            await InvestmentNavRepository(session).upsert(
                investment_id=shell_id,
                as_of_date=_LATER_DATE,
                nav_kind="actual",
                nav_value=Decimal("820000"),
                currency="EUR",
                source="Q3 statement",
                created_by=user_id,
            )
    finally:
        await engine.dispose()

    csrf = await _login_and_csrf(web_client, email, password)
    body = (
        await web_client.post(
            f"/api/transactions/ticket/{created.id}/reverse",
            data={"reason": _REASON, "csrf_token": csrf},
        )
    ).text

    assert "Investment retained, inactive — because:" in body
    assert "investment_navs" in body

    engine = _engine()
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            shell = await InvestmentRepository(session).get_by_id(shell_id)
    finally:
        await engine.dispose()
    assert shell is not None and shell.is_active is False


async def test_reverse_blocked_by_a_modified_row_rolls_back_and_names_the_cause(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A-14 / the TV-03 recipe: the service sentence verbatim, and nothing undone.

    The rollback half is the point. ``TicketReversalBlocked`` is caught
    *outside* the ``tenant_context`` block so the transaction unwinds; were it
    caught inside, this ticket would come back ``cancelled`` with its ledger
    half-deleted and a red block claiming nothing had happened.
    """
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    booked = await _order(user_id, world)

    engine = _engine()
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            ledger = PositionTransactionRepository(session)
            rows = await ledger.list_for_investment(world.instrument_id)
            emitted = next(row for row in rows if row.txn_type == "buy")
            await InvestmentService(
                investments=InvestmentRepository(session),
                navs=InvestmentNavRepository(session),
                cashflows=InvestmentCashflowRepository(session),
                identifiers=InvestmentIdentifierRepository(session),
                position_transactions=ledger,
                instrument_prices=InstrumentPriceRepository(session),
            ).update_position_transaction(
                investment_id=world.instrument_id,
                transaction_id=emitted.id,
                trade_date=_TRADE_DATE,
                units=Decimal("5"),
                price_per_unit=Decimal("10.00"),
                consideration=Decimal("50.00"),
                acting_user=user_id,
            )
    finally:
        await engine.dispose()

    csrf = await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        f"/api/transactions/ticket/{booked.id}/reverse",
        data={"reason": _REASON, "csrf_token": csrf},
    )

    assert response.status_code == 200
    body = response.text
    assert "cause=modified" in body
    assert "has been edited in position_transactions since" in body
    assert f"Reverse booking #{booked.ticket_number}?" in body

    # Nothing was undone, and the ticket is exactly where it was.
    status_value, reason, _booked_at = await _ticket_row(booked.id)
    assert status_value == STATUS_BOOKED
    assert reason is None

    engine = _engine()
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
            instrument_rows = await PositionTransactionRepository(session).list_for_investment(
                world.instrument_id
            )
            cash_rows = await PositionTransactionRepository(session).list_for_investment(
                world.cash_id
            )
    finally:
        await engine.dispose()
    assert sorted(row.txn_type for row in instrument_rows) == ["buy", "opening"]
    assert sorted(row.txn_type for row in cash_rows) == ["opening", "sell"]


async def test_reverse_refuses_a_non_owner(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    member_user: str,
) -> None:
    """The write is owner-gated; a member gets 403 and the booking stands."""
    user_id, _email, password = seeded_user
    world = await _seed_world(user_id)
    booked = await _order(user_id, world)

    csrf = await _login_and_csrf(web_client, member_user, password)
    response = await web_client.post(
        f"/api/transactions/ticket/{booked.id}/reverse",
        data={"reason": _REASON, "csrf_token": csrf},
    )

    assert response.status_code == 403
    assert (await _ticket_row(booked.id))[0] == STATUS_BOOKED


async def test_reverse_without_csrf_is_refused(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """No token, no write."""
    user_id, email, password = seeded_user
    world = await _seed_world(user_id)
    booked = await _order(user_id, world)

    await _login_and_csrf(web_client, email, password)
    response = await web_client.post(
        f"/api/transactions/ticket/{booked.id}/reverse",
        data={"reason": _REASON},
    )

    assert response.status_code == 403
    assert (await _ticket_row(booked.id))[0] == STATUS_BOOKED
