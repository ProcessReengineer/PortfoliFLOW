# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Planning Desk (ADR-0104 §4/§6/§8).

Two strands live in this module:

* **S2.3 — registration.** The seventh Area renders, its two Sections are
  present, its Modules are in the registry, and the Watch Desk's
  ``scenarios`` stub is gone (ADR-0104 §8).
* **S2.4b — the Cash Flow Planning lens.** The section renders a balance table
  and a chart payload over a seeded book; every control round-trips the whole
  parameter set; a ``t{n}_…`` set arriving by URL becomes chips whose remove
  links drop exactly one transformation, re-indexed; and each typed failure
  states itself as an actionable notice rather than a traceback.
* **S2.5 — the drawdown pacing rows.** One slider per capital-account fund over
  ``repace_flows``, merged server-side from a transient intent.
* **S2.6 — the hypothetical-transaction block.** An entry form over
  ``insert_transaction``, a row table rendered from the parameter set, and
  removal through the same link the strip's chip carries.

The live-DB fixture pattern is copied from
``tests/web/test_overview_section_routes.py`` (ASGITransport, sentinel tenant,
superuser-seeded user, inline repository seeding).
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import modules  # noqa: F401 — populates the ModuleRegistry
from core.repositories import (
    AssetClassRepository,
    FxRateRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    LimitsRepository,
    tenant_context,
)
from core.repositories.case_repository import CaseRepository
from core.tenant_constants import SENTINEL_TENANT_ID
from modules.module_registry import registry
from services.investments.cash_plan_materialisation import CASH_PLAN_SOURCE
from services.investments.pacing_rows import NO_PLAN_NOTE
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

SECTION_URL = "/api/planning-desk/cash-flow-planning"

#: The seeded book's seam — the only actual statement date it carries.
T0 = date(2026, 3, 31)


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB planning-desk tests.",
            allow_module_level=False,
        )


@pytest_asyncio.fixture
async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_schema(
    fresh_superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    truncate_sql = text(
        "TRUNCATE TABLE investment_navs, investment_cashflows, "
        "fx_rates, investments, asset_classes, "
        "data_upload_sheets, data_uploads, "
        "login_audit, sessions, audit_log, "
        "data_store_entries, users, tenants "
        "RESTART IDENTITY CASCADE"
    )
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(truncate_sql)
    try:
        yield
    finally:
        async with fresh_superuser_engine.begin() as conn:
            await conn.execute(truncate_sql)


@pytest_asyncio.fixture
async def seeded_user(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "planning-desk@example.com"
    async with fresh_superuser_engine.begin() as conn:
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
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)
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


async def _login(client: AsyncClient, email: str, password: str) -> None:
    """Drive ``GET /login`` + ``POST /login`` to seat a session cookie."""
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Book seeding
# ---------------------------------------------------------------------------


async def _seed_book(actor_id: UUID) -> UUID:
    """Seed a one-currency book with a cash plan path.

    * ``Cash EUR`` — the explicit cash position (ADR-0100). Its **actual** NAV
      at 2026-03-31 is the seam ``t₀`` *and* the anchor of the plan cash path;
      its materialised plan row (``source='computed:cash-plan'``, ADR-0103 §6)
      steps the balance down to 800 at 2026-09-30.
    * ``Equity Fund`` — a listed holding, so the book carries a non-cash
      investment for a hypothetical transaction to land on.

    Quarterly, the grid therefore opens at Q1 2026 — the only actual column the
    book can fill — with the seam immediately after it, and the plan columns
    read 1,000 carried forward until the materialised row steps them to 800.

    Returns:
        The equity's id — the target of the tests' hypothetical transactions.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
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
                as_of_date=date(2026, 9, 30),
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
            return equity.id
    finally:
        await engine.dispose()


async def _seed_capital_accounts(actor_id: UUID) -> tuple[UUID, UUID]:
    """Add the two capital-account funds the pacing rows speak for.

    * ``Buyout Fund IV`` — a **paceable** fund: 1,000 committed, 300 called for
      real (so 700 unfunded), and two remaining plan calls whose profile ends at
      2027-03-31 — 365 days past the seam, which is what makes ×1.5 a clean
      ``stretch +2 quarters`` and ×0.5 a clean ``compress −2 quarters``.
    * ``Venture Fund I`` — a capital account with **no plan flows at all**, but
      500 committed. Since ADR-0105 §4 that is the *TA-generated* row: the seam
      models a profile for it, so it paces like the buyout fund and is badged.
      (Before ADR-0105 this was the disabled row; the fund with nothing to model
      at all is :func:`_seed_unmodellable_fund`.)

    An ``investor_flow`` on the paceable fund guards the exemption invariant
    from the row side (ADR-0103 §5): it lies past the profile end, and must not
    extend it.

    Returns:
        ``(buyout_id, venture_id)``.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code="pd_pe", display_name="Private Equity"
            )
            investments = InvestmentRepository(session)
            cashflows = InvestmentCashflowRepository(session)

            buyout = await investments.create(
                name="Buyout Fund IV",
                investment_type="private_equity",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=actor_id,
                commitment_amount=Decimal("1000"),
            )
            venture = await investments.create(
                name="Venture Fund I",
                investment_type="private_equity",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=actor_id,
                commitment_amount=Decimal("500"),
            )

            async def _flow(
                investment_id: UUID,
                day: date,
                kind: str,
                amount: str,
                flow_type: str = "capital_call",
            ) -> None:
                await cashflows.create(
                    investment_id=investment_id,
                    flow_timestamp=datetime(day.year, day.month, day.day, 12, tzinfo=UTC),
                    flow_type=flow_type,
                    flow_kind=kind,
                    amount=Decimal(amount),
                    currency="EUR",
                    description=None,
                    created_by=actor_id,
                )

            # Realised: 300 called → 1,000 − 300 = 700 unfunded.
            await _flow(buyout.id, date(2026, 1, 31), "actual", "-300")
            # The remaining profile: two plan calls, ending 2027-03-31.
            await _flow(buyout.id, date(2026, 9, 30), "plan", "-200")
            await _flow(buyout.id, date(2027, 3, 31), "plan", "-100")
            # Exempt (ADR-0103 §5): past the profile end, and not part of it.
            await _flow(
                buyout.id,
                date(2029, 12, 31),
                "plan",
                "-50",
                flow_type="investor_flow",
            )
            return buyout.id, venture.id
    finally:
        await engine.dispose()


async def _seed_unmodellable_fund(actor_id: UUID) -> UUID:
    """Add the capital account that is **still** disabled after ADR-0105.

    A fund with no plan flows *and* no commitment. The TA generator will not
    invent a commitment to model from, so it returns nothing and the seam marks
    nothing — leaving the disabled row ADR-0104 §4 renders with a note rather
    than hiding. It is seeded on its own, not in
    :func:`_seed_capital_accounts`, so the tests that count pacing rows keep
    counting the two funds they were written about.

    Returns:
        The fund's id.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code="pd_pe_unmodellable", display_name="Private Equity (u)"
            )
            fund = await InvestmentRepository(session).create(
                name="Evergreen Fund",
                investment_type="private_equity",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=actor_id,
                commitment_amount=None,
            )
            return fund.id
    finally:
        await engine.dispose()


async def _seed_gilt_fund(actor_id: UUID) -> UUID:
    """Add a **GBP** holding the plan world has no cash position for.

    The book carries EUR cash only, so a hypothetical trade on this fund settles
    in a currency the plan world holds no balance in — the guard
    :class:`~services.overlay.errors.MissingCashPathError` states rather than
    invents (ADR-0103 §6). It is reachable from the entry form itself: the
    currency follows the *investment*, and this investment's currency has
    nowhere to settle.

    A holding, not a cash row: it therefore has a value path and no cash path,
    which is exactly the asymmetry the error is about — and it needs no GBP→EUR
    rate, because the timeline converts cash positions, of which GBP has none.

    Returns:
        The gilt fund's id.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code="pd_gilt", display_name="Gilts"
            )
            gilt = await InvestmentRepository(session).create(
                name="Gilt Fund",
                investment_type="listed_bonds",
                asset_class_id=asset_class.id,
                currency="GBP",
                created_by=actor_id,
            )
            await InvestmentNavRepository(session).upsert(
                investment_id=gilt.id,
                as_of_date=T0,
                nav_kind="actual",
                nav_value=Decimal("2000"),
                currency="GBP",
                source=None,
                created_by=actor_id,
            )
            return gilt.id
    finally:
        await engine.dispose()


