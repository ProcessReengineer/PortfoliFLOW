# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for ``case_add`` — Shirley's one write tool (ADR-0138 §8 channel 3, §10).

Layers, mirroring ``tests/assistants/test_case_tools.py``:

1. **DB-backed** — the tool writes a seeded tenant end to end, building its
   own loop-local engine from the context URL, exactly the production path:
   the five kinds (CA-01…05), every refusal with nothing written (CA-06), a
   waiting case takes the write and stays waiting (CA-07), another tenant's
   case is unknown (CA-08).
2. **No database** — the ADR-0022 lock on the real tool (CA-09), the
   registry at twenty with the description carrying the behaviour (CA-10),
   and the core's ``case_write`` side channel (CA-11).

The DB fixtures are defined locally (not imported from
``tests._db_fixtures``) so the autouse ``reset_schema`` there does not leak
into the no-database tests — the case-tools idiom.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories import UserRepository, tenant_context
from core.repositories.case_repository import CaseDTO, CaseRepository
from core.repositories.case_working_file_repository import (
    CaseConversationRepository,
    CaseFigureRepository,
    CasePointRepository,
    CaseThreadRepository,
)
from services.ai_models import ConnectionStatus, Conversation, Message, MessageRole
from services.ai_service_core import AIServiceCore, StreamEvent
from services.tool_classes import ToolClass
from services.tool_registry import _LOCKED_AFTER_UNTRUSTED_FETCH, ToolRegistry, get_tool_registry
from services.tools import case_tools
from services.tools._tool_context import (
    ToolExecutionContext,
    clear_tool_context,
    clear_tool_data,
    set_tool_context,
)
from services.tools.case_tools import case_add
from tests.fixtures.mock_tools import build_fresh_registry
from tests.fixtures.openrouter_responses import (
    SINGLE_TOOL_CALL_RESPONSE,
    TOOL_CALL_THEN_TEXT_RESPONSE,
)
from tests.fixtures.sse_helpers import encode_sse_events

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

_NO_NUMBER = (
    "Name the case by its number — the brief opens on the case's badge, or `list_cases` lists them."
)
_THREADS_LISTED = (
    "Name the thread by its exact title or as #n; the file lists them. "
    'Live threads: #1 "Liquidity terms", #2 "Fees".'
)


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; skipping live-DB case_add tests.",
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


def _badge(number: int) -> str:
    return f"CASE-{number:04d}"


# ---------------------------------------------------------------------------
# Layer 1 — DB-backed (the tool builds its own loop-local engine)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Book:
    """The seeded tenant: its PM and three cases — open (two threads), waiting, closed."""

    tenant_id: UUID
    pm: UUID
    open_id: UUID
    open_number: int
    waiting_id: UUID
    waiting_number: int
    closed_number: int


@pytest_asyncio.fixture
async def superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine bound to the Postgres superuser — fixture-only, for seeding."""
    _require_db()
    assert DATABASE_URL_SUPERUSER is not None
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine bound to the unprivileged application role (seeding, reading back)."""
    _require_db()
    assert DATABASE_URL is not None
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
    """An open case with two threads (one with prose), a waiting and a closed case."""
    tenant_id = await _seed_tenant(superuser_engine, "Case-Add Tenant")
    async with tenant_context(app_engine, tenant_id) as session:
        pm = await UserRepository(session).create(
            email="pm@case-add.example",
            password_hash="x" * 8,
            roles=("owner",),
            display_name="S. Behrens",
        )
    minute = timedelta(minutes=1)
    async with tenant_context(app_engine, tenant_id, user_id=pm.id) as session:
        repo = CaseRepository(session)
        opened = await repo.create(
            title="Fund II continuation vehicle", opened_by=pm.id, opened_actor="pm", now=_T0
        )
        threads = CaseThreadRepository(session)
        await threads.add(
            opened.id,
            title="Liquidity terms",
            prose="The vehicle would be gated for two years.",
            now=_T0 + minute,
        )
        await threads.add(opened.id, title="Fees", now=_T0 + 2 * minute)
        waiting = await repo.create(
            title="Waits for the Q2 statement",
            opened_by=pm.id,
            opened_actor="pm",
            now=_T0 + 3 * minute,
        )
        await repo.wait(waiting.id, wait_until=date(2099, 1, 1), by=pm.id, now=_T0 + 4 * minute)
        closed = await repo.create(
            title="Old secondary sale", opened_by=pm.id, opened_actor="pm", now=_T0 + 5 * minute
        )
        await repo.close(
            closed.id, closed_by=pm.id, closing_note="Sold at a discount.", now=_T0 + 6 * minute
        )
    return _Book(
        tenant_id=tenant_id,
        pm=pm.id,
        open_id=opened.id,
        open_number=opened.case_number,
        waiting_id=waiting.id,
        waiting_number=waiting.case_number,
        closed_number=closed.case_number,
    )


