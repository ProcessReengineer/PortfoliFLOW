# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""CaseRepository tests against the live compose Postgres.

The ``cases`` / ``case_entries`` tables are tenant-scoped (RLS-policed, per
ADR-0107 / ADR-0035). The timeline is append-only and closed cases are
immutable in their entirety (ADR-0107 §2/§4); these tests pin both the
happy paths and the guards that enforce those invariants.

Coverage
--------
* CR-01: tenant-sequential numbering + the ``uq_cases_tenant_case_number``
  constraint; the single-retry recovery on a number collision.
* CR-02: both creation entry points (manual / from-finding) write exactly
  one ``opened`` entry and never touch the finding.
* CR-03: kind / actor vocabulary enforcement writes nothing.
* CR-04: no update/delete surface for entries; timeline reads in order.
* CR-05: close requires a note, writes the transition + ``closed`` entry
  atomically, and refuses a second close.
* CR-06: a closed case rejects entry-append and attachment-create; reads
  still work.
* CR-07: list ordering + the "Mine" filter, recently-closed limit, and the
  archive search (title / closing-note only, escaped, whitespace → empty).
* CR-08: cross-tenant invisibility (RLS smoke).
* CR-09: ``list_closed_before`` pages closed cases by ``closed_at`` —
  newest first, open cases absent, exclusive ``before``, inclusive
  ``start``, the cap — and filters by the originating finding's family
  through the ``finding_id`` join (the Journal's case source, ADR-0133 §5).
* CR-10: ``create`` sets the owner and ``updated_at``; ``framing`` /
  ``counterparty`` / ``due_on`` round-trip; the framing vocabulary
  (ADR-0138 §3, §13).
* CR-11: ``update_working_file`` — partial writes, the empty-title guard,
  the ``updated_at`` bump, the closed-case refusal.
* CR-12: the states open · waiting · closed (ADR-0138 §4, D-CS-B-2) —
  set / lift, the lists, the wake-up read, close and append on a waiting
  case.
* CR-13: the extended kind vocabulary — milestone-only kinds refused on the
  generic path, ``system_line`` accepted, ``note`` still writable
  (D-CS-B-1).
* CR-14: ``search_working_file`` — hits across the working file, the DMS
  boundary held (ADR-0138 §11), the state filter, escaping.
* CR-15: ``set_owner`` — the owner changes, ``opened_by`` stays.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import (
    CaseActorInvalid,
    CaseClosedError,
    CaseClosingNoteMissing,
    CaseEntryKindInvalid,
    CaseStateInvalid,
    CaseWorkingFileInvalid,
)
from core.repositories import (
    IreneFindingRepository,
    UserRepository,
    tenant_context,
)
from core.repositories.case_attachment_repository import (
    CaseAttachmentRepository,
)
from core.repositories.case_repository import CaseRepository
from core.repositories.case_working_file_repository import (
    CaseConversationRepository,
    CaseDecisionRepository,
    CaseFigureRepository,
    CaseKeptRepository,
    CaseThreadRepository,
)

_T0 = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)


class _FakeUniqueViolation(Exception):
    """Stand-in for asyncpg's ``UniqueViolationError`` in the retry test."""

    constraint_name = "uq_cases_tenant_case_number"


async def _seed_user(app_engine: AsyncEngine, tenant_id, email: str):
    """Insert a user in ``tenant_id`` and return its id (for the case FKs)."""
    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(email=email, password_hash="x" * 8)
    return user.id


# ---------------------------------------------------------------------------
# CR-01: numbering, the unique constraint, and the single-retry recovery
# ---------------------------------------------------------------------------


async def test_cr01_numbering_increments_and_constraint_exists(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("CR-01")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr01.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        c1 = await repo.create(title="First", opened_by=user_id, opened_actor="pm", now=_T0)
        c2 = await repo.create(
            title="Second",
            opened_by=user_id,
            opened_actor="pm",
            now=_T0 + timedelta(minutes=1),
        )

    assert c1.case_number == 1
    assert c2.case_number == 2

    async with tenant_context(app_engine, tenant_id) as session:
        found = await session.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = 'uq_cases_tenant_case_number'")
        )
        assert found.scalar_one_or_none() == 1


