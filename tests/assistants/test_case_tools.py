# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.tools.case_tools` — Shirley's case read tools (ADR-0138 §10).

Layers, mirroring ``tests/assistants/test_analysis_tools.py``:

1. **Registration / contract** (no database) — the three tools register as
   ``READ_INTERNAL`` (CT-01), degrade gracefully without a context (CT-02),
   bring a bare ``AIServiceCore`` to nineteen tools (CT-03), and describe
   themselves as read-only — ``search_cases`` naming the closed cases (CT-09).
2. **DB-backed** — each tool reads a seeded tenant end to end, building its
   own loop-local engine from the context URL, exactly the production path:
   ``list_cases`` (CT-04), ``get_case`` (CT-05), ``search_cases`` (CT-06), the
   DMS boundary on the tool path (CT-07) and tenant isolation (CT-08).

The DB fixtures are defined locally (not imported from
``tests._db_fixtures``) so the autouse ``reset_schema`` there does not leak
into the no-database tests above — the analysis-tools idiom.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories import UserRepository, tenant_context
from core.repositories.case_attachment_repository import CaseAttachmentRepository
from core.repositories.case_repository import CaseRepository
from core.repositories.case_working_file_repository import (
    CaseFigureRepository,
    CaseKeptRepository,
    CaseThreadRepository,
)
from services.ai_service_core import AIServiceCore
from services.tool_classes import ToolClass
from services.tool_registry import get_tool_registry
from services.tools import case_tools
from services.tools._tool_context import (
    ToolExecutionContext,
    clear_tool_context,
    clear_tool_data,
    set_tool_context,
)
from services.tools.case_tools import get_case, list_cases, search_cases

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_TOOL_NAMES = ("list_cases", "get_case", "search_cases")

_T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB case-tool tests.",
            allow_module_level=False,
        )


@pytest.fixture(autouse=True)
def _clean_context() -> Generator[None, None, None]:
    """Clear the tool-execution context and data cache around every test."""
    clear_tool_context()
    clear_tool_data()
    yield
    clear_tool_context()
    clear_tool_data()


# ---------------------------------------------------------------------------
# Layer 1 — registration / contract (no database needed)
# ---------------------------------------------------------------------------


def _definition(name: str) -> dict:
    return next(
        d for d in get_tool_registry().get_tool_definitions() if d["function"]["name"] == name
    )


def test_ct01_the_three_tools_register_as_read_internal() -> None:
    """CT-01: all three appear in get_tool_definitions as READ_INTERNAL."""
    assert case_tools._TOOL_NAMES == _TOOL_NAMES
    reg = get_tool_registry()
    names = {d["function"]["name"] for d in reg.get_tool_definitions()}
    for name in _TOOL_NAMES:
        assert name in names, f"{name} not registered"
        assert reg.get_tool_class(name) is ToolClass.READ_INTERNAL
    assert _definition("get_case")["function"]["parameters"]["required"] == ["case_number"]
    assert _definition("search_cases")["function"]["parameters"]["required"] == ["query"]
    state = _definition("list_cases")["function"]["parameters"]["properties"]["state"]
    assert state["enum"] == ["open", "waiting", "closed", "all"]


def test_ct02_each_tool_degrades_without_a_context() -> None:
    """CT-02: a string, never a raise — and before any argument check."""
    for result in (list_cases(), get_case(1), search_cases("gated"), list_cases(state="nope")):
        assert isinstance(result, str)
        assert "context was not set" in result
        assert "web chat surface" in result


def test_ct03_a_bare_core_registers_nineteen_tools() -> None:
    """CT-03: the sixth tool module brings the registry to nineteen."""
    AIServiceCore()
    assert len(get_tool_registry().list_tools()) == 19


def test_ct09_every_description_is_read_only_and_search_names_closed_cases() -> None:
    """CT-09: the descriptions carry the behaviour (the Soul is byte-identical)."""
    for name in _TOOL_NAMES:
        description = _definition(name)["function"]["description"]
        assert description.endswith("Read-only."), name
    search = _definition("search_cases")["function"]["description"]
    assert "Closed cases are included" in search
    assert "the archive is the institution's memory" in search
    assert "Attachment contents and file names are never read" in search
    assert (
        "Attachment contents are never read" in _definition("get_case")["function"]["description"]
    )


# ---------------------------------------------------------------------------
# Layer 2 — DB-backed (the tools build their own loop-local engine)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Book:
    """The seeded tenant: its users and its cases' numbers."""

    tenant_id: UUID
    pm: UUID
    colleague: UUID
    open_mine: int
    open_theirs: int
    waiting: int
    closed: int


@pytest_asyncio.fixture
async def superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine bound to the Postgres superuser — fixture-only, for seeding."""
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine bound to the unprivileged ``portfoliflow_app`` role (seeding)."""
    _require_db()
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