async def _case(app_engine: AsyncEngine, book: _Book, case_id: UUID) -> CaseDTO:
    async with tenant_context(app_engine, book.tenant_id, user_id=book.pm) as session:
        case = await CaseRepository(session).get(case_id)
    assert case is not None
    return case


async def _file_state(app_engine: AsyncEngine, book: _Book, case_id: UUID) -> dict[str, Any]:
    """Everything ``case_add`` could have written to one case, plus its stamp."""
    async with tenant_context(app_engine, book.tenant_id, user_id=book.pm) as session:
        case = await CaseRepository(session).get(case_id)
        assert case is not None
        threads = await CaseThreadRepository(session).list_for_case(case_id, include_removed=True)
        return {
            "updated_at": case.updated_at,
            "threads": [(row.title, row.prose) for row in threads],
            "figures": len(await CaseFigureRepository(session).list_for_case(case_id)),
            "points": len(await CasePointRepository(session).list_for_case(case_id)),
            "conversations": len(await CaseConversationRepository(session).list_for_case(case_id)),
        }


def _artifact(result: str) -> dict[str, Any]:
    parsed = json.loads(result)
    assert parsed["__artifact__"] == "case_write", parsed
    return parsed


async def test_ca01_a_figure_is_a_conversation_figure_with_who_said_it(
    book: _Book, app_engine: AsyncEngine
) -> None:
    """CA-01: one row (label, value, source, note, as-of), the stamp bumped, the artefact."""
    before = await _case(app_engine, book, book.open_id)
    _context(book.tenant_id, book.pm)

    result = case_add(
        book.open_number,
        "figure",
        "1.5 %",
        label="Management fee",
        with_whom="the GP's CFO",
        held_on="2026-11-02",
    )

    payload = _artifact(result)
    async with tenant_context(app_engine, book.tenant_id, user_id=book.pm) as session:
        (figure,) = await CaseFigureRepository(session).list_for_case(book.open_id)
    assert (figure.label, figure.value_text, figure.source) == (
        "Management fee",
        "1.5 %",
        "conversation",
    )
    assert (figure.note, figure.as_of) == ("the GP's CFO", "2026-11-02")
    assert (await _case(app_engine, book, book.open_id)).updated_at > before.updated_at

    badge = _badge(book.open_number)
    assert payload["badge"] == badge
    assert payload["kind"] == "figure"
    assert payload["case_id"] == str(book.open_id)
    assert payload["summary"] == "Management fee: 1.5 %"
    assert payload["href"] == f"/cases/{book.open_id}#cs-figures"
    assert payload["href"].endswith("#cs-figures")
    assert payload["undo"] == {
        "kind": "figure",
        "row_id": str(figure.id),
        "case_id": str(book.open_id),
    }
    assert payload["llm_response"].startswith("Added to CASE-")
    assert payload["llm_response"] == f"Added to {badge} — figure: «Management fee: 1.5 %»."


