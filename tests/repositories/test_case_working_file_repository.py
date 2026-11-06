# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Working-file repository tests against the live compose Postgres.

The eight working-file tables of ADR-0138 (migration b037) are tenant-scoped
and RLS-policed. Every write refuses a closed case and bumps
``cases.updated_at`` (D-CS-B-5); reads work on any state. These tests pin
the per-table vocabularies and shape rules, the immutability rules of kept
payloads (§9) and recorded decisions (§14), the point-ticket link (§5), and
the two shared guards across every writer.

Coverage
--------
* CW-01: threads — positions, update, the ``concluded_at`` rule, status
  vocabulary, soft removal and the removed-inclusive ``get``.
* CW-02: kept — the canonical payload hash, the attachment rule, the
  class / ``via`` vocabularies, payload immutability except
  ``generated_document``, soft removal with the payload still readable
  (D-CS-M-5), a thread of another case refused.
* CW-03: figures — the ``book`` rule, a typed round-trip, hard delete.
* CW-04: conversations — add / update / delete, channel vocabulary,
  ``held_on DESC`` ordering.
* CW-05: points — add / update, done / open, ``next_due``, the ticket link
  once, no delete while linked.
* CW-06: decisions — one draft, option shape, ``chosen_key``, recording, a
  new draft after recording, a recorded row immutable.
* CW-07: investment links — idempotent link, unlink, the open-or-waiting
  case-id read.
* CW-08: proposals — request, land, item-by-item resolution.
* CW-09: the shared guards — every writer refuses a closed case and bumps
  ``updated_at`` on an open one; a waiting case is writable.
* CW-10: cross-tenant invisibility (RLS smoke).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core.exceptions import (
    CaseClosedError,
    CaseDecisionRecorded,
    CaseKeptImmutable,
    CasePointLinked,
    CaseWorkingFileInvalid,
)
from core.repositories import (
    AssetClassRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from core.repositories.case_repository import CaseRepository
from core.repositories.case_working_file_repository import (
    CaseConversationRepository,
    CaseDecisionRepository,
    CaseFigureRepository,
    CaseInvestmentRepository,
    CaseKeptRepository,
    CasePointRepository,
    CaseProposalRepository,
    CaseThreadRepository,
    kept_payload_sha256,
)
from core.repositories.trade_ticket_repository import TradeTicketRepository
from tests._case_record_seed import seed_record_entry

_T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(hours=1)
_T2 = _T0 + timedelta(hours=2)
_T_WRITE = _T0 + timedelta(days=3)


async def _seed_user(app_engine: AsyncEngine, tenant_id, email: str):
    """Insert a user in ``tenant_id`` and return its id (for the case FKs)."""
    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(email=email, password_hash="x" * 8)
    return user.id


async def _open_case(app_engine: AsyncEngine, tenant_id, user_id, title: str = "Case"):
    async with tenant_context(app_engine, tenant_id) as session:
        return await CaseRepository(session).create(
            title=title, opened_by=user_id, opened_actor="pm", now=_T0
        )


async def _seed_investments(app_engine: AsyncEngine, tenant_id, user_id, count: int) -> list:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="cw_class", display_name="CW Class"
        )
        investments = InvestmentRepository(session)
        return [
            (
                await investments.create(
                    name=f"CW Fund {i}",
                    investment_type="private_equity",
                    asset_class_id=asset_class.id,
                    currency="EUR",
                    created_by=user_id,
                )
            ).id
            for i in range(count)
        ]


async def _seed_ticket(app_engine: AsyncEngine, tenant_id, user_id, case_id=None):
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        ticket = await TradeTicketRepository(session).create_draft(
            kind="order",
            direction="buy",
            currency="EUR",
            trade_date=date(2026, 11, 2),
            created_by=user_id,
            case_id=case_id,
            now=_T0,
        )
    return ticket.id


