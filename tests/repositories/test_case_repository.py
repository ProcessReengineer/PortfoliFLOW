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
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import (
    CaseActorInvalid,
    CaseClosedError,
    CaseClosingNoteMissing,
    CaseEntryKindInvalid,
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