async def test_ca02_a_thread_is_live_with_its_title_and_no_prose(
    book: _Book, app_engine: AsyncEngine
) -> None:
    """CA-02: a live, open thread; the title collapsed to one line; the link to the thread."""
    _context(book.tenant_id, book.pm)

    payload = _artifact(case_add(book.open_number, "thread", "  Exit\n route  "))

    async with tenant_context(app_engine, book.tenant_id, user_id=book.pm) as session:
        threads = await CaseThreadRepository(session).list_for_case(book.open_id)
    added = threads[-1]
    assert (added.title, added.prose, added.status, added.removed_at) == (
        "Exit route",
        "",
        "open",
        None,
    )
    assert payload["href"] == f"/cases/{book.open_id}#cs-thread-{added.id}"
    assert payload["summary"] == "Exit route"
    assert "— thread: «Exit route»" in payload["llm_response"]


async def test_ca03_a_point_with_a_due_date(book: _Book, app_engine: AsyncEngine) -> None:
    """CA-03: an open point, due when asked, no owner."""
    _context(book.tenant_id, book.pm)

    payload = _artifact(
        case_add(book.open_number, "point", "Ask the GP for the gate terms", due_on="2026-12-01")
    )

    async with tenant_context(app_engine, book.tenant_id, user_id=book.pm) as session:
        (point,) = await CasePointRepository(session).list_for_case(book.open_id)
    assert (point.text, point.due_on, point.owner_user_id, point.done_at) == (
        "Ask the GP for the gate terms",
        date(2026, 12, 1),
        None,
        None,
    )
    assert payload["href"].endswith("#cs-points")
    assert "— open point: «Ask the GP for the gate terms»" in payload["llm_response"]


async def test_ca04_a_conversation_note_is_a_call(book: _Book, app_engine: AsyncEngine) -> None:
    """CA-04: with whom and when as given, a call, nothing promised; held on defaults to today."""
    _context(book.tenant_id, book.pm)

    payload = _artifact(
        case_add(
            book.open_number,
            "conversation",
            "The GP expects the gate to lift in 2027.",
            with_whom="Anna Weber (GP)",
            held_on="2026-11-03",
        )
    )
    case_add(book.open_number, "conversation", "No news.", with_whom="the auditor")

    async with tenant_context(app_engine, book.tenant_id, user_id=book.pm) as session:
        rows = await CaseConversationRepository(session).list_for_case(book.open_id)
    by_whom = {row.with_whom: row for row in rows}
    gp = by_whom["Anna Weber (GP)"]
    assert (gp.held_on, gp.channel, gp.what_was_said, gp.what_was_promised) == (
        date(2026, 11, 3),
        "call",
        "The GP expects the gate to lift in 2027.",
        None,
    )
    assert by_whom["the auditor"].held_on == datetime.now(timezone.utc).date()
    assert payload["href"].endswith("#cs-conversations")
    assert payload["summary"] == "Anna Weber (GP): The GP expects the gate to lift in 2027."
    assert "— conversation note: «Anna Weber (GP): " in payload["llm_response"]


async def test_ca05_prose_appends_a_paragraph_by_title_or_number(
    book: _Book, app_engine: AsyncEngine
) -> None:
    """CA-05: by the exact title (any case) and by #2; a paragraph break only after prose."""
    _context(book.tenant_id, book.pm)

    by_title = _artifact(
        case_add(
            book.open_number, "prose", "The GP confirmed the gate.", thread="  liquidity TERMS "
        )
    )
    by_number = _artifact(case_add(book.open_number, "prose", "Fees step up.", thread="#2"))

    async with tenant_context(app_engine, book.tenant_id, user_id=book.pm) as session:
        liquidity, fees = await CaseThreadRepository(session).list_for_case(book.open_id)
    assert liquidity.prose == (
        "The vehicle would be gated for two years.\n\nThe GP confirmed the gate."
    )
    assert fees.prose == "Fees step up."
    assert by_title["undo"] == {
        "kind": "prose",
        "row_id": str(liquidity.id),
        "case_id": str(book.open_id),
        "appended": "The GP confirmed the gate.",
    }
    assert by_number["undo"]["appended"] == "Fees step up."
    assert by_number["undo"]["row_id"] == str(fees.id)
    assert by_title["href"] == f"/cases/{book.open_id}#cs-thread-{liquidity.id}"
    assert by_title["llm_response"].endswith(
        'text in thread "Liquidity terms": «The GP confirmed the gate».'
    )
    assert by_number["llm_response"].endswith('text in thread "Fees": «Fees step up».')
    # The dock line keeps the summary as written; only the quote drops its stop.
    assert by_number["summary"] == "Fees step up."


