# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""DocumentReadingRepository tests against the live compose Postgres.

``document_readings`` and ``reading_purges`` (migration b038) are
tenant-scoped and RLS-policed. A removal purges the three payload columns
and leaves a tombstone (ADR-0139 §5); the audit images of a reading exclude
the same three columns (ADR-0140).

Coverage
--------
* DR-01: tenant-sequential numbering + the unique constraint; the single
  retry on a number collision; a tombstone keeps its number.
* DR-02: ``create`` round-trips every field; every refusal names its field
  and writes nothing.
* DR-03: ``get`` / ``get_by_number`` in any state; ``None`` for unknown and
  for another tenant's reading.
* DR-04: ``list_readings``: each filter, two combined, tombstones, order,
  limit; DR-04b: paging by ``offset``.
* DR-05: ``search``: case-insensitive, literal wildcards, live only.
* DR-06: the dedup lookup by hash.
* DR-07: ``assign``: both targets, clearing, the refusals; deleting the
  investment leaves the reading unassigned (RD-D-29).
* DR-08: ``remove`` purges the three payload columns to SQL NULL and
  changes nothing else but the removal fields.
* DR-09: retention: preview, run, the effective date, the cutoff, the
  recorded run.
* DR-10: audit (ADR-0140): the payload keys never enter ``audit_log``, the
  rest is there, attribution, the generic trigger beside it, and the
  excluded list is the purged list.
* DR-11: cross-tenant invisibility.
* DR-12: an investment's reading list, in document chronology.
* DR-13: the manager names live readings are assigned to.

All dates are fixed; none derives from the day the suite runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import ReadingInvalid, ReadingNotFound, ReadingRemoved
from core.repositories import (
    PAYLOAD_COLUMNS,
    AssetClassRepository,
    CaseAttachmentRepository,
    CaseRepository,
    DocumentReadingDTO,
    DocumentReadingRepository,
    InvestmentRepository,
    ReadingPurgeDTO,
    UserRepository,
    tenant_context,
)

_T0 = datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc)
_T1 = _T0 + timedelta(hours=1)
_T2 = _T0 + timedelta(hours=2)
_TODAY = date(2026, 11, 22)

_SHA = "a" * 64
_OTHER_SHA = "b" * 64
#: Appears in every payload column of a default reading and nowhere else.
_PAYLOAD_MARK = "payload-mark-7f3a"


class _FakeUniqueViolation(Exception):
    """Stand-in for asyncpg's ``UniqueViolationError`` in the retry test."""

    constraint_name = "uq_document_readings_tenant_reading_number"


def _finding(**overrides: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "key": "nav",
        "label": "NAV",
        "value": f"12.5 {_PAYLOAD_MARK}",
        "type": "Number",
        "unit": "EUR m",
        "page": 12,
        "source": "Page 12",
        "quote": "The NAV stood at 12.5 million.",
        "confidence": "High",
        "origin": "profile",
    }
    finding.update(overrides)
    return finding


def _reading_kwargs(**overrides: Any) -> dict[str, Any]:
    """A valid ``create`` call; the caller supplies ``read_by``."""
    kwargs: dict[str, Any] = {
        "filename": "fund-x-q3.pdf",
        "sha256": _SHA,
        "size_bytes": 123_456,
        "page_count": 24,
        "document_type": "quarterly_report",
        "profile_version": 1,
        "language": "en",
        "fund_name_read": "Fund X",
        "manager_name_read": "Example Capital",
        "document_date": date(2026, 9, 30),
        "period_label": "Q3 2026",
        "currency": "EUR",
        "findings": [_finding()],
        "note": {
            "highlights": [f"Two exits signed {_PAYLOAD_MARK}"],
            "quotes": [{"text": "A strong quarter.", "page": 3}],
        },
        "model": "vendor/model-a",
        "prompt_version": "0f1e2d3c",
        "adhoc_keywords": [{"name": "DPI", "type": "Number"}],
        "search_text": f"Fund X Q3 2026 exit pipeline {_PAYLOAD_MARK}",
        "now": _T0,
    }
    kwargs.update(overrides)
    return kwargs


async def _seed_user(app_engine: AsyncEngine, tenant_id, email: str) -> UUID:
    """Insert a user in ``tenant_id`` and return its id (for the reading FKs)."""
    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(email=email, password_hash="x" * 8)
    return user.id


async def _seed_investments(app_engine: AsyncEngine, tenant_id, user_id, count: int) -> list:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="dr_class", display_name="DR Class"
        )
        investments = InvestmentRepository(session)
        return [
            (
                await investments.create(
                    name=f"DR Fund {i}",
                    investment_type="private_equity",
                    asset_class_id=asset_class.id,
                    currency="EUR",
                    created_by=user_id,
                )
            ).id
            for i in range(count)
        ]


async def _seed_attachment(app_engine: AsyncEngine, tenant_id, user_id) -> UUID:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        case = await CaseRepository(session).create(
            title="Case", opened_by=user_id, opened_actor="pm", now=_T0
        )
        attachment = await CaseAttachmentRepository(session).create(
            case.id,
            filename="notice.pdf",
            mime_type="application/pdf",
            size_bytes=4,
            sha256=_OTHER_SHA,
            content=b"%PDF",
            uploaded_by=user_id,
            now=_T0,
        )
    return attachment.id