_TRUNCATE = text(
    "TRUNCATE TABLE case_attachments, case_entries, cases, "
    "login_audit, sessions, audit_log, data_store_entries, "
    "users, tenants RESTART IDENTITY CASCADE"
)


@pytest_asyncio.fixture
async def reset_schema(superuser_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """Truncate the case and tenant tables before AND after every DB-backed test."""
    async with superuser_engine.begin() as conn:
        await conn.execute(_TRUNCATE)
    try:
        yield
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(_TRUNCATE)


async def _seed_tenant(superuser_engine: AsyncEngine, name: str) -> UUID:
    """Insert a tenant row (superuser path) and return its id."""
    new_id = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES "
                "(:id, :name, gen_random_uuid()::text)"
            ),
            {"id": str(new_id), "name": name},
        )
    return new_id


def _context(tenant_id: UUID, user_id: UUID | None) -> None:
    assert DATABASE_URL is not None
    set_tool_context(
        ToolExecutionContext(tenant_id=tenant_id, database_url=DATABASE_URL, user_id=user_id)
    )


@pytest_asyncio.fixture
async def book(app_engine: AsyncEngine, superuser_engine: AsyncEngine, reset_schema: None) -> _Book:
    """Four cases: open (mine), open (a colleague's), waiting, closed — with a file each."""
    tenant_id = await _seed_tenant(superuser_engine, "Case-Tools Tenant")
    async with tenant_context(app_engine, tenant_id) as session:
        users = UserRepository(session)
        pm = await users.create(
            email="pm@case-tools.example",
            password_hash="x" * 8,
            roles=("owner",),
            display_name="S. Behrens",
        )
        colleague = await users.create(
            email="colleague@case-tools.example", password_hash="x" * 8, display_name="A. Keller"
        )

    minute = timedelta(minutes=1)
    async with tenant_context(app_engine, tenant_id, user_id=pm.id) as session:
        repo = CaseRepository(session)
        mine = await repo.create(
            title="Fund II continuation vehicle",
            opened_by=pm.id,
            opened_actor="pm",
            due_on=date(2026, 12, 15),
            now=_T0,
        )
        thread = await CaseThreadRepository(session).add(
            mine.id,
            title="Liquidity terms",
            prose="The vehicle would be gated for two years.",
            now=_T0 + minute,
        )
        await CaseFigureRepository(session).add(
            mine.id,
            label="Management fee",
            source="typed",
            value_text="1.5 %",
            now=_T0 + minute,
        )
        theirs = await repo.create(
            title="Nordbank 2031 review", opened_by=pm.id, opened_actor="pm", now=_T0 + 2 * minute
        )
        await repo.hand_over(theirs.id, to_user_id=colleague.id, by=pm.id, now=_T0 + 3 * minute)
        waiting = await repo.create(
            title="Waits for the Q2 statement",
            opened_by=pm.id,
            opened_actor="pm",
            now=_T0 + 4 * minute,
        )
        await repo.wait(waiting.id, wait_until=date(2099, 1, 1), by=pm.id, now=_T0 + 5 * minute)
        closed = await repo.create(
            title="Old secondary sale", opened_by=pm.id, opened_actor="pm", now=_T0 + 6 * minute
        )
        await repo.close(
            closed.id,
            closed_by=pm.id,
            closing_note="The fund was gated; we sold at a discount.",
            now=_T0 + 7 * minute,
        )
        assert thread.case_id == mine.id
    return _Book(
        tenant_id=tenant_id,
        pm=pm.id,
        colleague=colleague.id,
        open_mine=mine.case_number,
        open_theirs=theirs.case_number,
        waiting=waiting.case_number,
        closed=closed.case_number,
    )


def _badges(result: str) -> list[str]:
    """The ``CASE-nnnn`` of each case line (the head line excluded)."""
    return [line.split(" · ", 1)[0] for line in result.splitlines()[1:] if line.startswith("CASE-")]


def _badge(number: int) -> str:
    return f"CASE-{number:04d}"


async def test_ct04_list_cases_filters_by_state_and_owner(book: _Book) -> None:
    """CT-04: open by default, waiting, all, mine; an unknown state is the hint."""
    _context(book.tenant_id, book.pm)

    opened = list_cases()
    assert opened.splitlines()[0] == "2 cases (open)"
    assert sorted(_badges(opened)) == sorted([_badge(book.open_mine), _badge(book.open_theirs)])
    mine_line = next(line for line in opened.splitlines() if "Fund II" in line)
    assert mine_line.startswith(f"{_badge(book.open_mine)} · open · Fund II continuation vehicle")
    assert "owner S. Behrens" in mine_line and "due 15 Dec 2026" in mine_line
    assert "owner A. Keller" in next(line for line in opened.splitlines() if "Nordbank" in line)

    waiting = list_cases(state="waiting")
    assert waiting.splitlines()[0] == "1 case (waiting)"
    assert _badges(waiting) == [_badge(book.waiting)]
    assert "waiting until 01 Jan 2099" in waiting

    everything = list_cases(state="all")
    assert everything.splitlines()[0] == "4 cases (all)"
    assert "· closed · Old secondary sale" in everything
    assert "closed 01 Oct" in everything

    mine = list_cases(mine=True)
    assert mine.splitlines()[0] == "1 case (open, mine)"
    assert _badges(mine) == [_badge(book.open_mine)]
    assert list_cases(state="waiting", mine=True).splitlines()[0] == "1 case (waiting, mine)"
    assert list_cases(state="closed", mine=True) == "1 case (closed, mine)\n" + (
        next(line for line in everything.splitlines() if "Old secondary" in line)
    )

    assert list_cases(state="pending") == "Unknown state — use open, waiting, closed or all."
    _context(book.tenant_id, book.colleague)
    assert _badges(list_cases(mine=True)) == [_badge(book.open_theirs)]