async def test_ca06_every_refusal_writes_nothing(book: _Book, app_engine: AsyncEngine) -> None:
    """CA-06: each refusal is its calm line, and no row, no prose and no stamp changed."""
    open_before = await _file_state(app_engine, book, book.open_id)
    _context(book.tenant_id, book.pm)
    n = book.open_number
    closed = _badge(book.closed_number)

    refusals = {
        "no case_number": (case_add(kind="point", text="x"), _NO_NUMBER),
        "a None case_number": (case_add(None, "point", "x"), _NO_NUMBER),
        "a zero case_number": (case_add(0, "point", "x"), _NO_NUMBER),
        "a word for a number": (
            case_add("the hedge case", "point", "x"),  # type: ignore[arg-type]
            _NO_NUMBER,
        ),
        "an unknown number": (
            case_add(999, "point", "x"),
            "No case CASE-0999 in this book. `list_cases` names the open ones.",
        ),
        "a closed case": (
            case_add(book.closed_number, "point", "x"),
            f"{closed} is closed; nothing is added to a closed case.",
        ),
        "an unknown kind": (
            case_add(n, "decision", "We hold."),
            "kind must be one of figure, thread, point, conversation, prose.",
        ),
        "a figure without a label": (case_add(n, "figure", "1.5 %"), "A figure needs a label."),
        "prose without a thread": (case_add(n, "prose", "More."), _THREADS_LISTED),
        "prose to an unknown thread": (
            case_add(n, "prose", "More.", thread="Exit route"),
            _THREADS_LISTED,
        ),
        "prose to a thread number past the last": (
            case_add(n, "prose", "More.", thread="#3"),
            _THREADS_LISTED,
        ),
        "a conversation without with_whom": (
            case_add(n, "conversation", "Said things."),
            "Say who the conversation was with.",
        ),
        "a German date": (
            case_add(n, "conversation", "Said.", with_whom="GP", held_on="03.11.2026"),
            "Dates are YYYY-MM-DD.",
        ),
        "no such month": (
            case_add(n, "point", "x", due_on="2026-13-01"),
            "Dates are YYYY-MM-DD.",
        ),
        "a compact date": (
            case_add(n, "figure", "1.5 %", label="Fee", held_on="20261103"),
            "Dates are YYYY-MM-DD.",
        ),
        "an empty text": (case_add(n, "point", "   "), "Say what to add — text is empty."),
        "a thread title too long": (
            case_add(n, "thread", "x" * 121),
            "A thread title is one line of at most 120 characters.",
        ),
    }

    for what, (got, expected) in refusals.items():
        assert got == expected, what
    assert await _file_state(app_engine, book, book.open_id) == open_before
    async with tenant_context(app_engine, book.tenant_id, user_id=book.pm) as session:
        closed_case = await CaseRepository(session).get_by_number(book.closed_number)
        assert closed_case is not None
        assert await CasePointRepository(session).list_for_case(closed_case.id) == []


async def test_ca07_a_waiting_case_takes_the_write_and_stays_waiting(
    book: _Book, app_engine: AsyncEngine
) -> None:
    """CA-07: no lift — a PM write is not a §7 event; no record entry either."""
    async with tenant_context(app_engine, book.tenant_id, user_id=book.pm) as session:
        entries_before = len(await CaseRepository(session).list_entries(book.waiting_id))
    _context(book.tenant_id, book.pm)

    payload = _artifact(case_add(book.waiting_number, "point", "Chase the Q2 statement"))

    case = await _case(app_engine, book, book.waiting_id)
    assert (case.state, case.wait_until) == ("waiting", date(2099, 1, 1))
    async with tenant_context(app_engine, book.tenant_id, user_id=book.pm) as session:
        assert len(await CaseRepository(session).list_entries(book.waiting_id)) == entries_before
        (point,) = await CasePointRepository(session).list_for_case(book.waiting_id)
    assert point.text == "Chase the Q2 statement"
    assert payload["badge"] == _badge(book.waiting_number)