async def _seed_uncovered_usd_cash(actor_id: UUID) -> None:
    """Add a USD cash position and **no** USD rate (ADR-0099 §3).

    The total row cannot be stated without a USD→EUR rate, and a rate is never
    defaulted to 1:1 — so the lens has to say which pair it is missing.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code="pd_cash_usd", display_name="Cash USD Class"
            )
            cash = await InvestmentRepository(session).create(
                name="Cash USD",
                investment_type="cash",
                asset_class_id=asset_class.id,
                currency="USD",
                created_by=actor_id,
                vintage_year=None,
            )
            await InvestmentNavRepository(session).upsert(
                investment_id=cash.id,
                as_of_date=T0,
                nav_kind="actual",
                nav_value=Decimal("500"),
                currency="USD",
                source=None,
                created_by=actor_id,
            )
    finally:
        await engine.dispose()


async def _seed_covered_usd_cash(actor_id: UUID) -> None:
    """Add a USD cash position **and** its USD→EUR rate.

    The multi-currency book an ``fx_shock`` is meant for: 500 USD priced at 0.92
    EUR on the seam date. The rate is dated at ``T0``, so the plan columns read
    it held flat past the seam — the plan-world FX convention (ADR-0104 §3, N1)
    that a shock restates.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            asset_class = await AssetClassRepository(session).create(
                code="pd_cash_usd_fx", display_name="Cash USD FX Class"
            )
            cash = await InvestmentRepository(session).create(
                name="Cash USD",
                investment_type="cash",
                asset_class_id=asset_class.id,
                currency="USD",
                created_by=actor_id,
                vintage_year=None,
            )
            await InvestmentNavRepository(session).upsert(
                investment_id=cash.id,
                as_of_date=T0,
                nav_kind="actual",
                nav_value=Decimal("500"),
                currency="USD",
                source=None,
                created_by=actor_id,
            )
            await FxRateRepository(session).upsert(
                currency="USD",
                as_of_date=T0,
                rate_to_reference=Decimal("0.92"),
                reference_currency="EUR",
                source="test",
                created_by=actor_id,
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Query helpers — the parameter set, as the surface encodes it
# ---------------------------------------------------------------------------


def _txn(
    index: int,
    investment_id: UUID,
    *,
    trade_date: str,
    txn_type: str = "buy",
) -> list[tuple[str, str]]:
    """One ``insert_transaction`` in the fixed ``t{n}_`` encoding."""
    return [
        (f"t{index}_kind", "insert_transaction"),
        (f"t{index}_investment_id", str(investment_id)),
        (f"t{index}_txn_type", txn_type),
        (f"t{index}_trade_date", trade_date),
        (f"t{index}_units", "10"),
        (f"t{index}_price_per_unit", "100"),
        (f"t{index}_consideration", ""),
        (f"t{index}_currency", "EUR"),
    ]


def _hyp(
    investment_id: UUID,
    *,
    txn_type: str = "buy",
    trade_date: str = "2026-06-30",
    units: str = "10",
    price_per_unit: str = "100",
    consideration: str = "",
    currency: str = "EUR",
) -> list[tuple[str, str]]:
    """The entry form's seven transient fields, as the browser submits them.

    Not the ``t{n}_`` encoding: an entry *states* a transaction, and the server
    is what turns it into a parameter (ADR-0104 §4). The tests append these to
    the form's own ``hx-get`` exactly as htmx does — see :func:`_hyp_form_url`.
    """
    return [
        ("hyp_investment_id", str(investment_id)),
        ("hyp_txn_type", txn_type),
        ("hyp_trade_date", trade_date),
        ("hyp_units", units),
        ("hyp_price_per_unit", price_per_unit),
        ("hyp_consideration", consideration),
        ("hyp_currency", currency),
    ]


def _table(body: str) -> str:
    """Return the balance table's markup alone.

    The chart travels in the same response as a JSON ``data-spec`` payload, so
    an assertion about a *number the operator reads* has to be made against the
    table rather than against the whole body.
    """
    match = re.search(r'<table class="pd-tl">.*?</table>', body, flags=re.DOTALL)
    assert match is not None, "the section rendered no balance table"
    return match.group(0)


def _hx_gets(fragment: str) -> list[str]:
    """Every ``hx-get`` URL in a fragment, unescaped."""
    return [url.replace("&amp;", "&") for url in re.findall(r'hx-get="([^"]+)"', fragment)]


def _hyp_form(body: str) -> str:
    """Return the hypothetical-transaction entry form's markup alone."""
    match = re.search(r'<form class="pd-hyp__form".*?</form>', body, flags=re.DOTALL)
    assert match is not None, "the section rendered no entry form"
    return match.group(0)


def _hyp_table(body: str) -> str:
    """Return the hypothetical-transaction row table's markup alone."""
    match = re.search(r'<table class="pd-hyp__table">.*?</table>', body, flags=re.DOTALL)
    assert match is not None, "the section rendered no hypothetical-txn table"
    return match.group(0)


def _hyp_rows(body: str) -> int:
    """How many transactions the row table states — one ✕ per row."""
    return _hyp_table(body).count("pd-hyp__x")


def _hyp_form_url(body: str) -> str:
    """Return the entry form's ``hx-get`` — the base the browser appends to.

    The browser's half of the round-trip is htmx appending the form's own seven
    fields to this URL; the tests do the same by hand, so what they exercise is
    the URL the surface actually renders.
    """
    match = re.search(r'<form class="pd-hyp__form"[^>]*hx-get="([^"]+)"', body)
    assert match is not None, "the entry form carries no hx-get"
    return match.group(1).replace("&amp;", "&")


def _submit(body: str, fields: list[tuple[str, str]]) -> str:
    """Build the URL htmx issues when the entry form is submitted."""
    return f"{_hyp_form_url(body)}&{urlencode(fields)}"


def _details_attrs(body: str) -> str:
    """Return the entry-form disclosure's attributes — ``open`` lives here.

    The disclosure is opened by the **server**, not by a click that a swap would
    forget: a refused submission comes back open, holding what was typed.
    """
    match = re.search(r'<details class="pd-hyp__add"([^>]*)>', body)
    assert match is not None, "the section rendered no entry disclosure"
    return match.group(1)


# ---------------------------------------------------------------------------
# S2.3 — the area renders
# ---------------------------------------------------------------------------


async def test_planning_desk_renders_both_sections(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """``GET /planning-desk`` renders the area with its two Sections."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/planning-desk", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    assert 'data-area="planning_desk"' in body
    assert "Planning Desk" in body
    for slug in ("cash-flow-planning", "scenario-analysis"):
        assert f'id="{slug}"' in body, f'missing section anchor id="{slug}"'
        assert f'data-section="{slug}"' in body, f"missing section-indicator dot for {slug}"


async def test_planning_desk_htmx_fragment(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An HTMX swap returns the body partial plus the OOB sidebar."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(
        "/planning-desk",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body.lower()
    assert 'hx-swap-oob="outerHTML"' in body
    assert 'data-area="planning_desk"' in body
    assert 'id="cash-flow-planning"' in body
    assert 'id="scenario-analysis"' in body


# ---------------------------------------------------------------------------
# S2.4b — the area body wires the lens
# ---------------------------------------------------------------------------


async def test_the_area_body_lazy_loads_the_lens_and_mounts_the_strip(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Cash Flow Planning is a lazy section; the strip is mounted above it."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    body = (await web_client.get("/planning-desk")).text

    assert SECTION_URL in body, "the lazy shell does not target the section"
    assert 'hx-trigger="revealed"' in body
    assert 'id="pd-paramstrip"' in body
    # Both Sections are live since S34.4 — Cash Flow Planning lost its pill in
    # S2.4b, Scenario Analysis dropped its "planned" pill here — so the area
    # carries no status pill at all.
    assert body.count('class="pf-section__pill"') == 0
    # The Scenario Analysis section mounts its own builder container for the
    # Cash Flow Planning response to fill out of band.
    assert 'id="pd-sa-builders"' in body


async def test_the_area_page_carries_a_scenario_url_into_the_lazy_fetch(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A scenario link opens the *area* page — its query must reach the lens.

    ADR-0104 §4: a scenario is reproducible from *(book, URL)*. The URL a
    shared link carries is the area page's, so the lazy shell has to hand the
    query string on to the endpoint that renders the set.
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    await _login(web_client, email, password)

    query = urlencode([("horizon", "4"), *_txn(0, equity_id, trade_date="2026-06-30")])
    body = (await web_client.get(f"/planning-desk?{query}")).text

    lazy = next(url for url in _hx_gets(body) if url.startswith(SECTION_URL))
    assert "horizon=4" in lazy
    assert "t0_kind=insert_transaction" in lazy
    assert str(equity_id) in lazy


# ---------------------------------------------------------------------------
# S2.4b — the lens renders
# ---------------------------------------------------------------------------


async def test_the_section_renders_the_table_and_the_chart_payload(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A seeded book yields the balance table, the chart spec and the seam."""
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(SECTION_URL)
    assert response.status_code == 200
    body = response.text

    assert 'id="pd-cfp-body"' in body
    assert 'class="pf-plotly-target"' in body
    assert "data-spec=" in body

    table = _table(body)
    assert "Cash EUR" in table
    assert "Total (EUR, carry-forward FX)" in table
    assert "pd-seam-l" in table, "the actual/plan seam is not drawn"
    # The anchor carried forward, stepped by the materialised plan row.
    assert "1,000" in table
    assert "800" in table
    # The row header, then one actual column and the eight quarters of the
    # default horizon — the book can fill exactly one actual column.
    assert table.count('<th scope="col"') == 1 + 1 + 8
    # One currency row plus the functional total.
    assert table.count('<th scope="row"') == 2

    # The strip travels with the section as an out-of-band swap.
    assert 'id="pd-paramstrip"' in body
    assert 'hx-swap-oob="outerHTML"' in body
    assert "Plan-world FX: last actual rate carried flat (N1)" in body


async def test_the_functional_only_view_drops_the_currency_rows(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Only the converted total remains — the toggle selects what is shown."""
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _login(web_client, email, password)

    table = _table((await web_client.get(f"{SECTION_URL}?currency_view=functional-only")).text)

    assert "Cash EUR" not in table
    assert "Total (EUR, carry-forward FX)" in table


async def test_every_toggle_link_preserves_the_other_parameters(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The parameter set survives every interaction (ADR-0104 §4)."""
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    await _login(web_client, email, password)

    query = urlencode(
        [
            ("periodisation", "monthly"),
            ("horizon", "12"),
            ("currency_view", "functional-only"),
            *_txn(0, equity_id, trade_date="2026-06-30"),
        ]
    )
    body = (await web_client.get(f"{SECTION_URL}?{query}")).text

    control_links = [
        url for url in _hx_gets(body) if url.startswith(SECTION_URL) and "t0_kind" in url
    ]
    assert control_links, "no control re-issued the parameter set"

    # Every control link carries the whole state — the overlay included — with
    # exactly the one member it changes replaced.
    quarterly = next(u for u in control_links if "periodisation=quarterly" in u)
    assert "horizon=12" in quarterly
    assert "currency_view=functional-only" in quarterly

    four_quarters = next(u for u in control_links if "horizon=4" in u)
    assert "periodisation=monthly" in four_quarters
    assert "currency_view=functional-only" in four_quarters

    per_currency = next(u for u in control_links if "currency_view=per-currency" in u)
    assert "periodisation=monthly" in per_currency
    assert "horizon=12" in per_currency


# ---------------------------------------------------------------------------
# S2.4b — the two worlds
# ---------------------------------------------------------------------------


async def test_baseline_and_scenario_state_different_numbers(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The toggle re-renders the region from the same parameters.

    The hypothetical buy settles 10 × 100 = 1,000 EUR out of the cash path from
    2026-06-30 (ADR-0104 §2, settle-against-cash), so the scenario's Q3 2026
    balance is 800 − 1,000 = −200 where the baseline's is 800. Both are read
    off the *same* book and the *same* parameter set: only the world differs.
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    await _login(web_client, email, password)

    overlay = _txn(0, equity_id, trade_date="2026-06-30")

    scenario = _table((await web_client.get(f"{SECTION_URL}?{urlencode(overlay)}")).text)
    assert "-200" in scenario, "the scenario did not settle against cash"

    baseline_query = urlencode([("view", "baseline"), *overlay])
    baseline_body = (await web_client.get(f"{SECTION_URL}?{baseline_query}")).text
    baseline = _table(baseline_body)
    assert "-200" not in baseline, "baseline view must state the book's plan"
    assert "800" in baseline

    # The chips stay on screen in baseline view — greyed, not dropped: the set
    # is still the page state, it is simply not applied.
    assert "pd-chip" in baseline_body
    assert "is-muted" in baseline_body


# ---------------------------------------------------------------------------
# S2.4b — the parameter strip
# ---------------------------------------------------------------------------


async def test_a_url_borne_chip_renders_and_its_remove_link_reindexes(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Removing a chip drops exactly that transformation, re-indexed.

    Two hypothetical transactions arrive by URL. The first chip's remove link
    must re-issue the request with the *second* transformation alone — and at
    index ``t0``, because ``serialise_overlay`` numbers by list position. The
    UI never formats an overlay key itself.
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    await _login(web_client, email, password)

    query = urlencode(
        [
            *_txn(0, equity_id, trade_date="2026-06-30"),
            *_txn(1, equity_id, trade_date="2026-12-31"),
        ]
    )
    body = (await web_client.get(f"{SECTION_URL}?{query}")).text

    assert body.count("pd-chip pd-chip--txn") == 2
    assert "Hyp. Txn: buy 10 u — Equity Fund (2026-06-30)" in body
    assert "Hyp. Txn: buy 10 u — Equity Fund (2026-12-31)" in body

    # A chip's remove link is the one that carries a *shorter* set than the
    # request did: one transformation, at t0, and no t1 at all.
    remove_links = [
        url
        for url in _hx_gets(body)
        if url.startswith(SECTION_URL) and "t0_kind" in url and "t1_kind" not in url
    ]

    first_removed = next(url for url in remove_links if "t0_trade_date=2026-12-31" in url)
    assert "t1_" not in first_removed, "the survivor was not re-indexed to t0"
    assert "2026-06-30" not in first_removed

    second_removed = next(url for url in remove_links if "t0_trade_date=2026-06-30" in url)
    assert "t1_" not in second_removed
    assert "2026-12-31" not in second_removed


async def test_the_reset_link_drops_every_transformation(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Reset-all clears the set and keeps the timeline parameters."""
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    await _login(web_client, email, password)

    query = urlencode([("horizon", "4"), *_txn(0, equity_id, trade_date="2026-06-30")])
    body = (await web_client.get(f"{SECTION_URL}?{query}")).text

    reset = next(url for url in _hx_gets(body) if url.startswith(SECTION_URL) and "t0_" not in url)
    assert "horizon=4" in reset, "reset dropped the timeline parameters too"
    assert "kind" not in reset


async def test_an_empty_set_renders_the_quiet_strip(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """No chips: the strip states the baseline and offers nothing to reset.

    An empty parameter set *is* the baseline (ADR-0104 §4), so the
    Baseline/Scenario toggle has nothing to toggle between and is rendered
    inert rather than offering a choice that makes no difference.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _login(web_client, email, password)

    body = (await web_client.get(SECTION_URL)).text

    assert "No scenario parameters" in body
    assert "pd-chip" not in body
    assert "pd-toggle--disabled" in body


# ---------------------------------------------------------------------------
# S2.4b — typed failures, stated as outcomes
# ---------------------------------------------------------------------------


async def test_a_malformed_parameter_set_is_a_400_notice(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An unreadable link is a bad request, not an empty scenario."""
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(f"{SECTION_URL}?t0_kind=not_a_kind")
    assert response.status_code == 400
    assert "UnknownKindError" in response.text
    assert "could not be read" in response.text

    # A gap in the index sequence has no defined meaning (ADR-0104 §2).
    gapped = await web_client.get(
        f"{SECTION_URL}?{urlencode(_txn(1, uuid4(), trade_date='2026-06-30'))}"
    )
    assert gapped.status_code == 400
    assert "IndexSequenceError" in gapped.text


async def test_an_fx_shock_arriving_by_url_computes_a_scenario(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The successor of ``test_an_fx_shock_arrives_readable_and_is_refused_by_the_fold``.

    That test pinned the S34.1 state: an ``fx_shock`` parsed but the *fold*
    refused it (``ExecutorNotRegisteredError``, HTTP 200), because its executor
    was still to come. S34.2 does not write that executor — it routes the shock
    to the seam it always belonged at (ADR-0104 §2/§3, N3), where the plan-world
    FX path actually lives. So the refusal is gone from the request path, and
    this is where the property moved to.

    The book here is EUR-only, so the shock is a **no-op** — and that is itself
    the point worth pinning end-to-end: the converter is the ADR-0099 §3
    zero-read identity, and an ``fx_shock`` in the parameter set neither raises
    nor causes an FX row to be read. The scenario equals its baseline because
    there is nothing denominated in USD to translate, not because the shock was
    swallowed. The covered case, where the number actually moves, is the test
    below.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(
        f"{SECTION_URL}?t0_kind=fx_shock&t0_currency=USD&t0_magnitude=-10"
    )
    assert response.status_code == 200
    assert "ExecutorNotRegisteredError" not in response.text
    assert "KindNotImplementedError" not in response.text
    # The chip is on screen: the shock is a parameter of the set, not an error.
    assert "fx_shock" in response.text


async def test_an_fx_shock_moves_the_functional_total_end_to_end(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The scenario the Planning Desk exists to state, computed through the route.

    A USD cash position, a USD→EUR rate, and a −10 % USD shock: the functional
    total of the plan columns must *fall*, because the book is long USD and one
    dollar now buys 10 % less (ADR-0099 §2's quoting convention). Baseline and
    scenario are rendered from the same request, so the two totals appear
    together and must differ — a scenario that silently equalled its baseline is
    the failure mode ADR-0104 §4 is built to prevent, and until S34.2 this URL
    produced an error region instead of a number.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _seed_covered_usd_cash(actor_id)
    await _login(web_client, email, password)

    baseline = await web_client.get(SECTION_URL)
    scenario = await web_client.get(
        f"{SECTION_URL}?t0_kind=fx_shock&t0_currency=USD&t0_magnitude=-10"
    )

    assert baseline.status_code == 200
    assert scenario.status_code == 200
    assert "ExecutorNotRegisteredError" not in scenario.text
    assert "MissingFxRateError" not in scenario.text

    # 500 USD × 0.92 = 460 EUR of the total, baseline; × 0.828 = 414, shocked.
    assert "460" in baseline.text
    assert "414" in scenario.text
    assert scenario.text != baseline.text


async def test_an_fx_shock_on_an_uncovered_currency_still_names_the_pair(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A shock restates an FX path; it never invents one (ADR-0099 §3).

    The counterpart of ``test_a_missing_fx_rate_names_the_pair``, with a shock in
    the parameter set. Shocking an unpriced currency must not paper the missing
    pair over — a shock that made USD convertible by conjuring a rate to scale
    would be the 1:1 fallback by another route, and would hand the operator a
    number with nothing behind it. The actionable error still fires, and still
    names what to supply.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _seed_uncovered_usd_cash(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(
        f"{SECTION_URL}?t0_kind=fx_shock&t0_currency=USD&t0_magnitude=-10"
    )
    assert response.status_code == 200
    assert "MissingFxRateError" in response.text
    assert "USD → EUR" in response.text, "the missing pair is not named"


async def test_a_market_shock_is_executed_rather_than_refused(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Its counterpart: the executable shock kind reaches the projection (S34.1).

    The builder UI is S34.3+ — nothing on the page emits a ``market_shock`` yet
    — but the parameter set is the page state (ADR-0104 §4), so a shock that
    arrives by URL must already *compute*. This pins that it does, rather than
    joining ``fx_shock`` in the error region.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(
        f"{SECTION_URL}?t0_kind=market_shock&t0_archetype=capital_account&t0_magnitude=-20"
    )
    assert response.status_code == 200
    assert "ExecutorNotRegisteredError" not in response.text
    assert "KindNotImplementedError" not in response.text


# ---------------------------------------------------------------------------
# S34.4 — the shock builders (market_shock / fx_shock entry surface)
# ---------------------------------------------------------------------------


async def test_the_section_renders_both_shock_builders_over_the_plan_world(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The two cards render one form shape, each Scope over the plan world.

    The builders travel out of band with the Cash Flow Planning response (they
    need the plan world's archetypes and currencies, which only this endpoint
    holds). The market card's Scope is the archetypes the book resolves to
    (here the equity fund's ``Listed equities``); the fx card's Scope is the
    non-functional currencies it holds (here ``USD``, the functional ``EUR``
    excluded). Operator and Timing are the fixed v1 options (E6).
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _seed_covered_usd_cash(actor_id)
    await _login(web_client, email, password)

    body = (await web_client.get(SECTION_URL)).text

    # The out-of-band builder container, both cards, both fields per card.
    assert 'id="pd-sa-builders"' in body
    assert 'hx-swap-oob="outerHTML"' in body
    assert "Shock builder — Market shock" in body
    assert "Shock builder — FX shock" in body
    assert 'name="mshock_archetype"' in body
    assert 'name="mshock_magnitude"' in body
    assert 'name="fxshock_currency"' in body
    assert 'name="fxshock_magnitude"' in body

    # Scope is the plan world's, through the shared display map.
    assert "Listed equities" in body
    assert 'value="total_return_equity"' in body
    assert 'value="USD"' in body

    # Operator and Timing are the single fixed option, disabled — not a choice.
    assert "Price / NAV level shift" in body
    assert "FX rate shift vs functional" in body
    assert body.count("Immediate (t₀)") == 2


async def test_the_fx_builder_says_so_when_the_book_is_single_currency(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A functional-only book offers the fx card nothing, and names why.

    The base book holds only ``EUR`` positions, so the fx Scope — the
    non-functional currencies — is empty. The card states the numéraire rather
    than offering a shock on it that the identity would swallow.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _login(web_client, email, password)

    body = (await web_client.get(SECTION_URL)).text

    assert "Shock builder — FX shock" in body
    assert 'name="fxshock_currency"' not in body
    assert "an FX shock on the numéraire is" in body
    # The market card still has its archetype to offer.
    assert 'name="mshock_archetype"' in body


async def test_a_market_shock_builder_appends_the_shock_and_chips_it(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Builder fields → overlay → chip, the round trip (ADR-0104 §4).

    The card states ``(archetype, magnitude)``; the server appends a
    ``market_shock`` and re-emits the canonical ``t{n}_`` encoding. The UI never
    formats an overlay key — the encoding appears only in the server's push URL
    and the chip's remove link.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(
        f"{SECTION_URL}?mshock_archetype=total_return_equity&mshock_magnitude=-15"
    )
    assert response.status_code == 200
    body = response.text

    # The chip renders through the shared display name and the signed per-cent.
    assert "pd-chip--shock" in body
    assert "Market shock: Listed equities" in body
    assert "15" in body

    # The address bar gets the *encoding*, never the transient builder fields.
    pushed = response.headers["HX-Push-Url"]
    assert pushed.startswith("/planning-desk?")
    assert "t0_kind=market_shock" in pushed
    assert "t0_archetype=total_return_equity" in pushed
    assert "mshock_archetype" not in pushed
    assert "mshock_magnitude" not in pushed


async def test_an_fx_shock_builder_appends_the_shock_and_chips_it(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The fx card's round trip: builder fields → overlay → chip.

    The USD cash is priced, so the shock computes rather than erroring — S34.4
    only asks that the builder produce the encoded ``fx_shock`` and the chip.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _seed_covered_usd_cash(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(f"{SECTION_URL}?fxshock_currency=USD&fxshock_magnitude=-10")
    assert response.status_code == 200
    body = response.text

    assert "pd-chip--shock" in body
    assert "FX shock: USD" in body

    pushed = response.headers["HX-Push-Url"]
    assert "t0_kind=fx_shock" in pushed
    assert "t0_currency=USD" in pushed
    assert "fxshock_currency" not in pushed


async def test_a_market_shock_builder_rejects_an_archetype_the_book_lacks(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A junk scope renders the loud notice, never a silent no-op (§3.12).

    The market Scope is a select over the book's archetypes, so a value outside
    that set is a hand-built or stale request. It is refused into the typed
    notice rather than folded into a shock that marks down a class the plan
    world does not hold — a scenario that quietly equals its baseline is the one
    failure the Planning Desk must never have (ADR-0104 §4).
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(f"{SECTION_URL}?mshock_archetype=nonsense&mshock_magnitude=-15")
    assert response.status_code == 400
    body = response.text
    assert "not added" in body
    assert "nonsense" in body
    # The set is untouched — nothing was appended, nothing pushed.
    assert "t0_kind=market_shock" not in body
    assert "HX-Push-Url" not in response.headers


async def test_an_fx_shock_builder_rejects_a_currency_the_book_lacks(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The fx counterpart: a currency the plan world holds nothing in.

    An fx shock on an unheld currency is vacuous by design at the seam
    (``shock_plan_fx_path``); the offered-set gate is what turns that silent
    no-op into the loud notice §3.12 requires.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(f"{SECTION_URL}?fxshock_currency=ZZZ&fxshock_magnitude=-10")
    assert response.status_code == 400
    body = response.text
    assert "not added" in body
    assert "ZZZ" in body
    assert "t0_kind=fx_shock" not in body
    assert "HX-Push-Url" not in response.headers


async def test_an_unoffered_horizon_is_refused_rather_than_clamped(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A horizon nobody chose is a wrong answer, not a near one."""
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(f"{SECTION_URL}?horizon=7")
    assert response.status_code == 400
    assert "PlanHorizonInvalidError" in response.text


async def test_a_missing_fx_rate_names_the_pair(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The operator has to know *which* rate to supply (ADR-0104 §3)."""
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _seed_uncovered_usd_cash(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(SECTION_URL)
    assert response.status_code == 200
    body = response.text

    assert "MissingFxRateError" in body
    assert "USD → EUR" in body, "the missing pair is not named"
    assert "1:1" in body


async def test_a_book_without_a_seam_says_import_a_statement(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An empty book has no plan world — and the lens declines to invent one."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get(SECTION_URL)
    assert response.status_code == 200
    body = response.text

    assert "PlanSeamMissingError" in body
    assert "Import a statement first" in body


async def test_a_stale_scenario_link_names_the_transformation_it_refuses(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A well-formed set the book cannot carry is an outcome, not a 500.

    The ordinary way to produce one: a scenario shared after its investment
    left the book (ADR-0104 §4).
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _login(web_client, email, password)

    query = urlencode(_txn(0, uuid4(), trade_date="2026-06-30"))
    response = await web_client.get(f"{SECTION_URL}?{query}")

    assert response.status_code == 200
    assert "UnknownInvestmentError" in response.text
    assert "pd-chip" in response.text, "the offending chip must stay removable"


# ---------------------------------------------------------------------------
# S2.5 — drawdown pacing rows
# ---------------------------------------------------------------------------


def _pace(index: int, investment_id: UUID, factor: str) -> list[tuple[str, str]]:
    """One ``repace_flows`` in the fixed ``t{n}_`` encoding."""
    return [
        (f"t{index}_kind", "repace_flows"),
        (f"t{index}_investment_id", str(investment_id)),
        (f"t{index}_factor", factor),
    ]


def _pacing_block(body: str) -> str:
    """Return the pacing block's markup alone."""
    match = re.search(r'<div class="pd-pace">.*?\n    </div>', body, flags=re.DOTALL)
    assert match is not None, "the section rendered no pacing block"
    return match.group(0)


def _slider_query(body: str, investment_id: UUID) -> str:
    """Return the slider's ``hx-get`` URL for one fund.

    The browser's half of the round-trip is htmx appending the range input's
    own ``pace_factor`` to this URL; the tests do the same by hand, so what
    they exercise is the URL the surface actually renders.
    """
    slider = next(url for url in _hx_gets(body) if f"pace_id={investment_id}" in url)
    return slider


def _repace_entries(body: str) -> list[str]:
    """Every ``t{n}_factor`` value the strip's own links carry, deduplicated."""
    return sorted(set(re.findall(r"t\d+_factor=([\d.]+)", body)))


async def test_pacing_rows_render_for_capital_account_funds_only(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """One row per capital account — and none for the listed fund or the cash row.

    The executor refuses every other archetype (``NotRepaceableError``), so an
    affordance for one would only ever produce an error.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _seed_capital_accounts(actor_id)
    await _login(web_client, email, password)

    body = (await web_client.get(SECTION_URL)).text
    block = _pacing_block(body)

    assert "Drawdown pacing — capital accounts" in block
    assert block.count('class="pd-pace__row"') == 2
    assert "Buyout Fund IV" in block
    assert "Venture Fund I" in block
    assert "Equity Fund" not in block, "a listed holding has no drawdown profile"
    assert "Cash EUR" not in block

    # 1,000 committed − 300 called for real.
    assert "reported · unfunded 700 EUR" in block
    # At plan: the readout is muted and there is nothing to reset.
    assert "on plan" in block


async def test_a_plan_less_fund_paces_on_a_generated_profile(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The disabled→enabled transition ADR-0105 §5 exists to produce.

    ``Venture Fund I`` has 500 committed and no manager plan. Before ADR-0105 it
    rendered disabled with a note promising a profile "later"; now the seam
    models one and the row paces on it — a slider to drive, and the badge that
    says the profile is a standard model rather than a manager's plan.

    The badge is the whole of what distinguishes the two on this surface
    (ADR-0105 §Consequences: "a user can still over-read a TA path … the UI copy
    must stay blunt"), which is why it is asserted here rather than left to the
    row test.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    _buyout_id, venture_id = await _seed_capital_accounts(actor_id)
    await _login(web_client, email, password)

    block = _pacing_block((await web_client.get(SECTION_URL)).text)

    # Badged, and the unfunded figure is still the book's own: 500 committed,
    # nothing called for real.
    assert "TA-generated profile · unfunded 500 EUR" in block
    # Enabled: the generated profile is something to drive.
    assert f"pace_id={venture_id}" in block
    # The manager-plan fund beside it is untouched, and still says so.
    assert "reported · unfunded 700 EUR" in block


async def test_a_fund_with_nothing_to_model_states_why_and_offers_no_slider(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The residue ADR-0105 leaves disabled: no plan, and no commitment either.

    ADR-0105 §Consequences: "the disabled-row state remains only for genuinely
    un-modellable cases (e.g. missing commitment — surfaced as such)". The row
    is still rendered rather than hidden (ADR-0104 §4), and the note names the
    current reason instead of promising a profile that has already arrived.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _seed_capital_accounts(actor_id)
    evergreen_id = await _seed_unmodellable_fund(actor_id)
    await _login(web_client, email, password)

    block = _pacing_block((await web_client.get(SECTION_URL)).text)

    assert "Evergreen Fund" in block, "a row the operator cannot pace is a fact"
    assert NO_PLAN_NOTE in block
    assert "#023" not in block, "the note must not still promise TA 'later'"
    assert "disabled" in block
    # Nothing to model means nothing to drive.
    assert f"pace_id={evergreen_id}" not in block


async def test_a_slider_adds_exactly_one_repace_entry_and_pushes_the_url(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Releasing at ×1.5 emits one chip, one readout, and a canonical URL.

    The profile ends 365 days past the seam, so ×1.5 lands 548 days out —
    2027-09-29, two calendar quarters later than 2027-03-31.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    buyout_id, _venture_id = await _seed_capital_accounts(actor_id)
    await _login(web_client, email, password)

    initial = (await web_client.get(SECTION_URL)).text
    slider = _slider_query(initial, buyout_id)

    response = await web_client.get(f"{slider}&pace_factor=1.5")
    assert response.status_code == 200
    body = response.text

    assert body.count("pd-chip pd-chip--pacing") == 1
    assert "Pacing ×1.50 — Buyout Fund IV" in body
    assert "stretch +2 quarters" in _pacing_block(body)

    # The address bar gets the *encoding*, never the intent that produced it.
    pushed = response.headers["HX-Push-Url"]
    assert pushed.startswith("/planning-desk?")
    assert "t0_kind=repace_flows" in pushed
    assert f"t0_investment_id={buyout_id}" in pushed
    assert "t0_factor=1.5" in pushed
    assert "pace_id" not in pushed
    assert "pace_factor" not in pushed


async def test_a_second_release_replaces_the_entry_and_spares_the_rest(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The UI never holds two repace chips for one fund — and re-pacing keeps its slot.

    A hypothetical transaction sits *after* the re-pacing in the set. Dragging
    the slider must replace the re-pacing **in place**: application order is
    list order (ADR-0104 §2), so an entry that jumped to the end would silently
    re-order the operator's scenario.
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    buyout_id, _venture_id = await _seed_capital_accounts(actor_id)
    await _login(web_client, email, password)

    query = urlencode(
        [
            ("horizon", "12"),
            ("periodisation", "monthly"),
            *_pace(0, buyout_id, "1.5"),
            *_txn(1, equity_id, trade_date="2026-06-30"),
        ]
    )
    body = (await web_client.get(f"{SECTION_URL}?{query}")).text
    slider = _slider_query(body, buyout_id)

    response = await web_client.get(f"{slider}&pace_factor=0.75")
    pushed = response.headers["HX-Push-Url"]

    # Exactly one re-pacing, at the new factor, still at index 0 — and the
    # hypothetical transaction survives at index 1.
    assert _repace_entries(pushed) == ["0.75"]
    assert "t0_kind=repace_flows" in pushed
    assert "t1_kind=insert_transaction" in pushed
    # Every other parameter survived the interaction.
    assert "horizon=12" in pushed
    assert "periodisation=monthly" in pushed

    reissued = response.text
    assert reissued.count("pd-chip pd-chip--pacing") == 1
    assert "Pacing ×0.75 — Buyout Fund IV" in reissued
    assert "compress −1 quarter" in _pacing_block(reissued)
    assert reissued.count("pd-chip pd-chip--txn") == 1


async def test_the_mid_position_removes_the_entry_rather_than_writing_it(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A mid-position pacing emits no chip — it *is* the plan (ADR-0104 §4)."""
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    buyout_id, _venture_id = await _seed_capital_accounts(actor_id)
    await _login(web_client, email, password)

    query = urlencode([("horizon", "4"), *_pace(0, buyout_id, "1.5")])
    body = (await web_client.get(f"{SECTION_URL}?{query}")).text
    slider = _slider_query(body, buyout_id)

    response = await web_client.get(f"{slider}&pace_factor=1.0")
    pushed = response.headers["HX-Push-Url"]

    assert "repace_flows" not in pushed, "×1.0 was written as a chip"
    assert "t0_" not in pushed
    assert "horizon=4" in pushed
    assert "pd-chip--pacing" not in response.text
    assert "on plan" in _pacing_block(response.text)


async def test_the_row_reset_clears_that_funds_pacing_alone(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Per-row Reset drops the fund's re-pacing and nothing else."""
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    buyout_id, _venture_id = await _seed_capital_accounts(actor_id)
    await _login(web_client, email, password)

    query = urlencode(
        [
            ("horizon", "4"),
            *_pace(0, buyout_id, "1.5"),
            *_txn(1, equity_id, trade_date="2026-06-30"),
        ]
    )
    block = _pacing_block((await web_client.get(f"{SECTION_URL}?{query}")).text)

    reset = next(
        url for url in _hx_gets(block) if "pace_id" not in url and "repace_flows" not in url
    )
    assert "horizon=4" in reset
    # The re-indexed survivor: the hypothetical transaction, now at t0.
    assert "t0_kind=insert_transaction" in reset
    assert "t1_" not in reset


async def test_a_duplicate_repace_set_renders_the_row_off_slider(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Two re-pacings of one fund: no slider position states that.

    The encoding permits it and the executors compose in order, so the chips
    stay in the strip stating each factor exactly — but the row renders at the
    mid-position, disabled, with a note, and its Reset clears **both**. Honest
    display beats clever composition.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    buyout_id, _venture_id = await _seed_capital_accounts(actor_id)
    await _login(web_client, email, password)

    query = urlencode([*_pace(0, buyout_id, "1.5"), *_pace(1, buyout_id, "0.8")])
    body = (await web_client.get(f"{SECTION_URL}?{query}")).text

    # Both chips stay on screen and stay removable.
    assert body.count("pd-chip pd-chip--pacing") == 2
    assert "Pacing ×1.50 — Buyout Fund IV" in body
    assert "Pacing ×0.80 — Buyout Fund IV" in body

    block = _pacing_block(body)
    assert "off-slider parameter set" in block
    assert f"pace_id={buyout_id}" not in block, "an unstatable row kept a slider"

    reset = next(url for url in _hx_gets(block) if "horizon" in url)
    assert "repace_flows" not in reset, "Reset left one of the two behind"


async def test_an_out_of_bounds_factor_is_a_400_notice(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The bounds are the contract's, and a hand-built URL cannot get around them.

    Both doors are checked: the encoded ``t{n}_factor`` and the slider's own
    transient ``pace_factor``. Neither can be emitted by the surface — the
    slider's range comes from the same constants — so both are bad requests.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    buyout_id, _venture_id = await _seed_capital_accounts(actor_id)
    await _login(web_client, email, password)

    encoded = await web_client.get(f"{SECTION_URL}?{urlencode(_pace(0, buyout_id, '3.0'))}")
    assert encoded.status_code == 400
    assert "FactorOutOfBoundsError" in encoded.text

    transient = await web_client.get(f"{SECTION_URL}?pace_id={buyout_id}&pace_factor=3.0")
    assert transient.status_code == 400
    assert "FactorOutOfBoundsError" in transient.text


# ---------------------------------------------------------------------------
# S2.6 — hypothetical transactions
# ---------------------------------------------------------------------------


async def test_an_empty_set_renders_the_quiet_line_and_keeps_the_add_button(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """No hypothetical transactions: a quiet line, and the affordance stays."""
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _login(web_client, email, password)

    body = (await web_client.get(SECTION_URL)).text

    assert "Hypothetical transactions" in body
    assert "No hypothetical transactions in this scenario." in body
    assert "+ Add hypothetical transaction" in body
    assert 'class="pd-hyp__table"' not in body, "an empty set rendered a table"
    # The form is closed until the operator opens it — or until the server
    # hands it back with a refusal.
    assert "open" not in _details_attrs(body)


async def test_the_form_offers_the_plan_worlds_universe_and_binds_the_currency(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The selector is the plan world's non-cash universe; currency follows it.

    Three things at once, because they are one rule (S2.6 binding 1): the
    currency is not a choice, it is the selected investment's. So each option
    carries its currency, the field is read-only, the form's script re-binds it
    on every selection — and a cash position is not on offer at all, since an
    ``insert_transaction`` settles *against* cash and never lands on it
    (ADR-0104 §1: cash lives in the frames' cash paths, not their investments).
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    gilt_id = await _seed_gilt_fund(actor_id)
    await _login(web_client, email, password)

    body = (await web_client.get(SECTION_URL)).text
    form = _hyp_form(body)

    assert f'value="{equity_id}"' in form
    assert f'value="{gilt_id}"' in form
    assert 'data-currency="EUR"' in form
    assert 'data-currency="GBP"' in form
    assert "Cash EUR" not in form, "a cash position is not a tradeable universe"

    # Name-ascending, like every other list on the surface: Equity Fund, then
    # Gilt Fund — so the read-only field opens at the first option's currency.
    assert form.index("Equity Fund") < form.index("Gilt Fund")
    assert 'name="hyp_currency"' in form
    assert 'value="EUR"' in form
    assert "readonly" in form

    # The binding itself: the script that seats the selected option's currency.
    assert "form.querySelector('[name=\"hyp_currency\"]')" in body
    assert "option.dataset.currency" in body

    # The date floor is the seam plus a day — read off the book, not a clock.
    assert 'min="2026-04-01"' in form

    # buy and sell, and nothing else (binding 4).
    assert '<option value="buy"' in form
    assert '<option value="sell"' in form
    assert '<option value="opening"' not in form
    assert '<option value="transfer"' not in form


async def test_a_submitted_buy_becomes_one_entry_a_row_a_chip_and_moves_cash(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The entry form's round trip, end to end.

    The submission states seven transient fields; the server appends one
    ``insert_transaction`` to the set, re-emits the whole thing in the canonical
    encoding, and hands the address bar the URL — never the intent. The buy of
    10 × 100 settles 1,000 EUR out of the cash path from 2026-06-30, so the
    scenario's Q3 2026 balance is 800 − 1,000 = −200.
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    await _login(web_client, email, password)

    initial = (await web_client.get(SECTION_URL)).text
    response = await web_client.get(_submit(initial, _hyp(equity_id)))
    assert response.status_code == 200
    body = response.text

    # Exactly one entry, encoded — and the transient fields are gone.
    pushed = response.headers["HX-Push-Url"]
    assert pushed.startswith("/planning-desk?")
    assert pushed.count("_kind=") == 1
    assert "t0_kind=insert_transaction" in pushed
    assert f"t0_investment_id={equity_id}" in pushed
    assert "t0_trade_date=2026-06-30" in pushed
    assert "t0_units=10" in pushed
    assert "t0_price_per_unit=100" in pushed
    assert "t0_currency=EUR" in pushed
    assert "hyp_" not in pushed, "the address bar holds the intent, not the set"

    # The row, the chip, and the moved cell — one parameter, three views.
    table = _hyp_table(body)
    assert "pd-pill--buy" in table
    assert "Equity Fund" in table
    assert "2026-06-30" in table
    assert "−1,000 EUR cash" in table, "the cash effect is not the executor's"
    assert "±0 by construction" in table
    assert body.count("pd-chip pd-chip--txn") == 1
    assert "Hyp. Txn: buy 10 u — Equity Fund (2026-06-30)" in body
    assert "-200" in _table(body), "the scenario did not settle against cash"

    # The form closes behind a successful entry and comes back empty.
    assert "open" not in _details_attrs(body)


async def test_a_submission_composes_with_a_pacing_entry_and_keeps_the_order(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Both transformations survive, and the new one is **appended**.

    Application order is list order (ADR-0104 §2), so an entry that jumped the
    queue would silently re-order the operator's scenario against the pacing
    around it.
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    buyout_id, _venture_id = await _seed_capital_accounts(actor_id)
    await _login(web_client, email, password)

    query = urlencode(
        [("horizon", "12"), ("periodisation", "monthly"), *_pace(0, buyout_id, "1.5")]
    )
    paced = (await web_client.get(f"{SECTION_URL}?{query}")).text
    response = await web_client.get(_submit(paced, _hyp(equity_id)))
    assert response.status_code == 200

    pushed = response.headers["HX-Push-Url"]
    assert "t0_kind=repace_flows" in pushed, "the pacing lost its slot"
    assert "t1_kind=insert_transaction" in pushed, "the entry was not appended"
    assert "horizon=12" in pushed
    assert "periodisation=monthly" in pushed

    body = response.text
    assert body.count("pd-chip pd-chip--pacing") == 1
    assert body.count("pd-chip pd-chip--txn") == 1
    assert "Pacing ×1.50 — Buyout Fund IV" in body
    assert _hyp_rows(body) == 1


async def test_a_stated_consideration_beats_units_times_price(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The executor's derivation, and the cell that states it.

    ``derive_consideration``: a stated consideration wins outright, and only
    otherwise is ``units × price_per_unit`` computed. The cash effect is its
    negation (value in, cash out) — so a buy stated at 750 moves 750, not the
    1,000 its units and price would have implied, and a sale moves cash *in*.
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    await _login(web_client, email, password)

    initial = (await web_client.get(SECTION_URL)).text

    bought = (await web_client.get(_submit(initial, _hyp(equity_id, consideration="750")))).text
    assert "−750 EUR cash" in _hyp_table(bought)
    assert "−1,000 EUR cash" not in bought, "units × price beat the statement"
    # 1,000 − 750 at Q2, 800 − 750 at Q3: the cash path moved by the amount
    # the operator stated, not by the one their units and price implied.
    assert "250" in _table(bought)

    # A sale: units negative (the operator states the sign, ADR-0097 §2), and a
    # negative consideration — so the cash effect is positive. Cash comes in.
    sold = (
        await web_client.get(
            _submit(
                initial,
                _hyp(
                    equity_id,
                    txn_type="sell",
                    units="-10",
                    consideration="-750",
                ),
            )
        )
    ).text
    assert "+750 EUR cash" in _hyp_table(sold)
    assert "pd-pill--sell" in _hyp_table(sold)
    # 800 + 750.
    assert "1,550" in _table(sold)


async def test_the_row_x_and_the_chip_x_are_the_same_link(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Removing a row and removing its chip are one behaviour, not two.

    Both carry the round-trip of the set minus that transformation, re-indexed
    by ``serialise_overlay`` — so the same URL appears twice in the response,
    once in the strip and once in the table, and neither of them was formatted
    by the UI.
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    await _login(web_client, email, password)

    query = urlencode(
        [
            *_txn(0, equity_id, trade_date="2026-06-30"),
            *_txn(1, equity_id, trade_date="2026-12-31"),
        ]
    )
    body = (await web_client.get(f"{SECTION_URL}?{query}")).text

    assert body.count("pd-chip pd-chip--txn") == 2
    assert _hyp_rows(body) == 2

    # The links that drop exactly one of the two: the survivor, re-indexed to
    # t0. Each is rendered twice — the chip's ✕ and the row's ✕ — and they are
    # the *same* string, not two consistent ones.
    removals = [
        url
        for url in _hx_gets(body)
        if url.startswith(SECTION_URL) and "t0_kind=insert_transaction" in url and "t1_" not in url
    ]
    assert len(removals) == 4, "a removal is missing from the strip or the row"
    assert len(set(removals)) == 2

    first_removed = next(url for url in removals if "t0_trade_date=2026-12-31" in url)
    assert removals.count(first_removed) == 2
    assert "2026-06-30" not in first_removed

    # And it does what it says: the other entry survives, alone, at t0.
    reissued = (await web_client.get(first_removed)).text
    assert reissued.count("pd-chip pd-chip--txn") == 1
    assert _hyp_rows(reissued) == 1
    assert "2026-12-31" in _hyp_table(reissued)


async def test_a_trade_dated_at_the_seam_names_the_earliest_legal_date(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """History is identical in every scenario (ADR-0104 §5) — and the form says so.

    The seam itself is realised history, so the earliest legal trade date is the
    day after it. The notice names the date; the form comes back open, holding
    what was typed, so the operator fixes one field rather than the URL.
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    await _login(web_client, email, password)

    initial = (await web_client.get(SECTION_URL)).text
    response = await web_client.get(_submit(initial, _hyp(equity_id, trade_date=T0.isoformat())))

    assert response.status_code == 400
    body = response.text
    assert "HistoricTradeDateError" in body
    assert "2026-04-01" in body, "the earliest legal date is not named"
    assert "was <strong>not added</strong>" in body

    # The set is untouched and nothing was pushed: a refused entry never became
    # a parameter.
    assert "HX-Push-Url" not in response.headers
    assert "insert_transaction" not in body
    assert "No scenario parameters" in body

    # The form is handed back, open, with the operator's inputs in it.
    assert "open" in _details_attrs(body)
    form = _hyp_form(body)
    assert f'value="{T0.isoformat()}"' in form
    assert 'value="10"' in form
    assert 'value="100"' in form


async def test_the_typed_refusals_of_a_submission_are_400_notices(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Every way the plan world can refuse an entry, stated as itself.

    Three refusals the executor owns, and one the entry surface does. None of
    them is reachable by clicking — the form constrains the type set, requires a
    price where no consideration is stated, and pins the currency to the
    investment — so each arrives here from a hand-built request and is answered
    as a bad request, typed and named.
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    gilt_id = await _seed_gilt_fund(actor_id)
    await _login(web_client, email, password)

    initial = (await web_client.get(SECTION_URL)).text

    # Neither a price nor a consideration: there is no cash effect to settle,
    # and a zero would state "no impact" where the truth is "not stated".
    underivable = await web_client.get(_submit(initial, _hyp(equity_id, price_per_unit="")))
    assert underivable.status_code == 400
    assert "UnderivableConsiderationError" in underivable.text

    # A currency the investment is not held in. The form renders it read-only
    # and re-binds it to the selection, so only a hand-built URL gets here.
    mismatched = await web_client.get(_submit(initial, _hyp(equity_id, currency="USD")))
    assert mismatched.status_code == 400
    assert "CurrencyMismatchError" in mismatched.text
    assert "never converts" in mismatched.text

    # A currency the plan world holds no cash position in. Reachable by
    # selection — the gilt fund *is* on offer — so the notice names GBP.
    uncovered = await web_client.get(_submit(initial, _hyp(gilt_id, currency="GBP")))
    assert uncovered.status_code == 400
    assert "MissingCashPathError" in uncovered.text
    assert "GBP" in uncovered.text
    assert "cannot invent a balance nobody funded" in uncovered.text

    # A type the form does not offer. `opening` is an Excel-import artefact and
    # `transfer` has no cash leg: neither settles against cash, which is the
    # whole of what this surface states.
    untyped = await web_client.get(_submit(initial, _hyp(equity_id, txn_type="opening")))
    assert untyped.status_code == 400
    assert "hyp_txn_type=opening" in untyped.text
    assert "trades that settle against cash" in untyped.text

    # The sign is typed, never derived — exactly as on the actual-entry form.
    unsigned = await web_client.get(_submit(initial, _hyp(equity_id, txn_type="sell", units="10")))
    assert unsigned.status_code == 400
    assert "units must be negative for a sell transaction" in unsigned.text


async def test_a_url_borne_foreign_type_renders_rather_than_being_censored(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The form offers two types; the table states whatever the set says.

    An ``opening`` cannot be *entered* here (binding 4), but the overlay
    contract carries all four types and a URL may state one. The row renders it
    honestly — a plain type cell, no buy/sell pill — because the display's job
    is to say what the parameter set says, not to decide what it should have
    said.
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    await _login(web_client, email, password)

    query = urlencode(_txn(0, equity_id, trade_date="2026-06-30", txn_type="opening"))
    response = await web_client.get(f"{SECTION_URL}?{query}")

    assert response.status_code == 200
    table = _hyp_table(response.text)
    assert "opening" in table
    assert "pd-pill--buy" not in table
    assert "pd-pill--sell" not in table
    # It executes like any other insert: value in, cash out (800 − 1,000).
    assert "−1,000 EUR cash" in table
    assert "-200" in _table(response.text)


# ---------------------------------------------------------------------------
# S34.5 — the Scenario Analysis result region (chart pair, KPI strip, headroom)
# ---------------------------------------------------------------------------


async def _seed_limit_sets(actor_id: UUID) -> None:
    """Seed one SAA and one AnlV limit set so the scenario coverage scores.

    The result region's headroom table and its AnlV / breach KPIs run the
    coverage engine, which needs a set in force at every plan date (else
    ``LimitSetNotEffective``, which the region catches into a notice). A
    production tenant always carries both families; the base ``_seed_book`` does
    not, so the S34.5 tests add them. SAA groups on ``asset_class_code``
    (``pd_class`` here); the AnlV set only has to exist for the family to score.
    """
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            limits = LimitsRepository(session)
            await limits.create_set_with_limits(
                family="saa",
                effective_from=date(2020, 1, 1),
                label="SAA base",
                notes=None,
                limits={"pd_class": Decimal("80.0")},
                created_by=actor_id,
            )
            await limits.create_set_with_limits(
                family="anlv",
                effective_from=date(2020, 1, 1),
                label="AnlV base",
                notes=None,
                limits={"listed_equity": Decimal("35.0")},
                created_by=actor_id,
            )
    finally:
        await engine.dispose()


async def test_the_section_carries_the_scenario_result_region_out_of_band(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The result region rides along with the section response, swapped OOB.

    Even with an empty overlay the region is scored — the baseline, which the
    Baseline/Scenario toggle needs (ADR-0104 §4). It carries the two impact
    chart targets (shared-axis pair), the four-tile KPI strip, and the headroom
    deltatable — delivered into ``#pd-sa-results`` the way the strip and the
    shock builders are.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _seed_limit_sets(actor_id)
    await _login(web_client, email, password)

    body = (await web_client.get(SECTION_URL)).text

    # The out-of-band result container.
    assert 'id="pd-sa-results"' in body
    assert body.count('hx-swap-oob="outerHTML"') >= 2  # strip/builders + results
    # The shared-axis chart pair — two dual-axis panels.
    assert 'id="pd-sa-chart-baseline"' in body
    assert 'id="pd-sa-chart-scenario"' in body
    # The KPI strip in the ADR-0067 pair idiom.
    assert "pd-kpi__base" in body
    assert "pd-kpi__scen" in body
    assert "AUM (Σ NAV incl. cash)" in body
    # The headroom deltatable (its columns render even with no limit set seeded).
    assert "pd-deltatable" in body
    assert "Baseline util." in body
    assert "Scenario util." in body


async def test_a_market_shock_moves_the_scenario_panel_off_the_baseline(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A shock arriving by URL re-scores the region — the pair is drawn.

    The chart pair is rendered from the ScenarioResult of the same overlay, so a
    ``market_shock`` on the plan world produces a scenario panel whose figures
    differ from the baseline's; here we pin that the region renders both panels
    without error under a live shock (the numeric divergence is the chart-spec
    and assembly's own pins).
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _seed_limit_sets(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(
        f"{SECTION_URL}?mshock_archetype=total_return_equity&mshock_magnitude=-15"
    )
    assert response.status_code == 200
    body = response.text

    assert 'id="pd-sa-results"' in body
    assert 'id="pd-sa-chart-scenario"' in body
    assert "could not be scored" not in body
    # The composition drill-down remains a lazy shell — not loaded inline.
    assert 'hx-get="/api/planning-desk/scenario-composition' in body
    assert 'hx-trigger="revealed"' in body


async def test_the_composition_endpoint_returns_the_lazy_drill_down(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The composition endpoint renders its own partial on demand (S34.5, §7).

    The secondary surface: the result region's lazy shell fetches it, and it
    answers with the per-fund baseline-vs-scenario share diff — a standalone
    partial, not the whole section.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _seed_limit_sets(actor_id)
    await _login(web_client, email, password)

    response = await web_client.get(
        "/api/planning-desk/scenario-composition?periodisation=quarterly&horizon=8"
    )
    assert response.status_code == 200
    body = response.text

    # The drill-down partial, not the section body.
    assert "pd-comp__detail" in body
    assert 'id="pd-sa-results"' not in body
    assert 'id="pd-cfp-body"' not in body
    # The diff itself, not the "could not be built" notice.
    assert "is unavailable" not in body
    assert "Equity Fund" in body


# ---------------------------------------------------------------------------
# C5 — the capture marker + the scenario-snapshot pin (ADR-0107)
# ---------------------------------------------------------------------------

PIN_URL = "/api/planning-desk/pin-scenario"


async def _session_csrf(client: AsyncClient, engine: AsyncEngine) -> str:
    """Read the logged-in session's CSRF token straight from the DB.

    The pin POST is CSRF-guarded like every write; the token is the session's
    own value (mirrors ``tests/web/test_cases_area.py``).
    """
    cookie = client.cookies.get("portfoliflow_session")
    assert cookie is not None, "not logged in"
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT csrf_token FROM sessions WHERE session_token = :t"),
                {"t": cookie},
            )
        ).first()
    assert row is not None
    return str(row.csrf_token)


async def _seed_case(
    actor_id: UUID, *, title: str = "Capture target", closed: bool = False
) -> tuple[UUID, int]:
    """Open a case (optionally closed) through the repository under RLS."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            repo = CaseRepository(session)
            case = await repo.create(
                title=title,
                opened_by=actor_id,
                opened_actor="pm",
                now=datetime.now(UTC),
            )
            if closed:
                await repo.close(
                    case.id,
                    closed_by=actor_id,
                    closing_note="closed for the test",
                    now=datetime.now(UTC),
                )
            return case.id, case.case_number
    finally:
        await engine.dispose()


async def _snapshot_pins(actor_id: UUID, case_id: UUID) -> list[Any]:
    """Return a case's ``scenario_snapshot`` pin entries (app-role read)."""
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
            entries = await CaseRepository(session).list_entries(case_id)
            return [
                entry
                for entry in entries
                if entry.kind == "pin"
                and (entry.payload or {}).get("artifact") == "scenario_snapshot"
            ]
    finally:
        await engine.dispose()


def test_the_marker_never_enters_the_overlay_serialisation() -> None:
    """The ``case=`` marker rides alongside the overlay, never through it.

    Binding decision 4: the marker is navigation context, not a transformation.
    ``parse_overlay`` ignores it (it is outside the ``t{n}_`` namespace), so
    ``serialise_overlay`` — the overlay's own encoding — never carries it.
    """
    from services.overlay import parse_overlay, serialise_overlay

    overlay = parse_overlay(
        [
            ("t0_kind", "fx_shock"),
            ("t0_currency", "USD"),
            ("t0_magnitude", "10"),
            ("case", "de-ad-be-ef-00"),
        ]
    )
    keys = [key for key, _ in serialise_overlay(overlay)]
    assert keys, "the overlay parsed to nothing"
    assert "case" not in keys
    assert not any(key.endswith("case") for key in keys)


async def test_the_capture_marker_rides_every_control_link(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The marker survives every page-state round-trip (Step 1, test 1).

    With an active overlay and all four view toggles off-default, every link the
    page emits — the toggles, the chip's remove ✕, reset-all, the pacing and
    hypothetical-transaction forms, the composition drill-down and the pin
    affordance — carries ``case=``. The one deliberate exception is the capturing
    chip's own dismiss, which exists to drop the marker.
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    await _seed_limit_sets(actor_id)
    case_id, _ = await _seed_case(actor_id, title="Rebalance review")
    await _login(web_client, email, password)

    query = urlencode(
        [
            ("periodisation", "monthly"),
            ("horizon", "12"),
            ("currency_view", "functional-only"),
            *_txn(0, equity_id, trade_date="2026-06-30"),
            ("case", str(case_id)),
        ]
    )
    body = (await web_client.get(f"{SECTION_URL}?{query}")).text
    marker = f"case={case_id}"

    # Strip the one link that is meant to drop the marker, then sweep the rest.
    body_wo_dismiss = re.sub(r'<a class="pd-capturing__dismiss".*?</a>', "", body, flags=re.DOTALL)
    links = _hx_gets(body_wo_dismiss)
    assert links, "the section emitted no links"
    for url in links:
        assert marker in url, f"a rendered link dropped the marker: {url}"
        # The marker is a single top-level pair, never folded into an overlay.
        assert url.count("case=") == 1
        assert "t0_case=" not in url

    # The canonical copy-scenario link carries it too (a rendered link).
    assert marker in body


async def test_a_stale_or_bad_marker_never_breaks_the_desk(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Malformed / unknown / closed markers are dropped silently (test 2).

    A stale link must never break the Desk: each renders the page with no chip
    and no error. A valid, open case renders the chip with its badge and title,
    and the chip's dismiss link drops only the ``case=`` param.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)
    await _seed_limit_sets(actor_id)
    open_id, open_num = await _seed_case(actor_id, title="Live case")
    closed_id, _ = await _seed_case(actor_id, title="Shut case", closed=True)
    await _login(web_client, email, password)

    for bad in ("not-a-uuid", str(uuid4()), str(closed_id)):
        response = await web_client.get(f"{SECTION_URL}?case={bad}")
        assert response.status_code == 200
        assert "pd-capturing" not in response.text
        # The lens still renders and the region still scores — no error state.
        assert "could not be scored" not in response.text

    body = (await web_client.get(f"{SECTION_URL}?case={open_id}")).text
    assert "pd-capturing" in body
    assert f"CASE-{open_num:04d}" in body
    assert "Live case" in body
    dismiss = re.search(r'<a class="pd-capturing__dismiss"[^>]*hx-get="([^"]+)"', body)
    assert dismiss is not None
    assert "case=" not in dismiss.group(1).replace("&amp;", "&")


async def test_a_pinned_snapshot_equals_the_rendered_results(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """The snapshot serialises exactly what the results region renders (test 3).

    Pin a scenario state, then read the stored ``snapshot`` and the section
    rendered from the *same* state: every KPI pair string, both feet and every
    parameter chip the snapshot froze is the string the region shows — the C0
    sentence made literal. The stored query is scenario state, so it carries no
    ``case=`` marker.
    """
    actor_id, email, password = seeded_user
    equity_id = await _seed_book(actor_id)
    await _seed_limit_sets(actor_id)
    case_id, case_number = await _seed_case(actor_id, title="Snapshot target")
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    state = [
        ("periodisation", "quarterly"),
        ("horizon", "8"),
        ("currency_view", "per-currency"),
        ("view", "scenario"),
        *_txn(0, equity_id, trade_date="2026-06-30"),
    ]
    response = await web_client.post(
        PIN_URL,
        data={
            "csrf_token": csrf,
            "case_id": str(case_id),
            "comment": "kept as the assessed-and-rejected option",
            **dict(state),
        },
    )
    assert response.status_code == 200
    assert "Snapshot pinned to" in response.text
    assert f"CASE-{case_number:04d}" in response.text

    pins = await _snapshot_pins(actor_id, case_id)
    assert len(pins) == 1
    snap = pins[0].payload["snapshot"]
    assert pins[0].payload["comment"] == "kept as the assessed-and-rejected option"
    # The stored query is documentation, and it is scenario state: no marker.
    assert "case=" not in snap["query"]
    assert "t0_kind=insert_transaction" in snap["query"]
    # No charts entered the payload.
    assert "baseline_spec" not in snap
    assert "scenario_spec" not in snap

    section = (await web_client.get(f"{SECTION_URL}?{urlencode(state)}")).text
    assert snap["kpis"], "no KPI pairs were frozen"
    for kpi in snap["kpis"]:
        assert kpi["base"] in section
        assert kpi["scen"] in section
        assert kpi["delta"] in section
    assert snap["baseline_foot"]["nav"] in section
    assert snap["scenario_foot"]["nav_delta"] in section
    for chip in snap["chips"]:
        assert chip["label"] in section
        assert "remove_query" not in chip  # a frozen record has no live links


async def test_the_pin_endpoint_gates_write_nothing_on_failure(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Each gate re-renders the dialog with its error and writes nothing (test 4).

    The book carries **no** limit set, so a well-formed request reaches the
    fourth gate and finds nothing to score — the "nothing to pin" case. The
    first three gates (comment, case exists, case open) fail before any
    projection.
    """
    actor_id, email, password = seeded_user
    await _seed_book(actor_id)  # deliberately no _seed_limit_sets
    open_id, _ = await _seed_case(actor_id, title="Gate case")
    closed_id, _ = await _seed_case(actor_id, title="Closed case", closed=True)
    await _login(web_client, email, password)
    csrf = await _session_csrf(web_client, fresh_superuser_engine)

    base = {
        "periodisation": "quarterly",
        "horizon": "8",
        "currency_view": "per-currency",
        "view": "scenario",
    }

    # Gate 1 — empty comment.
    r = await web_client.post(
        PIN_URL,
        data={"csrf_token": csrf, "case_id": str(open_id), "comment": "  ", **base},
    )
    assert r.status_code == 200
    assert "capture comment is required" in r.text
    assert await _snapshot_pins(actor_id, open_id) == []

    # Gate 2 — the case does not exist.
    r = await web_client.post(
        PIN_URL,
        data={"csrf_token": csrf, "case_id": str(uuid4()), "comment": "x", **base},
    )
    assert "could not be found" in r.text

    # Gate 3 — the case is closed.
    r = await web_client.post(
        PIN_URL,
        data={"csrf_token": csrf, "case_id": str(closed_id), "comment": "x", **base},
    )
    assert "closed" in r.text.lower()
    assert await _snapshot_pins(actor_id, closed_id) == []

    # Gate 4 — a valid, open case but no scorable result (no limit set).
    r = await web_client.post(
        PIN_URL,
        data={"csrf_token": csrf, "case_id": str(open_id), "comment": "x", **base},
    )
    assert "no result to pin" in r.text
    assert await _snapshot_pins(actor_id, open_id) == []


async def test_the_pin_dialog_preselects_the_marker_case(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The dialog preselects the marker's case, else nothing (test 5)."""
    actor_id, email, password = seeded_user
    id_a, _ = await _seed_case(actor_id, title="Alpha")
    id_b, _ = await _seed_case(actor_id, title="Bravo")
    await _login(web_client, email, password)

    base = "periodisation=quarterly&horizon=8&currency_view=per-currency&view=scenario"

    under_marker = (await web_client.get(f"{PIN_URL}?{base}&case={id_a}")).text
    assert re.search(rf'<option value="{id_a}"[^>]*selected', under_marker)
    assert not re.search(rf'<option value="{id_b}"[^>]*selected', under_marker)

    no_marker = (await web_client.get(f"{PIN_URL}?{base}")).text
    assert "selected" not in no_marker
    # Both cases are still offered — the picker is list_open, not the marker.
    assert f'value="{id_a}"' in no_marker
    assert f'value="{id_b}"' in no_marker

    # Cancel clears the slot.
    cancelled = await web_client.get(f"{PIN_URL}?{base}&cancel=1")
    assert cancelled.text.strip() == ""


async def test_the_pin_dialog_is_calm_with_no_open_cases(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """With no open cases the dialog is calm and offers no dead dropdown (test 5)."""
    actor_id, email, password = seeded_user
    await _seed_case(actor_id, title="Only a closed one", closed=True)
    await _login(web_client, email, password)

    body = (
        await web_client.get(
            f"{PIN_URL}?periodisation=quarterly&horizon=8&currency_view=per-currency&view=scenario"
        )
    ).text
    assert "no open cases" in body
    assert "Go to Cases" in body
    assert 'name="case_id"' not in body


# ---------------------------------------------------------------------------
# Registry — ADR-0058 completeness
# ---------------------------------------------------------------------------


def test_registry_lists_the_planning_desk_modules() -> None:
    """``list_by_area("planning_desk")`` yields the two Section modules."""
    names = {cls.module_name for cls in registry.list_by_area("planning_desk")}
    assert names == {"cash_flow_planning", "scenario_analysis"}


def test_registry_no_longer_lists_the_watch_desk_scenarios_stub() -> None:
    """The retired ``scenarios`` module is gone from the Watch Desk (ADR-0104 §8)."""
    names = {cls.module_name for cls in registry.list_by_area("watch_desk")}
    assert "scenarios" not in names
    assert names == {"briefing", "journal", "calibration"}


# ---------------------------------------------------------------------------
# The Watch Desk keeps three sections
# ---------------------------------------------------------------------------


async def test_watch_desk_renders_three_sections_without_scenarios(
    web_client: AsyncClient,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The Watch Desk page renders Briefing / Journal / Calibration and no more."""
    _id, email, password = seeded_user
    await _login(web_client, email, password)

    response = await web_client.get("/watch-desk", follow_redirects=False)
    assert response.status_code == 200
    body = response.text

    for slug in ("briefing", "journal", "calibration"):
        assert f'id="{slug}"' in body
    assert 'id="scenarios"' not in body
    assert 'data-section="scenarios"' not in body
    assert body.count('class="pf-section-indicator__dot"') == 3
