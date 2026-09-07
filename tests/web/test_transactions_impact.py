# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the pre-trade impact panel (S6 P-6a, ADR-0128 Q-3).

ASGI-level tests over a live Postgres, on the fixture pattern of
``tests/web/test_transactions_blotter.py``, with a plan world seeded the way
``tests/web/test_planning_desk.py`` seeds one — the panel needs both: a ticket
*and* a book with a seam, a cash path and two limit families.

What they pin, and why each is worth a live-DB test rather than a pure one:

* **Nothing is written.** The panel is a ``GET`` over the overlay, and the
  overlay is ephemeral by contract (ADR-0104 §1). The row counts before and
  after are the assertion that says so — a regression that started persisting
  a snapshot would otherwise pass every other test in this file.
* **The sign, through the endpoint.** ``test_impact.py`` pins it against the
  executor; here it is pinned against the *rendered* panel, so a route that
  swapped before and after would not slip past.
* **The scope states and the book error**, which exist only as rendered copy.
* **The blotter row's slot**, renamed from ``#tx-cancel-{id}`` to
  ``#tx-detail-{id}`` (D-6b) now that two panels share it.
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
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.limits_repository import LimitsRepository
from core.repositories.trade_ticket_repository import (
    TradeTicketDTO,
    TradeTicketRepository,
)
from core.tenant_constants import SENTINEL_TENANT_ID
from services.investments.cash_plan_materialisation import CASH_PLAN_SOURCE
from services.password_hashing import hash_password
from services.transactions.constants import (
    DIRECTION_BUY,
    DIRECTION_SELL,
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
    STATUS_DRAFT,
    STATUS_PROPOSED,
)
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

#: The seeded book's seam — its only actual statement date.
T0 = _date(2026, 3, 31)

#: A trade date comfortably inside the plan horizon.
_TRADE_DATE = _date(2026, 6, 30)

_NOW = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)


def _url(value: str | None) -> str:
    assert value is not None
    return value


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB impact-panel tests.",
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
    "case_entries, cases, limits, limit_sets, "
    "position_transactions, instrument_prices, fx_rates, "
    "investment_cashflows, investment_navs, investments, asset_classes, "
    "login_audit, sessions, audit_log, data_store_entries, users, tenants "
    "RESTART IDENTITY CASCADE"
)

#: The tables a booking would touch. Counted before and after every panel
#: request in :func:`test_the_panel_writes_nothing`.
_EPHEMERAL_TABLES: tuple[str, ...] = (
    "trade_tickets",
    "trade_ticket_effects",
    "position_transactions",
    "investment_navs",
    "investment_cashflows",
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
    email = "impact-owner@example.com"
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


async def _login(client: AsyncClient, email: str, password: str) -> None:
    """Seat a session cookie. The panel is a ``GET``, so no CSRF token is needed."""
    get_response = await client.get("/login")
    pre_csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert pre_csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": pre_csrf},
        follow_redirects=False,
    )


async def _seed_plan_world(actor_id: UUID) -> tuple[UUID, UUID]:
    """Seed a one-currency book with a plan cash path and both limit families.

    ``test_planning_desk._seed_book`` plus its ``_seed_limit_sets``, reduced to
    what the panel reads:

    * ``Cash EUR`` — the explicit cash position (ADR-0100). Its **actual** NAV
      at ``T0`` is the seam *and* the anchor of the plan cash path; a
      materialised plan row (``source='computed:cash-plan'``, ADR-0103 §6)
      steps the balance to 800 at 2026-09-30.
    * ``Equity Fund`` — the listed holding a hypothetical trade lands on.
    * one SAA set on ``pd_class`` and one AnlV set on ``listed_equity``, so
      the coverage engine has a set in force at every plan date and both
      lenses score rather than degrading to a notice.

    Returns:
        ``(equity_id, cash_id)``.
    """
    engine = create_async_engine(_url(DATABASE_URL), future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code="pd_class", display_name="Planning Desk Class"
            )
            investments = InvestmentRepository(session)
            navs = InvestmentNavRepository(session)

            cash = await investments.create(
                name="Cash EUR",
                investment_type="cash",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=actor_id,
                vintage_year=None,
            )
            await navs.upsert(
                investment_id=cash.id,
                as_of_date=T0,
                nav_kind="actual",
                nav_value=Decimal("1000"),
                currency="EUR",
                source=None,
                created_by=actor_id,
            )
            await navs.upsert(
                investment_id=cash.id,
                as_of_date=_date(2026, 9, 30),
                nav_kind="plan",
                nav_value=Decimal("800"),
                currency="EUR",
                source=CASH_PLAN_SOURCE,
                created_by=actor_id,
            )

            equity = await investments.create(
                name="Equity Fund",
                investment_type="listed_equity",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=actor_id,
            )
            await navs.upsert(
                investment_id=equity.id,
                as_of_date=T0,
                nav_kind="actual",
                nav_value=Decimal("5000"),
                currency="EUR",
                source=None,
                created_by=actor_id,
            )

            limits = LimitsRepository(session)
            await limits.create_set_with_limits(
                family="saa",
                effective_from=_date(2020, 1, 1),
                label="SAA base",
                notes=None,
                limits={"pd_class": Decimal("80.0")},
                created_by=actor_id,
            )
            await limits.create_set_with_limits(
                family="anlv",
                effective_from=_date(2020, 1, 1),
                label="AnlV base",
                notes=None,
                limits={"listed_equity": Decimal("35.0")},
                created_by=actor_id,
            )
            return equity.id, cash.id
    finally:
        await engine.dispose()


