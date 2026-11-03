# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""System lines — templates, the NAV diff and the writers (ADR-0138 §7, P-CS-2b).

Runs against the test container on the Irene book fixture (one equity
"Alpha" at :data:`CALM_NAV` of a :data:`AUM_EUR` book, SAA ceiling 50 %).

Coverage
--------
* SL-01: the four template texts, exact, and their payloads (D-CS-B-20).
* SL-02: ``nav_changes`` — new, changed, unchanged, deleted, order, scale.
* SL-03: the ``subject_within_ceiling`` writer — targets, a waiting case
  lifted by its line (D-CS-M-4), dedup.
* SL-04: the ``nav_restated`` writer — linked cases only, a waiting case
  lifted by its first line and carried open into the second (D-CS-B-22),
  dedup, closed cases skipped.
* SL-05: the beat seam — a falling edge writes one line; a calm beat none.
* SL-09: the ``ticket_booked`` / ``job_requested`` / ``job_landed``
  templates, exact (P-CS-2c).
* SL-10: the ``ticket_booked`` writer — open, waiting, closed, another
  tenant's case, dedup.
* SL-11: the ``job_requested`` / ``job_landed`` writers — one line each per
  proposal, an asked job lifts a waiting case.

SL-06 (the NAV routes), SL-07 (the import) and SL-08 (the booking) sit
beside their seams' own tests.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.repositories import (
    CaseInvestmentRepository,
    CaseRepository,
    IreneFindingRepository,
    tenant_context,
)
from core.repositories.case_repository import CaseDTO, CaseEntryDTO
from core.repositories.trade_ticket_repository import TradeTicketDTO
from services.ai_service_core import ResolvedLLM
from services.cases import (
    EVENT_JOB_LANDED,
    EVENT_JOB_REQUESTED,
    EVENT_NAV_RESTATED,
    EVENT_SUBJECT_WITHIN_CEILING,
    EVENT_TICKET_BOOKED,
    job_landed,
    job_requested,
    nav_changes,
    nav_restated,
    subject_within_ceiling,
    ticket_booked,
)
from services.cases.system_lines import (
    job_landed_line,
    job_requested_line,
    limit_within_ceiling_line,
    nav_restated_line,
    signal_within_threshold_line,
    ticket_booked_line,
)
from services.irene.beat import run_beat
from tests.services.irene._book_fixtures import (
    ANLV_SUBJECT,
    BREACH_NAV,
    CALM_NAV,
    SAA_SUBJECT,
    SurfacingCore,
    seed_book,
    seed_user,
    set_latest_nav,
)

_LLM = ResolvedLLM(base_url="https://openrouter.test/api/v1", api_key="sk-sl", model="test-model")
_AS_OF = date(2026, 11, 2)
_T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _finding_case(
    session: AsyncSession,
    user_id: UUID,
    subject_key: str,
    *,
    resolution: str = "opened_case",
    title: str | None = None,
) -> CaseDTO:
    """Open a case from a finding on ``subject_key``, resolved as given."""
    finding = await IreneFindingRepository(session).append(
        subject_key=subject_key, payload={"trigger": "x"}, urgency=5, band="critical"
    )
    if resolution != "open":
        await IreneFindingRepository(session).resolve(
            finding_id=finding.id, resolution=resolution, resolved_by=user_id, resolved_at=_T0
        )
    return await CaseRepository(session).create(
        title=title or f"Case on {subject_key}",
        opened_by=user_id,
        finding_id=finding.id,
        opened_actor="pm",
        now=_T0,
    )


async def _manual_case(session: AsyncSession, user_id: UUID, title: str = "Manual") -> CaseDTO:
    return await CaseRepository(session).create(
        title=title, opened_by=user_id, opened_actor="pm", now=_T0
    )


