# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Where we stand — the read-only composition (ADR-0138 §6, P-CS-2a).

Runs against the test container on the Irene book fixture (one equity at
:data:`CALM_NAV` of a :data:`AUM_EUR` book, SAA ceiling 50 %), so the
subject's live reading can be compared with a direct
``read_family_status`` on the same resolution.

Coverage
--------
* ST-01: the context resolves once and degrades on a bare tenant.
* ST-02: the subject now vs at opening — both sources (D-CS-B-13).
* ST-03: signal, press and unkeyed subjects.
* ST-04: linked investments with the book's figures.
* ST-05: the counterparty listing (D-CS-B-12).
* ST-06: FX at opening and now.
* ST-07: the case's tickets.
* ST-08: the system lines.
* ST-09: ``book_values_for`` and the freeze round trip (D-CS-B-14).
* ST-10: a closed case is refused.
* ST-11: the headline, shared by the list and the block.
* ST-12: ``to_snapshot`` is JSON-ready.
* ST-13: nothing is cached across contexts.
"""

from __future__ import annotations

import json
import math
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import services.cases.stand as stand_module
from core.exceptions import CaseStateInvalid
from core.repositories import (
    CaseFigureRepository,
    CaseInvestmentRepository,
    CaseRepository,
    FxRateRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    IreneFindingRepository,
    TradeTicketRepository,
    tenant_context,
)
from core.repositories.case_repository import CaseDTO
from core.repositories.case_snapshot import build_file_snapshot
from services.cases import (
    CounterpartyReading,
    StandContext,
    book_values_for,
    compose,
    headline_for,
    to_snapshot,
)
from services.investments.investment_service import InvestmentService
from services.money_format import format_money_compact
from services.watch_desk.family_status import read_family_status
from services.watch_desk.overlay import resolve_watch_desk
from tests.services.irene._book_fixtures import (
    BREACH_NAV,
    CALM_NAV,
    LATEST_DATE,
    SAA_SUBJECT,
    seed_book,
    seed_user,
    set_latest_nav,
)

_LINE = "Equities at 25.0 % of the 30.0 % ceiling (OK)."


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _book(app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant, name: str):
    """Seed a tenant with the calm Irene book; return ``(tenant, user, equity)``."""
    tenant_id = await seed_tenant(name)
    user_id = await seed_user(superuser_engine, tenant_id)
    equity_id = await seed_book(app_engine, tenant_id, user_id, latest_nav=CALM_NAV)
    return tenant_id, user_id, equity_id


async def _finding_case(
    session: AsyncSession,
    user_id: UUID,
    subject_key: str = SAA_SUBJECT,
    *,
    stand: dict | None = None,
    opened_at: datetime | None = None,
) -> CaseDTO:
    """Open a case from a fresh finding with the C4 materiality line."""
    finding = await IreneFindingRepository(session).append(
        subject_key=subject_key, payload={"trigger": "x"}, urgency=3, band="critical"
    )
    return await CaseRepository(session).create(
        title=f"Case on {subject_key}",
        opened_by=user_id,
        finding_id=finding.id,
        opened_actor="pm",
        opened_payload={"materiality_at_opening": {"lines": [_LINE]}},
        now=opened_at or _now(),
        stand=stand,
    )


async def _bare_case(
    session: AsyncSession, user_id: UUID, *, counterparty: str | None = None
) -> CaseDTO:
    return await CaseRepository(session).create(
        title="Manual case",
        opened_by=user_id,
        opened_actor="pm",
        counterparty=counterparty,
        now=_now(),
    )


async def _link(session: AsyncSession, case: CaseDTO, user_id: UUID, *ids: UUID) -> None:
    links = CaseInvestmentRepository(session)
    for investment_id in ids:
        await links.link(case.id, investment_id=investment_id, linked_by=user_id, now=_now())


async def _investment(
    session: AsyncSession,
    user_id: UUID,
    like: UUID,
    name: str,
    *,
    currency: str = "EUR",
    manager_name: str | None = None,
    is_active: bool = True,
) -> UUID:
    """Create an investment in the asset class of ``like``."""
    repo = InvestmentRepository(session)
    template = await repo.get_by_id(like)
    assert template is not None
    created = await repo.create(
        name=name,
        investment_type="private_equity",
        asset_class_id=template.asset_class_id,
        currency=currency,
        created_by=user_id,
        manager_name=manager_name,
        is_active=is_active,
    )
    return created.id


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or math.isnan(float(value)):
        return None
    return Decimal(str(float(value)))


# ---------------------------------------------------------------------------
# ST-01
# ---------------------------------------------------------------------------


async def test_st01_context_resolves_once_and_degrades(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    seed_tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, user_id, equity_id = await _book(app_engine, superuser_engine, seed_tenant, "ST-01")

    calls = {"resolve": 0, "status": 0}
    real_resolve = stand_module.resolve_watch_desk
    real_status = stand_module.read_family_status

    async def counting_resolve(*args: Any, **kwargs: Any) -> Any:
        calls["resolve"] += 1
        return await real_resolve(*args, **kwargs)

    async def counting_status(*args: Any, **kwargs: Any) -> Any:
        calls["status"] += 1
        return await real_status(*args, **kwargs)

    monkeypatch.setattr(stand_module, "resolve_watch_desk", counting_resolve)
    monkeypatch.setattr(stand_module, "read_family_status", counting_status)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        finding_case = await _finding_case(session, user_id)
        investment_case = await _bare_case(session, user_id)
        await _link(session, investment_case, user_id, equity_id)

        ctx = await StandContext.load(session, now=_now())
        await compose(ctx, finding_case)
        await compose(ctx, investment_case)
        await headline_for(ctx, finding_case)
        await headline_for(ctx, investment_case)

    # One resolution and one status read for the whole request.
    assert calls == {"resolve": 1, "status": 1}
    assert ctx.status is not None
    assert ctx.status_unavailable_reason is None
    assert any(
        subject.subject_key == SAA_SUBJECT
        for group in ctx.status.limits
        for subject in group.subjects
    )
    assert ctx.functional_currency == "EUR"

    # A bare tenant: no book, no limit set — the context still loads.
    bare_tenant = await seed_tenant("ST-01-bare")
    bare_user = await seed_user(superuser_engine, bare_tenant)
    async with tenant_context(app_engine, bare_tenant, user_id=bare_user) as session:
        bare_ctx = await StandContext.load(session, now=_now())
    assert bare_ctx.status is not None or bare_ctx.status_unavailable_reason
    if bare_ctx.status is not None:
        assert all(group.subjects == () for group in bare_ctx.status.limits)


# ---------------------------------------------------------------------------
# ST-02
# ---------------------------------------------------------------------------


async def test_st02_subject_now_against_at_opening(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id, equity_id = await _book(app_engine, superuser_engine, seed_tenant, "ST-02")

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        case = await _finding_case(session, user_id)
        now = _now()
        ctx = await StandContext.load(session, now=now)
        calm = await compose(ctx, case)

        resolution = await resolve_watch_desk(session, as_of=now)
        direct = await read_family_status(session, resolution=resolution, now=now)

    expected = next(
        subject
        for group in direct.limits
        for subject in group.subjects
        if subject.subject_key == SAA_SUBJECT
    )
    assert calm.subject is not None
    reading = calm.subject.now
    assert reading is not None
    assert reading.subject_key == SAA_SUBJECT
    assert reading.family == "saa"
    assert reading.class_key == "equities"
    assert reading.value == expected.coverage_pct
    assert reading.ceiling == expected.max_pct
    assert reading.status == expected.status == "OK"
    assert reading.unit == "pct"
    assert reading.as_of == direct.coverage_as_of
    assert calm.subject.at_opening is None
    assert calm.subject.at_opening_lines == (_LINE,)
    assert calm.subject.unavailable_reason is None

    # The book moves: a fresh context reads the breach beside the line at opening.
    await set_latest_nav(app_engine, tenant_id, user_id, equity_id, BREACH_NAV)
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        ctx = await StandContext.load(session, now=_now())
        moved = await compose(ctx, case)

    assert moved.subject is not None and moved.subject.now is not None
    assert moved.subject.now.status == "BREACH"
    assert moved.subject.now.value != reading.value
    assert moved.subject.at_opening_lines == (_LINE,)

    # CS-3 passes the stand at opening: the structured reading comes back.
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        structured_case = await _finding_case(session, user_id, stand=to_snapshot(calm))
        ctx = await StandContext.load(session, now=_now())
        structured = await compose(ctx, structured_case)

    assert structured.subject is not None
    assert structured.subject.at_opening == reading
    assert isinstance(structured.subject.at_opening.value, Decimal)
    assert isinstance(structured.subject.at_opening.as_of, date)
    assert structured.at_opening is not None
    assert structured.at_opening["case_id"] == str(case.id)


# ---------------------------------------------------------------------------
# ST-03
# ---------------------------------------------------------------------------


async def test_st03_signal_press_and_unkeyed_subjects(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id, _ = await _book(app_engine, superuser_engine, seed_tenant, "ST-03")

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        ctx = await StandContext.load(session, now=_now())
        assert ctx.status is not None and ctx.status.press
        press_key = ctx.status.press[0].subject_key

        price = await compose(ctx, await _finding_case(session, user_id, "price:NOT-WATCHED"))
        press = await compose(ctx, await _finding_case(session, user_id, press_key))
        unkeyed = await compose(ctx, await _finding_case(session, user_id, "freeform"))

    assert price.subject is not None and price.subject.now is not None
    assert price.subject.now.status is None
    assert price.subject.now.note == "not watched"
    assert price.subject.now.value is None

    assert press.subject is not None and press.subject.now is not None
    assert press.subject.now.family == "rss"
    assert press.subject.now.status_label == "watched"
    assert press.subject.now.value is None and press.subject.now.unit is None

    assert unkeyed.subject is not None and unkeyed.subject.now is not None
    assert unkeyed.subject.now.class_key is None
    assert unkeyed.subject.now.note


# ---------------------------------------------------------------------------
# ST-04
# ---------------------------------------------------------------------------


async def test_st04_linked_investments_carry_the_books_figures(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id, equity_id = await _book(app_engine, superuser_engine, seed_tenant, "ST-04")

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        dormant = await _investment(session, user_id, equity_id, "Dormant", is_active=False)
        case = await _bare_case(session, user_id)
        await _link(session, case, user_id, equity_id, dormant)
        stand = await compose(await StandContext.load(session, now=_now()), case)

        bundle = await InvestmentService(
            InvestmentRepository(session),
            InvestmentNavRepository(session),
            InvestmentCashflowRepository(session),
        ).get_charts_data(equity_id, include_irr=True)

    assert bundle is not None
    if len(bundle.rolling_multiples) > 0:
        last = bundle.rolling_multiples.iloc[-1]
        expected = tuple(_as_decimal(last[c]) for c in ("tvpi", "dpi", "rvpi"))
    else:
        expected = (None, None, None)
    expected_irr = _as_decimal(bundle.rolling_irr.iloc[-1]) if len(bundle.rolling_irr) else None

    assert [r.investment_id for r in stand.investments] == [equity_id, dormant]
    alpha, dormant_reading = stand.investments
    assert alpha.name == "Alpha"
    assert alpha.currency == "EUR"
    assert alpha.nav_value == CALM_NAV
    assert alpha.nav_as_of == LATEST_DATE
    assert (alpha.tvpi, alpha.dpi, alpha.rvpi) == expected
    assert alpha.irr == expected_irr
    assert alpha.linked is True and alpha.is_active is True
    # The seed books no cashflows: the multiples and IRR are whatever the
    # investment service yields for that — the stand restates, never computes.

    assert dormant_reading.is_active is False
    assert dormant_reading.linked is True
    assert dormant_reading.nav_value is None and dormant_reading.nav_as_of is None


# ---------------------------------------------------------------------------
# ST-05
# ---------------------------------------------------------------------------


async def test_st05_counterparty_lists_its_funds_without_a_total(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id, equity_id = await _book(app_engine, superuser_engine, seed_tenant, "ST-05")

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        fund_a = await _investment(session, user_id, equity_id, "Fund A", manager_name="Acme")
        fund_b = await _investment(session, user_id, equity_id, "Fund B", manager_name="Acme")
        fund_c = await _investment(session, user_id, equity_id, "Fund C", manager_name="Other")
        ctx = await StandContext.load(session, now=_now())

        named = await _bare_case(session, user_id, counterparty="Acme")
        await _link(session, named, user_id, fund_a)
        named_stand = await compose(ctx, named)

        shared = await _bare_case(session, user_id)
        await _link(session, shared, user_id, fund_a, fund_b)
        shared_stand = await compose(ctx, shared)

        mixed = await _bare_case(session, user_id)
        await _link(session, mixed, user_id, fund_a, fund_c)
        mixed_stand = await compose(ctx, mixed)

    assert {f.name for f in fields(CounterpartyReading)} == {"name", "funds"}

    assert named_stand.counterparty is not None
    assert named_stand.counterparty.name == "Acme"
    by_id = {f.investment_id: f for f in named_stand.counterparty.funds}
    assert set(by_id) == {fund_a, fund_b}
    assert by_id[fund_a].linked is True
    assert by_id[fund_b].linked is False
    assert [r.investment_id for r in named_stand.investments] == [fund_a]

    assert shared_stand.counterparty is not None
    assert shared_stand.counterparty.name == "Acme"
    assert {f.investment_id for f in shared_stand.counterparty.funds} == {fund_a, fund_b}
    assert all(f.linked for f in shared_stand.counterparty.funds)

    assert mixed_stand.counterparty is None


# ---------------------------------------------------------------------------
# ST-06
# ---------------------------------------------------------------------------


async def test_st06_fx_at_opening_and_now(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id, equity_id = await _book(app_engine, superuser_engine, seed_tenant, "ST-06")
    now = _now()
    opened_at = now - timedelta(days=10)
    opening_rate_day = opened_at.date() - timedelta(days=1)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        usd = await _investment(session, user_id, equity_id, "US Fund", currency="USD")
        fx = FxRateRepository(session)
        await fx.upsert("USD", opening_rate_day, Decimal("1.10"), "EUR", "test", user_id)
        await fx.upsert("USD", now.date(), Decimal("1.20"), "EUR", "test", user_id)
        case = await CaseRepository(session).create(
            title="FX case", opened_by=user_id, opened_actor="pm", now=opened_at
        )
        await _link(session, case, user_id, usd, equity_id)
        stand = await compose(await StandContext.load(session, now=now), case)

    assert len(stand.fx) == 1
    (reading,) = stand.fx
    assert reading.currency == "USD"
    assert reading.functional_currency == "EUR"
    assert reading.rate_at_opening == Decimal("1.10")
    assert reading.rate_at_opening_as_of == opening_rate_day
    assert reading.rate_now == Decimal("1.20")
    assert reading.rate_now_as_of == now.date()

    # A USD investment in a tenant holding no rates: both readings empty.
    other_tenant, other_user, other_equity = await _book(
        app_engine, superuser_engine, seed_tenant, "ST-06-norates"
    )
    async with tenant_context(app_engine, other_tenant, user_id=other_user) as session:
        usd = await _investment(session, other_user, other_equity, "US Fund", currency="USD")
        case = await _bare_case(session, other_user)
        await _link(session, case, other_user, usd)
        bare = await compose(await StandContext.load(session, now=_now()), case)

    assert len(bare.fx) == 1
    assert bare.fx[0].rate_now is None and bare.fx[0].rate_now_as_of is None
    assert bare.fx[0].rate_at_opening is None and bare.fx[0].rate_at_opening_as_of is None


# ---------------------------------------------------------------------------
# ST-07
# ---------------------------------------------------------------------------


async def test_st07_the_cases_tickets(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id, equity_id = await _book(app_engine, superuser_engine, seed_tenant, "ST-07")

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        case = await _bare_case(session, user_id)
        tickets = TradeTicketRepository(session)
        draft = dict(
            kind="order",
            direction="buy",
            currency="EUR",
            trade_date=date(2026, 8, 31),
            created_by=user_id,
            investment_id=equity_id,
            now=_now(),
        )
        ticket = await tickets.create_draft(**draft, case_id=case.id)
        await tickets.create_draft(**draft)
        stand = await compose(await StandContext.load(session, now=_now()), case)

    assert len(stand.tickets) == 1
    (reading,) = stand.tickets
    assert reading.ticket_id == ticket.id
    assert reading.status == "draft"
    assert reading.ticket_number == ticket.ticket_number
    assert reading.investment_name == "Alpha"
    assert reading.booked_at is None


# ---------------------------------------------------------------------------
# ST-08
# ---------------------------------------------------------------------------


async def test_st08_system_lines_only(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id, equity_id = await _book(app_engine, superuser_engine, seed_tenant, "ST-08")

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        case = await _bare_case(session, user_id)
        repo = CaseRepository(session)
        subject = f"{equity_id}:2026-10-31"
        payload = {
            "event": "nav_restated",
            "subject": subject,
            "text": "NAV restated for Alpha.",
            "investment_id": str(equity_id),
        }
        line = await repo.add_system_line(
            case.id,
            event="nav_restated",
            subject=subject,
            text="NAV restated for Alpha.",
            payload={"investment_id": str(equity_id)},
            now=_now(),
        )
        assert line is not None
        for kind in ("note", "pin"):
            await repo.append_entry(
                case.id,
                kind=kind,
                actor="pm",
                actor_user_id=user_id,
                payload={"text": kind},
                now=_now(),
            )
        stand = await compose(await StandContext.load(session, now=_now()), case)

    # The opened milestone's frozen row exists and is not a system line either.
    assert len(stand.system_lines) == 1
    (reading,) = stand.system_lines
    assert reading.entry_id == line.id
    assert reading.event == "nav_restated"
    assert reading.text == "NAV restated for Alpha."
    assert reading.payload == payload


# ---------------------------------------------------------------------------
# ST-09
# ---------------------------------------------------------------------------


async def test_st09_book_values_and_the_freeze_round_trip(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id, equity_id = await _book(app_engine, superuser_engine, seed_tenant, "ST-09")

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        case = await _finding_case(session, user_id)
        await _link(session, case, user_id, equity_id)
        figure_repo = CaseFigureRepository(session)

        async def book(label: str, investment_id: str, metric: str):
            return await figure_repo.add(
                case.id,
                label=label,
                source="book",
                source_ref={"investment_id": investment_id, "metric": metric},
                now=_now(),
            )

        nav = await book("NAV", str(equity_id), "nav")
        tvpi = await book("TVPI", str(equity_id), "tvpi")
        commitment = await book("Commitment", str(equity_id), "commitment")
        currency = await book("Currency", str(equity_id), "currency")
        unknown_metric = await book("Unknown", str(equity_id), "sharpe")
        unknown_investment = await book("Elsewhere", str(uuid4()), "nav")
        typed = await figure_repo.add(
            case.id, label="Typed", source="typed", value_text="1", now=_now()
        )
        figures = await figure_repo.list_for_case(case.id)

        stand = await compose(await StandContext.load(session, now=_now()), case, figures=figures)
        values = book_values_for(stand, figures)
        snapshot = await build_file_snapshot(
            session, case.id, stand=to_snapshot(stand), book_values=values
        )

    book_ids = {str(f.id) for f in (nav, tvpi, commitment, currency)}
    book_ids |= {str(unknown_metric.id), str(unknown_investment.id)}
    assert set(values) == book_ids
    assert str(typed.id) not in values

    assert values[str(nav.id)] == {
        "value_text": format_money_compact(float(CALM_NAV), "EUR"),
        "as_of": LATEST_DATE.isoformat(),
    }
    assert values[str(currency.id)] == {"value_text": "EUR", "as_of": None}
    # Alpha carries no commitment: not available, and never dated.
    assert values[str(commitment.id)] == {"value_text": None, "as_of": None}
    assert values[str(unknown_metric.id)] == {"value_text": None, "as_of": None}
    assert values[str(unknown_investment.id)] == {"value_text": None, "as_of": None}
    alpha = stand.investments[0]
    if alpha.tvpi is None:
        assert values[str(tvpi.id)] == {"value_text": None, "as_of": None}
    else:
        assert values[str(tvpi.id)]["as_of"] == LATEST_DATE.isoformat()

    assert isinstance(snapshot["where_we_stand"]["subject"]["now"]["value"], str)
    frozen_nav = next(f for f in snapshot["figures"] if f["id"] == str(nav.id))
    assert frozen_nav["value_text"] == values[str(nav.id)]["value_text"]


# ---------------------------------------------------------------------------
# ST-10
# ---------------------------------------------------------------------------


async def test_st10_a_closed_case_is_refused(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id, _ = await _book(app_engine, superuser_engine, seed_tenant, "ST-10")

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        case = await _finding_case(session, user_id)
        repo = CaseRepository(session)
        closed = await repo.close(case.id, closed_by=user_id, closing_note="done", now=_now())
        ctx = await StandContext.load(session, now=_now())

        with pytest.raises(CaseStateInvalid, match="ADR-0138 §6"):
            await compose(ctx, closed)
        assert await headline_for(ctx, closed) == ""


# ---------------------------------------------------------------------------
# ST-11
# ---------------------------------------------------------------------------


async def test_st11_one_headline_for_the_list_and_the_block(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id, equity_id = await _book(app_engine, superuser_engine, seed_tenant, "ST-11")

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        finding_case = await _finding_case(session, user_id)
        investment_case = await _bare_case(session, user_id)
        await _link(session, investment_case, user_id, equity_id)
        bare = await _bare_case(session, user_id)
        ctx = await StandContext.load(session, now=_now())

        headlines = {
            name: (await headline_for(ctx, case), (await compose(ctx, case)).headline)
            for name, case in (
                ("finding", finding_case),
                ("investment", investment_case),
                ("bare", bare),
            )
        }

    for listed, composed in headlines.values():
        assert listed == composed

    finding_line = headlines["finding"][0]
    assert SAA_SUBJECT in finding_line
    assert "10.0 %" in finding_line  # 100k of the 1M book
    assert "50.0 %" in finding_line  # the SAA ceiling
    assert "(OK)" in finding_line

    nav = format_money_compact(float(CALM_NAV), "EUR")
    assert headlines["investment"][0] == f"Alpha · NAV {nav} · {LATEST_DATE.isoformat()}"
    assert headlines["bare"][0] == ""


# ---------------------------------------------------------------------------
# ST-12
# ---------------------------------------------------------------------------


async def test_st12_to_snapshot_is_json_ready(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id, equity_id = await _book(app_engine, superuser_engine, seed_tenant, "ST-12")

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        case = await _finding_case(session, user_id)
        await _link(session, case, user_id, equity_id)
        now = _now()
        stand = await compose(await StandContext.load(session, now=now), case)

    snapshot = to_snapshot(stand)
    json.dumps(snapshot)
    assert "_readings_by_id" not in snapshot
    assert snapshot["composed_at"] == now.isoformat()
    assert snapshot["case_id"] == str(case.id)
    assert isinstance(snapshot["subject"]["now"]["value"], str)
    assert Decimal(snapshot["investments"][0]["nav_value"]) == CALM_NAV
    assert snapshot["investments"][0]["nav_as_of"] == LATEST_DATE.isoformat()
    assert set(snapshot) == {f.name for f in fields(stand) if not f.name.startswith("_")}


# ---------------------------------------------------------------------------
# ST-13
# ---------------------------------------------------------------------------


async def test_st13_nothing_is_cached_across_contexts(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id, user_id, equity_id = await _book(app_engine, superuser_engine, seed_tenant, "ST-13")

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        case = await _finding_case(session, user_id)
        before = await compose(await StandContext.load(session, now=_now()), case)

    await set_latest_nav(app_engine, tenant_id, user_id, equity_id, BREACH_NAV)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        after = await compose(await StandContext.load(session, now=_now()), case)

    assert before.subject is not None and before.subject.now is not None
    assert after.subject is not None and after.subject.now is not None
    assert after.subject.now.value != before.subject.now.value