async def _seed_second_cash_position(actor_id: UUID) -> None:
    """A second active EUR cash position — the ``DuplicateCashPositionError`` book."""
    engine = create_async_engine(_url(DATABASE_URL), future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code="pd_class_2", display_name="Second Cash Class"
            )
            investment = await InvestmentRepository(session).create(
                name="Cash EUR (Treasury)",
                investment_type="cash",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=actor_id,
                vintage_year=None,
            )
            await InvestmentNavRepository(session).upsert(
                investment_id=investment.id,
                as_of_date=T0,
                nav_kind="actual",
                nav_value=Decimal("50"),
                currency="EUR",
                source=None,
                created_by=actor_id,
            )
    finally:
        await engine.dispose()


#: The stations a ticket passes on the way to each seedable status
#: (``test_transactions_blotter._STATION_PATH``).
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
    """Create one ticket and, optionally, walk it to ``status``."""
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


async def _seed_order(
    user_id: UUID,
    *,
    investment_id: UUID,
    cash_investment_id: UUID,
    status: str = STATUS_PROPOSED,
    **fields: Any,
) -> TradeTicketDTO:
    """An order ticket complete enough to map onto an ``insert_transaction``."""
    defaults: dict[str, Any] = {
        "units": Decimal("10"),
        "price_per_unit": Decimal("20"),
    }
    return await _seed_ticket(
        user_id,
        status=status,
        investment_id=investment_id,
        cash_investment_id=cash_investment_id,
        **{**defaults, **fields},
    )


def _impact_url(ticket: TradeTicketDTO) -> str:
    return f"/api/transactions/ticket/{ticket.id}/impact"