async def _create(
    app_engine: AsyncEngine, tenant_id, user_id, **overrides: Any
) -> DocumentReadingDTO:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        return await DocumentReadingRepository(session).create(
            read_by=user_id, **_reading_kwargs(**overrides)
        )


async def _assign(
    app_engine: AsyncEngine, tenant_id, user_id, reading_id, **target: Any
) -> DocumentReadingDTO:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        return await DocumentReadingRepository(session).assign(
            reading_id, assigned_by=user_id, now=_T1, **target
        )


async def _remove(
    app_engine: AsyncEngine, tenant_id, user_id, reading_id, reason: str = "Uploaded in error"
) -> DocumentReadingDTO:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        return await DocumentReadingRepository(session).remove(
            reading_id, reason=reason, removed_by=user_id, now=_T2
        )


async def _run_purge(
    app_engine: AsyncEngine, tenant_id, user_id, *, years: int = 5, now: datetime = _T2
) -> ReadingPurgeDTO:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        return await DocumentReadingRepository(session).run_purge(
            years=years, today=_TODAY, run_by=user_id, now=now
        )


async def _get(app_engine: AsyncEngine, tenant_id, reading_id) -> DocumentReadingDTO:
    async with tenant_context(app_engine, tenant_id) as session:
        reading = await DocumentReadingRepository(session).get(reading_id)
    assert reading is not None
    return reading


async def _count(app_engine: AsyncEngine, tenant_id) -> int:
    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(text("SELECT COUNT(*) FROM document_readings"))
        return result.scalar_one()


# ---------------------------------------------------------------------------
# DR-01: numbering
# ---------------------------------------------------------------------------


async def test_dr01_numbering_is_per_tenant(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant("DR-01 A")
    tenant_b = await seed_tenant("DR-01 B")
    user_a = await _seed_user(app_engine, tenant_a, "pm@dr01a.example")
    user_b = await _seed_user(app_engine, tenant_b, "pm@dr01b.example")

    numbers = [(await _create(app_engine, tenant_a, user_a)).reading_number for _ in range(3)]
    assert numbers == [1, 2, 3]
    assert (await _create(app_engine, tenant_b, user_b)).reading_number == 1

    async with tenant_context(app_engine, tenant_a) as session:
        found = await session.execute(
            text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'uq_document_readings_tenant_reading_number'"
            )
        )
        assert found.scalar_one_or_none() == 1


async def test_dr01_number_collision_retries_once(
    app_engine: AsyncEngine, seed_tenant, monkeypatch
) -> None:
    tenant_id = await seed_tenant("DR-01c")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr01c.example")
    await _create(app_engine, tenant_id, user_id)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = DocumentReadingRepository(session)
        calls = {"n": 0}
        original = repo._attempt_create

        async def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("INSERT ...", {}, _FakeUniqueViolation())
            return await original(**kwargs)

        monkeypatch.setattr(repo, "_attempt_create", flaky)
        second = await repo.create(read_by=user_id, **_reading_kwargs())

    assert calls["n"] == 2
    assert second.reading_number == 2


async def test_dr01_second_collision_propagates(
    app_engine: AsyncEngine, seed_tenant, monkeypatch
) -> None:
    tenant_id = await seed_tenant("DR-01d")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr01d.example")

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = DocumentReadingRepository(session)
        calls = {"n": 0}

        async def colliding(**kwargs):
            calls["n"] += 1
            raise IntegrityError("INSERT ...", {}, _FakeUniqueViolation())

        monkeypatch.setattr(repo, "_attempt_create", colliding)
        with pytest.raises(IntegrityError):
            await repo.create(read_by=user_id, **_reading_kwargs())

    assert calls["n"] == 2