async def _assert_lifted_by(repo: CaseRepository, case_id: UUID, line: CaseEntryDTO) -> None:
    """The case is open again, lifted by ``system`` on ``line`` in the line's instant (D-CS-M-4).

    The record ends ``system_line · waiting_lifted · frozen`` (the line
    precedes the milestone it woke; the snapshot follows its milestone),
    the lift names the line, and the snapshot shows the case open.
    """
    case = await repo.get(case_id)
    assert case is not None
    assert (case.state, case.wait_until) == ("open", None)
    entries = await repo.list_entries(case_id, include_frozen=True)
    assert [e.kind for e in entries[-3:]] == ["system_line", "waiting_lifted", "frozen"]
    woken_by, lifted, frozen = entries[-3:]
    assert woken_by.id == line.id
    assert (lifted.actor, lifted.actor_user_id) == ("system", None)
    assert lifted.created_at == line.created_at == frozen.created_at
    assert lifted.payload["reason"] == "event"
    assert lifted.payload["event"] == {
        "event": line.payload["event"],
        "subject": line.payload["subject"],
        "entry_id": str(line.id),
    }
    assert frozen.payload["milestone_entry_id"] == str(lifted.id)
    assert frozen.payload["file"]["case"]["state"] == "open"


# ---------------------------------------------------------------------------
# SL-01: the templates
# ---------------------------------------------------------------------------


def test_sl01_templates_are_fixed_strings() -> None:
    finding_id = uuid4()
    subject, line, payload = limit_within_ceiling_line(
        subject_key="saa:equities",
        family="saa",
        status="OK",
        coverage_pct=Decimal("42.04"),
        max_pct=Decimal("50"),
        as_of=_AS_OF,
        finding_id=finding_id,
    )
    assert subject == "saa:equities"
    assert line == (
        "saa:equities is back within its ceiling: 42.0 % of the 50.0 % ceiling (OK) "
        "as of 2026-11-02."
    )
    assert payload == {
        "family": "saa",
        "status": "OK",
        "coverage_pct": "42.04",
        "max_pct": "50",
        "as_of": "2026-11-02",
        "finding_id": str(finding_id),
    }

    _, no_ceiling, payload = limit_within_ceiling_line(
        subject_key="anlv:16",
        family="anlv",
        status="WARN",
        coverage_pct=Decimal("7.25"),
        max_pct=None,
        as_of=_AS_OF,
        finding_id=None,
    )
    assert no_ceiling == "anlv:16 is back within its ceiling: 7.2 % (WARN) as of 2026-11-02."
    assert payload["max_pct"] is None and payload["finding_id"] is None

    subject, line, payload = signal_within_threshold_line(
        subject_key="fx:EUR/USD",
        family="fx",
        display_name="EUR/USD",
        status="Calm",
        as_of=_AS_OF,
        finding_id=finding_id,
        value=Decimal("1.5"),
        threshold=Decimal("5"),
        unit="pp",
    )
    assert subject == "fx:EUR/USD"
    assert line == "EUR/USD is back within its threshold (Calm) as of 2026-11-02."
    assert payload == {
        "family": "fx",
        "status": "Calm",
        "value": "1.5",
        "threshold": "5",
        "unit": "pp",
        "as_of": "2026-11-02",
        "finding_id": str(finding_id),
    }

    investment_id = uuid4()
    subject, line, payload = nav_restated_line(
        investment_id=investment_id,
        investment_name="Alpha",
        currency="EUR",
        as_of=_AS_OF,
        previous_value=None,
        nav_value=Decimal("1250000"),
    )
    assert subject == f"{investment_id}:2026-11-02"
    assert line == "NAV reported for Alpha: €1.2M as of 2026-11-02."
    assert payload == {
        "investment_id": str(investment_id),
        "investment_name": "Alpha",
        "as_of": "2026-11-02",
        "nav_value": "1250000",
        "previous_value": None,
        "currency": "EUR",
        "change": "new",
    }

    _, line, payload = nav_restated_line(
        investment_id=investment_id,
        investment_name="Alpha",
        currency="CHF",
        as_of=_AS_OF,
        previous_value=Decimal("1250000"),
        nav_value=Decimal("1300000.50"),
    )
    assert line == "NAV restated for Alpha as of 2026-11-02: CHF 1.2M → CHF 1.3M."
    assert (payload["previous_value"], payload["nav_value"], payload["change"]) == (
        "1250000",
        "1300000.50",
        "restated",
    )


# ---------------------------------------------------------------------------
# SL-02: nav_changes
# ---------------------------------------------------------------------------


