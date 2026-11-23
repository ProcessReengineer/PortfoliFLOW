# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Reading Desk writer against the live test Postgres (ADR-0139 §5).

``write_readings`` pairs extractions and uploads by position, writes one
reading per successful extraction in its own transaction, retries a
reading-number conflict as a whole transaction, and fails per file with one
log line that names the file and the exception type and nothing else.

All dates are fixed; none derives from the day the suite runs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    DocumentReadingDTO,
    DocumentReadingRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from services.reading_desk import (
    AssignmentProposal,
    ReadingOutcome,
    UploadedDocument,
    describe_upload,
    find_earlier_readings,
    write_readings,
)
from services.reading_desk import writer as writer_module
from services.scraper.models import (
    FINDING_ORIGIN_ADHOC,
    FINDING_ORIGIN_PROFILE,
    Confidence,
    Finding,
    Keyword,
    KeywordType,
    NoteQuote,
    ReadingNote,
    ReportExtraction,
    ScraperResult,
)

_T0 = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
_MODEL = "vendor/model-a"
_PROMPT_VERSION = "0f1e2d3c"
_LOGGER = "services.reading_desk.writer"
_MARKER = "statement-param-marker-5e1f"


class _FakeUniqueViolation(Exception):
    """Stand-in for asyncpg's ``UniqueViolationError`` on the reading number."""

    constraint_name = "uq_document_readings_tenant_reading_number"


class _FakeOtherViolation(Exception):
    """Stand-in for a driver error on some other constraint."""

    constraint_name = "ck_some_other_rule"


# ---------------------------------------------------------------------------
# Helpers (after tests/repositories/test_document_reading_repository.py)
# ---------------------------------------------------------------------------


async def _seed_user(app_engine: AsyncEngine, tenant_id: UUID, email: str) -> UUID:
    """Insert a user in ``tenant_id`` and return its id (for the reading FKs)."""
    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(email=email, password_hash="x" * 8)
    return user.id


async def _seed_investments(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    named: list[tuple[str, str | None]],
) -> None:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="rd_class", display_name="RD Class"
        )
        investments = InvestmentRepository(session)
        for name, manager_name in named:
            await investments.create(
                name=name,
                investment_type="private_equity",
                asset_class_id=asset_class.id,
                currency="EUR",
                created_by=user_id,
                manager_name=manager_name,
            )


def _extraction(**overrides: Any) -> ReportExtraction:
    extraction = ReportExtraction(
        filename="fund-x-q3.pdf",
        fund_name="Fund X",
        period="Q3 2026",
        findings=[
            Finding(
                keyword=Keyword(name="NAV", type=KeywordType.NUMBER),
                value="12.5",
                source="Page 12",
                confidence=Confidence.HIGH,
                key="nav",
                label="NAV",
                unit="EUR m",
                page=12,
                quote="The NAV stood at 12.5 million.",
                origin=FINDING_ORIGIN_PROFILE,
            ),
            Finding(
                keyword=Keyword(name="DPI", type=KeywordType.NUMBER),
                value="0.4",
                source="Page 3",
                confidence=Confidence.MEDIUM,
                key="dpi",
                label="",
                origin=FINDING_ORIGIN_ADHOC,
            ),
        ],
        document_type="quarterly_report",
        profile_version=1,
        language="en",
        manager_name="Example Capital",
        document_date=date(2026, 9, 30),
        currency="EUR",
        page_count=24,
        note=ReadingNote(
            highlights=["Two exits signed"],
            quotes=[NoteQuote(text="A strong quarter.", page=3)],
            events_and_risks=["Key person change"],
            outlook="Stable",
        ),
    )
    return dataclasses.replace(extraction, **overrides)


def _failed(filename: str = "broken.pdf") -> ReportExtraction:
    """A failed extraction as the scraper returns it: error set, no document type."""
    return ReportExtraction(filename=filename, error="The model returned no JSON.")


def _document(filename: str, content: bytes) -> UploadedDocument:
    return describe_upload(filename, content)


async def _write(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    documents: list[UploadedDocument],
    extractions: list[ReportExtraction],
    *,
    keywords: list[Keyword] | None = None,
) -> list[ReadingOutcome]:
    return await write_readings(
        app_engine,
        tenant_id=tenant_id,
        read_by=user_id,
        documents=documents,
        result=ScraperResult(extractions=extractions, prompt_version=_PROMPT_VERSION),
        keywords=keywords or [],
        model=_MODEL,
        now=_T0,
    )