async def test_dr01_tombstone_keeps_its_number(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("DR-01e")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr01e.example")
    await _create(app_engine, tenant_id, user_id)
    newest = await _create(app_engine, tenant_id, user_id)

    tombstone = await _remove(app_engine, tenant_id, user_id, newest.id)
    assert tombstone.reading_number == 2
    assert (await _create(app_engine, tenant_id, user_id)).reading_number == 3


# ---------------------------------------------------------------------------
# DR-02: create
# ---------------------------------------------------------------------------


async def test_dr02_create_round_trips_every_field(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("DR-02")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr02.example")

    created = await _create(app_engine, tenant_id, user_id)
    expected = _reading_kwargs()
    expected["read_at"] = expected.pop("now")
    expected.update(
        id=created.id,
        tenant_id=tenant_id,
        reading_number=1,
        read_by=user_id,
        origin="upload",
        trust="untrusted",
        case_attachment_id=None,
        investment_id=None,
        manager_name=None,
        assigned_by=None,
        assigned_at=None,
        removed_at=None,
        removed_by=None,
        removal_reason=None,
        purge_id=None,
    )
    assert asdict(created) == expected
    assert await _get(app_engine, tenant_id, created.id) == created
    assert created.label == "R-0001"
    assert created.trust == "untrusted"
    assert created.is_removed is False


async def test_dr02_create_from_case_attachment(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("DR-02a")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr02a.example")
    attachment_id = await _seed_attachment(app_engine, tenant_id, user_id)

    created = await _create(
        app_engine,
        tenant_id,
        user_id,
        origin="case_attachment",
        case_attachment_id=attachment_id,
    )
    assert (created.origin, created.case_attachment_id) == ("case_attachment", attachment_id)


async def test_dr02_create_accepts_the_optional_fields_empty(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("DR-02b")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr02b.example")
    optional = (
        "page_count",
        "language",
        "fund_name_read",
        "manager_name_read",
        "document_date",
        "period_label",
        "currency",
    )

    created = await _create(
        app_engine,
        tenant_id,
        user_id,
        findings=[],
        note={},
        adhoc_keywords=[],
        search_text="",
        **dict.fromkeys(optional),
    )
    assert all(getattr(created, name) is None for name in optional)
    assert (created.findings, created.note, created.search_text) == ([], {}, "")


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"filename": "  "}, "filename", id="filename-blank"),
        pytest.param({"sha256": "A" * 64}, "sha256", id="sha256-upper-case"),
        pytest.param({"sha256": "a" * 63}, "sha256", id="sha256-63-chars"),
        pytest.param({"sha256": "a" * 64 + "\n"}, "sha256", id="sha256-trailing-newline"),
        pytest.param({"size_bytes": -1}, "size_bytes", id="size-negative"),
        pytest.param({"page_count": 0}, "page_count", id="page-count-zero"),
        pytest.param({"origin": "email"}, "origin", id="origin-unknown"),
        pytest.param(
            {"origin": "case_attachment"}, "case_attachment_id", id="attachment-without-id"
        ),
        pytest.param({"case_attachment_id": uuid4()}, "case_attachment_id", id="upload-with-id"),
        pytest.param(
            {"origin": "case_attachment", "case_attachment_id": uuid4()},
            "case_attachment_id",
            id="attachment-unknown",
        ),
        pytest.param({"document_type": "agm_papers"}, "document_type", id="type-unknown"),
        pytest.param({"profile_version": 0}, "profile_version", id="profile-version-zero"),
        pytest.param({"model": " "}, "model", id="model-blank"),
        pytest.param({"prompt_version": ""}, "prompt_version", id="prompt-version-empty"),
        pytest.param({"findings": None}, "findings", id="findings-none"),
        pytest.param({"findings": {"key": "nav"}}, "findings", id="findings-not-a-list"),
        pytest.param({"findings": ["nav"]}, "findings", id="finding-not-a-dict"),
        pytest.param({"findings": [_finding(key="")]}, "findings", id="finding-key-empty"),
        pytest.param(
            {"findings": [{k: v for k, v in _finding().items() if k != "key"}]},
            "findings",
            id="finding-key-missing",
        ),
        pytest.param({"findings": [_finding(origin="guess")]}, "findings", id="finding-origin"),
        pytest.param(
            {"findings": [_finding(confidence="high")]}, "findings", id="finding-confidence"
        ),
        pytest.param({"findings": [_finding(type="Money")]}, "findings", id="finding-type"),
        pytest.param(
            {"findings": [_finding(type=["Number"])]}, "findings", id="finding-type-unhashable"
        ),
        pytest.param({"note": None}, "note", id="note-none"),
        pytest.param({"note": ["highlight"]}, "note", id="note-not-a-dict"),
        pytest.param({"note": {"summary": "x"}}, "note", id="note-unknown-key"),
        pytest.param({"adhoc_keywords": None}, "adhoc_keywords", id="adhoc-none"),
        pytest.param({"search_text": None}, "search_text", id="search-text-none"),
    ],
)
async def test_dr02_create_refusals_write_nothing(
    app_engine: AsyncEngine, seed_tenant, overrides: dict[str, Any], field: str
) -> None:
    tenant_id = await seed_tenant("DR-02r")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr02r.example")
    await _create(app_engine, tenant_id, user_id)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        with pytest.raises(ReadingInvalid) as excinfo:
            await DocumentReadingRepository(session).create(
                read_by=user_id, **_reading_kwargs(**overrides)
            )
    assert excinfo.value.field == field
    assert await _count(app_engine, tenant_id) == 1