async def _updated_at(app_engine: AsyncEngine, tenant_id, case_id) -> datetime:
    async with tenant_context(app_engine, tenant_id) as session:
        case = await CaseRepository(session).get(case_id)
    assert case is not None
    return case.updated_at


# ---------------------------------------------------------------------------
# CW-01: threads
# ---------------------------------------------------------------------------


async def test_cw01_threads(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CW-01")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cw01.example")
    case = await _open_case(app_engine, tenant_id, user_id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseThreadRepository(session)
        first = await repo.add(case.id, title="  Fee question  ", now=_T1)
        second = await repo.add(case.id, title="FX hypothesis", now=_T1, prose="an idea")
        assert (first.position, second.position) == (1, 2)
        assert first.title == "Fee question"
        assert first.status == "open" and first.prose == ""
        assert second.prose == "an idea"

        edited = await repo.update(first.id, now=_T1, title="Fees", prose="typed")
        assert (edited.title, edited.prose) == ("Fees", "typed")

        concluded = await repo.update(first.id, now=_T2, status="confirmed", conclusion="yes")
        assert concluded.status == "confirmed"
        assert concluded.concluded_at == _T2
        assert concluded.conclusion == "yes"
        reopened = await repo.update(first.id, now=_T2, status="open")
        assert reopened.concluded_at is None

        with pytest.raises(CaseWorkingFileInvalid):
            await repo.update(first.id, now=_T2, status="maybe")
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.add(case.id, title="   ", now=_T2)

        removed = await repo.remove(second.id, now=_T2)
        assert removed.removed_at == _T2
        assert [t.id for t in await repo.list_for_case(case.id)] == [first.id]
        assert [t.id for t in await repo.list_for_case(case.id, include_removed=True)] == [
            first.id,
            second.id,
        ]
        fetched = await repo.get(second.id)
        assert fetched is not None and fetched.removed_at == _T2


# ---------------------------------------------------------------------------
# CW-02: kept artefacts
# ---------------------------------------------------------------------------


async def test_cw02_kept(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CW-02")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cw02.example")
    case = await _open_case(app_engine, tenant_id, user_id)
    other = await _open_case(app_engine, tenant_id, user_id, title="Other")
    payload = {"b": 1, "a": "ü"}

    async with tenant_context(app_engine, tenant_id) as session:
        threads = CaseThreadRepository(session)
        thread = await threads.add(case.id, title="T", now=_T1)
        other_thread = await threads.add(other.id, title="Elsewhere", now=_T1)
        repo = CaseKeptRepository(session)

        chart = await repo.keep(
            case.id,
            artifact="chart_snapshot",
            payload=payload,
            kept_by=user_id,
            via="keep",
            now=_T1,
            thread_id=thread.id,
        )
        # The canonical JSON, by hand: sorted keys, no spaces, UTF-8 as is.
        by_hand = hashlib.sha256('{"a":"ü","b":1}'.encode()).hexdigest()
        assert chart.payload_sha256 == kept_payload_sha256(payload) == by_hand
        assert kept_payload_sha256({"a": "ü", "b": 1}) == by_hand  # key order is irrelevant
        assert chart.payload == payload
        assert chart.kept_at == _T1 and chart.thread_id == thread.id

        with pytest.raises(CaseWorkingFileInvalid):
            await repo.keep(
                case.id, artifact="document", payload={}, kept_by=user_id, via="keep", now=_T1
            )
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.keep(
                case.id,
                artifact="chart_snapshot",
                payload={},
                kept_by=user_id,
                via="keep",
                now=_T1,
                attachment_id=chart.id,
            )
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.keep(
                case.id, artifact="pin", payload={}, kept_by=user_id, via="keep", now=_T1
            )
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.keep(
                case.id,
                artifact="chart_snapshot",
                payload={},
                kept_by=user_id,
                via="email",
                now=_T1,
            )
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.keep(
                case.id,
                artifact="chart_snapshot",
                payload={},
                kept_by=user_id,
                via="keep",
                now=_T1,
                thread_id=other_thread.id,
            )

        with pytest.raises(CaseKeptImmutable):
            await repo.update_generated_document(chart.id, payload={"x": 1}, now=_T2)

        memo = await repo.keep(
            case.id,
            artifact="generated_document",
            payload={"body": "draft"},
            kept_by=user_id,
            via="proposal",
            now=_T1,
        )
        rewritten = await repo.update_generated_document(
            memo.id, payload={"body": "final"}, now=_T2
        )
        assert rewritten.payload == {"body": "final"}
        assert rewritten.payload_sha256 == kept_payload_sha256({"body": "final"})
        assert rewritten.payload_sha256 != memo.payload_sha256

        placed = await repo.update_placement(chart.id, now=_T2, thread_id=None, caption="c")
        assert placed.thread_id is None and placed.caption == "c"
        assert placed.payload_sha256 == chart.payload_sha256

        removed = await repo.remove(chart.id, now=_T2)
        assert removed.removed_at == _T2
        assert chart.id not in {k.id for k in await repo.list_for_case(case.id)}
        # The D-CS-M-5 read: a removed row still reads back with its payload.
        still = await repo.get(chart.id)
        assert still is not None
        assert still.payload == payload and still.removed_at == _T2

        assert not hasattr(repo, "delete"), "case_kept rows are never deleted (ADR-0138 §9)."


# ---------------------------------------------------------------------------
# CW-03: figures
# ---------------------------------------------------------------------------


async def test_cw03_figures(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CW-03")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cw03.example")
    case = await _open_case(app_engine, tenant_id, user_id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseFigureRepository(session)
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.add(
                case.id,
                label="NAV",
                source="book",
                now=_T1,
                value_text="12.3",
                source_ref={"investment_id": "x", "metric": "nav"},
            )
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.add(
                case.id, label="NAV", source="book", now=_T1, source_ref={"metric": "nav"}
            )
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.add(case.id, label="X", source="guess", now=_T1)

        book = await repo.add(
            case.id,
            label="NAV",
            source="book",
            now=_T1,
            source_ref={"investment_id": "x", "metric": "nav"},
        )
        assert book.value_text is None
        typed = await repo.add(
            case.id,
            label="Fee",
            source="typed",
            now=_T1,
            value_text="2 %",
            as_of="2026-09-30",
            note="from the LPA",
        )
        assert (typed.value_text, typed.as_of, typed.note) == ("2 %", "2026-09-30", "from the LPA")
        assert typed.source_ref == {}
        assert typed.position == 2

        # The book rule is checked on the resulting row.
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.update(typed.id, now=_T2, source="book")

        fetched = await repo.get(typed.id)
        assert fetched == typed

        await repo.delete(typed.id, now=_T2)
        assert await repo.get(typed.id) is None
        assert [f.id for f in await repo.list_for_case(case.id)] == [book.id]


# ---------------------------------------------------------------------------
# CW-04: conversations
# ---------------------------------------------------------------------------


async def test_cw04_conversations(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CW-04")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cw04.example")
    case = await _open_case(app_engine, tenant_id, user_id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseConversationRepository(session)
        older = await repo.add(case.id, held_on=date(2026, 10, 1), now=_T1, with_whom="GP")
        newer = await repo.add(
            case.id,
            held_on=date(2026, 10, 15),
            now=_T1,
            channel="meeting",
            what_was_said="they will report",
            what_was_promised="special report",
            duration_minutes=45,
        )
        assert older.channel == "call" and older.what_was_said == ""
        assert newer.duration_minutes == 45

        with pytest.raises(CaseWorkingFileInvalid):
            await repo.add(case.id, held_on=date(2026, 10, 2), now=_T1, channel="fax")
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.update(older.id, now=_T2, channel="fax")

        updated = await repo.update(older.id, now=_T2, what_was_said="all fine", channel="other")
        assert (updated.what_was_said, updated.channel) == ("all fine", "other")

        assert [c.id for c in await repo.list_for_case(case.id)] == [newer.id, older.id]

        await repo.delete(older.id, now=_T2)
        assert await repo.get(older.id) is None
        assert [c.id for c in await repo.list_for_case(case.id)] == [newer.id]


# ---------------------------------------------------------------------------
# CW-05: open points
# ---------------------------------------------------------------------------


async def test_cw05_points(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CW-05")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cw05.example")
    case = await _open_case(app_engine, tenant_id, user_id)
    ticket_id = await _seed_ticket(app_engine, tenant_id, user_id, case_id=case.id)
    other_ticket = await _seed_ticket(app_engine, tenant_id, user_id, case_id=case.id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CasePointRepository(session)
        undated = await repo.add(case.id, text="Ask the GP", now=_T1)
        late = await repo.add(case.id, text="Late", now=_T1, due_on=date(2026, 12, 1))
        early = await repo.add(
            case.id, text="Early", now=_T1, due_on=date(2026, 11, 10), owner_user_id=user_id
        )
        earliest = await repo.add(case.id, text="Earliest", now=_T1, due_on=date(2026, 11, 5))
        assert early.owner_user_id == user_id

        with pytest.raises(CaseWorkingFileInvalid):
            await repo.add(case.id, text="  ", now=_T1)

        updated = await repo.update(undated.id, now=_T2, text="Ask the GP again")
        assert updated.text == "Ask the GP again" and updated.due_on is None

        done = await repo.mark_done(earliest.id, now=_T2)
        assert done.done_at == _T2
        # next_due ignores the done point and the undated one.
        nxt = await repo.next_due(case.id)
        assert nxt is not None and nxt.id == early.id
        reopened = await repo.mark_open(earliest.id, now=_T2)
        assert reopened.done_at is None
        nxt = await repo.next_due(case.id)
        assert nxt is not None and nxt.id == earliest.id

        await repo.mark_done(earliest.id, now=_T2)
        assert [p.id for p in await repo.list_for_case(case.id)] == [
            early.id,
            late.id,
            undated.id,
            earliest.id,
        ]
        assert earliest.id not in {p.id for p in await repo.list_for_case(case.id, open_only=True)}

        linked = await repo.link_ticket(late.id, ticket_id=ticket_id, now=_T2)
        assert linked.ticket_id == ticket_id
        with pytest.raises(CasePointLinked):
            await repo.link_ticket(late.id, ticket_id=other_ticket, now=_T2)
        with pytest.raises(CasePointLinked):
            await repo.delete(late.id, now=_T2)

        await repo.delete(undated.id, now=_T2)
        assert await repo.get(undated.id) is None
        assert await repo.get(late.id) is not None


# ---------------------------------------------------------------------------
# CW-06: decisions
# ---------------------------------------------------------------------------


async def test_cw06_decisions(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CW-06")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cw06.example")
    case = await _open_case(app_engine, tenant_id, user_id)
    options = [{"key": "a", "text": "Coordinate and defer"}, {"key": "b", "text": "Redeem"}]

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseDecisionRepository(session)
        assert await repo.get_draft(case.id) is None
        draft = await repo.upsert_draft(case.id, now=_T1, options=options)
        assert draft.options == options and draft.recorded_at is None
        again = await repo.upsert_draft(
            case.id, now=_T1, chosen_key="a", rationale="wait", decided_by="Committee"
        )
        assert again.id == draft.id
        assert (again.chosen_key, again.rationale, again.decided_by) == ("a", "wait", "Committee")
        count = await session.execute(
            text("SELECT count(*) FROM case_decisions WHERE case_id = :c"), {"c": str(case.id)}
        )
        assert count.scalar_one() == 1

        for bad in (
            "not a list",
            [{"key": "", "text": "x"}],
            [{"key": "a", "text": "x"}, {"key": "a", "text": "y"}],
            [{"text": "no key"}],
        ):
            with pytest.raises(CaseWorkingFileInvalid):
                await repo.upsert_draft(case.id, now=_T1, options=bad)
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.upsert_draft(case.id, now=_T1, chosen_key="z")
        # Options that drop the chosen key are refused on the resulting row.
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.upsert_draft(case.id, now=_T1, options=[{"key": "b", "text": "Redeem"}])

        entry_id = await seed_record_entry(
            session,
            tenant_id=tenant_id,
            case_id=case.id,
            kind="decision_record",
            actor="pm",
            actor_user_id=user_id,
            payload={"chosen": "a"},
            now=_T2,
        )
        recorded = await repo.mark_recorded(
            draft.id, recorded_by=user_id, record_entry_id=entry_id, now=_T2
        )
        assert recorded.recorded_at == _T2 and recorded.record_entry_id == entry_id
        assert await repo.get_draft(case.id) is None
        assert [d.id for d in await repo.list_recorded(case.id)] == [draft.id]

        fresh = await repo.upsert_draft(case.id, now=_T2, options=options)
        assert fresh.id != draft.id and fresh.recorded_at is None

        with pytest.raises(CaseDecisionRecorded):
            await repo.mark_recorded(
                draft.id, recorded_by=user_id, record_entry_id=entry_id, now=_T2
            )
        unchanged = await repo.get(draft.id)
        assert unchanged == recorded


# ---------------------------------------------------------------------------
# CW-07: investment links
# ---------------------------------------------------------------------------


async def test_cw07_investment_links(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CW-07")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cw07.example")
    (investment_id,) = await _seed_investments(app_engine, tenant_id, user_id, 1)
    open_case = await _open_case(app_engine, tenant_id, user_id, "Open")
    waiting_case = await _open_case(app_engine, tenant_id, user_id, "Waiting")
    closed_case = await _open_case(app_engine, tenant_id, user_id, "Closed")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseInvestmentRepository(session)
        first = await repo.link(
            open_case.id, investment_id=investment_id, linked_by=user_id, now=_T1
        )
        second = await repo.link(
            open_case.id, investment_id=investment_id, linked_by=user_id, now=_T2
        )
        assert second == first
        assert len(await repo.list_for_case(open_case.id)) == 1
        for case in (waiting_case, closed_case):
            await repo.link(case.id, investment_id=investment_id, linked_by=user_id, now=_T1)

        cases = CaseRepository(session)
        await cases.wait(waiting_case.id, wait_until=date(2027, 5, 1), by=user_id, now=_T2)
        await cases.close(closed_case.id, closed_by=user_id, closing_note="done", now=_T2)

        assert await repo.list_case_ids_for_investment(investment_id) == [
            open_case.id,
            waiting_case.id,
        ]

        await repo.unlink(open_case.id, investment_id=investment_id, now=_T2)
        assert await repo.list_for_case(open_case.id) == []
        assert await repo.list_case_ids_for_investment(investment_id) == [waiting_case.id]


# ---------------------------------------------------------------------------
# CW-08: proposals
# ---------------------------------------------------------------------------


async def test_cw08_proposals(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CW-08")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cw08.example")
    case = await _open_case(app_engine, tenant_id, user_id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseProposalRepository(session)
        requested = await repo.request(
            case.id, requested_by=user_id, request_text="summarise into the file", now=_T1
        )
        assert requested.landed_at is None and requested.items == []

        with pytest.raises(CaseWorkingFileInvalid):
            await repo.land(requested.id, items=[{"kind": "figure", "status": "accepted"}], now=_T1)

        landed = await repo.land(
            requested.id,
            items=[
                {"kind": "figure", "status": "pending", "label": "Fee"},
                {"kind": "thread", "status": "pending"},
            ],
            now=_T1,
        )
        assert landed.landed_at == _T1 and landed.resolved_at is None
        assert [p.id for p in await repo.list_for_case(case.id, open_only=True)] == [requested.id]

        created = UUID("00000000-0000-0000-0000-0000000000aa")
        first = await repo.resolve_item(
            requested.id, index=0, status="accepted", now=_T2, created_id=created
        )
        assert first.items[0] == {
            "kind": "figure",
            "status": "accepted",
            "label": "Fee",
            "created_id": str(created),
        }
        assert first.resolved_at is None

        with pytest.raises(CaseWorkingFileInvalid):
            await repo.resolve_item(requested.id, index=1, status="maybe", now=_T2)

        done = await repo.resolve_item(requested.id, index=1, status="discarded", now=_T2)
        assert done.items[1]["status"] == "discarded"
        assert done.resolved_at == _T2
        assert await repo.list_for_case(case.id, open_only=True) == []
        assert [p.id for p in await repo.list_for_case(case.id)] == [requested.id]


# ---------------------------------------------------------------------------
# CW-09: the two shared guards, across every writer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _World:
    """One case with a row in every working-file table (and the refs they need)."""

    tenant_id: Any
    user_id: Any
    case_id: Any
    thread_id: Any
    memo_id: Any
    figure_id: Any
    conversation_id: Any
    point_id: Any
    decision_id: Any
    entry_id: Any
    ticket_id: Any
    linked_investment: Any
    free_investment: Any
    landed_proposal: Any
    requested_proposal: Any


async def _build_world(app_engine: AsyncEngine, seed_tenant, label: str, *, state: str) -> _World:
    tenant_id = await seed_tenant(label)
    user_id = await _seed_user(app_engine, tenant_id, f"pm@{label.lower()}.example")
    linked_investment, free_investment = await _seed_investments(app_engine, tenant_id, user_id, 2)
    case = await _open_case(app_engine, tenant_id, user_id)
    ticket_id = await _seed_ticket(app_engine, tenant_id, user_id, case_id=case.id)
    async with tenant_context(app_engine, tenant_id) as session:
        thread = await CaseThreadRepository(session).add(case.id, title="T", now=_T0)
        memo = await CaseKeptRepository(session).keep(
            case.id,
            artifact="generated_document",
            payload={"body": "memo"},
            kept_by=user_id,
            via="proposal",
            now=_T0,
            thread_id=thread.id,
        )
        figure = await CaseFigureRepository(session).add(
            case.id, label="Fee", source="typed", now=_T0, value_text="2 %"
        )
        conversation = await CaseConversationRepository(session).add(
            case.id, held_on=date(2026, 10, 1), now=_T0
        )
        point = await CasePointRepository(session).add(case.id, text="Ask", now=_T0)
        decision = await CaseDecisionRepository(session).upsert_draft(
            case.id, now=_T0, options=[{"key": "a", "text": "Hold"}]
        )
        entry_id = await seed_record_entry(
            session,
            tenant_id=tenant_id,
            case_id=case.id,
            kind="decision_record",
            actor="pm",
            actor_user_id=user_id,
            payload={},
            now=_T0,
        )
        await CaseInvestmentRepository(session).link(
            case.id, investment_id=linked_investment, linked_by=user_id, now=_T0
        )
        proposals = CaseProposalRepository(session)
        landed = await proposals.request(case.id, requested_by=user_id, request_text="one", now=_T0)
        await proposals.land(landed.id, items=[{"kind": "figure", "status": "pending"}], now=_T0)
        requested = await proposals.request(
            case.id, requested_by=user_id, request_text="two", now=_T0
        )
        if state == "closed":
            await CaseRepository(session).close(
                case.id, closed_by=user_id, closing_note="done", now=_T1
            )
        elif state == "waiting":
            await CaseRepository(session).wait(
                case.id, wait_until=date(2027, 1, 1), by=user_id, now=_T1
            )
    return _World(
        tenant_id=tenant_id,
        user_id=user_id,
        case_id=case.id,
        thread_id=thread.id,
        memo_id=memo.id,
        figure_id=figure.id,
        conversation_id=conversation.id,
        point_id=point.id,
        decision_id=decision.id,
        entry_id=entry_id,
        ticket_id=ticket_id,
        linked_investment=linked_investment,
        free_investment=free_investment,
        landed_proposal=landed.id,
        requested_proposal=requested.id,
    )


_Writer = Callable[[AsyncSession, _World, datetime], Awaitable[Any]]

# Every write method of every working-file repository, aimed at a valid
# target in the world above.
_WRITERS: list[tuple[str, _Writer]] = [
    ("thread.add", lambda s, w, n: CaseThreadRepository(s).add(w.case_id, title="New", now=n)),
    (
        "thread.update",
        lambda s, w, n: CaseThreadRepository(s).update(w.thread_id, now=n, prose="p"),
    ),
    ("thread.remove", lambda s, w, n: CaseThreadRepository(s).remove(w.thread_id, now=n)),
    (
        "kept.keep",
        lambda s, w, n: CaseKeptRepository(s).keep(
            w.case_id,
            artifact="consultation",
            payload={"q": "?"},
            kept_by=w.user_id,
            via="keep",
            now=n,
        ),
    ),
    (
        "kept.update_placement",
        lambda s, w, n: CaseKeptRepository(s).update_placement(w.memo_id, now=n, caption="c"),
    ),
    (
        "kept.update_generated_document",
        lambda s, w, n: CaseKeptRepository(s).update_generated_document(
            w.memo_id, payload={"body": "v2"}, now=n
        ),
    ),
    ("kept.remove", lambda s, w, n: CaseKeptRepository(s).remove(w.memo_id, now=n)),
    (
        "figure.add",
        lambda s, w, n: CaseFigureRepository(s).add(w.case_id, label="L", source="external", now=n),
    ),
    (
        "figure.update",
        lambda s, w, n: CaseFigureRepository(s).update(w.figure_id, now=n, note="n"),
    ),
    ("figure.delete", lambda s, w, n: CaseFigureRepository(s).delete(w.figure_id, now=n)),
    (
        "conversation.add",
        lambda s, w, n: CaseConversationRepository(s).add(
            w.case_id, held_on=date(2026, 10, 2), now=n
        ),
    ),
    (
        "conversation.update",
        lambda s, w, n: CaseConversationRepository(s).update(
            w.conversation_id, now=n, with_whom="GP"
        ),
    ),
    (
        "conversation.delete",
        lambda s, w, n: CaseConversationRepository(s).delete(w.conversation_id, now=n),
    ),
    ("point.add", lambda s, w, n: CasePointRepository(s).add(w.case_id, text="P", now=n)),
    ("point.update", lambda s, w, n: CasePointRepository(s).update(w.point_id, now=n, text="Q")),
    ("point.mark_done", lambda s, w, n: CasePointRepository(s).mark_done(w.point_id, now=n)),
    ("point.mark_open", lambda s, w, n: CasePointRepository(s).mark_open(w.point_id, now=n)),
    (
        "point.link_ticket",
        lambda s, w, n: CasePointRepository(s).link_ticket(
            w.point_id, ticket_id=w.ticket_id, now=n
        ),
    ),
    ("point.delete", lambda s, w, n: CasePointRepository(s).delete(w.point_id, now=n)),
    (
        "decision.upsert_draft",
        lambda s, w, n: CaseDecisionRepository(s).upsert_draft(w.case_id, now=n, rationale="r"),
    ),
    (
        "decision.mark_recorded",
        lambda s, w, n: CaseDecisionRepository(s).mark_recorded(
            w.decision_id, recorded_by=w.user_id, record_entry_id=w.entry_id, now=n
        ),
    ),
    (
        "investment.link",
        lambda s, w, n: CaseInvestmentRepository(s).link(
            w.case_id, investment_id=w.free_investment, linked_by=w.user_id, now=n
        ),
    ),
    (
        "investment.unlink",
        lambda s, w, n: CaseInvestmentRepository(s).unlink(
            w.case_id, investment_id=w.linked_investment, now=n
        ),
    ),
    (
        "proposal.request",
        lambda s, w, n: CaseProposalRepository(s).request(
            w.case_id, requested_by=w.user_id, request_text="r", now=n
        ),
    ),
    (
        "proposal.land",
        lambda s, w, n: CaseProposalRepository(s).land(
            w.requested_proposal, items=[{"kind": "thread", "status": "pending"}], now=n
        ),
    ),
    (
        "proposal.resolve_item",
        lambda s, w, n: CaseProposalRepository(s).resolve_item(
            w.landed_proposal, index=0, status="accepted", now=n
        ),
    ),
]
_WRITER_IDS = [name for name, _ in _WRITERS]
_WRITER_FNS = [fn for _, fn in _WRITERS]


@pytest.mark.parametrize("writer", _WRITER_FNS, ids=_WRITER_IDS)
async def test_cw09_closed_case_refuses_every_write(
    app_engine: AsyncEngine, seed_tenant, writer: _Writer
) -> None:
    world = await _build_world(app_engine, seed_tenant, "CW-09a", state="closed")
    async with tenant_context(app_engine, world.tenant_id) as session:
        with pytest.raises(CaseClosedError):
            await writer(session, world, _T_WRITE)
    assert await _updated_at(app_engine, world.tenant_id, world.case_id) == _T1


@pytest.mark.parametrize("writer", _WRITER_FNS, ids=_WRITER_IDS)
async def test_cw09_open_case_write_bumps_updated_at(
    app_engine: AsyncEngine, seed_tenant, writer: _Writer
) -> None:
    world = await _build_world(app_engine, seed_tenant, "CW-09b", state="open")
    assert await _updated_at(app_engine, world.tenant_id, world.case_id) == _T0
    async with tenant_context(app_engine, world.tenant_id) as session:
        await writer(session, world, _T_WRITE)
    assert await _updated_at(app_engine, world.tenant_id, world.case_id) == _T_WRITE


async def test_cw09_waiting_case_is_writable(app_engine: AsyncEngine, seed_tenant) -> None:
    world = await _build_world(app_engine, seed_tenant, "CW-09c", state="waiting")
    async with tenant_context(app_engine, world.tenant_id) as session:
        case = await CaseRepository(session).get(world.case_id)
        assert case is not None and case.state == "waiting"
        thread = await CaseThreadRepository(session).add(world.case_id, title="New", now=_T_WRITE)
        await CaseFigureRepository(session).update(world.figure_id, now=_T_WRITE, note="n")
    assert thread.position == 2
    assert await _updated_at(app_engine, world.tenant_id, world.case_id) == _T_WRITE


# ---------------------------------------------------------------------------
# CW-10: cross-tenant invisibility (RLS smoke)
# ---------------------------------------------------------------------------


async def test_cw10_cross_tenant_invisibility(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")
    user_a = await _seed_user(app_engine, tenant_a, "pm@a-cw.example")
    case = await _open_case(app_engine, tenant_a, user_a)

    async with tenant_context(app_engine, tenant_a) as session:
        thread = await CaseThreadRepository(session).add(case.id, title="A-only", now=_T1)

    async with tenant_context(app_engine, tenant_b) as session:
        repo = CaseThreadRepository(session)
        assert await repo.get(thread.id) is None
        assert await repo.list_for_case(case.id) == []
        count = await session.execute(text("SELECT count(*) FROM case_threads"))
        assert count.scalar_one() == 0
        # A write aimed at another tenant's thread finds nothing.
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.update(thread.id, now=_T2, prose="x")