async def test_cr01_number_collision_retries_once(
    app_engine: AsyncEngine, seed_tenant, monkeypatch
) -> None:
    tenant_id = await seed_tenant("CR-01b")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr01b.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        await repo.create(title="one", opened_by=user_id, opened_actor="pm", now=_T0)
        await repo.create(
            title="two",
            opened_by=user_id,
            opened_actor="pm",
            now=_T0 + timedelta(minutes=1),
        )

        # The first attempt collides on uq_cases_tenant_case_number; the
        # retry re-reads MAX(case_number) and lands the next number.
        calls = {"n": 0}
        original = repo._attempt_create

        async def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("INSERT INTO cases ...", {}, _FakeUniqueViolation())
            return await original(**kwargs)

        monkeypatch.setattr(repo, "_attempt_create", flaky)

        c3 = await repo.create(
            title="three",
            opened_by=user_id,
            opened_actor="pm",
            now=_T0 + timedelta(minutes=2),
        )

    assert calls["n"] == 2  # first raised, retry delegated to the real insert
    assert c3.case_number == 3


async def test_cr01c_savepoint_recovers_from_real_unique_violation(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A genuine ``uq_cases_tenant_case_number`` violation inside a SAVEPOINT
    leaves the session usable — the semantics the C1 retry path relies on.

    The retry in :meth:`CaseRepository.create` only recovers because each
    attempt runs in a ``begin_nested()`` block: a duplicate-number collision
    rolls back to the savepoint rather than poisoning the whole transaction.
    This pins that with a *real* Postgres unique violation (a raw duplicate
    INSERT), not a hand-built error, then proves the same session still
    creates and reads a case afterwards.
    """
    tenant_id = await seed_tenant("CR-01c")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr01c.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        first = await repo.create(title="First", opened_by=user_id, opened_actor="pm", now=_T0)

        # A genuine unique violation on (tenant_id, case_number), isolated in
        # a SAVEPOINT so it rolls back just this statement.
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO cases "
                        "(tenant_id, case_number, title, opened_by) "
                        "VALUES (:tid, :num, :title, :uid)"
                    ),
                    {
                        "tid": str(first.tenant_id),
                        "num": first.case_number,
                        "title": "Duplicate number",
                        "uid": str(user_id),
                    },
                )

        # The savepoint rolled back; the outer transaction is still healthy,
        # so the same session both writes and reads a fresh case.
        second = await repo.create(
            title="Second",
            opened_by=user_id,
            opened_actor="pm",
            now=_T0 + timedelta(minutes=1),
        )
        read_back = await repo.get(second.id)

    assert second.case_number == 2
    assert read_back is not None
    assert read_back.case_number == 2


# ---------------------------------------------------------------------------
# CR-02: both creation entry points; the finding is never touched
# ---------------------------------------------------------------------------


async def test_cr02_create_manual_and_from_finding(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CR-02")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr02.example")

    # Manual: description present, no finding, PM actor on the opened entry.
    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        manual = await repo.create(
            title="Manual case",
            opened_by=user_id,
            description="typed by hand",
            opened_actor="pm",
            now=_T0,
        )
        manual_entries = await repo.list_entries(manual.id)

    assert manual.finding_id is None
    assert manual.description == "typed by hand"
    assert len(manual_entries) == 1
    assert manual_entries[0].kind == "opened"
    assert manual_entries[0].actor == "pm"
    assert manual_entries[0].actor_user_id == user_id
    assert manual_entries[0].payload == {}

    # From finding: finding_id set, description null, system actor, opaque
    # opened payload carried verbatim.
    async with tenant_context(app_engine, tenant_id) as session:
        finding = await IreneFindingRepository(session).append(
            subject_key="anlv:16",
            payload={"trigger": "x"},
            urgency=4,
            band="critical",
        )
    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        from_finding = await repo.create(
            title="From finding",
            opened_by=user_id,
            finding_id=finding.id,
            opened_payload={"materiality": "high"},
            opened_actor="system",
            now=_T0 + timedelta(minutes=1),
        )
        ff_entries = await repo.list_entries(from_finding.id)

    assert from_finding.finding_id == finding.id
    assert from_finding.description is None
    assert len(ff_entries) == 1
    assert ff_entries[0].kind == "opened"
    assert ff_entries[0].actor == "system"
    assert ff_entries[0].actor_user_id is None
    assert ff_entries[0].payload == {"materiality": "high"}

    # The finding row is untouched by create — resolving it is C4's concern.
    async with tenant_context(app_engine, tenant_id) as session:
        res = await session.execute(
            text("SELECT resolution FROM irene_finding WHERE id = :fid"),
            {"fid": str(finding.id)},
        )
        assert res.scalar_one() == "open"


# ---------------------------------------------------------------------------
# CR-03: vocabulary enforcement writes nothing
# ---------------------------------------------------------------------------


async def test_cr03_vocabulary_enforced_and_writes_nothing(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("CR-03")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr03.example")

    async with tenant_context(app_engine, tenant_id) as session:
        case = await CaseRepository(session).create(
            title="Vocab case", opened_by=user_id, opened_actor="pm", now=_T0
        )

    # An invalid opened_actor is rejected before any row is written.
    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        with pytest.raises(CaseActorInvalid):
            await repo.create(
                title="bad actor",
                opened_by=user_id,
                opened_actor="robot",
                now=_T0,
            )

    # Invalid kind, invalid actor, and the closed-kind guard — all raise
    # before touching SQL, so the session stays usable across them.
    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        with pytest.raises(CaseEntryKindInvalid):
            await repo.append_entry(
                case.id,
                kind="bogus",
                actor="pm",
                actor_user_id=user_id,
                payload={},
                now=_T0,
            )
        with pytest.raises(CaseActorInvalid):
            await repo.append_entry(
                case.id,
                kind="note",
                actor="robot",
                actor_user_id=user_id,
                payload={},
                now=_T0,
            )
        with pytest.raises(CaseEntryKindInvalid):
            await repo.append_entry(
                case.id,
                kind="closed",
                actor="pm",
                actor_user_id=user_id,
                payload={},
                now=_T0,
            )

    # Only the mandatory opened entry survives.
    async with tenant_context(app_engine, tenant_id) as session:
        entries = await CaseRepository(session).list_entries(case.id)
    assert len(entries) == 1
    assert entries[0].kind == "opened"


# ---------------------------------------------------------------------------
# CR-04: append-only surface + timeline ordering
# ---------------------------------------------------------------------------


async def test_cr04_append_only_surface_and_order(app_engine: AsyncEngine, seed_tenant) -> None:
    # The repository exposes no mutation of the append-only record.
    for absent in (
        "update_entry",
        "delete_entry",
        "update_case",
        "delete_case",
        "reopen",
    ):
        assert not hasattr(CaseRepository, absent), (
            f"CaseRepository must not expose {absent!r} — the timeline is "
            "append-only and closed cases are immutable (ADR-0107 §2/§4)."
        )

    tenant_id = await seed_tenant("CR-04")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr04.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        case = await repo.create(title="Timeline", opened_by=user_id, opened_actor="pm", now=_T0)
        await repo.append_entry(
            case.id,
            kind="note",
            actor="pm",
            actor_user_id=user_id,
            payload={"n": 1},
            now=_T0 + timedelta(minutes=1),
        )
        await repo.append_entry(
            case.id,
            kind="note",
            actor="shirley",
            actor_user_id=None,
            payload={"n": 2},
            now=_T0 + timedelta(minutes=2),
        )
        entries = await repo.list_entries(case.id)

    assert [(e.kind, e.payload.get("n")) for e in entries] == [
        ("opened", None),
        ("note", 1),
        ("note", 2),
    ]


# ---------------------------------------------------------------------------
# CR-05: close requires a note and writes atomically
# ---------------------------------------------------------------------------


async def test_cr05_close_requires_note_and_writes_atomically(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("CR-05")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr05.example")
    close_at = datetime(2026, 7, 22, 15, 30, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        case = await CaseRepository(session).create(
            title="To close", opened_by=user_id, opened_actor="pm", now=_T0
        )

    # A whitespace-only note is no note.
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(CaseClosingNoteMissing):
            await CaseRepository(session).close(
                case.id, closed_by=user_id, closing_note="   ", now=close_at
            )
    async with tenant_context(app_engine, tenant_id) as session:
        still = await CaseRepository(session).get(case.id)
    assert still is not None and still.state == "open"

    # A real close: the note is stripped, the transition and the closed
    # entry are written together.
    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        closed = await repo.close(
            case.id,
            closed_by=user_id,
            closing_note="  resolved cleanly  ",
            now=close_at,
        )
        entries = await repo.list_entries(case.id)

    assert closed.state == "closed"
    assert closed.closed_by == user_id
    assert closed.closed_at == close_at
    assert closed.closing_note == "resolved cleanly"

    closed_entries = [e for e in entries if e.kind == "closed"]
    assert len(closed_entries) == 1
    assert closed_entries[0].actor == "pm"
    assert closed_entries[0].actor_user_id == user_id
    assert closed_entries[0].payload == {"closing_note": "resolved cleanly"}

    # A closed case cannot be closed again.
    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(CaseClosedError):
            await CaseRepository(session).close(
                case.id, closed_by=user_id, closing_note="again", now=close_at
            )


# ---------------------------------------------------------------------------
# CR-06: closed-case immutability
# ---------------------------------------------------------------------------


async def test_cr06_closed_case_is_immutable(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CR-06")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr06.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        case = await repo.create(title="Immutable", opened_by=user_id, opened_actor="pm", now=_T0)
        await repo.close(
            case.id,
            closed_by=user_id,
            closing_note="done",
            now=_T0 + timedelta(hours=1),
        )

    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(CaseClosedError):
            await CaseRepository(session).append_entry(
                case.id,
                kind="note",
                actor="pm",
                actor_user_id=user_id,
                payload={},
                now=_T0 + timedelta(hours=2),
            )

    async with tenant_context(app_engine, tenant_id) as session:
        with pytest.raises(CaseClosedError):
            await CaseAttachmentRepository(session).create(
                case.id,
                filename="late.pdf",
                mime_type="application/pdf",
                size_bytes=3,
                sha256="abc",
                content=b"pdf",
                uploaded_by=user_id,
                now=_T0 + timedelta(hours=2),
            )

    # Reads still work: the record is readable, just not writable.
    async with tenant_context(app_engine, tenant_id) as session:
        entries = await CaseRepository(session).list_entries(case.id)
    assert [e.kind for e in entries] == ["opened", "closed"]


# ---------------------------------------------------------------------------
# CR-07a: list ordering + the "Mine" filter + recently-closed limit
# ---------------------------------------------------------------------------


async def test_cr07_lists_order_and_filter(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CR-07a")
    pm1 = await _seed_user(app_engine, tenant_id, "pm1@cr07.example")
    pm2 = await _seed_user(app_engine, tenant_id, "pm2@cr07.example")
    base = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        a = await repo.create(title="A", opened_by=pm1, opened_actor="pm", now=base)
        b = await repo.create(
            title="B",
            opened_by=pm2,
            opened_actor="pm",
            now=base + timedelta(minutes=1),
        )
        c = await repo.create(
            title="C",
            opened_by=pm1,
            opened_actor="pm",
            now=base + timedelta(minutes=2),
        )
        open_all = await repo.list_open()
        open_pm1 = await repo.list_open(opened_by=pm1)

    assert [x.id for x in open_all] == [c.id, b.id, a.id]  # newest opened first
    assert [x.id for x in open_pm1] == [c.id, a.id]  # the "Mine" filter

    # Six closed cases; the recently-closed window caps at five, newest first.
    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        closed_ids = []
        for i in range(6):
            ci = await repo.create(
                title=f"Z{i}",
                opened_by=pm1,
                opened_actor="pm",
                now=base + timedelta(hours=1, minutes=i),
            )
            await repo.close(
                ci.id,
                closed_by=pm1,
                closing_note=f"note {i}",
                now=base + timedelta(hours=2, minutes=i),
            )
            closed_ids.append(ci.id)
        recent = await repo.list_recently_closed(limit=5)
        # The Journal source (C4) is uncapped-by-default: all six, same order.
        journal_source = await repo.list_closed(limit=100)

    assert [x.id for x in recent] == list(reversed(closed_ids))[:5]
    assert [x.id for x in journal_source] == list(reversed(closed_ids))
    # Same ordering as the five-row UI list, but not capped to it.
    assert len(journal_source) == 6


# ---------------------------------------------------------------------------
# CR-07b: archive search — title / closing-note only, escaped, whitespace
# ---------------------------------------------------------------------------


async def test_cr07_search_archive(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CR-07b")
    pm1 = await _seed_user(app_engine, tenant_id, "pm1@cr07b.example")
    base = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)

        # A closed case whose distinctive word lives ONLY in an entry payload.
        payload_case = await repo.create(
            title="Ordinary title", opened_by=pm1, opened_actor="pm", now=base
        )
        await repo.append_entry(
            payload_case.id,
            kind="note",
            actor="pm",
            actor_user_id=pm1,
            payload={"secretword": "zzuniquezz"},
            now=base + timedelta(minutes=1),
        )
        await repo.close(
            payload_case.id,
            closed_by=pm1,
            closing_note="closed normally",
            now=base + timedelta(minutes=2),
        )

        # Title match and closing-note match.
        title_case = await repo.create(
            title="Alpha Rebalancing Review",
            opened_by=pm1,
            opened_actor="pm",
            now=base + timedelta(hours=1),
        )
        await repo.close(
            title_case.id,
            closed_by=pm1,
            closing_note="watched the beta closely",
            now=base + timedelta(hours=1, minutes=1),
        )

        # Escaping: the underscore must match literally, not as a wildcard.
        underscore_case = await repo.create(
            title="report_v2 final",
            opened_by=pm1,
            opened_actor="pm",
            now=base + timedelta(hours=2),
        )
        await repo.close(
            underscore_case.id,
            closed_by=pm1,
            closing_note="ok",
            now=base + timedelta(hours=2, minutes=1),
        )
        decoy_case = await repo.create(
            title="reportXv2 final",
            opened_by=pm1,
            opened_actor="pm",
            now=base + timedelta(hours=3),
        )
        await repo.close(
            decoy_case.id,
            closed_by=pm1,
            closing_note="ok",
            now=base + timedelta(hours=3, minutes=1),
        )

        by_title = await repo.search_archive("alpha")
        by_note = await repo.search_archive("BETA")
        by_payload = await repo.search_archive("zzuniquezz")
        by_underscore = await repo.search_archive("report_v2")
        empty = await repo.search_archive("   ")

    assert [x.id for x in by_title] == [title_case.id]
    assert [x.id for x in by_note] == [title_case.id]  # case-insensitive
    assert by_payload == []  # payload text is never searched (DMS boundary)
    assert [x.id for x in by_underscore] == [underscore_case.id]  # decoy excluded
    assert empty == []


# ---------------------------------------------------------------------------
# CR-08: cross-tenant invisibility (RLS smoke)
# ---------------------------------------------------------------------------


async def test_cr08_cross_tenant_invisibility(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")
    user_a = await _seed_user(app_engine, tenant_a, "pm@a-cr.example")

    async with tenant_context(app_engine, tenant_a) as session:
        repo = CaseRepository(session)
        case = await repo.create(title="A-only", opened_by=user_a, opened_actor="pm", now=_T0)
        await repo.append_entry(
            case.id,
            kind="note",
            actor="pm",
            actor_user_id=user_a,
            payload={},
            now=_T0 + timedelta(minutes=1),
        )

    async with tenant_context(app_engine, tenant_b) as session:
        repo = CaseRepository(session)
        assert await repo.list_open() == []
        assert await repo.get(case.id) is None
        assert await repo.list_entries(case.id) == []
        for table in ("cases", "case_entries"):
            count = await session.execute(text(f"SELECT count(*) FROM {table}"))
            assert count.scalar_one() == 0


# ---------------------------------------------------------------------------
# CR-09: list_closed_before — the Journal's case source, paged by closed_at
# (ADR-0133 §5; P-WD-B14)
# ---------------------------------------------------------------------------


async def test_cr09_list_closed_before_pages_and_filters_by_origin_family(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("CR-09")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr09.example")
    day = timedelta(days=1)

    async with tenant_context(app_engine, tenant_id) as session:
        finding = await IreneFindingRepository(session).append(
            subject_key="anlv:16", payload={"trigger": "x"}, urgency=4, band="critical"
        )
        repo = CaseRepository(session)
        manual = await repo.create(title="Manual", opened_by=user_id, opened_actor="pm", now=_T0)
        from_finding = await repo.create(
            title="From finding",
            opened_by=user_id,
            finding_id=finding.id,
            opened_actor="system",
            now=_T0,
        )
        still_open = await repo.create(
            title="Still open", opened_by=user_id, opened_actor="pm", now=_T0
        )
        await repo.close(manual.id, closed_by=user_id, closing_note="done", now=_T0 + day)
        await repo.close(from_finding.id, closed_by=user_id, closing_note="done", now=_T0 + 2 * day)

    far = _T0 + 10 * day
    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        page = await repo.list_closed_before(before=far, limit=10)
        assert [case.title for case in page] == ["From finding", "Manual"]
        assert still_open.id not in {case.id for case in page}
        # Exclusive before, inclusive start, the cap.
        assert [
            case.title
            for case in await repo.list_closed_before(
                before=_T0 + 2 * day, limit=10, start=_T0 + day
            )
        ] == ["Manual"]
        assert len(await repo.list_closed_before(before=far, limit=1)) == 1
        # The family is the origin finding's; a manual case has none.
        assert [
            case.title
            for case in await repo.list_closed_before(before=far, limit=10, family="anlv")
        ] == ["From finding"]
        assert await repo.list_closed_before(before=far, limit=10, family="saa") == []


# ---------------------------------------------------------------------------
# CR-10: create — owner, updated_at and the working-file columns (ADR-0138)
# ---------------------------------------------------------------------------


async def test_cr10_create_sets_owner_and_working_file_columns(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("CR-10")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr10.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        case = await repo.create(
            title="Re-up Fund V?",
            opened_by=user_id,
            opened_actor="pm",
            counterparty="Alpine Partners",
            due_on=date(2026, 12, 15),
            framing="task",
            now=_T0,
        )
        bare = await repo.create(title="Bare", opened_by=user_id, opened_actor="pm", now=_T0)
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.create(
                title="Bad", opened_by=user_id, opened_actor="pm", framing="watch", now=_T0
            )
        read_back = await repo.get(case.id)

    assert case.owner_user_id == case.opened_by == user_id
    assert case.updated_at == _T0
    assert (case.framing, case.counterparty, case.due_on) == (
        "task",
        "Alpine Partners",
        date(2026, 12, 15),
    )
    assert case.wait_until is None
    assert read_back == case
    assert (bare.framing, bare.counterparty, bare.due_on) == (None, None, None)


# ---------------------------------------------------------------------------
# CR-11: update_working_file
# ---------------------------------------------------------------------------


async def test_cr11_update_working_file(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CR-11")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr11.example")
    later = _T0 + timedelta(days=1)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        case = await repo.create(
            title="Question",
            opened_by=user_id,
            opened_actor="pm",
            counterparty="GP",
            now=_T0,
        )
        updated = await repo.update_working_file(
            case.id, now=later, description="why now", framing="question"
        )
        assert updated.title == "Question"  # untouched
        assert updated.counterparty == "GP"  # untouched
        assert (updated.description, updated.framing) == ("why now", "question")
        assert updated.updated_at == later

        cleared = await repo.update_working_file(
            case.id, now=later, title="  Renamed  ", counterparty=None, due_on=date(2027, 1, 31)
        )
        assert cleared.title == "Renamed"
        assert cleared.counterparty is None
        assert cleared.due_on == date(2027, 1, 31)
        assert cleared.description == "why now"

        with pytest.raises(CaseWorkingFileInvalid):
            await repo.update_working_file(case.id, now=later, title="   ")
        with pytest.raises(CaseWorkingFileInvalid):
            await repo.update_working_file(case.id, now=later, framing="watch")

        await repo.close(case.id, closed_by=user_id, closing_note="done", now=later)
        with pytest.raises(CaseClosedError):
            await repo.update_working_file(case.id, now=later, description="late")


# ---------------------------------------------------------------------------
# CR-12: open · waiting · closed
# ---------------------------------------------------------------------------


async def test_cr12_states_waiting(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CR-12")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr12.example")
    today = date(2026, 11, 2)
    tomorrow = today + timedelta(days=1)
    minute = timedelta(minutes=1)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        a = await repo.create(title="A", opened_by=user_id, opened_actor="pm", now=_T0)
        b = await repo.create(title="B", opened_by=user_id, opened_actor="pm", now=_T0 + minute)
        c = await repo.create(title="C", opened_by=user_id, opened_actor="pm", now=_T0 + 2 * minute)

        waiting_b = await repo.set_waiting(b.id, wait_until=today, now=_T0 + 10 * minute)
        assert (waiting_b.state, waiting_b.wait_until) == ("waiting", today)
        assert waiting_b.updated_at == _T0 + 10 * minute
        await repo.set_waiting(c.id, wait_until=tomorrow, now=_T0 + 5 * minute)
        with pytest.raises(CaseStateInvalid):
            await repo.set_waiting(b.id, wait_until=tomorrow, now=_T0 + 11 * minute)

        assert [x.id for x in await repo.list_open()] == [a.id]
        assert [x.id for x in await repo.list_cases(state="waiting")] == [b.id, c.id]
        assert [x.id for x in await repo.list_cases()] == [b.id, c.id, a.id]
        assert [x.id for x in await repo.list_due_for_wake(today=today)] == [b.id]
        with pytest.raises(CaseStateInvalid):
            await repo.list_cases(state="pending")

        # D-CS-B-2: a waiting case is closed by lifting the wait first.
        with pytest.raises(CaseStateInvalid):
            await repo.close(b.id, closed_by=user_id, closing_note="done", now=_T0 + 12 * minute)

        # A waiting case is mutable like an open one.
        entry = await repo.append_entry(
            b.id,
            kind="note",
            actor="pm",
            actor_user_id=user_id,
            payload={},
            now=_T0 + 12 * minute,
        )
        assert entry.kind == "note"

        lifted = await repo.lift_waiting(c.id, now=_T0 + 13 * minute)
        assert (lifted.state, lifted.wait_until) == ("open", None)
        with pytest.raises(CaseStateInvalid):
            await repo.lift_waiting(a.id, now=_T0 + 13 * minute)

        await repo.close(a.id, closed_by=user_id, closing_note="done", now=_T0 + 14 * minute)
        assert [x.id for x in await repo.list_cases(state="closed")] == [a.id]
        with pytest.raises(CaseClosedError):
            await repo.append_entry(
                a.id,
                kind="note",
                actor="pm",
                actor_user_id=user_id,
                payload={},
                now=_T0 + 15 * minute,
            )


# ---------------------------------------------------------------------------
# CR-13: the extended kind vocabulary (D-CS-B-1)
# ---------------------------------------------------------------------------


async def test_cr13_entry_kinds(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CR-13")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr13.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        case = await repo.create(title="Kinds", opened_by=user_id, opened_actor="pm", now=_T0)
        for milestone_kind in ("frozen", "handed_over", "waiting_set", "waiting_lifted", "closed"):
            with pytest.raises(CaseEntryKindInvalid):
                await repo.append_entry(
                    case.id,
                    kind=milestone_kind,
                    actor="pm",
                    actor_user_id=user_id,
                    payload={},
                    now=_T0,
                )
        line = await repo.append_entry(
            case.id,
            kind="system_line",
            actor="system",
            actor_user_id=None,
            payload={"event": "nav_restated"},
            now=_T0 + timedelta(minutes=1),
        )
        note = await repo.append_entry(
            case.id,
            kind="note",
            actor="pm",
            actor_user_id=user_id,
            payload={},
            now=_T0 + timedelta(minutes=2),
        )
        entries = await repo.list_entries(case.id)

    assert (line.kind, line.actor, line.actor_user_id) == ("system_line", "system", None)
    assert note.kind == "note"
    assert [e.kind for e in entries] == ["opened", "system_line", "note"]


# ---------------------------------------------------------------------------
# CR-14: search_working_file — the working file is searched, the DMS is not
# ---------------------------------------------------------------------------


async def test_cr14_search_working_file(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CR-14")
    user_id = await _seed_user(app_engine, tenant_id, "pm@cr14.example")
    minute = timedelta(minutes=1)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)

        async def new_case(title: str, offset: int):
            return await repo.create(
                title=title, opened_by=user_id, opened_actor="pm", now=_T0 + offset * minute
            )

        via_thread = await new_case("Case one", 1)
        await CaseThreadRepository(session).add(
            via_thread.id, title="T", prose="the zebrathread idea", now=_T0 + minute
        )
        via_figure = await new_case("Case two", 2)
        await CaseFigureRepository(session).add(
            via_figure.id, label="Quokka fee", source="typed", now=_T0 + 2 * minute
        )
        via_conversation = await new_case("Case three", 3)
        await CaseConversationRepository(session).add(
            via_conversation.id,
            held_on=date(2026, 10, 1),
            what_was_said="the wombat said so",
            now=_T0 + 3 * minute,
        )
        via_decision = await new_case("Case four", 4)
        await CaseDecisionRepository(session).upsert_draft(
            via_decision.id, now=_T0 + 4 * minute, rationale="narwhal reasons"
        )
        via_excerpt = await new_case("Case five", 5)
        attachment = await CaseAttachmentRepository(session).create(
            via_excerpt.id,
            filename="secretfilename.pdf",
            mime_type="application/pdf",
            size_bytes=13,
            sha256="h",
            content=b"bytescontents",
            uploaded_by=user_id,
            now=_T0 + 5 * minute,
        )
        await CaseKeptRepository(session).keep(
            via_excerpt.id,
            artifact="document_excerpt",
            payload={"passage": "an ocelot clause", "page": 3},
            kept_by=user_id,
            via="keep",
            attachment_id=attachment.id,
            now=_T0 + 5 * minute,
        )
        closed = await new_case("Case six", 6)
        await repo.close(
            closed.id, closed_by=user_id, closing_note="pangolin outcome", now=_T0 + 6 * minute
        )
        chart = await new_case("Case seven", 7)
        await CaseKeptRepository(session).keep(
            chart.id,
            artifact="chart_snapshot",
            payload={"title": "lemurchart", "passage": "lemurchart"},
            kept_by=user_id,
            via="keep",
            now=_T0 + 7 * minute,
        )
        removed = await new_case("Case eight", 8)
        threads = CaseThreadRepository(session)
        gone = await threads.add(removed.id, title="axolotl", now=_T0 + 8 * minute)
        await threads.remove(gone.id, now=_T0 + 8 * minute)
        percent = await new_case("100% sure", 9)
        await new_case("1000 units", 10)

        async def hits(query: str, state: str | None = None) -> list:
            return [x.id for x in await repo.search_working_file(query, state=state)]

        assert await hits("ZEBRATHREAD") == [via_thread.id]
        assert await hits("quokka") == [via_figure.id]
        assert await hits("wombat") == [via_conversation.id]
        assert await hits("narwhal") == [via_decision.id]
        assert await hits("ocelot") == [via_excerpt.id]
        assert await hits("pangolin") == [closed.id]
        # The DMS boundary (ADR-0138 §11): no filename, no bytes, no other
        # kept payload field, no removed thread.
        assert await hits("secretfilename") == []
        assert await hits("bytescontents") == []
        assert await hits("lemurchart") == []
        assert await hits("axolotl") == []
        # The state filter, the empty query, the escaped wildcard.
        assert await hits("pangolin", state="closed") == [closed.id]
        assert await hits("zebrathread", state="closed") == []
        assert await hits("   ") == []
        assert await hits("0%") == [percent.id]
        # Ordering: updated_at descending.
        assert await hits("Case") == [
            removed.id,
            chart.id,
            closed.id,
            via_excerpt.id,
            via_decision.id,
            via_conversation.id,
            via_figure.id,
            via_thread.id,
        ]


# ---------------------------------------------------------------------------
# CR-15: set_owner (ADR-0138 §13)
# ---------------------------------------------------------------------------


async def test_cr15_set_owner(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("CR-15")
    pm1 = await _seed_user(app_engine, tenant_id, "pm1@cr15.example")
    pm2 = await _seed_user(app_engine, tenant_id, "pm2@cr15.example")
    later = _T0 + timedelta(hours=3)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = CaseRepository(session)
        case = await repo.create(title="Hand me over", opened_by=pm1, opened_actor="pm", now=_T0)
        handed = await repo.set_owner(case.id, owner_user_id=pm2, now=later)
        assert handed.owner_user_id == pm2
        assert handed.opened_by == pm1
        assert handed.updated_at == later
        assert [x.id for x in await repo.list_cases(owner_user_id=pm2)] == [case.id]
        assert await repo.list_cases(owner_user_id=pm1) == []

        await repo.close(case.id, closed_by=pm2, closing_note="done", now=later)
        with pytest.raises(CaseClosedError):
            await repo.set_owner(case.id, owner_user_id=pm1, now=later)