async def test_ct05_get_case_returns_the_file_in_any_state(book: _Book) -> None:
    """CT-05: the file as text; a closed case At close; an unknown number the calm line."""
    _context(book.tenant_id, book.pm)

    text_open = get_case(book.open_mine)
    assert text_open.startswith("The question\n")
    assert f"{_badge(book.open_mine)} — Fund II continuation vehicle" in text_open
    assert "Liquidity terms · Open" in text_open
    assert "Management fee · 1.5 % · typed" in text_open
    assert "\nWhere we stand\n" in text_open

    text_closed = get_case(book.closed)
    assert "\nAt close\n" in text_closed
    assert "\nWhere we stand\n" not in text_closed
    assert "Closing note: The fund was gated; we sold at a discount." in text_closed

    # The schema asks for an integer; the badge or a numeric string reads the same.
    assert get_case(str(book.closed)) == text_closed  # type: ignore[arg-type]
    assert get_case(_badge(book.closed)) == text_closed  # type: ignore[arg-type]
    assert get_case(999) == "No case CASE-0999 in this book. `list_cases` names the open ones."
    assert get_case(0) == "Give the case number as a whole number, e.g. 31 for CASE-0031."


async def test_ct06_search_cases_reads_the_archive(book: _Book) -> None:
    """CT-06: a closed case by its closing note, an open one by a thread's prose."""
    _context(book.tenant_id, book.pm)

    found = search_cases("gated")
    assert found.splitlines()[0] == '2 matches for "gated"'
    assert sorted(_badges(found)) == sorted([_badge(book.open_mine), _badge(book.closed)])

    only_open = search_cases("  gated ", state="open")
    assert only_open.splitlines()[0] == '1 match for "gated"'
    assert _badges(only_open) == [_badge(book.open_mine)]

    assert search_cases("zebra") == '0 matches for "zebra"\nnone'
    assert search_cases("g") == "Give at least two characters to search for."
    assert search_cases("gated", state="nope") == (
        "Unknown state — use open, waiting, closed or all."
    )


async def test_ct07_search_never_matches_an_attachments_file_name(
    book: _Book, app_engine: AsyncEngine
) -> None:
    """CT-07: a document row as the attach route keeps it — caption and filename hold the query."""
    async with tenant_context(app_engine, book.tenant_id, user_id=book.pm) as session:
        case = await CaseRepository(session).get_by_number(book.open_theirs)
        assert case is not None
        attachment = await CaseAttachmentRepository(session).create(
            case.id,
            filename="okapi-term-sheet.pdf",
            mime_type="application/pdf",
            size_bytes=9,
            sha256="h",
            content=b"okapi body",
            uploaded_by=book.pm,
            now=_T0 + timedelta(hours=1),
        )
        await CaseKeptRepository(session).keep(
            case.id,
            artifact="document",
            payload={"filename": "okapi-term-sheet.pdf", "attachment_id": str(attachment.id)},
            kept_by=book.pm,
            via="keep",
            caption="okapi-term-sheet.pdf",
            attachment_id=attachment.id,
            now=_T0 + timedelta(hours=1),
        )
    _context(book.tenant_id, book.pm)

    assert search_cases("okapi") == '0 matches for "okapi"\nnone'
    # The document is in the file, so get_case names it by its kept caption.
    assert "Document: okapi-term-sheet.pdf" in get_case(book.open_theirs)


async def test_ct08_another_tenants_cases_are_invisible(
    book: _Book, superuser_engine: AsyncEngine
) -> None:
    """CT-08: RLS on the tool session's tenant — nothing of the first book shows."""
    other = await _seed_tenant(superuser_engine, "Other Tenant")
    _context(other, None)

    assert list_cases(state="all") == "0 cases (all)\nnone"
    assert get_case(book.open_mine) == (
        f"No case {_badge(book.open_mine)} in this book. `list_cases` names the open ones."
    )
    assert search_cases("gated") == '0 matches for "gated"\nnone'
    assert list_cases(mine=True).startswith("There is no signed-in user in this context")