async def test_ca08_another_tenants_case_is_unknown(
    book: _Book, app_engine: AsyncEngine, superuser_engine: AsyncEngine
) -> None:
    """CA-08: RLS on the tool session's tenant — the number names nothing there."""
    before = await _file_state(app_engine, book, book.open_id)
    other = await _seed_tenant(superuser_engine, "Other Tenant")
    _context(other, None)

    assert case_add(book.open_number, "point", "Slipped in") == (
        f"No case {_badge(book.open_number)} in this book. `list_cases` names the open ones."
    )
    assert await _file_state(app_engine, book, book.open_id) == before


async def test_cs5c08_the_acknowledgement_is_true_on_every_channel(
    book: _Book, app_engine: AsyncEngine
) -> None:
    """CS5c-08 (D-CS-B-128 a–c): no dock sentence, no doubled stop, a paragraph capped."""
    _context(book.tenant_id, book.pm)
    badge = _badge(book.open_number)

    point = _artifact(case_add(book.open_number, "point", "The gate."))
    assert point["llm_response"] == f"Added to {badge} — open point: «The gate»."
    assert point["llm_response"].endswith("».")
    assert "dock" not in point["llm_response"] and "Undo" not in point["llm_response"]
    asked = _artifact(case_add(book.open_number, "point", "Is it gated?!"))
    assert asked["llm_response"].endswith("open point: «Is it gated».")

    before = await _file_state(app_engine, book, book.open_id)
    refusal = case_add(book.open_number, "prose", "x" * 2001, thread="Fees")
    assert refusal == (
        "Keep a paragraph under 2 000 characters — add a second one if you need more."
    )
    assert await _file_state(app_engine, book, book.open_id) == before

    longest = _artifact(case_add(book.open_number, "prose", "y" * 2000, thread="Fees"))
    assert longest["kind"] == "prose"
    async with tenant_context(app_engine, book.tenant_id, user_id=book.pm) as session:
        _, fees = await CaseThreadRepository(session).list_for_case(book.open_id)
    assert fees.prose == "y" * 2000


# ---------------------------------------------------------------------------
# Layer 2 — no database
# ---------------------------------------------------------------------------


def test_ca09_the_real_tool_is_locked_after_an_untrusted_fetch() -> None:
    """CA-09 (D-CS-B-116): the lock refuses before the tool runs; the class and the lock set."""
    reg = ToolRegistry()
    reg.register_tool(
        name="fetch_untrusted",
        function=lambda: "fetched-content",
        description="Fetch untrusted.",
        parameters={"type": "object", "properties": {}, "required": []},
        tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
    )
    reg.register_tool(
        name="case_add",
        function=case_tools.case_add,
        description="Add to a case.",
        parameters={"type": "object", "properties": {}, "required": []},
        tool_class=ToolClass.WRITE_INTERNAL,
    )
    arguments = {"case_number": 31, "kind": "point", "text": "Injected by a web page"}

    reg.begin_turn()
    assert reg.execute_tool("fetch_untrusted", {}) == "fetched-content"
    refusal = reg.execute_tool("case_add", arguments)
    assert "locked" in refusal.lower()
    assert "ADR-0022" in refusal
    assert refusal != case_tools._CONTEXT_NOT_SET_MSG
    reg.end_turn()

    # Without the fetch the same call reaches the tool — and, with no context
    # set, degrades before any database is touched.
    reg.begin_turn()
    assert reg.execute_tool("case_add", arguments) == case_tools._CONTEXT_NOT_SET_MSG
    reg.end_turn()

    assert get_tool_registry().get_tool_class("case_add") is ToolClass.WRITE_INTERNAL
    assert ToolClass.WRITE_INTERNAL in _LOCKED_AFTER_UNTRUSTED_FETCH