async def _row_counts() -> dict[str, int]:
    """Count every table a booking would touch, outside the app."""
    engine = create_async_engine(_url(DATABASE_URL_SUPERUSER), future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            counts: dict[str, int] = {}
            for table in _EPHEMERAL_TABLES:
                result = await conn.execute(text(f"SELECT count(*) FROM {table}"))
                counts[table] = int(result.scalar_one())
            return counts
    finally:
        await engine.dispose()


def _after_value(body: str, *, label: str) -> str:
    """Return the ``tx-delta__after`` figure of the delta row carrying ``label``.

    The panel states each figure as a struck-through baseline followed by the
    scenario value, so a test that only asserted a number's presence could not
    tell the two apart — which is exactly the mistake the sign anchor exists
    to catch.
    """
    start = body.index(label)
    marker = '<span class="tx-delta__after">'
    at = body.index(marker, start) + len(marker)
    return body[at : body.index("</span>", at)].strip()


# ---------------------------------------------------------------------------
# The invariant: nothing is written
# ---------------------------------------------------------------------------


async def test_the_panel_writes_nothing(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The overlay is ephemeral (ADR-0104 §1) and the panel keeps it that way."""
    actor_id, email, password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    ticket = await _seed_order(actor_id, investment_id=equity_id, cash_investment_id=cash_id)
    await _login(web_client, email, password)

    before = await _row_counts()
    response = await web_client.get(_impact_url(ticket))
    after = await _row_counts()

    assert response.status_code == 200
    assert "Impact of #" in response.text
    assert before == after


# ---------------------------------------------------------------------------
# The sign, through the endpoint
# ---------------------------------------------------------------------------


async def test_a_buy_lowers_the_cash_balance_on_the_trade_date(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Cash 1,000 at the seam, less a 200 buy, is 800 on the trade date."""
    actor_id, email, password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    ticket = await _seed_order(actor_id, investment_id=equity_id, cash_investment_id=cash_id)
    await _login(web_client, email, password)

    body = (await web_client.get(_impact_url(ticket))).text

    assert _after_value(body, label="On trade date") == "800.00"


async def test_a_sell_raises_the_cash_balance_on_the_trade_date(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The mirror — and the one assertion an inverted mapping would fail."""
    actor_id, email, password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    ticket = await _seed_order(
        actor_id,
        investment_id=equity_id,
        cash_investment_id=cash_id,
        direction=DIRECTION_SELL,
    )
    await _login(web_client, email, password)

    body = (await web_client.get(_impact_url(ticket))).text

    assert _after_value(body, label="On trade date") == "1,200.00"


# ---------------------------------------------------------------------------
# The costs line
# ---------------------------------------------------------------------------


async def test_fees_are_named_as_sitting_inside_the_consideration(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """D-6a (i), stated rather than hidden: the value leg is overstated."""
    actor_id, email, password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    ticket = await _seed_order(
        actor_id,
        investment_id=equity_id,
        cash_investment_id=cash_id,
        fees=Decimal("5"),
    )
    await _login(web_client, email, password)

    body = (await web_client.get(_impact_url(ticket))).text

    assert "5.00 EUR inside C" in body
    assert "value leg overstated by the costs" in body
    # C = 200 + 5 — the cash leg is exact.
    assert "consideration 205.00" in body


async def test_a_ticket_without_costs_says_none(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The ordinary case, and the line still appears — an absent line reads as an omission."""
    actor_id, email, password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    ticket = await _seed_order(actor_id, investment_id=equity_id, cash_investment_id=cash_id)
    await _login(web_client, email, password)

    body = (await web_client.get(_impact_url(ticket))).text

    assert "<dt>Costs</dt><dd>none</dd>" in body


# ---------------------------------------------------------------------------
# OP-07 (a)
# ---------------------------------------------------------------------------


async def test_a_ticket_dated_at_the_seam_is_previewed_a_day_later(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The shift is allowed because it is labelled — and never silent."""
    actor_id, email, password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    ticket = await _seed_order(
        actor_id,
        investment_id=equity_id,
        cash_investment_id=cash_id,
        trade_date=T0,
    )
    await _login(web_client, email, password)

    body = " ".join((await web_client.get(_impact_url(ticket))).text.split())

    assert "Previewed as if traded on 2026-04-01." in body
    assert "The ticket is dated 2026-03-31, at or before the book's last statement" in body
    assert "(2026-03-31)" in body


async def test_a_ticket_after_the_seam_carries_no_shift_block(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """No shift, nothing to say about one."""
    actor_id, email, password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    ticket = await _seed_order(
        actor_id,
        investment_id=equity_id,
        cash_investment_id=cash_id,
        trade_date=_date(2026, 4, 30),
    )
    await _login(web_client, email, password)

    body = (await web_client.get(_impact_url(ticket))).text

    assert "Previewed as if traded on" not in body


# ---------------------------------------------------------------------------
# The scope states
# ---------------------------------------------------------------------------


async def test_a_draft_is_answered_with_the_propose_first_state(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The button is absent on a draft row (D-6f); a hand-typed URL still answers."""
    actor_id, email, password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    ticket = await _seed_order(
        actor_id,
        investment_id=equity_id,
        cash_investment_id=cash_id,
        status=STATUS_DRAFT,
    )
    await _login(web_client, email, password)

    response = await web_client.get(_impact_url(ticket))

    assert response.status_code == 200
    assert "A draft is private workspace." in response.text


async def test_a_reported_flow_is_answered_with_the_fast_follow_state(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A secondary restates NAV and cash; ``insert_transaction`` is the wrong shape."""
    actor_id, email, password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    ticket = await _seed_ticket(
        actor_id,
        status=STATUS_PROPOSED,
        kind=KIND_SECONDARY,
        direction=DIRECTION_SELL,
        investment_id=equity_id,
        cash_investment_id=cash_id,
        net_amount=Decimal("400"),
    )
    await _login(web_client, email, password)

    body = (await web_client.get(_impact_url(ticket))).text

    assert "Impact covers order tickets in v1." in body
    assert "named fast-follow" in body


async def test_a_creating_flow_is_answered_with_the_no_row_yet_state(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """U-NEW: the plan world does not contain the investment until booking (MD-12)."""
    actor_id, email, password = seeded_user
    await _seed_plan_world(actor_id)
    ticket = await _seed_ticket(
        actor_id,
        status=STATUS_PROPOSED,
        units=Decimal("10"),
        price_per_unit=Decimal("20"),
        master_data={
            MD_CURRENCY: "EUR",
            MD_NAME: "Amundi MSCI EM UCITS",
            MD_INVESTMENT_TYPE: "listed_equity",
            MD_ASSET_CLASS_ID: str(uuid4()),
            MD_ANLV_CODE: "1.1",
        },
    )
    await _login(web_client, email, password)

    body = (await web_client.get(_impact_url(ticket))).text

    assert "This ticket creates its investment at booking." in body


# ---------------------------------------------------------------------------
# The feeding statuses, and the gate above them
# ---------------------------------------------------------------------------


async def test_an_approved_ticket_renders_the_full_panel(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Decision 2: a four-eyes approver is exactly who wants this before approving."""
    actor_id, email, password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    ticket = await _seed_order(
        actor_id,
        investment_id=equity_id,
        cash_investment_id=cash_id,
        status=STATUS_APPROVED,
    )
    await _login(web_client, email, password)

    body = (await web_client.get(_impact_url(ticket))).text

    assert "SAA drift" in body
    assert "Limit headroom · AnlV" in body
    assert "Liquidity · EUR cash" in body
    assert "FX exposure unchanged." in body


@pytest.mark.parametrize("status", [STATUS_BOOKED, STATUS_CANCELLED])
async def test_a_terminal_ticket_is_not_found(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    status: str,
) -> None:
    """The ``_in_flight`` contract, shared with the resume GET and the cancel panel."""
    actor_id, email, password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    ticket = await _seed_order(
        actor_id,
        investment_id=equity_id,
        cash_investment_id=cash_id,
        status=status,
    )
    await _login(web_client, email, password)

    assert (await web_client.get(_impact_url(ticket))).status_code == 404


async def test_an_unknown_ticket_is_not_found(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A malformed id and an absent one are the same answer, on purpose."""
    _actor_id, email, password = seeded_user
    await _login(web_client, email, password)

    for path in (f"/api/transactions/ticket/{uuid4()}/impact", "/api/transactions/ticket/x/impact"):
        assert (await web_client.get(path)).status_code == 404


# ---------------------------------------------------------------------------
# The book error
# ---------------------------------------------------------------------------


async def test_a_book_with_two_eur_cash_positions_states_the_service_sentence(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The Planning Desk's *book* error, rendered here verbatim (A-7)."""
    actor_id, email, password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    ticket = await _seed_order(actor_id, investment_id=equity_id, cash_investment_id=cash_id)
    await _seed_second_cash_position(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(_impact_url(ticket))

    assert response.status_code == 200
    assert "tx-msg tx-msg--block" in response.text
    assert "two active cash positions" in response.text


# ---------------------------------------------------------------------------
# The blotter row's slot (D-6b, D-6f)
# ---------------------------------------------------------------------------


async def test_the_blotter_row_offers_impact_on_a_proposed_ticket_only(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Two panels, one slot — and no gesture that could only ever say "not yet"."""
    actor_id, email, password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    proposed = await _seed_order(actor_id, investment_id=equity_id, cash_investment_id=cash_id)
    draft = await _seed_order(
        actor_id,
        investment_id=equity_id,
        cash_investment_id=cash_id,
        status=STATUS_DRAFT,
    )
    await _login(web_client, email, password)

    body = (await web_client.get("/api/transactions/blotter")).text

    assert f'hx-get="/api/transactions/ticket/{proposed.id}/impact"' in body
    assert f'hx-target="#tx-detail-{proposed.id}"' in body
    assert f'id="tx-detail-{proposed.id}"' in body
    # The cancel panel targets the same cell, by its new name.
    assert body.count(f'hx-target="#tx-detail-{proposed.id}"') == 2

    assert f'hx-get="/api/transactions/ticket/{draft.id}/impact"' not in body
    assert f'id="tx-detail-{draft.id}"' in body
    assert "tx-cancel-" not in body


# ---------------------------------------------------------------------------
# The session gate
# ---------------------------------------------------------------------------


async def test_the_panel_needs_a_session(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """``require_session``, the ``test_transactions_wizard`` idiom."""
    actor_id, _email, _password = seeded_user
    equity_id, cash_id = await _seed_plan_world(actor_id)
    ticket = await _seed_order(actor_id, investment_id=equity_id, cash_investment_id=cash_id)

    response = await web_client.get(_impact_url(ticket))

    assert response.status_code in (302, 303, 401, 403)
