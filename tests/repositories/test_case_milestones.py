# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Milestone tests against the live compose Postgres (ADR-0138 §2, P-CS-1b).

Every milestone of a case — ``opened`` · ``waiting_set`` · ``waiting_lifted``
· ``handed_over`` · ``decision_record`` · ``closed`` — is written as a
milestone entry plus a ``frozen`` whole-file snapshot in one transaction,
with its state or owner transition. The ``frozen`` rows are hidden from
:meth:`CaseRepository.list_entries` by default and fetched by milestone
(D-CS-B-6).

Coverage
--------
* CM-01: ``create`` writes ``opened`` + ``frozen``; the read side; ``stand``.
* CM-02: ``wait`` / ``lift_wait`` — the three lift reasons and their actor
  rules, the state guards, the record sequence.
* CM-03: ``hand_over`` — owner, ``opened_by`` history, payload, guards.
* CM-04: ``record_decision`` — the D-CS-B-10 guards, the order inside the
  transaction, more than one recording (D-CS-M-3), immutability.
* CM-05: ``close`` — the closing pair, ``stand`` and ``book_values`` in the
  snapshot, the waiting guard.
* CM-06: D-CS-M-5 — a freeze made before a removal still resolves its
  artefact; ``generated_document`` by value, the rest by reference.
* CM-07: atomicity — a failing snapshot rolls back the whole milestone.
* CM-08: snapshot content of a full file, field sets, JSON round-trip.
* CM-09: the read side — non-milestone ids, the opening snapshot,
  cross-tenant invisibility.
* CM-10: the lift race guard (D-CS-B-72) — a lift whose case stopped
  waiting behind its read raises and writes no milestone.