def test_ca10_a_bare_core_registers_twenty_and_the_description_carries_the_behaviour() -> None:
    """CA-10 (D-CS-B-119): twenty tools; additive, correctable, the number, never on her own."""
    AIServiceCore()
    registry = get_tool_registry()
    assert len(registry.list_tools()) == 20
    definition = next(
        d["function"]
        for d in registry.get_tool_definitions()
        if d["function"]["name"] == "case_add"
    )
    description = definition["description"]
    for phrase in ("additive", "correct it in the application", "number", "never"):
        assert phrase in description, phrase
    assert "Undo" not in description and "dock" not in description  # channel-neutral
    assert "never closes a case, records a decision, hands over, sets waiting" in description
    assert "never guess a case" in description
    assert "never add on your own initiative" in description
    assert "Locked for the rest of a turn once web_research ran" in description
    parameters = definition["parameters"]
    assert parameters["required"] == ["case_number", "kind", "text"]
    assert parameters["properties"]["kind"]["enum"] == [
        "figure",
        "thread",
        "point",
        "conversation",
        "prose",
    ]
    assert parameters["properties"]["case_number"]["type"] == "integer"


def test_cs5c08_the_description_names_the_badge_not_the_first_line() -> None:
    """CS5c-08 (D-CS-B-128 d): the brief opens on the case's badge; the refusal says so too."""
    AIServiceCore()
    definition = next(
        d["function"]
        for d in get_tool_registry().get_tool_definitions()
        if d["function"]["name"] == "case_add"
    )
    assert "the brief opens on the case's badge" in definition["description"]
    assert "first line" not in definition["description"]
    assert case_tools._NO_CASE_NUMBER_MSG == _NO_NUMBER
    assert "the brief opens on the case's badge" in _NO_NUMBER


_BASE_URL = "https://openrouter.ai/api/v1"
_COMPLETIONS_URL = f"{_BASE_URL}/chat/completions"


async def test_ca11_the_core_turns_the_envelope_into_a_case_write_event(httpx_mock: Any) -> None:
    """CA-11 (D-CS-B-114): a ``case_write`` event without the acknowledgement; the model gets it."""
    envelope = {
        "__artifact__": "case_write",
        "case_id": "c-1",
        "badge": "CASE-0031",
        "kind": "point",
        "summary": "Ask the GP",
        "href": "/cases/c-1#cs-points",
        "undo": {"kind": "point", "row_id": "r-1", "case_id": "c-1"},
        "llm_response": "Added to CASE-0031 — open point: «Ask the GP».",
    }
    for response in (SINGLE_TOOL_CALL_RESPONSE, TOOL_CALL_THEN_TEXT_RESPONSE):
        httpx_mock.add_response(
            method="POST",
            url=_COMPLETIONS_URL,
            content=encode_sse_events(response),
            headers={"content-type": "text/event-stream"},
        )
    fresh = build_fresh_registry(include_success=False)
    fresh.register_tool(
        name="list_datasets",  # the name SINGLE_TOOL_CALL_RESPONSE calls
        function=lambda **_: json.dumps(envelope, ensure_ascii=False),
        description="mock",
        parameters={"type": "object", "properties": {}, "required": []},
        tool_class=ToolClass.WRITE_INTERNAL,
    )
    core = AIServiceCore()
    core.configure(_BASE_URL, "sk-test-key")
    core.set_status(ConnectionStatus.CONNECTED)
    core.set_model("test/model")
    conv = Conversation()
    conv.add_message(Message(role=MessageRole.USER, content="Add it to the case."))

    with patch("services.tool_registry.get_tool_registry", return_value=fresh):
        events: list[StreamEvent] = [e async for e in core.stream_response(conv, "system")]

    (write,) = [e for e in events if e.event_type == "case_write"]
    assert dict(write.payload) == {
        key: value for key, value in envelope.items() if key not in ("__artifact__", "llm_response")
    }
    (completed,) = [e for e in events if e.event_type == "tool_completed"]
    assert completed.payload["result"] == envelope["llm_response"]
    second = json.loads([r for r in httpx_mock.get_requests() if r.method == "POST"][1].content)
    tool_message = next(m for m in second["messages"] if m["role"] == "tool")
    assert tool_message["content"] == envelope["llm_response"]