async def test_dr02_attachment_of_another_tenant_is_refused(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_a = await seed_tenant("DR-02x A")
    tenant_b = await seed_tenant("DR-02x B")
    user_a = await _seed_user(app_engine, tenant_a, "pm@dr02xa.example")
    user_b = await _seed_user(app_engine, tenant_b, "pm@dr02xb.example")
    foreign = await _seed_attachment(app_engine, tenant_a, user_a)

    async with tenant_context(app_engine, tenant_b, user_id=user_b) as session:
        with pytest.raises(ReadingInvalid) as excinfo:
            await DocumentReadingRepository(session).create(
                read_by=user_b,
                origin="case_attachment",
                case_attachment_id=foreign,
                **_reading_kwargs(),
            )
    assert excinfo.value.field == "case_attachment_id"
    assert await _count(app_engine, tenant_b) == 0


# ---------------------------------------------------------------------------
# DR-03: get
# ---------------------------------------------------------------------------


async def test_dr03_get_and_get_by_number(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant("DR-03 A")
    tenant_b = await seed_tenant("DR-03 B")
    user_a = await _seed_user(app_engine, tenant_a, "pm@dr03.example")
    live = await _create(app_engine, tenant_a, user_a)
    removed = await _create(app_engine, tenant_a, user_a)
    removed = await _remove(app_engine, tenant_a, user_a, removed.id)

    async with tenant_context(app_engine, tenant_a) as session:
        repo = DocumentReadingRepository(session)
        assert await repo.get(live.id) == live
        assert await repo.get(removed.id) == removed
        assert await repo.get_by_number(1) == live
        assert await repo.get_by_number(2) == removed
        assert await repo.get(uuid4()) is None
        assert await repo.get_by_number(99) is None

    async with tenant_context(app_engine, tenant_b) as session:
        repo = DocumentReadingRepository(session)
        assert await repo.get(live.id) is None
        assert await repo.get_by_number(1) is None


# ---------------------------------------------------------------------------
# DR-04: list
# ---------------------------------------------------------------------------


async def test_dr04_list_filters_order_and_limit(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("DR-04")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr04.example")
    [fund] = await _seed_investments(app_engine, tenant_id, user_id, 1)

    held = await _create(
        app_engine,
        tenant_id,
        user_id,
        manager_name_read="Alpha Partners",
        document_date=date(2026, 3, 31),
        now=_T0 + timedelta(days=4),
    )
    await _assign(app_engine, tenant_id, user_id, held.id, investment_id=fund)
    followed = await _create(
        app_engine,
        tenant_id,
        user_id,
        document_type="pitch_deck",
        manager_name_read="Beta LLP",
        document_date=date(2026, 6, 30),
        now=_T0 + timedelta(days=1),
    )
    await _assign(app_engine, tenant_id, user_id, followed.id, manager_name="Gamma Capital")
    undated = await _create(
        app_engine,
        tenant_id,
        user_id,
        manager_name_read="  gamma CAPITAL\n",
        document_date=None,
        now=_T0 + timedelta(days=3),
    )
    same_instant = await _create(
        app_engine,
        tenant_id,
        user_id,
        document_type="capital_notice",
        manager_name_read=None,
        document_date=date(2026, 9, 30),
        now=_T0 + timedelta(days=1),
    )
    gone = await _create(app_engine, tenant_id, user_id, now=_T0 + timedelta(days=2))
    await _remove(app_engine, tenant_id, user_id, gone.id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = DocumentReadingRepository(session)

        async def ids(**filters: Any) -> list[UUID]:
            return [reading.id for reading in await repo.list_readings(**filters)]

        # read_at descending, then the number descending.
        assert await ids() == [held.id, undated.id, same_instant.id, followed.id]
        assert await ids(include_removed=True) == [
            held.id,
            undated.id,
            gone.id,
            same_instant.id,
            followed.id,
        ]
        assert await ids(limit=2) == [held.id, undated.id]

        assert await ids(document_type="pitch_deck") == [followed.id]
        assert await ids(investment_id=fund) == [held.id]
        # The assigned name and the name as read, case-insensitively.
        assert await ids(manager_name=" GAMMA capital ") == [undated.id, followed.id]
        assert await ids(manager_name="beta llp") == [followed.id]
        assert await ids(manager_name="alpha partners") == [held.id]
        assert await ids(manager_name="Gamma") == []
        assert await ids(manager_name="  ") == []
        assert await ids(assigned=True) == [held.id, followed.id]
        assert await ids(assigned=False) == [undated.id, same_instant.id]

        # Inclusive on both ends; a reading without a document date drops out.
        assert await ids(document_date_from=date(2026, 6, 30)) == [same_instant.id, followed.id]
        assert await ids(document_date_to=date(2026, 6, 30)) == [held.id, followed.id]
        assert await ids(
            document_date_from=date(2026, 3, 31), document_date_to=date(2026, 6, 30)
        ) == [held.id, followed.id]

        assert await ids(document_type="quarterly_report", assigned=False) == [undated.id]

        with pytest.raises(ReadingInvalid) as excinfo:
            await repo.list_readings(document_type="agm_papers")
        assert excinfo.value.field == "document_type"


async def test_dr04b_list_offset_pages_without_gap_or_repeat(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("DR-04b")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr04b.example")
    readings = [
        await _create(
            app_engine,
            tenant_id,
            user_id,
            document_type="pitch_deck" if i % 2 else "quarterly_report",
            now=_T0 + timedelta(hours=i),
        )
        for i in range(5)
    ]
    await _remove(app_engine, tenant_id, user_id, readings[2].id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = DocumentReadingRepository(session)

        async def ids(**filters: Any) -> list[UUID]:
            return [reading.id for reading in await repo.list_readings(**filters)]

        for filters in (
            {"include_removed": True},
            {},
            {"document_type": "quarterly_report", "include_removed": True},
            {"document_type": "pitch_deck"},
        ):
            unpaged = await ids(**filters)
            paged = [
                reading_id
                for offset in (0, 2, 4)
                for reading_id in await ids(limit=2, offset=offset, **filters)
            ]
            assert paged == unpaged, filters

        assert len(await ids(include_removed=True)) == 5
        assert await ids(limit=2, offset=2, include_removed=True) == [
            readings[2].id,
            readings[1].id,
        ]
        assert await ids(offset=5, include_removed=True) == []
        assert await ids(limit=2, offset=10) == []
        assert await ids(document_type="pitch_deck", offset=1) == [readings[1].id]

        with pytest.raises(ReadingInvalid) as excinfo:
            await repo.list_readings(offset=-1)
        assert excinfo.value.field == "offset"


# ---------------------------------------------------------------------------
# DR-05: search
# ---------------------------------------------------------------------------


async def test_dr05_search(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant("DR-05 A")
    tenant_b = await seed_tenant("DR-05 B")
    user_a = await _seed_user(app_engine, tenant_a, "pm@dr05a.example")
    user_b = await _seed_user(app_engine, tenant_b, "pm@dr05b.example")

    percent = await _create(app_engine, tenant_a, user_a, search_text="Realised 80% of NAV")
    digits = await _create(app_engine, tenant_a, user_a, search_text="Realised 800 of nav", now=_T1)
    underscore = await _create(app_engine, tenant_a, user_a, search_text=r"exit_pipeline C:\deal")
    await _create(app_engine, tenant_a, user_a, search_text="exitXpipeline")
    gone = await _create(app_engine, tenant_a, user_a, search_text="Tombstoned phrase")
    await _remove(app_engine, tenant_a, user_a, gone.id)
    await _create(app_engine, tenant_b, user_b, search_text="Foreign phrase")

    async with tenant_context(app_engine, tenant_a) as session:
        repo = DocumentReadingRepository(session)

        async def ids(query: str, limit: int | None = None) -> list[UUID]:
            return [reading.id for reading in await repo.search(query, limit=limit)]

        assert await ids("REALISED") == [digits.id, percent.id]
        assert await ids("realised", limit=1) == [digits.id]
        assert await ids("80%") == [percent.id]
        assert await ids("exit_pipeline") == [underscore.id]
        assert await ids(r"c:\deal") == [underscore.id]
        assert await ids("") == []
        assert await ids(" \n ") == []
        assert await ids("tombstoned") == []
        assert await ids("foreign") == []


# ---------------------------------------------------------------------------
# DR-06: dedup lookup
# ---------------------------------------------------------------------------


async def test_dr06_find_live_by_sha256(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant("DR-06 A")
    tenant_b = await seed_tenant("DR-06 B")
    user_a = await _seed_user(app_engine, tenant_a, "pm@dr06a.example")
    user_b = await _seed_user(app_engine, tenant_b, "pm@dr06b.example")

    # The newest read carries the lower number: the order is read_at's.
    newest = await _create(app_engine, tenant_a, user_a, now=_T2)
    older = await _create(app_engine, tenant_a, user_a, now=_T0)
    gone = await _create(app_engine, tenant_a, user_a, now=_T1)
    await _remove(app_engine, tenant_a, user_a, gone.id)
    await _create(app_engine, tenant_a, user_a, sha256=_OTHER_SHA)
    await _create(app_engine, tenant_b, user_b)

    async with tenant_context(app_engine, tenant_a) as session:
        repo = DocumentReadingRepository(session)
        assert await repo.find_live_by_sha256(_SHA) == [newest, older]
        with pytest.raises(ReadingInvalid) as excinfo:
            await repo.find_live_by_sha256("A" * 64)
        assert excinfo.value.field == "sha256"


# ---------------------------------------------------------------------------
# DR-07: assign
# ---------------------------------------------------------------------------


async def test_dr07_assign_and_clear(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("DR-07")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr07.example")
    second_user = await _seed_user(app_engine, tenant_id, "deputy@dr07.example")
    [fund] = await _seed_investments(app_engine, tenant_id, user_id, 1)
    reading = await _create(app_engine, tenant_id, user_id)

    held = await _assign(app_engine, tenant_id, user_id, reading.id, investment_id=fund)
    assert (held.investment_id, held.manager_name) == (fund, None)
    assert (held.assigned_by, held.assigned_at) == (user_id, _T1)

    named = await _assign(app_engine, tenant_id, user_id, reading.id, manager_name="  Example  ")
    assert (named.investment_id, named.manager_name) == (None, "Example")

    async with tenant_context(app_engine, tenant_id, user_id=second_user) as session:
        cleared = await DocumentReadingRepository(session).assign(
            reading.id, assigned_by=second_user, now=_T2
        )
    assert (cleared.investment_id, cleared.manager_name) == (None, None)
    assert (cleared.assigned_by, cleared.assigned_at) == (second_user, _T2)
    assert await _get(app_engine, tenant_id, reading.id) == cleared


async def test_dr07_assign_refusals(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant("DR-07r A")
    tenant_b = await seed_tenant("DR-07r B")
    user_a = await _seed_user(app_engine, tenant_a, "pm@dr07ra.example")
    user_b = await _seed_user(app_engine, tenant_b, "pm@dr07rb.example")
    [own] = await _seed_investments(app_engine, tenant_a, user_a, 1)
    [foreign] = await _seed_investments(app_engine, tenant_b, user_b, 1)
    reading = await _create(app_engine, tenant_a, user_a)
    gone = await _create(app_engine, tenant_a, user_a)
    await _remove(app_engine, tenant_a, user_a, gone.id)

    refusals: list[tuple[dict[str, Any], str]] = [
        ({"investment_id": own, "manager_name": "Example"}, "assignment"),
        ({"manager_name": "  "}, "manager_name"),
        ({"investment_id": foreign}, "investment_id"),
        ({"investment_id": uuid4()}, "investment_id"),
    ]
    for target, field in refusals:
        with pytest.raises(ReadingInvalid) as excinfo:
            await _assign(app_engine, tenant_a, user_a, reading.id, **target)
        assert excinfo.value.field == field
    assert await _get(app_engine, tenant_a, reading.id) == reading

    with pytest.raises(ReadingRemoved):
        await _assign(app_engine, tenant_a, user_a, gone.id, manager_name="Example")
    with pytest.raises(ReadingNotFound):
        await _assign(app_engine, tenant_a, user_a, uuid4(), manager_name="Example")


async def test_dr07_deleting_the_investment_unassigns(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("DR-07d")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr07d.example")
    [fund] = await _seed_investments(app_engine, tenant_id, user_id, 1)
    reading = await _create(app_engine, tenant_id, user_id)
    await _assign(app_engine, tenant_id, user_id, reading.id, investment_id=fund)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        assert await InvestmentRepository(session).delete(fund) is True

    survivor = await _get(app_engine, tenant_id, reading.id)
    assert survivor.investment_id is None
    assert survivor.fund_name_read == "Fund X"
    # Left set on purpose: no CHECK ties them to the assignment columns.
    assert (survivor.assigned_by, survivor.assigned_at) == (user_id, _T1)


# ---------------------------------------------------------------------------
# DR-08: remove
# ---------------------------------------------------------------------------


async def test_dr08_remove_purges_the_three_payload_columns(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("DR-08")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr08.example")
    created = await _create(app_engine, tenant_id, user_id)
    live = await _assign(app_engine, tenant_id, user_id, created.id, manager_name="Example")
    for column in PAYLOAD_COLUMNS:
        assert getattr(live, column) is not None

    for blank in ("", "  \n"):
        with pytest.raises(ReadingInvalid) as excinfo:
            await _remove(app_engine, tenant_id, user_id, live.id, reason=blank)
        assert excinfo.value.field == "reason"
    assert await _get(app_engine, tenant_id, live.id) == live

    tombstone = await _remove(app_engine, tenant_id, user_id, live.id, reason="  Wrong fund  ")
    assert tombstone.is_removed is True
    assert (tombstone.removed_at, tombstone.removed_by) == (_T2, user_id)
    assert (tombstone.removal_reason, tombstone.purge_id) == ("Wrong fund", None)
    for column in PAYLOAD_COLUMNS:
        assert getattr(tombstone, column) is None

    untouched = set(asdict(live)) - {*PAYLOAD_COLUMNS, "removed_at", "removed_by", "removal_reason"}
    for name in untouched:
        assert getattr(tombstone, name) == getattr(live, name), name

    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(
            text(
                "SELECT findings IS NULL AND note IS NULL AND search_text IS NULL "
                "FROM document_readings WHERE id = :id"
            ),
            {"id": str(live.id)},
        )
        # SQL NULL, not the JSON value null.
        assert result.scalar_one() is True

    with pytest.raises(ReadingRemoved):
        await _remove(app_engine, tenant_id, user_id, live.id)
    with pytest.raises(ReadingNotFound):
        await _remove(app_engine, tenant_id, user_id, uuid4())


# ---------------------------------------------------------------------------
# DR-09: retention
# ---------------------------------------------------------------------------


async def test_dr09_retention_run(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant("DR-09 A")
    tenant_b = await seed_tenant("DR-09 B")
    user_a = await _seed_user(app_engine, tenant_a, "pm@dr09a.example")
    user_b = await _seed_user(app_engine, tenant_b, "pm@dr09b.example")
    cutoff = date(2021, 11, 22)
    long_ago = datetime(2020, 5, 1, 9, 0, tzinfo=timezone.utc)

    async def reading(document_date: date | None, now: datetime = _T0) -> DocumentReadingDTO:
        return await _create(app_engine, tenant_a, user_a, document_date=document_date, now=now)

    day_before = await reading(cutoff - timedelta(days=1))
    undated_old = await reading(None, long_ago)
    # Still 21 November in UTC, whatever the session's time zone says.
    undated_boundary = await reading(None, datetime(2021, 11, 21, 23, 30, tzinfo=timezone.utc))
    on_cutoff = await reading(cutoff)
    newer = await reading(cutoff + timedelta(days=1))
    undated_recent = await reading(None)
    # The document date takes precedence over an old read date.
    read_long_ago = await reading(date(2026, 9, 30), long_ago)
    already = await reading(date(2019, 1, 1))
    already = await _remove(app_engine, tenant_a, user_a, already.id)
    foreign = await _create(app_engine, tenant_b, user_b, document_date=date(2019, 1, 1))

    async with tenant_context(app_engine, tenant_a) as session:
        await session.execute(text("SET LOCAL TIME ZONE 'Pacific/Auckland'"))
        preview = await DocumentReadingRepository(session).preview_purge(years=5, today=_TODAY)
    assert (preview.years, preview.cutoff, preview.reading_count) == (5, cutoff, 3)
    assert await _get(app_engine, tenant_a, day_before.id) == day_before  # writes nothing

    purge = await _run_purge(app_engine, tenant_a, user_a)
    assert purge.tenant_id == tenant_a
    assert purge.criterion == "document_date"
    assert (purge.years, purge.cutoff, purge.reading_count) == (5, cutoff, 3)
    assert (purge.run_by, purge.run_at) == (user_a, _T2)

    for taken in (day_before, undated_old, undated_boundary):
        tombstone = await _get(app_engine, tenant_a, taken.id)
        assert (tombstone.removed_at, tombstone.removed_by) == (_T2, user_a)
        assert (tombstone.purge_id, tombstone.removal_reason) == (purge.id, None)
        for column in PAYLOAD_COLUMNS:
            assert getattr(tombstone, column) is None
    for untouched in (on_cutoff, newer, undated_recent, read_long_ago, already):
        assert await _get(app_engine, tenant_a, untouched.id) == untouched
    assert await _get(app_engine, tenant_b, foreign.id) == foreign

    async with tenant_context(app_engine, tenant_a) as session:
        assert await DocumentReadingRepository(session).list_purges() == [purge]

    # A run that happened is recorded, also when it matched nothing.
    empty = await _run_purge(app_engine, tenant_a, user_a, now=_T2 + timedelta(hours=1))
    assert empty.reading_count == 0
    async with tenant_context(app_engine, tenant_a) as session:
        assert await DocumentReadingRepository(session).list_purges() == [empty, purge]


@pytest.mark.parametrize(
    ("years", "today", "cutoff"),
    [
        (1, date(2028, 2, 29), date(2027, 2, 28)),
        (4, date(2028, 2, 29), date(2024, 2, 29)),
        (10, date(2026, 11, 22), date(2016, 11, 22)),
    ],
)
async def test_dr09_cutoff_in_calendar_years(
    app_engine: AsyncEngine, seed_tenant, years: int, today: date, cutoff: date
) -> None:
    tenant_id = await seed_tenant("DR-09c")
    async with tenant_context(app_engine, tenant_id) as session:
        preview = await DocumentReadingRepository(session).preview_purge(years=years, today=today)
    assert preview.cutoff == cutoff


@pytest.mark.parametrize("years", [0, -1, 2026])
async def test_dr09_years_out_of_range(app_engine: AsyncEngine, seed_tenant, years: int) -> None:
    tenant_id = await seed_tenant("DR-09y")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr09y.example")
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = DocumentReadingRepository(session)
        with pytest.raises(ReadingInvalid) as preview_error:
            await repo.preview_purge(years=years, today=_TODAY)
        with pytest.raises(ReadingInvalid) as run_error:
            await repo.run_purge(years=years, today=_TODAY, run_by=user_id, now=_T2)
        assert await repo.list_purges() == []
    assert preview_error.value.field == run_error.value.field == "years"


# ---------------------------------------------------------------------------
# DR-10: audit (ADR-0140)
# ---------------------------------------------------------------------------


async def test_dr10_audit_images_exclude_the_payload(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("DR-10")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr10.example")
    reason = "Uploaded in error"

    # Separate transactions, so audit_log.created_at orders the rows.
    a = await _create(app_engine, tenant_id, user_id)
    await _assign(app_engine, tenant_id, user_id, a.id, manager_name="Example")
    await _remove(app_engine, tenant_id, user_id, a.id, reason=reason)
    b = await _create(app_engine, tenant_id, user_id, document_date=date(2019, 1, 1))
    purge = await _run_purge(app_engine, tenant_id, user_id)

    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(
            text(
                """
                SELECT tenant_id, user_id, record_id, table_name, operation, old_data, new_data
                FROM audit_log
                WHERE table_name IN ('document_readings', 'reading_purges')
                ORDER BY created_at
                """
            )
        )
        rows = result.mappings().all()
        columns: dict[str, set[str]] = {}
        for table in ("document_readings", "reading_purges"):
            found = await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :t"
                ),
                {"t": table},
            )
            columns[table] = set(found.scalars().all())

    readings = [row for row in rows if row["table_name"] == "document_readings"]
    by_record: dict[UUID, list] = {}
    for row in readings:
        by_record.setdefault(row["record_id"], []).append(row)

    # DR-10a: exactly these rows, and the payload keys in none of their images.
    assert {key: [row["operation"] for row in group] for key, group in by_record.items()} == {
        a.id: ["INSERT", "UPDATE", "UPDATE"],
        b.id: ["INSERT", "UPDATE"],
    }
    for row in readings:
        for image in (row["old_data"], row["new_data"]):
            if image is None:
                continue
            assert set(image).isdisjoint(PAYLOAD_COLUMNS)
            assert _PAYLOAD_MARK not in json.dumps(image)
            # DR-10b: the rest is there.
            assert set(image) == columns["document_readings"] - set(PAYLOAD_COLUMNS)
        assert (row["old_data"] is None) == (row["operation"] == "INSERT")
        assert row["new_data"] is not None
        # DR-10c: attribution. Record id and operation are pinned by 10a above.
        assert row["tenant_id"] == tenant_id
        assert row["user_id"] == user_id
        assert row["new_data"]["id"] == str(row["record_id"])

    # DR-10d: the removal's row, and the retention run's.
    removal = by_record[a.id][-1]
    assert removal["new_data"]["removal_reason"] == reason
    assert removal["new_data"]["removed_by"] == str(user_id)
    assert removal["old_data"]["removal_reason"] is None
    purged = by_record[b.id][-1]
    assert purged["new_data"]["purge_id"] == str(purge.id)
    assert purged["new_data"]["removal_reason"] is None

    # DR-10e: the generic trigger beside it, with a full image.
    [run] = [row for row in rows if row["table_name"] == "reading_purges"]
    assert (run["operation"], run["record_id"]) == ("INSERT", purge.id)
    assert (run["tenant_id"], run["user_id"]) == (tenant_id, user_id)
    assert set(run["new_data"]) == columns["reading_purges"]


async def test_dr10f_excluded_list_is_the_purged_list(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("DR-10f")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr10f.example")
    created = await _create(app_engine, tenant_id, user_id)
    live = await _assign(app_engine, tenant_id, user_id, created.id, manager_name="Example")
    tombstone = await _remove(app_engine, tenant_id, user_id, live.id)

    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(
            text(
                "SELECT pg_get_triggerdef(oid) FROM pg_trigger "
                "WHERE tgname = 'document_readings_audit_trigger'"
            )
        )
        definition = result.scalar_one()
    match = re.search(r"audit_trigger_function_excluding\((.*)\)$", definition)
    assert match is not None, definition
    assert re.findall(r"'([^']*)'", match.group(1)) == list(PAYLOAD_COLUMNS)

    after = asdict(tombstone)
    purged = {
        name for name, value in asdict(live).items() if value is not None and after[name] is None
    }
    assert purged == set(PAYLOAD_COLUMNS)


# ---------------------------------------------------------------------------
# DR-11: isolation
# ---------------------------------------------------------------------------


async def test_dr11_isolation(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant("DR-11 A")
    tenant_b = await seed_tenant("DR-11 B")
    user_a = await _seed_user(app_engine, tenant_a, "pm@dr11a.example")
    user_b = await _seed_user(app_engine, tenant_b, "pm@dr11b.example")
    [fund] = await _seed_investments(app_engine, tenant_a, user_a, 1)
    created = await _create(app_engine, tenant_a, user_a)
    reading = await _assign(app_engine, tenant_a, user_a, created.id, investment_id=fund)
    await _run_purge(app_engine, tenant_a, user_a)

    async with tenant_context(app_engine, tenant_b, user_id=user_b) as session:
        repo = DocumentReadingRepository(session)
        assert await repo.list_readings(include_removed=True) == []
        assert await repo.search("pipeline") == []
        assert await repo.find_live_by_sha256(_SHA) == []
        assert await repo.list_for_investment(fund) == []
        assert await repo.list_purges() == []
        with pytest.raises(ReadingNotFound):
            await repo.assign(reading.id, manager_name="Other", assigned_by=user_b, now=_T2)
        with pytest.raises(ReadingNotFound):
            await repo.remove(reading.id, reason="Not mine", removed_by=user_b, now=_T2)

    assert await _get(app_engine, tenant_a, reading.id) == reading


# ---------------------------------------------------------------------------
# DR-12: the investment's list
# ---------------------------------------------------------------------------


async def test_dr12_list_for_investment(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("DR-12")
    user_id = await _seed_user(app_engine, tenant_id, "pm@dr12.example")
    fund, other_fund = await _seed_investments(app_engine, tenant_id, user_id, 2)

    async def assigned(investment_id: UUID, **overrides: Any) -> UUID:
        reading = await _create(app_engine, tenant_id, user_id, **overrides)
        await _assign(app_engine, tenant_id, user_id, reading.id, investment_id=investment_id)
        return reading.id

    june_first = await assigned(fund, document_date=date(2026, 6, 30), now=_T2)
    undated = await assigned(fund, document_date=None, now=_T0)  # effective: the read date
    march = await assigned(fund, document_date=date(2026, 3, 31), now=_T2)
    june_second = await assigned(fund, document_date=date(2026, 6, 30), now=_T0)
    gone = await assigned(fund, document_date=date(2026, 9, 30))
    await _remove(app_engine, tenant_id, user_id, gone)
    await assigned(other_fund)
    await _create(app_engine, tenant_id, user_id)  # unassigned

    async with tenant_context(app_engine, tenant_id) as session:
        listed = await DocumentReadingRepository(session).list_for_investment(fund)
    assert [reading.id for reading in listed] == [undated, june_second, june_first, march]


# ---------------------------------------------------------------------------
# DR-13: the manager names
# ---------------------------------------------------------------------------


async def test_dr13_list_manager_names(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant("DR-13 A")
    tenant_b = await seed_tenant("DR-13 B")
    user_a = await _seed_user(app_engine, tenant_a, "pm@dr13a.example")
    user_b = await _seed_user(app_engine, tenant_b, "pm@dr13b.example")
    [fund] = await _seed_investments(app_engine, tenant_a, user_a, 1)

    async def assigned(tenant_id, user_id, **target: Any) -> DocumentReadingDTO:
        reading = await _create(app_engine, tenant_id, user_id, manager_name_read="Read Only LLP")
        return await _assign(app_engine, tenant_id, user_id, reading.id, **target)

    await assigned(tenant_a, user_a, manager_name="beta Partners")
    await assigned(tenant_a, user_a, manager_name="Alpha Capital")
    await assigned(tenant_a, user_a, manager_name="Alpha Capital")
    await assigned(tenant_a, user_a, manager_name="Zeta Advisors")
    await assigned(tenant_a, user_a, investment_id=fund)
    await _create(app_engine, tenant_a, user_a, manager_name_read="Unassigned Read GP")
    gone = await assigned(tenant_a, user_a, manager_name="Gone GP")
    await _remove(app_engine, tenant_a, user_a, gone.id)
    await assigned(tenant_b, user_b, manager_name="Foreign GP")

    async with tenant_context(app_engine, tenant_a) as session:
        names = await DocumentReadingRepository(session).list_manager_names()
    assert names == ["Alpha Capital", "beta Partners", "Zeta Advisors"]

    async with tenant_context(app_engine, tenant_b) as session:
        assert await DocumentReadingRepository(session).list_manager_names() == ["Foreign GP"]