def test_sl02_nav_changes() -> None:
    d1, d2, d3, d4 = (date(2026, m, 28) for m in (1, 2, 3, 4))
    before = {d1: Decimal("1.0"), d2: Decimal("2"), d3: Decimal("3")}
    after = {d4: Decimal("4"), d2: Decimal("2.5"), d1: Decimal("1.00")}
    # d1: scale only (1.0 == 1.00) → nothing; d2 changed; d3 deleted → nothing;
    # d4 new. Sorted by as-of.
    assert nav_changes(before, after) == [
        (d2, Decimal("2"), Decimal("2.5")),
        (d4, None, Decimal("4")),
    ]
    assert nav_changes(after, after) == []
    assert nav_changes({}, {}) == []
    assert nav_changes(before, {}) == []


# ---------------------------------------------------------------------------
# SL-03: the subject_within_ceiling writer
# ---------------------------------------------------------------------------


async def test_sl03_subject_within_ceiling_writer(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SL-03")
    user_id = await seed_user(superuser_engine, tenant_id)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = CaseRepository(session)
        open_case = await _finding_case(session, user_id, SAA_SUBJECT, title="open")
        waiting_case = await _finding_case(session, user_id, SAA_SUBJECT, title="waiting")
        await repo.wait(waiting_case.id, wait_until=date(2027, 1, 1), by=user_id, now=_T0)
        closed_case = await _finding_case(session, user_id, SAA_SUBJECT, title="closed")
        await repo.close(closed_case.id, closed_by=user_id, closing_note="done", now=_T0)
        other_case = await _finding_case(session, user_id, ANLV_SUBJECT, title="other")

        written = await subject_within_ceiling(
            session,
            subject_key=SAA_SUBJECT,
            family="saa",
            status="OK",
            as_of=_AS_OF,
            now=_T0 + timedelta(hours=1),
            coverage_pct=Decimal("10.0"),
            max_pct=Decimal("50.0"),
        )
        assert sorted(e.case_id for e in written) == sorted([open_case.id, waiting_case.id])
        expected = (
            "saa:equities is back within its ceiling: 10.0 % of the 50.0 % ceiling (OK) "
            "as of 2026-11-02."
        )
        for case in (open_case, waiting_case):
            (line,) = await repo.list_system_lines(case.id)
            assert (line.kind, line.actor, line.actor_user_id) == ("system_line", "system", None)
            assert line.payload["event"] == EVENT_SUBJECT_WITHIN_CEILING
            assert line.payload["subject"] == SAA_SUBJECT
            assert line.payload["text"] == expected
            assert line.payload["finding_id"] == str(case.finding_id)
            assert line.payload["coverage_pct"] == "10.0"
        assert await repo.list_system_lines(closed_case.id) == []
        assert await repo.list_system_lines(other_case.id) == []

        # The line woke the waiting case: lifted by system, in one transaction.
        (waking,) = await repo.list_system_lines(waiting_case.id)
        await _assert_lifted_by(repo, waiting_case.id, waking)
        entry_count = len(await repo.list_entries(waiting_case.id, include_frozen=True))

        # Dedup: the same event and subject again writes nothing.
        again = await subject_within_ceiling(
            session,
            subject_key=SAA_SUBJECT,
            family="saa",
            status="OK",
            as_of=_AS_OF,
            now=_T0 + timedelta(hours=2),
            coverage_pct=Decimal("9.0"),
            max_pct=Decimal("50.0"),
        )
        assert again == []
        assert len(await repo.list_system_lines(open_case.id)) == 1
        # ... and on the now-open case it changes nothing: no line, no lift.
        assert len(await repo.list_entries(waiting_case.id, include_frozen=True)) == entry_count
        reread = await repo.get(waiting_case.id)
        assert reread is not None and reread.state == "open"


# ---------------------------------------------------------------------------
# SL-04: the nav_restated writer
# ---------------------------------------------------------------------------


async def test_sl04_nav_restated_writer(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SL-04")
    user_id = await seed_user(superuser_engine, tenant_id)
    equity_id = await seed_book(app_engine, tenant_id, user_id, latest_nav=CALM_NAV)
    d_new, d_old = date(2024, 7, 31), date(2024, 6, 30)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = CaseRepository(session)
        links = CaseInvestmentRepository(session)
        linking = await _manual_case(session, user_id, "linking")
        await links.link(linking.id, investment_id=equity_id, linked_by=user_id, now=_T0)
        waiting = await _manual_case(session, user_id, "waiting")
        await links.link(waiting.id, investment_id=equity_id, linked_by=user_id, now=_T0)
        await repo.wait(waiting.id, wait_until=date(2027, 1, 1), by=user_id, now=_T0)
        unlinked = await _manual_case(session, user_id, "unlinked")
        closed = await _manual_case(session, user_id, "closed")
        await links.link(closed.id, investment_id=equity_id, linked_by=user_id, now=_T0)
        await repo.close(closed.id, closed_by=user_id, closing_note="done", now=_T0)

        changes = [
            (d_old, Decimal("100000"), Decimal("120000")),
            (d_new, None, Decimal("130000")),
        ]
        line_time = _T0 + timedelta(hours=1)
        written = await nav_restated(
            session, investment_id=equity_id, changes=changes, now=line_time
        )
        assert len(written) == 4
        lines = {e.payload["subject"]: e.payload for e in await repo.list_system_lines(linking.id)}
        assert set(lines) == {f"{equity_id}:2024-06-30", f"{equity_id}:2024-07-31"}
        restated = lines[f"{equity_id}:2024-06-30"]
        assert restated["event"] == EVENT_NAV_RESTATED
        assert restated["text"] == "NAV restated for Alpha as of 2024-06-30: €100k → €120k."
        assert restated["change"] == "restated"
        reported = lines[f"{equity_id}:2024-07-31"]
        assert reported["text"] == "NAV reported for Alpha: €130k as of 2024-07-31."
        assert reported["change"] == "new"
        assert reported["currency"] == "EUR"
        assert await repo.list_system_lines(unlinked.id) == []
        assert await repo.list_system_lines(closed.id) == []

        # The waiting case: the first line (as-of order) woke it, the second
        # found it open — one lift, never two (D-CS-B-22). Same-instant lines
        # read back in subject order, then the lift and its snapshot.
        first, second = await repo.list_system_lines(waiting.id)
        assert [first.payload["subject"], second.payload["subject"]] == [
            f"{equity_id}:2024-06-30",
            f"{equity_id}:2024-07-31",
        ]
        entries = await repo.list_entries(waiting.id, include_frozen=True)
        assert [e.kind for e in entries[-4:]] == [
            "system_line",
            "system_line",
            "waiting_lifted",
            "frozen",
        ]
        lifts = [e for e in entries if e.kind == "waiting_lifted"]
        assert len(lifts) == 1
        assert lifts[0].payload["event"]["entry_id"] == str(first.id)
        reread = await repo.get(waiting.id)
        assert reread is not None and (reread.state, reread.wait_until) == ("open", None)

        # Dedup: the same investment and as-of is written once.
        again = await nav_restated(session, investment_id=equity_id, changes=changes, now=_T0)
        assert again == []
        assert len(await repo.list_system_lines(linking.id)) == 2


# ---------------------------------------------------------------------------
# SL-05: the beat seam
# ---------------------------------------------------------------------------


async def _entry_count(session: AsyncSession) -> int:
    return (await session.execute(text("SELECT count(*) FROM case_entries"))).scalar_one()


async def test_sl05_beat_writes_the_falling_edge(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SL-05")
    user_id = await seed_user(superuser_engine, tenant_id)
    equity_id = await seed_book(app_engine, tenant_id, user_id, latest_nav=BREACH_NAV)
    core: Any = SurfacingCore()

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        case = await _finding_case(session, user_id, SAA_SUBJECT)
        manual = await _manual_case(session, user_id)

    # Beat 1: the breach rises — no line (a rising edge is no system line).
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        before = await _entry_count(session)
        result = await run_beat(session, core, llm=_LLM, now=_now())
        assert result.error is None
        assert await _entry_count(session) == before

    # Beat 2: back under the ceiling — the falling edge is one line.
    await set_latest_nav(app_engine, tenant_id, user_id, equity_id, CALM_NAV)
    beat_two = _now()
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        result = await run_beat(session, core, llm=_LLM, now=beat_two)
        assert result.error is None
        repo = CaseRepository(session)
        (line,) = await repo.list_system_lines(case.id)
        assert line.payload["event"] == EVENT_SUBJECT_WITHIN_CEILING
        assert line.payload["subject"] == SAA_SUBJECT
        assert line.payload["text"] == (
            "saa:equities is back within its ceiling: 10.0 % of the 50.0 % ceiling (OK) "
            f"as of {beat_two.date().isoformat()}."
        )
        assert line.payload["status"] == "OK"
        assert await repo.list_system_lines(manual.id) == []
        after_two = await _entry_count(session)

    # Beat 3: calm and unchanged — no edge, no line, no entry at all.
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        result = await run_beat(session, core, llm=_LLM, now=_now())
        assert result.error is None
        assert await _entry_count(session) == after_two


# ---------------------------------------------------------------------------
# SL-09: the P-CS-2c templates
# ---------------------------------------------------------------------------


def _ticket(**overrides: Any) -> TradeTicketDTO:
    """A booked ticket as ``TicketService.book`` hands it to the writer."""
    values: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "ticket_number": 7,
        "kind": "secondary",
        "direction": "sell",
        "status": "booked",
        "investment_id": uuid4(),
        "cash_investment_id": uuid4(),
        "trade_date": date(2026, 8, 31),
        "settlement_date": None,
        "units": None,
        "price_per_unit": None,
        "gross_amount": None,
        "fees": None,
        "taxes": None,
        "net_amount": Decimal("2400000"),
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
        "booked_at": _T0,
        "cancelled_at": None,
        "created_by": uuid4(),
        "created_at": _T0,
        "updated_at": _T0,
    }
    values.update(overrides)
    return TradeTicketDTO(**values)


def test_sl09_ticket_and_job_templates() -> None:
    ticket = _ticket()
    subject, line, payload = ticket_booked_line(ticket, investment_name="Alpha")
    assert subject == str(ticket.id)
    assert line == "Ticket #7 booked: secondary sell Alpha, €2.4M, trade date 2026-08-31."
    assert payload == {
        "ticket_id": str(ticket.id),
        "ticket_number": 7,
        "kind": "secondary",
        "direction": "sell",
        "status": "booked",
        "investment_id": str(ticket.investment_id),
        "investment_name": "Alpha",
        "trade_date": "2026-08-31",
        "net_amount": "2400000",
        "currency": "EUR",
        "booked_at": _T0.isoformat(),
    }

    # The currency is the formatter's prefix — never repeated after the amount.
    _, chf, _ = ticket_booked_line(_ticket(currency="CHF"), investment_name="Alpha")
    assert chf == "Ticket #7 booked: secondary sell Alpha, CHF 2.4M, trade date 2026-08-31."

    # No net amount (a commitment) and no known investment.
    bare = _ticket(kind="commitment", direction="buy", net_amount=None, investment_id=None)
    _, line, payload = ticket_booked_line(bare, investment_name=None)
    assert line == "Ticket #7 booked: commitment buy —, trade date 2026-08-31."
    assert (payload["net_amount"], payload["investment_id"], payload["investment_name"]) == (
        None,
        None,
        None,
    )

    proposal_id = uuid4()
    subject, line, payload = job_requested_line(
        proposal_id=proposal_id, request_text="Check the pacing"
    )
    assert subject == str(proposal_id)
    assert line == "Shirley was asked: “Check the pacing” — working on it."
    assert payload == {"proposal_id": str(proposal_id), "request_text": "Check the pacing"}

    long_request = "x" * 200
    _, line, payload = job_requested_line(proposal_id=proposal_id, request_text=long_request)
    quoted = line.removeprefix("Shirley was asked: “").removesuffix("” — working on it.")
    assert quoted == "x" * 119 + "…"
    assert len(quoted) == 120
    assert payload["request_text"] == long_request

    subject, line, payload = job_landed_line(proposal_id=proposal_id, item_count=1)
    assert subject == str(proposal_id)
    assert line == "Shirley's proposal landed: 1 item to review."
    assert payload == {"proposal_id": str(proposal_id), "item_count": 1}
    _, line, _ = job_landed_line(proposal_id=proposal_id, item_count=3)
    assert line == "Shirley's proposal landed: 3 items to review."


# ---------------------------------------------------------------------------
# SL-10: the ticket_booked writer
# ---------------------------------------------------------------------------


async def test_sl10_ticket_booked_writer(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SL-10")
    user_id = await seed_user(superuser_engine, tenant_id)
    other_tenant = await seed_tenant("SL-10-other")
    other_user = await seed_user(superuser_engine, other_tenant)
    line_time = _T0 + timedelta(hours=1)

    async with tenant_context(app_engine, other_tenant, user_id=other_user) as session:
        foreign = await _manual_case(session, other_user, "another tenant's")

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = CaseRepository(session)
        open_case = await _manual_case(session, user_id, "open")
        waiting = await _manual_case(session, user_id, "waiting")
        await repo.wait(waiting.id, wait_until=date(2027, 1, 1), by=user_id, now=_T0)
        closed = await _manual_case(session, user_id, "closed")
        await repo.close(closed.id, closed_by=user_id, closing_note="done", now=_T0)

        # An open case: one line, stating the booked status.
        ticket = _ticket(tenant_id=tenant_id, case_id=open_case.id)
        line = await ticket_booked(repo, ticket=ticket, investment_name="Alpha", now=line_time)
        assert line is not None
        (stored,) = await repo.list_system_lines(open_case.id)
        assert stored.id == line.id
        assert stored.payload["event"] == EVENT_TICKET_BOOKED
        assert stored.payload["subject"] == str(ticket.id)
        assert stored.payload["status"] == "booked"
        # Dedup on the ticket id.
        assert (
            await ticket_booked(repo, ticket=ticket, investment_name="Alpha", now=line_time) is None
        )
        assert len(await repo.list_system_lines(open_case.id)) == 1

        # A waiting case: the line, then the lift.
        woken = await ticket_booked(
            repo,
            ticket=_ticket(tenant_id=tenant_id, case_id=waiting.id),
            investment_name="Alpha",
            now=line_time,
        )
        assert woken is not None
        await _assert_lifted_by(repo, waiting.id, woken)

        # A closed case and another tenant's case receive nothing.
        for case_id in (closed.id, foreign.id):
            assert (
                await ticket_booked(
                    repo,
                    ticket=_ticket(tenant_id=tenant_id, case_id=case_id),
                    investment_name="Alpha",
                    now=line_time,
                )
                is None
            )
        assert await repo.list_system_lines(closed.id) == []
        # No case at all: nothing to write.
        assert (
            await ticket_booked(repo, ticket=_ticket(), investment_name=None, now=line_time) is None
        )

    async with tenant_context(app_engine, other_tenant, user_id=other_user) as session:
        assert await CaseRepository(session).list_system_lines(foreign.id) == []


# ---------------------------------------------------------------------------
# SL-11: the job_* writers
# ---------------------------------------------------------------------------


async def test_sl11_job_writers(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SL-11")
    user_id = await seed_user(superuser_engine, tenant_id)
    line_time = _T0 + timedelta(hours=1)
    proposal_id = uuid4()

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = CaseRepository(session)
        case = await _manual_case(session, user_id, "asked")

        requested = await job_requested(
            repo, case=case, proposal_id=proposal_id, request_text="Check the pacing", now=_T0
        )
        assert requested is not None
        assert requested.payload["event"] == EVENT_JOB_REQUESTED
        assert (
            await job_requested(
                repo, case=case, proposal_id=proposal_id, request_text="Again", now=line_time
            )
            is None
        )

        landed = await job_landed(
            repo, case=case, proposal_id=proposal_id, item_count=2, now=line_time
        )
        assert landed is not None
        assert landed.payload["event"] == EVENT_JOB_LANDED
        assert landed.payload["text"] == "Shirley's proposal landed: 2 items to review."
        assert (
            await job_landed(repo, case=case, proposal_id=proposal_id, item_count=5, now=line_time)
            is None
        )
        events = [e.payload["event"] for e in await repo.list_system_lines(case.id)]
        assert events == [EVENT_JOB_REQUESTED, EVENT_JOB_LANDED]

        # An asked job is an event on the case: it lifts a waiting case.
        waiting = await _manual_case(session, user_id, "waiting")
        waiting = await repo.wait(waiting.id, wait_until=date(2027, 1, 1), by=user_id, now=_T0)
        woken = await job_requested(
            repo, case=waiting, proposal_id=uuid4(), request_text="Draft a memo", now=line_time
        )
        assert woken is not None
        await _assert_lifted_by(repo, waiting.id, woken)

        # A stale DTO that still says "waiting" never lifts twice: the line
        # lands, the case stays open with one lift.
        stale = replace(waiting, state="waiting")
        late = await job_landed(
            repo, case=stale, proposal_id=uuid4(), item_count=1, now=line_time + timedelta(days=1)
        )
        assert late is not None
        entries = await repo.list_entries(waiting.id)
        assert [e.kind for e in entries].count("waiting_lifted") == 1
        reread = await repo.get(waiting.id)
        assert reread is not None and reread.state == "open"