async def _readings(app_engine: AsyncEngine, tenant_id: UUID) -> list[DocumentReadingDTO]:
    async with tenant_context(app_engine, tenant_id) as session:
        readings = await DocumentReadingRepository(session).list_readings(include_removed=True)
    return sorted(readings, key=lambda reading: reading.reading_number)


def _writer_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == _LOGGER]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_one_successful_extraction_writes_one_reading(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("RDW-1")
    user_id = await _seed_user(app_engine, tenant_id, "pm@rdw1.example")
    document = _document("fund-x-q3.pdf", b"%PDF one")

    outcomes = await _write(
        app_engine,
        tenant_id,
        user_id,
        [document],
        [_extraction()],
        keywords=[Keyword(name="DPI", type=KeywordType.NUMBER)],
    )

    assert outcomes == [ReadingOutcome(filename="fund-x-q3.pdf", label="R-0001", proposal=None)]
    async with tenant_context(app_engine, tenant_id) as session:
        reading = await DocumentReadingRepository(session).get_by_number(1)
    assert reading is not None
    assert reading.filename == "fund-x-q3.pdf"
    assert reading.sha256 == document.sha256
    assert reading.size_bytes == len(b"%PDF one")
    assert reading.page_count == 24
    assert reading.read_at == _T0
    assert reading.read_by == user_id
    assert reading.origin == "upload"
    assert reading.case_attachment_id is None
    assert reading.document_type == "quarterly_report"
    assert reading.profile_version == 1
    assert reading.language == "en"
    assert reading.fund_name_read == "Fund X"
    assert reading.manager_name_read == "Example Capital"
    assert reading.document_date == date(2026, 9, 30)
    assert reading.period_label == "Q3 2026"
    assert reading.currency == "EUR"
    assert reading.findings is not None
    assert [finding["key"] for finding in reading.findings] == ["nav", "dpi"]
    assert reading.findings[1]["label"] == "DPI"
    assert reading.findings[1]["origin"] == "adhoc"
    assert reading.findings[0]["confidence"] == "High"
    assert reading.note == {
        "highlights": ["Two exits signed"],
        "quotes": [{"text": "A strong quarter.", "page": 3}],
        "events_and_risks": ["Key person change"],
        "outlook": "Stable",
    }
    assert reading.model == _MODEL
    assert reading.prompt_version == _PROMPT_VERSION
    assert reading.adhoc_keywords == [{"name": "DPI", "type": "Number"}]
    assert reading.trust == "untrusted"
    assert reading.search_text is not None
    assert "Fund X" in reading.search_text
    assert reading.investment_id is None
    assert reading.manager_name is None
    assert reading.removed_at is None


async def test_failed_extraction_is_skipped_silently(
    app_engine: AsyncEngine, seed_tenant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    tenant_id = await seed_tenant("RDW-2")
    user_id = await _seed_user(app_engine, tenant_id, "pm@rdw2.example")

    outcomes = await _write(
        app_engine,
        tenant_id,
        user_id,
        [_document("broken.pdf", b"one"), _document("good.pdf", b"two")],
        [_failed(), _extraction()],
    )

    assert [(outcome.filename, outcome.label) for outcome in outcomes] == [
        ("broken.pdf", None),
        ("good.pdf", "R-0001"),
    ]
    assert outcomes[0].proposal is None
    assert len(await _readings(app_engine, tenant_id)) == 1
    assert _writer_records(caplog) == []


async def test_cancelled_run_writes_the_prefix(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("RDW-3")
    user_id = await _seed_user(app_engine, tenant_id, "pm@rdw3.example")
    documents = [_document(f"doc-{i}.pdf", f"bytes {i}".encode()) for i in range(3)]

    outcomes = await _write(
        app_engine, tenant_id, user_id, documents, [_extraction(), _extraction()]
    )

    assert [(outcome.filename, outcome.label) for outcome in outcomes] == [
        ("doc-0.pdf", "R-0001"),
        ("doc-1.pdf", "R-0002"),
    ]
    readings = await _readings(app_engine, tenant_id)
    assert [reading.sha256 for reading in readings] == [d.sha256 for d in documents[:2]]


async def test_surplus_extractions_are_not_written(
    app_engine: AsyncEngine, seed_tenant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    tenant_id = await seed_tenant("RDW-3b")
    user_id = await _seed_user(app_engine, tenant_id, "pm@rdw3b.example")

    outcomes = await _write(
        app_engine,
        tenant_id,
        user_id,
        [_document("only.pdf", b"only")],
        [_extraction(), _extraction(fund_name=f"Fund {_MARKER}")],
    )

    assert [outcome.label for outcome in outcomes] == ["R-0001"]
    assert len(await _readings(app_engine, tenant_id)) == 1
    records = _writer_records(caplog)
    assert len(records) == 1
    assert "2 extractions for 1 documents" in records[0].getMessage()
    assert _MARKER not in caplog.text


async def test_repeated_filename_pairs_by_position(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("RDW-4")
    user_id = await _seed_user(app_engine, tenant_id, "pm@rdw4.example")
    first = _document("report.pdf", b"first bytes")
    second = _document("report.pdf", b"second bytes")
    assert first.sha256 != second.sha256

    outcomes = await _write(
        app_engine,
        tenant_id,
        user_id,
        [first, second],
        [_extraction(fund_name="Fund One"), _extraction(fund_name="Fund Two")],
    )

    assert [outcome.label for outcome in outcomes] == ["R-0001", "R-0002"]
    readings = await _readings(app_engine, tenant_id)
    assert [(reading.sha256, reading.fund_name_read) for reading in readings] == [
        (first.sha256, "Fund One"),
        (second.sha256, "Fund Two"),
    ]


async def test_filename_and_keyword_are_stored_sanitised(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("RDW-5")
    user_id = await _seed_user(app_engine, tenant_id, "pm@rdw5.example")
    document = describe_upload("Q3\x00 report\ud800.pdf", b"%PDF five")

    outcomes = await _write(
        app_engine,
        tenant_id,
        user_id,
        [document],
        [_extraction()],
        keywords=[Keyword(name="D\x00P\udc00I 📈", type=KeywordType.NUMBER)],
    )

    assert outcomes[0].filename == "Q3 report.pdf"
    assert outcomes[0].label == "R-0001"
    (reading,) = await _readings(app_engine, tenant_id)
    assert reading.filename == "Q3 report.pdf"
    assert reading.adhoc_keywords == [{"name": "DPI 📈", "type": "Number"}]


async def test_not_composable_extraction_is_logged_once(
    app_engine: AsyncEngine, seed_tenant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    tenant_id = await seed_tenant("RDW-6")
    user_id = await _seed_user(app_engine, tenant_id, "pm@rdw6.example")

    outcomes = await _write(
        app_engine,
        tenant_id,
        user_id,
        [_document("untyped.pdf", b"six")],
        [_extraction(document_type="", fund_name=f"Fund {_MARKER}")],
    )

    assert outcomes == [ReadingOutcome(filename="untyped.pdf", label=None, proposal=None)]
    assert await _readings(app_engine, tenant_id) == []
    records = _writer_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "ReadingNotComposable" in message
    assert "untyped.pdf" in message
    assert _MARKER not in caplog.text


async def test_conflict_retried_as_a_whole_transaction(
    app_engine: AsyncEngine, seed_tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = await seed_tenant("RDW-7a")
    user_id = await _seed_user(app_engine, tenant_id, "pm@rdw7a.example")
    original = DocumentReadingRepository.create
    calls = {"n": 0}

    async def flaky(self: DocumentReadingRepository, **kwargs: Any) -> DocumentReadingDTO:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise IntegrityError("INSERT ...", {}, _FakeUniqueViolation())
        return await original(self, **kwargs)

    monkeypatch.setattr(DocumentReadingRepository, "create", flaky)
    outcomes = await _write(
        app_engine, tenant_id, user_id, [_document("a.pdf", b"a")], [_extraction()]
    )

    assert calls["n"] == 3
    assert [outcome.label for outcome in outcomes] == ["R-0001"]
    assert len(await _readings(app_engine, tenant_id)) == 1


async def test_conflict_gives_up_after_the_attempts(
    app_engine: AsyncEngine,
    seed_tenant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    tenant_id = await seed_tenant("RDW-7b")
    user_id = await _seed_user(app_engine, tenant_id, "pm@rdw7b.example")
    calls = {"n": 0}

    async def colliding(self: DocumentReadingRepository, **kwargs: Any) -> DocumentReadingDTO:
        calls["n"] += 1
        raise IntegrityError("INSERT ...", {}, _FakeUniqueViolation())

    monkeypatch.setattr(DocumentReadingRepository, "create", colliding)
    outcomes = await _write(
        app_engine, tenant_id, user_id, [_document("a.pdf", b"a")], [_extraction()]
    )

    assert calls["n"] == writer_module._CREATE_ATTEMPTS
    assert [outcome.label for outcome in outcomes] == [None]
    assert await _readings(app_engine, tenant_id) == []
    records = _writer_records(caplog)
    assert len(records) == 1
    assert "IntegrityError" in records[0].getMessage()


async def test_parallel_runs_in_one_tenant_number_uniquely(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("RDW-8")
    user_id = await _seed_user(app_engine, tenant_id, "pm@rdw8.example")

    runs = await asyncio.gather(
        *(
            _write(
                app_engine,
                tenant_id,
                user_id,
                [_document(f"parallel-{i}.pdf", f"parallel {i}".encode())],
                [_extraction()],
            )
            for i in range(4)
        )
    )

    assert sorted(run[0].label or "" for run in runs) == ["R-0001", "R-0002", "R-0003", "R-0004"]
    readings = await _readings(app_engine, tenant_id)
    assert {reading.reading_number for reading in readings} == {1, 2, 3, 4}


async def test_log_line_names_type_and_file_only(
    app_engine: AsyncEngine,
    seed_tenant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    tenant_id = await seed_tenant("RDW-9")
    user_id = await _seed_user(app_engine, tenant_id, "pm@rdw9.example")
    calls = {"n": 0}

    async def refusing(self: DocumentReadingRepository, **kwargs: Any) -> DocumentReadingDTO:
        calls["n"] += 1
        raise IntegrityError(
            "INSERT INTO document_readings ...", {"search_text": _MARKER}, _FakeOtherViolation()
        )

    monkeypatch.setattr(DocumentReadingRepository, "create", refusing)
    outcomes = await _write(
        app_engine, tenant_id, user_id, [_document("secret-q3.pdf", b"nine")], [_extraction()]
    )

    assert calls["n"] == 1
    assert [outcome.label for outcome in outcomes] == [None]
    assert await _readings(app_engine, tenant_id) == []
    assert "IntegrityError" in caplog.text
    assert "secret-q3.pdf" in caplog.text
    assert _MARKER not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert len(_writer_records(caplog)) == 1


async def test_proposals_through_the_writer(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("RDW-10")
    user_id = await _seed_user(app_engine, tenant_id, "pm@rdw10.example")
    await _seed_investments(
        app_engine,
        tenant_id,
        user_id,
        [("FUND X Growth", "Alpha Partners"), ("Fund Y", "Example Capital LLP")],
    )

    outcomes = await _write(
        app_engine,
        tenant_id,
        user_id,
        [_document("x.pdf", b"x"), _document("z.pdf", b"z"), _document("n.pdf", b"n")],
        [
            _extraction(fund_name=" fund x growth ", manager_name="Example Capital LLP"),
            _extraction(fund_name="Fund Z", manager_name="alpha partners"),
            _extraction(fund_name="Fund N", manager_name="Nobody"),
        ],
    )

    assert [outcome.proposal for outcome in outcomes] == [
        AssignmentProposal(kind="investment", name="FUND X Growth"),
        AssignmentProposal(kind="manager", name="Alpha Partners"),
        None,
    ]
    assert [outcome.label for outcome in outcomes] == ["R-0001", "R-0002", "R-0003"]
    readings = await _readings(app_engine, tenant_id)
    assert all(reading.investment_id is None for reading in readings)
    assert all(reading.manager_name is None for reading in readings)


async def test_find_earlier_readings(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_a = await seed_tenant("RDW-11 A")
    tenant_b = await seed_tenant("RDW-11 B")
    user_a = await _seed_user(app_engine, tenant_a, "pm@rdw11a.example")
    user_b = await _seed_user(app_engine, tenant_b, "pm@rdw11b.example")
    document = _document("known.pdf", b"known bytes")
    unknown = _document("unknown.pdf", b"unknown bytes")
    await _write(app_engine, tenant_a, user_a, [document], [_extraction()])

    found = await find_earlier_readings(
        app_engine,
        tenant_id=tenant_a,
        user_id=user_a,
        sha256s=[document.sha256, unknown.sha256, document.sha256],
    )
    assert list(found) == [document.sha256]
    assert [reading.label for reading in found[document.sha256]] == ["R-0001"]

    from_b = await find_earlier_readings(
        app_engine, tenant_id=tenant_b, user_id=user_b, sha256s=[document.sha256]
    )
    assert from_b == {}

    async with tenant_context(app_engine, tenant_a, user_id=user_a) as session:
        await DocumentReadingRepository(session).remove(
            found[document.sha256][0].id,
            reason="Uploaded in error",
            removed_by=user_a,
            now=_T0 + timedelta(hours=1),
        )
    after = await find_earlier_readings(
        app_engine, tenant_id=tenant_a, user_id=user_a, sha256s=[document.sha256]
    )
    assert after == {}