* CM-11: snapshot fidelity (D-CS-B-100) — a live kept row of a removed
  thread is frozen at file level, as the live file shows it.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import (
    CaseClosedError,
    CaseStateInvalid,
    CaseWorkingFileInvalid,
)
from core.repositories import (
    AssetClassRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from core.repositories.case_repository import CaseEntryDTO, CaseRepository
from core.repositories.case_snapshot import snapshot_kept_refs
from core.repositories.case_working_file_repository import (
    CaseConversationRepository,
    CaseDecisionRepository,
    CaseFigureRepository,
    CaseInvestmentRepository,
    CaseKeptRepository,
    CasePointRepository,
    CaseThreadRepository,
    kept_payload_sha256,
)
from tests._case_record_seed import seed_record_entry

_T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
_MIN = timedelta(minutes=1)

_KEPT_REF_FIELDS = {
    "kept_id",
    "artifact",
    "caption",
    "comment",
    "payload_sha256",
    "attachment_id",
    "kept_by",
    "via",
    "kept_at",
    "position",
}
_CASE_FIELDS = {
    "id",
    "case_number",
    "title",
    "description",
    "state",
    "framing",
    "counterparty",
    "due_on",
    "wait_until",
    "owner_user_id",
    "opened_by",
    "opened_at",
    "finding_id",
    "closed_by",
    "closed_at",
    "closing_note",
}


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


def _kinds(entries: list[CaseEntryDTO]) -> list[str]:
    return [entry.kind for entry in entries]


# ---------------------------------------------------------------------------
# CM-01: create writes opened + frozen
# ---------------------------------------------------------------------------


async def test_cm01_create_writes_opened_and_frozen(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CM-01")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cm01.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        case = await repo.create(title="Opening", opened_by=user_id, opened_actor="pm", now=_T0)

        entries = await repo.list_entries(case.id)
        assert _kinds(entries) == ["opened"]
        assert _kinds(await repo.list_entries(case.id, include_frozen=True)) == [
            "opened",
            "frozen",
        ]

        frozen = await repo.get_frozen_for(entries[0].id)
        assert frozen is not None
        assert frozen.kind == "frozen"
        assert (frozen.actor, frozen.actor_user_id) == ("system", None)
        assert frozen.created_at == _T0
        payload = frozen.payload
        assert payload["milestone_entry_id"] == str(entries[0].id)
        assert payload["milestone_kind"] == "opened"
        assert payload["snapshot_version"] == 1
        assert datetime.fromisoformat(payload["frozen_at"]) == _T0
        file = payload["file"]
        assert file["snapshot_version"] == 1
        assert file["case"]["title"] == "Opening"
        assert file["case"]["state"] == "open"
        assert file["where_we_stand"] is None
        for key in ("investments", "threads", "kept", "figures", "conversations", "open_points"):
            assert file[key] == [], key
        assert file["decisions"] == {"recorded": [], "draft": None}

        with_stand = await repo.create(
            title="With stand",
            opened_by=user_id,
            opened_actor="pm",
            now=_T0 + _MIN,
            stand={"x": 1},
        )
        snap = await repo.latest_frozen(with_stand.id)
        assert snap is not None
        assert snap.payload["file"]["where_we_stand"] == {"x": 1}


# ---------------------------------------------------------------------------
# CM-02: wait / lift_wait
# ---------------------------------------------------------------------------


async def test_cm02_wait_and_lift_wait(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CM-02")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cm02.example")
    until = date(2026, 12, 1)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        case = await repo.create(title="Wait", opened_by=user_id, opened_actor="pm", now=_T0)

        waiting = await repo.wait(case.id, wait_until=until, by=user_id, now=_T0 + _MIN, note="LP")
        assert (waiting.state, waiting.wait_until) == ("waiting", until)
        set_entry = (await repo.list_entries(case.id))[-1]
        assert set_entry.kind == "waiting_set"
        assert (set_entry.actor, set_entry.actor_user_id) == ("pm", user_id)
        assert set_entry.payload == {"wait_until": "2026-12-01", "note": "LP"}
        set_frozen = await repo.get_frozen_for(set_entry.id)
        assert set_frozen is not None
        assert set_frozen.payload["file"]["case"]["state"] == "waiting"
        assert set_frozen.payload["file"]["case"]["wait_until"] == "2026-12-01"

        with pytest.raises(CaseStateInvalid):
            await repo.wait(case.id, wait_until=until, by=user_id, now=_T0 + 2 * _MIN)

        lifted = await repo.lift_wait(case.id, by=user_id, now=_T0 + 3 * _MIN)
        assert (lifted.state, lifted.wait_until) == ("open", None)
        lift_entry = (await repo.list_entries(case.id))[-1]
        assert lift_entry.kind == "waiting_lifted"
        assert (lift_entry.actor, lift_entry.actor_user_id) == ("pm", user_id)
        assert lift_entry.payload == {"reason": "manual", "wait_until": "2026-12-01", "event": None}

        await repo.wait(case.id, wait_until=until, by=user_id, now=_T0 + 4 * _MIN)
        await repo.lift_wait(case.id, reason="date", now=_T0 + 5 * _MIN)
        by_date = (await repo.list_entries(case.id))[-1]
        assert (by_date.actor, by_date.actor_user_id) == ("system", None)
        assert by_date.payload["reason"] == "date"

        await repo.wait(case.id, wait_until=until, by=user_id, now=_T0 + 6 * _MIN)
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.lift_wait(case.id, reason="date", by=user_id, now=_T0 + 7 * _MIN)
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.lift_wait(case.id, reason="event", now=_T0 + 7 * _MIN)
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.lift_wait(case.id, reason="whim", by=user_id, now=_T0 + 7 * _MIN)
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.lift_wait(case.id, now=_T0 + 7 * _MIN)  # manual without by
        await repo.lift_wait(
            case.id, reason="event", event={"event": "ticket_booked"}, now=_T0 + 8 * _MIN
        )
        by_event = (await repo.list_entries(case.id))[-1]
        assert (by_event.actor, by_event.actor_user_id) == ("system", None)
        assert by_event.payload["event"] == {"event": "ticket_booked"}

        with pytest.raises(CaseStateInvalid):
            await repo.lift_wait(case.id, by=user_id, now=_T0 + 9 * _MIN)

        # A fresh case through one wait and one lift: the record in full.
        other = await repo.create(title="Seq", opened_by=user_id, opened_actor="pm", now=_T0)
        await repo.wait(other.id, wait_until=until, by=user_id, now=_T0 + _MIN)
        await repo.lift_wait(other.id, by=user_id, now=_T0 + 2 * _MIN)
        assert _kinds(await repo.list_entries(other.id, include_frozen=True)) == [
            "opened",
            "frozen",
            "waiting_set",
            "frozen",
            "waiting_lifted",
            "frozen",
        ]


# ---------------------------------------------------------------------------
# CM-03: hand_over
# ---------------------------------------------------------------------------


async def test_cm03_hand_over(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CM-03")
    pm1 = await _seed_user(app_engine, tenant_id, "pm1@cm03.example")
    pm2 = await _seed_user(app_engine, tenant_id, "pm2@cm03.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        case = await repo.create(title="Hand", opened_by=pm1, opened_actor="pm", now=_T0)

        handed = await repo.hand_over(
            case.id, to_user_id=pm2, by=pm1, now=_T0 + _MIN, note="holiday"
        )
        assert (handed.owner_user_id, handed.opened_by) == (pm2, pm1)
        entry = (await repo.list_entries(case.id))[-1]
        assert entry.kind == "handed_over"
        assert (entry.actor, entry.actor_user_id) == ("pm", pm1)
        assert entry.payload == {
            "from_user_id": str(pm1),
            "to_user_id": str(pm2),
            "note": "holiday",
        }
        frozen = await repo.get_frozen_for(entry.id)
        assert frozen is not None
        assert frozen.payload["file"]["case"]["owner_user_id"] == str(pm2)
        assert frozen.payload["file"]["case"]["opened_by"] == str(pm1)

        with pytest.raises(CaseWorkingFileInvalid):
            await repo.hand_over(case.id, to_user_id=pm2, by=pm2, now=_T0 + 2 * _MIN)

        assert [x.id for x in await repo.list_cases(owner_user_id=pm2)] == [case.id]
        assert await repo.list_cases(owner_user_id=pm1) == []

        await repo.close(case.id, closed_by=pm2, closing_note="done", now=_T0 + 3 * _MIN)
        with pytest.raises(CaseClosedError):
            await repo.hand_over(case.id, to_user_id=pm1, by=pm2, now=_T0 + 4 * _MIN)


# ---------------------------------------------------------------------------
# CM-04: record_decision
# ---------------------------------------------------------------------------


async def test_cm04_record_decision(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CM-04")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cm04.example")
    options = [{"key": "a", "text": "Hold"}, {"key": "b", "text": "Sell"}]
    t_record = _T0 + 10 * _MIN

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        decisions = CaseDecisionRepository(session)
        case = await repo.create(title="Decide", opened_by=user_id, opened_actor="pm", now=_T0)

        with pytest.raises(CaseWorkingFileInvalid) as no_draft:
            await repo.record_decision(case.id, recorded_by=user_id, now=t_record)
        assert no_draft.value.field == "decision"

        await decisions.upsert_draft(case.id, now=_T0 + _MIN, options=options, rationale="why")
        with pytest.raises(CaseWorkingFileInvalid) as no_choice:
            await repo.record_decision(case.id, recorded_by=user_id, now=t_record)
        assert no_choice.value.field == "chosen_key"

        await decisions.upsert_draft(case.id, now=_T0 + 2 * _MIN, chosen_key="b", rationale="  ")
        with pytest.raises(CaseWorkingFileInvalid) as no_rationale:
            await repo.record_decision(case.id, recorded_by=user_id, now=t_record)
        assert no_rationale.value.field == "rationale"

        draft = await decisions.upsert_draft(
            case.id,
            now=_T0 + 3 * _MIN,
            rationale="Exit before the step-down.",
            decided_by="IC",
            decided_at=date(2026, 10, 30),
        )
        entry = await repo.record_decision(case.id, recorded_by=user_id, now=t_record)
        assert entry.kind == "decision_record"
        assert (entry.actor, entry.actor_user_id) == ("pm", user_id)
        assert entry.payload == {
            "decision_id": str(draft.id),
            "options": options,
            "chosen_key": "b",
            "chosen_text": "Sell",
            "rationale": "Exit before the step-down.",
            "decided_by": "IC",
            "decided_at": "2026-10-30",
        }
        first = await decisions.get(draft.id)
        assert first is not None
        assert (first.record_entry_id, first.recorded_by, first.recorded_at) == (
            entry.id,
            user_id,
            t_record,
        )
        assert await decisions.get_draft(case.id) is None
        current = await repo.get(case.id)
        assert current is not None and current.updated_at == t_record

        frozen = await repo.get_frozen_for(entry.id)
        assert frozen is not None
        snap_decisions = frozen.payload["file"]["decisions"]
        assert snap_decisions["draft"] is None
        assert [d["id"] for d in snap_decisions["recorded"]] == [str(draft.id)]
        assert snap_decisions["recorded"][0]["record_entry_id"] == str(entry.id)

        # D-CS-M-3: a second decision is its own milestone pair.
        await decisions.upsert_draft(
            case.id, now=_T0 + 20 * _MIN, options=options, chosen_key="a", rationale="Rethink"
        )
        second = await repo.record_decision(case.id, recorded_by=user_id, now=_T0 + 30 * _MIN)
        recorded = await decisions.list_recorded(case.id)
        assert [d.record_entry_id for d in recorded] == [entry.id, second.id]
        assert _kinds(await repo.list_entries(case.id, include_frozen=True)) == [
            "opened",
            "frozen",
            "decision_record",
            "frozen",
            "decision_record",
            "frozen",
        ]
        assert await decisions.get(draft.id) == first


# ---------------------------------------------------------------------------
# CM-05: close
# ---------------------------------------------------------------------------


async def test_cm05_close_writes_the_closing_pair(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CM-05")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cm05.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        figures = CaseFigureRepository(session)
        case = await repo.create(title="Close", opened_by=user_id, opened_actor="pm", now=_T0)
        book = await figures.add(
            case.id,
            label="NAV",
            source="book",
            now=_T0 + _MIN,
            source_ref={"investment_id": "x", "metric": "nav"},
        )
        typed = await figures.add(
            case.id, label="Fee", source="typed", now=_T0 + _MIN, value_text="2 %"
        )
        await repo.close(
            case.id,
            closed_by=user_id,
            closing_note="  Sold.  ",
            now=_T0 + 2 * _MIN,
            stand={"line": "sold"},
            book_values={
                str(book.id): {"value_text": "12.3m", "as_of": "2026-09-30"},
                str(typed.id): {"value_text": "ignored", "as_of": "ignored"},
            },
        )
        entries = await repo.list_entries(case.id, include_frozen=True)
        assert _kinds(entries)[-2:] == ["closed", "frozen"]
        latest = await repo.latest_frozen(case.id)
        assert latest is not None and latest.id == entries[-1].id
        assert latest.payload["milestone_entry_id"] == str(entries[-2].id)
        assert latest.payload["milestone_kind"] == "closed"
        file = latest.payload["file"]
        assert file["case"]["state"] == "closed"
        assert file["case"]["closing_note"] == "Sold."
        assert file["case"]["closed_by"] == str(user_id)
        assert file["where_we_stand"] == {"line": "sold"}
        by_id = {figure["id"]: figure for figure in file["figures"]}
        assert (by_id[str(book.id)]["value_text"], by_id[str(book.id)]["as_of"]) == (
            "12.3m",
            "2026-09-30",
        )
        assert (by_id[str(typed.id)]["value_text"], by_id[str(typed.id)]["as_of"]) == ("2 %", None)
        assert _kinds(await repo.list_entries(case.id)) == ["opened", "closed"]

        waiting = await repo.create(title="Wait", opened_by=user_id, opened_actor="pm", now=_T0)
        await repo.wait(waiting.id, wait_until=date(2027, 1, 1), by=user_id, now=_T0 + _MIN)
        with pytest.raises(CaseStateInvalid):
            await repo.close(waiting.id, closed_by=user_id, closing_note="done", now=_T0 + 2 * _MIN)


# ---------------------------------------------------------------------------
# CM-06: D-CS-M-5 — a freeze before a removal still resolves its artefact
# ---------------------------------------------------------------------------


async def test_cm06_freeze_before_removal_still_resolves(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("CM-06")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cm06.example")
    chart_payload = {"spec": {"data": [1, 2]}, "title": "NAV"}
    doc_payload = {"markdown": "# Memo"}

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        kept_repo = CaseKeptRepository(session)
        case = await repo.create(title="Keep", opened_by=user_id, opened_actor="pm", now=_T0)
        thread = await CaseThreadRepository(session).add(case.id, title="T", now=_T0)
        chart = await kept_repo.keep(
            case.id,
            artifact="chart_snapshot",
            payload=chart_payload,
            kept_by=user_id,
            via="keep",
            now=_T0 + _MIN,
            thread_id=thread.id,
        )
        memo = await kept_repo.keep(
            case.id,
            artifact="generated_document",
            payload=doc_payload,
            kept_by=user_id,
            via="keep",
            now=_T0 + _MIN,
        )

        await repo.wait(case.id, wait_until=date(2027, 1, 1), by=user_id, now=_T0 + 2 * _MIN)
        f1 = await repo.latest_frozen(case.id)
        assert f1 is not None and f1.payload["milestone_kind"] == "waiting_set"

        await kept_repo.remove(chart.id, now=_T0 + 3 * _MIN)
        await kept_repo.remove(memo.id, now=_T0 + 3 * _MIN)
        await repo.lift_wait(case.id, by=user_id, now=_T0 + 4 * _MIN)
        f2 = await repo.latest_frozen(case.id)
        assert f2 is not None and f2.payload["milestone_kind"] == "waiting_lifted"

        refs = {ref["kept_id"]: ref for ref in snapshot_kept_refs(f1.payload["file"])}
        chart_ref = refs[str(chart.id)]
        assert chart_ref["payload_sha256"] == chart.payload_sha256
        assert "payload" not in chart_ref
        assert set(chart_ref) == _KEPT_REF_FIELDS
        assert refs[str(memo.id)]["payload"] == doc_payload
        assert set(refs[str(memo.id)]) == _KEPT_REF_FIELDS | {"payload"}

        resolved = await kept_repo.get(chart.id)
        assert resolved is not None and resolved.removed_at is not None
        assert kept_payload_sha256(resolved.payload) == chart_ref["payload_sha256"]

        assert snapshot_kept_refs(f2.payload["file"]) == []


# ---------------------------------------------------------------------------
# CM-07: atomicity
# ---------------------------------------------------------------------------


async def test_cm07_failed_snapshot_rolls_back_the_milestone(
    app_engine: AsyncEngine, seed_tenant, monkeypatch
) -> None:
    tenant_id = await seed_tenant("CM-07")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cm07.example")
    case = await _open_case(app_engine, tenant_id, user_id)

    async def _boom(*args: Any, **kwargs: Any) -> dict:
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr("core.repositories.case_repository.build_file_snapshot", _boom)

    with pytest.raises(RuntimeError):
        async with tenant_context(app_engine, tenant_id) as session:
            await CaseRepository(session).wait(
                case.id, wait_until=date(2027, 1, 1), by=user_id, now=_T0 + _MIN
            )

    async def _count_cases() -> int:
        async with tenant_context(app_engine, tenant_id) as session:
            result = await session.execute(text("SELECT count(*) FROM cases"))
            return int(result.scalar_one())

    before = await _count_cases()
    with pytest.raises(RuntimeError):
        async with tenant_context(app_engine, tenant_id) as session:
            await CaseRepository(session).create(
                title="Never", opened_by=user_id, opened_actor="pm", now=_T0
            )
    monkeypatch.undo()

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        current = await repo.get(case.id)
        assert current is not None
        assert (current.state, current.wait_until) == ("open", None)
        assert _kinds(await repo.list_entries(case.id, include_frozen=True)) == [
            "opened",
            "frozen",
        ]
        never = await session.execute(text("SELECT count(*) FROM cases WHERE title = 'Never'"))
        assert never.scalar_one() == 0
    assert await _count_cases() == before == 1


# ---------------------------------------------------------------------------
# CM-08: snapshot content of a full file
# ---------------------------------------------------------------------------


def _assert_plain(value: Any, path: str = "file") -> None:
    """Fail if any value in the snapshot is not plain JSON."""
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str), path
            _assert_plain(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_plain(item, f"{path}[{index}]")
    else:
        assert value is None or isinstance(value, (str, int, float, bool)), path


def _is_iso(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


async def test_cm08_snapshot_reproduces_the_full_file(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CM-08")
    pm1 = await _seed_user(app_engine, tenant_id, "pm1@cm08.example")
    pm2 = await _seed_user(app_engine, tenant_id, "pm2@cm08.example")
    async with tenant_context(app_engine, tenant_id, user_id=pm1) as session:
        asset_class = await AssetClassRepository(session).create(
            code="cm_class", display_name="CM Class"
        )
        investment_ids = [
            (
                await InvestmentRepository(session).create(
                    name=f"CM Fund {i}",
                    investment_type="private_equity",
                    asset_class_id=asset_class.id,
                    currency="EUR",
                    created_by=pm1,
                )
            ).id
            for i in range(2)
        ]

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        case = await repo.create(
            title="Full",
            opened_by=pm1,
            opened_actor="pm",
            now=_T0,
            description="Should we sell?",
            counterparty="GP",
            due_on=date(2026, 12, 31),
            framing="question",
        )
        for i, investment_id in enumerate(investment_ids):
            await CaseInvestmentRepository(session).link(
                case.id, investment_id=investment_id, linked_by=pm1, now=_T0 + i * _MIN
            )
        threads = CaseThreadRepository(session)
        t1 = await threads.add(case.id, title="Valuation", now=_T0, prose="NAV moved")
        t2 = await threads.add(case.id, title="Fees", now=_T0)
        t3 = await threads.add(case.id, title="Gone", now=_T0)
        await threads.update(t2.id, now=_T0 + _MIN, status="parked", conclusion="Not now")
        await threads.remove(t3.id, now=_T0 + _MIN)
        kept = CaseKeptRepository(session)
        k1 = await kept.keep(
            case.id,
            artifact="chart_snapshot",
            payload={"c": 1},
            kept_by=pm1,
            via="keep",
            now=_T0,
            thread_id=t1.id,
            caption="NAV chart",
        )
        k2 = await kept.keep(
            case.id,
            artifact="consultation",
            payload={"q": "?"},
            kept_by=pm1,
            via="shirley",
            now=_T0,
            thread_id=t2.id,
            comment="useful",
        )
        k3 = await kept.keep(
            case.id,
            artifact="generated_document",
            payload={"markdown": "memo"},
            kept_by=pm1,
            via="keep",
            now=_T0,
        )
        figures = CaseFigureRepository(session)
        book = await figures.add(
            case.id,
            label="NAV",
            source="book",
            now=_T0,
            source_ref={"investment_id": str(investment_ids[0]), "metric": "nav"},
        )
        typed = await figures.add(
            case.id, label="Fee", source="typed", now=_T0, value_text="2 %", as_of="2026-09-30"
        )
        conversation = await CaseConversationRepository(session).add(
            case.id,
            held_on=date(2026, 10, 1),
            now=_T0,
            with_whom="GP",
            what_was_said="Exit 2027",
            duration_minutes=30,
        )
        points = CasePointRepository(session)
        p_open = await points.add(case.id, text="Ask GP", now=_T0, due_on=date(2026, 11, 15))
        p_done = await points.add(case.id, text="Read LPA", now=_T0, owner_user_id=pm2)
        await points.mark_done(p_done.id, now=_T0 + _MIN)

        await repo.hand_over(case.id, to_user_id=pm2, by=pm1, now=_T0 + 5 * _MIN)
        handed = (await repo.list_entries(case.id))[-1]
        frozen = await repo.get_frozen_for(handed.id)
        assert frozen is not None

    file = frozen.payload["file"]
    _assert_plain(file)
    assert json.loads(json.dumps(file)) == file

    assert set(file) == {
        "snapshot_version",
        "case",
        "where_we_stand",
        "investments",
        "threads",
        "kept",
        "figures",
        "conversations",
        "open_points",
        "decisions",
    }
    snap_case = file["case"]
    assert set(snap_case) == _CASE_FIELDS
    assert snap_case["id"] == str(case.id)
    assert snap_case["owner_user_id"] == str(pm2)
    assert snap_case["due_on"] == "2026-12-31"
    assert datetime.fromisoformat(snap_case["opened_at"]) == _T0
    assert (snap_case["framing"], snap_case["counterparty"]) == ("question", "GP")

    assert [i["investment_id"] for i in file["investments"]] == [str(i) for i in investment_ids]
    assert all(set(i) == {"investment_id", "linked_by", "linked_at"} for i in file["investments"])

    thread_fields = {
        "id",
        "position",
        "title",
        "status",
        "prose",
        "conclusion",
        "created_at",
        "concluded_at",
        "kept",
    }
    assert [t["id"] for t in file["threads"]] == [str(t1.id), str(t2.id)]  # t3 removed
    assert all(set(t) == thread_fields for t in file["threads"])
    snap_t1, snap_t2 = file["threads"]
    assert snap_t1["prose"] == "NAV moved"
    assert (snap_t2["status"], snap_t2["conclusion"]) == ("parked", "Not now")
    assert _is_iso(snap_t2["concluded_at"])
    assert [k["kept_id"] for k in snap_t1["kept"]] == [str(k1.id)]
    assert snap_t1["kept"][0]["caption"] == "NAV chart"
    assert [k["kept_id"] for k in snap_t2["kept"]] == [str(k2.id)]
    assert snap_t2["kept"][0]["comment"] == "useful"
    assert [k["kept_id"] for k in file["kept"]] == [str(k3.id)]
    assert file["kept"][0]["payload"] == {"markdown": "memo"}
    assert len(snapshot_kept_refs(file)) == 3
    assert all(
        set(ref) - {"payload"} == _KEPT_REF_FIELDS and _is_iso(ref["kept_at"])
        for ref in snapshot_kept_refs(file)
    )

    figure_fields = {
        "id",
        "position",
        "label",
        "source",
        "value_text",
        "as_of",
        "note",
        "source_ref",
    }
    assert [f["id"] for f in file["figures"]] == [str(book.id), str(typed.id)]
    assert all(set(f) == figure_fields for f in file["figures"])
    assert file["figures"][0]["value_text"] is None  # no book_values injected
    assert file["figures"][1]["value_text"] == "2 %"

    conv_fields = {
        "id",
        "position",
        "held_on",
        "channel",
        "with_whom",
        "what_was_said",
        "what_was_promised",
        "duration_minutes",
    }
    (snap_conv,) = file["conversations"]
    assert set(snap_conv) == conv_fields
    assert (snap_conv["id"], snap_conv["held_on"]) == (str(conversation.id), "2026-10-01")

    point_fields = {"id", "position", "text", "owner_user_id", "due_on", "done_at", "ticket_id"}
    assert [p["id"] for p in file["open_points"]] == [str(p_open.id), str(p_done.id)]
    assert all(set(p) == point_fields for p in file["open_points"])
    assert file["open_points"][0]["due_on"] == "2026-11-15"
    assert _is_iso(file["open_points"][1]["done_at"])
    assert file["open_points"][1]["owner_user_id"] == str(pm2)

    assert file["decisions"] == {"recorded": [], "draft": None}


# ---------------------------------------------------------------------------
# CM-09: the read side
# ---------------------------------------------------------------------------


async def test_cm09_read_side(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant("CM-09a")
    tenant_b = await seed_tenant("CM-09b")
    user_a = await _seed_user(app_engine, tenant_a, "pm@cm09a.example")

    async with tenant_context(app_engine, tenant_a) as session:
        repo = CaseRepository(session)
        case = await repo.create(title="Read", opened_by=user_a, opened_actor="pm", now=_T0)
        note_id = await seed_record_entry(
            session,
            tenant_id=tenant_a,
            case_id=case.id,
            kind="note",
            actor="pm",
            actor_user_id=user_a,
            payload={},
            now=_T0 + _MIN,
        )
        assert await repo.get_frozen_for(note_id) is None
        (opened,) = [e for e in await repo.list_entries(case.id) if e.kind == "opened"]
        latest = await repo.latest_frozen(case.id)
        assert latest is not None
        assert latest.payload["milestone_entry_id"] == str(opened.id)
        assert latest.payload["milestone_kind"] == "opened"
        assert await repo.get_frozen_for(opened.id) == latest

    async with tenant_context(app_engine, tenant_b) as session:
        repo_b = CaseRepository(session)
        assert await repo_b.get_frozen_for(opened.id) is None
        assert await repo_b.latest_frozen(case.id) is None
        assert await repo_b.list_entries(case.id, include_frozen=True) == []


# ---------------------------------------------------------------------------
# CM-10: the lift race guard (D-CS-B-72)
# ---------------------------------------------------------------------------


async def test_cm10_a_lost_lift_race_writes_no_milestone(
    app_engine: AsyncEngine, seed_tenant, monkeypatch
) -> None:
    tenant_id = await seed_tenant("CM-10")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cm10.example")
    case = await _open_case(app_engine, tenant_id, user_id)
    async with tenant_context(app_engine, tenant_id) as session:
        stale = await CaseRepository(session).wait(
            case.id, wait_until=date(2027, 1, 1), by=user_id, now=_T0 + _MIN
        )
    # The program lifts it in a transaction of its own, committed first.
    async with tenant_context(app_engine, tenant_id) as session:
        await CaseRepository(session).lift_wait(case.id, reason="date", now=_T0 + 2 * _MIN)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        record = [
            "opened",
            "frozen",
            "waiting_set",
            "frozen",
            "waiting_lifted",
            "frozen",
        ]
        assert _kinds(await repo.list_entries(case.id, include_frozen=True)) == record

        async def _read_before_the_program_lifted(case_id: Any) -> Any:
            return stale

        # lift_wait's own read still sees the case waiting: the state flipped
        # to open behind that DTO, before the column write.
        monkeypatch.setattr(repo, "get", _read_before_the_program_lifted)
        with pytest.raises(CaseStateInvalid, match="lifted by the program meanwhile"):
            await repo.lift_wait(case.id, by=user_id, now=_T0 + 3 * _MIN)
        monkeypatch.undo()

        assert _kinds(await repo.list_entries(case.id, include_frozen=True)) == record
        current = await repo.get(case.id)
        assert current is not None
        assert (current.state, current.updated_at) == ("open", _T0 + 2 * _MIN)


# ---------------------------------------------------------------------------
# CM-11: a removed thread's live kept row freezes at file level (D-CS-B-100)
# ---------------------------------------------------------------------------


async def test_cm11_removed_threads_kept_row_freezes_at_file_level(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("CM-11")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cm11.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        threads = CaseThreadRepository(session)
        kept_repo = CaseKeptRepository(session)
        case = await repo.create(title="Keep", opened_by=user_id, opened_actor="pm", now=_T0)
        gone = await threads.add(case.id, title="Gone", now=_T0)
        stays = await threads.add(case.id, title="Stays", now=_T0)
        orphan = await kept_repo.keep(
            case.id,
            artifact="chart_snapshot",
            payload={"spec": {"data": [1]}, "title": "NAV"},
            kept_by=user_id,
            via="keep",
            now=_T0 + _MIN,
            thread_id=gone.id,
        )
        held = await kept_repo.keep(
            case.id,
            artifact="chart_snapshot",
            payload={"spec": {"data": [2]}, "title": "Flows"},
            kept_by=user_id,
            via="keep",
            now=_T0 + _MIN,
            thread_id=stays.id,
        )
        await threads.remove(gone.id, now=_T0 + 2 * _MIN)

        await repo.wait(case.id, wait_until=date(2027, 1, 1), by=user_id, now=_T0 + 3 * _MIN)
        frozen = await repo.latest_frozen(case.id)
        assert frozen is not None and frozen.payload["milestone_kind"] == "waiting_set"

    file = frozen.payload["file"]
    assert [thread["title"] for thread in file["threads"]] == ["Stays"]
    assert [ref["kept_id"] for ref in file["threads"][0]["kept"]] == [str(held.id)]
    assert [ref["kept_id"] for ref in file["kept"]] == [str(orphan.id)]
    assert set(file["kept"][0]) == _KEPT_REF_FIELDS
