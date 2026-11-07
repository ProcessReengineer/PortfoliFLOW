# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Shirley case brief (ADR-0107 C6), Keep from the dock
(ADR-0138 §8 channel 2, §9; P-CS-4a), and the Watch Desk briefing hand-off
(ADR-0133 §6).

ASGI-level tests over a live Postgres. Three surfaces are exercised:

* the **case brief** — a per-session stash the assistants surface sets from a
  ``?case=<id>`` marker (an open or a waiting case, D-CS-B-93), the case page
  sets in place (``POST /chat/brief/case/<id>``) and a dock opened on a case
  page sets from its own marker (P-CS-5a), appended to Shirley's system prompt
  at the SSE call site (never an ``ai_service_core`` change). Since P-CS-5a the
  brief is the working file as text (ADR-0138 §10, CS5a-01…03). A scripted
  ``_FakeCore`` records the system prompt it was handed, so the brief is
  observable without a live model;
* **Keep** — every answer and every figure carries ``Keep →`` (one click into
  the session's keep target: the stashed case, at file level or in its thread)
  and ``Keep to…`` (the picker). No comment, no dialog: a ``case_kept`` row,
  ``via="keep"`` (CS4a-01…06); the kept line's *Undo* removes the row again
  (CS4b-11). The consultation and chart pin dialogs are retired;
* **Shirley's writes** — a ``case_add`` write is a dock line with *Undo*
  (ADR-0138 §8 channel 3, §10; P-CS-5b): the line renders from the event's
  fields (CS5b-01) and its Undo takes the row out again, or answers a calm
  line and writes nothing (CS5b-02);
* the **briefing hand-off** — the same stash, generalised to a second kind: a
  written Watch Desk briefing set by a CSRF-protected POST, bannered in the
  dock, and appended to the prompt as its own fenced block (obligation 17).

The Telegram surface is untouched: the brief and Keep presume the web UI's
banner and keep loop, so the boundary test asserts the briefless path still
yields the plain system prompt.
"""

from __future__ import annotations

import html
import json
import os
import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories._session import tenant_context
from core.repositories.case_repository import CaseRepository
from core.repositories.case_working_file_repository import (
    CaseConversationRepository,
    CaseFigureRepository,
    CaseKeptDTO,
    CaseKeptRepository,
    CasePointRepository,
    CaseThreadRepository,
)
from core.repositories.irene_briefing_repository import IreneBriefingRepository
from core.repositories.irene_finding_repository import IreneFindingRepository
from core.tenant_constants import SENTINEL_TENANT_ID
from services.ai_models import ConnectionStatus, Message, MessageRole
from services.ai_service_core import StreamEvent
from services.irene.briefing_material import MATERIAL_VERSION
from services.password_hashing import hash_password
from services.tools import case_tools
from tests._case_record_seed import seed_record_entry
from web.case_keep import CASE_WRITE_KIND_LABELS, resolve_keep_target
from web.main import create_app
from web.routes.chat import (
    _CHART_SPEC_BYTE_CAP,
    BRIEF_KIND_CASE,
    BRIEF_KIND_WATCH_BRIEFING,
    ActiveBrief,
    _active_case_id,
    _set_active_brief,
)
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB consultation tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fake AIServiceCore — records the system prompt it is handed each turn.
# ---------------------------------------------------------------------------


class _FakeCore:
    """Stand-in for :class:`AIServiceCore` that records the per-turn prompt.

    ``get_system_prompt`` returns a fixed base so the brief-injection tests can
    tell an unbriefed prompt (exactly the base) from a briefed one (base plus
    the fenced block). ``stream_response`` records ``system_prompt`` so the
    brief is observable without a live model.
    """

    def __init__(self, events: list[StreamEvent], base_prompt: str = "") -> None:
        self._events = list(events)
        self._base_prompt = base_prompt
        self._status = ConnectionStatus.CONNECTED
        self.last_system_prompt: str | None = None

    def get_status(self) -> ConnectionStatus:
        return self._status

    def get_model(self) -> str:
        return "fake/model"

    def get_system_prompt(self, prompt_name: str = "shirley") -> str:
        return self._base_prompt

    async def stream_response(
        self,
        conversation: object,
        system_prompt: str = "",
        temperature: float = 0.7,
        tool_context: object = None,
        llm: object = None,
    ) -> AsyncIterator[StreamEvent]:
        self.last_system_prompt = system_prompt
        # ADR-0112 §4b: the route resolves per turn and hands the
        # resolution in. Captured so tests can assert what drove it.
        self.last_llm = llm
        for event in self._events:
            yield event


# ---------------------------------------------------------------------------
# DB fixtures — the case-tables truncate set (mirrors test_cases_area.py).
# ---------------------------------------------------------------------------

_TRUNCATE = text(
    "TRUNCATE TABLE case_attachments, case_entries, cases, "
    "irene_finding, irene_schedule, irene_watch_state, "
    "login_audit, sessions, audit_log, "
    "data_store_entries, users, tenants RESTART IDENTITY CASCADE"
)


@pytest_asyncio.fixture
async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_schema(
    fresh_superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(_TRUNCATE)
    try:
        yield
    finally:
        async with fresh_superuser_engine.begin() as conn:
            await conn.execute(_TRUNCATE)


@pytest_asyncio.fixture
async def seeded_user(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    """Seed the primary tenant and its owner (display name "S. Behrens")."""
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "consult-owner@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital')"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, display_name,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, :dn,
                     ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
                "dn": "S. Behrens",
            },
        )
    return user_id, email, plaintext


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """App-role engine for seeding cases through the repository under RLS."""
    _require_db()
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client_factory(
    seeded_user: tuple[UUID, str, str],
):
    """Yield a factory building an ``AsyncClient`` wired to a ``_FakeCore``.

    Returns ``(client, core)`` so a test can read ``core.last_system_prompt``.
    """
    settings = WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        csrf_cookie_name="portfoliflow_csrf_pre_session",
        database_url=DATABASE_URL,
        database_url_superuser=DATABASE_URL_SUPERUSER,
        session_cookie_secure=False,
    )

    stack = AsyncExitStack()
    await stack.__aenter__()

    async def _make(
        events: list[StreamEvent] | None = None, base_prompt: str = ""
    ) -> tuple[AsyncClient, _FakeCore]:
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        core = _FakeCore(events or [], base_prompt=base_prompt)
        app.state.ai_core = core
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://testserver")
        await stack.enter_async_context(client)
        return client, core

    try:
        yield _make
    finally:
        await stack.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login_and_csrf(client: AsyncClient, email: str, password: str) -> str:
    """Log in and return the session CSRF token (read off the composer form).

    The composer moved to ``GET /chat/dock`` in P-UX-A0e — Shirley is a
    shell element now, loaded on first open — so the token is read from
    the dock fragment rather than the Assistants page.
    """
    get_response = await client.get("/login")
    pre_csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert pre_csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": pre_csrf},
        follow_redirects=False,
    )
    page = await client.get("/chat/dock", follow_redirects=False)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None, page.text
    return match.group(1)


async def _seed_case(
    app_engine: AsyncEngine,
    *,
    opened_by: UUID,
    title: str = "Consult target",
    closed: bool = False,
    opened_payload: dict | None = None,
) -> tuple[UUID, int]:
    """Open a case (optionally closed) through the repository under RLS."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        repo = CaseRepository(session)
        case = await repo.create(
            title=title,
            opened_by=opened_by,
            opened_actor="pm",
            opened_payload=opened_payload,
            now=datetime.now(UTC),
        )
        if closed:
            await repo.close(
                case.id,
                closed_by=opened_by,
                closing_note="closed for the test",
                now=datetime.now(UTC),
            )
        return case.id, case.case_number


async def _close_case(app_engine: AsyncEngine, opened_by: UUID, case_id: UUID) -> None:
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        await CaseRepository(session).close(
            case_id,
            closed_by=opened_by,
            closing_note="closed mid-test",
            now=datetime.now(UTC),
        )


async def _kept_rows(app_engine: AsyncEngine, opened_by: UUID, case_id: UUID) -> list[CaseKeptDTO]:
    """A case's kept rows (the working file, ADR-0138 §9), file level first."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        return await CaseKeptRepository(session).list_for_case(case_id)


async def _wait_case(app_engine: AsyncEngine, opened_by: UUID, case_id: UUID) -> None:
    """Set a case waiting through the milestone path."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        await CaseRepository(session).wait(
            case_id, wait_until=date(2099, 1, 1), by=opened_by, now=datetime.now(UTC)
        )


async def _add_thread(app_engine: AsyncEngine, opened_by: UUID, case_id: UUID, title: str) -> UUID:
    """Add a thread to a case's working file; return its id."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        thread = await CaseThreadRepository(session).add(
            case_id, title=title, now=datetime.now(UTC)
        )
    return thread.id


async def _remove_thread(app_engine: AsyncEngine, opened_by: UUID, thread_id: UUID) -> None:
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        await CaseThreadRepository(session).remove(thread_id, now=datetime.now(UTC))


async def _keep(client: AsyncClient, csrf: str, **fields: str) -> Any:
    """POST the Keep form (``message_id`` or ``artifact_id``, optional target)."""
    return await client.post("/api/chat/keep", data={"csrf_token": csrf, **fields})


def _trigger(response: Any) -> dict[str, Any]:
    """The JSON ``HX-Trigger`` of a successful keep."""
    return json.loads(response.headers["HX-Trigger"])


async def _drive_turn(
    client: AsyncClient, csrf: str, message: str = "What is the standing here?"
) -> str:
    """POST a message and consume its SSE stream; return the raw SSE body."""
    resp = await client.post(
        "/chat/messages",
        data={"message": message, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    match = re.search(r'data-pf-sse-url="/chat/stream/([0-9a-f]+)"', resp.text)
    assert match is not None, resp.text
    stream = await client.get(f"/chat/stream/{match.group(1)}")
    assert stream.status_code == 200
    return stream.text


def _finish_events(msg: Message) -> list[StreamEvent]:
    """A minimal happy-path event script that finishes with ``msg``."""
    return [
        StreamEvent("chunk", {"text": msg.content or ""}),
        StreamEvent("stream_finished", {"message": msg, "iterations": 0}),
    ]


# ---------------------------------------------------------------------------
# Test 1 — brief injection
# ---------------------------------------------------------------------------


async def test_active_stash_briefs_the_turn_and_absent_stash_does_not(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """A stashed case briefs the turn; without a stash the prompt is unchanged.

    Opened from a finding, so the materiality frozen at opening reaches the
    brief the way the page shows it — under Where we stand, at case opening
    (P-CS-5a: the brief is the file).
    """
    actor_id, email, password = seeded_user
    finding_id = await _seed_finding(app_engine, actor_id)
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
        case = await CaseRepository(session).create(
            title="PE ceiling review",
            opened_by=actor_id,
            finding_id=finding_id,
            opened_actor="pm",
            opened_payload={
                "materiality_at_opening": {
                    "lines": ["PE at 27.4% vs a 25% ceiling — 2.4 pts over."]
                }
            },
            now=datetime.now(UTC),
        )
    case_id, case_number = case.id, case.case_number
    # A legacy note (D-CS-M-7) still reads into the Record.
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
        await seed_record_entry(
            session,
            tenant_id=SENTINEL_TENANT_ID,
            case_id=case_id,
            kind="note",
            actor="pm",
            actor_user_id=actor_id,
            payload={"text": "Waiting on the Q2 statement before acting."},
            now=datetime.now(UTC),
        )

    msg = Message(role=MessageRole.ASSISTANT, content="Understood.")
    client, core = await client_factory(_finish_events(msg), base_prompt="BASE")
    csrf = await _login_and_csrf(client, email, password)

    # Without a stash: the prompt is exactly the base.
    await _drive_turn(client, csrf)
    assert core.last_system_prompt == "BASE"

    # Set the stash via the marker, then drive a briefed turn.
    await client.get(f"/assistants?case={case_id}")
    await _drive_turn(client, csrf)
    briefed = core.last_system_prompt or ""
    assert briefed.startswith("BASE\n\n")
    assert "<<<PORTFOLIFLOW CASE BRIEF>>>" in briefed
    assert "<<<END PORTFOLIFLOW CASE BRIEF>>>" in briefed
    assert f"CASE-{case_number:04d} — PE ceiling review" in briefed
    assert "Origin: finding on anlv:real_estate (band noteworthy)" in briefed
    # The frozen materiality line, under Where we stand at case opening.
    assert "At case opening: PE at 27.4% vs a 25% ceiling" in briefed
    assert "· Note · Waiting on the Q2 statement" in briefed  # the Record


async def test_a_stale_closed_since_stash_clears_and_runs_unbriefed(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """A case that closed since the stash was set clears it; the turn is plain."""
    actor_id, email, password = seeded_user
    case_id, _ = await _seed_case(app_engine, opened_by=actor_id)

    msg = Message(role=MessageRole.ASSISTANT, content="ok")
    client, core = await client_factory(_finish_events(msg), base_prompt="BASE")
    csrf = await _login_and_csrf(client, email, password)

    await client.get(f"/assistants?case={case_id}")  # stash set
    await _close_case(app_engine, actor_id, case_id)  # closed behind its back
    await _drive_turn(client, csrf)
    assert core.last_system_prompt == "BASE"  # stale stash cleared

    # The stash is gone: the banner no longer renders. Asserted on the
    # dock fragment, which is where the banner lives since P-UX-A0e.
    dock = await client.get("/chat/dock")
    assert "Consulting for" not in dock.text


# ---------------------------------------------------------------------------
# Test 2 — marker hygiene + the banner
# ---------------------------------------------------------------------------


async def test_marker_hygiene_banner_dismiss_and_replace(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """Malformed/unknown/closed markers are silent; a valid one banners."""
    actor_id, email, password = seeded_user
    open_a, num_a = await _seed_case(app_engine, opened_by=actor_id, title="Alpha")
    open_b, num_b = await _seed_case(app_engine, opened_by=actor_id, title="Bravo")
    closed_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Done", closed=True)

    client, _core = await client_factory()
    await _login_and_csrf(client, email, password)

    # The marker still arrives on ``/assistants`` — that is what a case's
    # "Consult Shirley" links to — and sets the session stash there. The
    # banner it promises renders in the dock fragment (P-UX-A0e), so each
    # marker below is followed by the ``/chat/dock`` render it produced.

    # Malformed, unknown, closed — no banner, no error.
    for marker in ("not-a-uuid", str(uuid4()), str(closed_id)):
        page = await client.get(f"/assistants?case={marker}")
        assert page.status_code == 200
        dock = await client.get("/chat/dock")
        assert "Consulting for" not in dock.text

    # A valid open case → banner with badge + title.
    await client.get(f"/assistants?case={open_a}")
    dock = await client.get("/chat/dock")
    assert "Consulting for" in dock.text
    assert f"CASE-{num_a:04d}" in dock.text
    assert "Alpha" in dock.text

    # A second valid marker replaces the first.
    await client.get(f"/assistants?case={open_b}")
    dock = await client.get("/chat/dock")
    assert f"CASE-{num_b:04d}" in dock.text
    assert f"CASE-{num_a:04d}" not in dock.text

    # Dismiss clears the stash — the banner is gone on the next render.
    csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', dock.text)
    assert csrf_match is not None
    csrf = csrf_match.group(1)
    dismissed = await client.post("/chat/brief/dismiss", data={"csrf_token": csrf})
    assert dismissed.status_code == 200
    assert dismissed.text.strip() == ""
    dock = await client.get("/chat/dock")
    assert "Consulting for" not in dock.text


# ---------------------------------------------------------------------------
# Test 6 — the case-side affordance
# ---------------------------------------------------------------------------


async def test_consult_shirley_action_open_only(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """Open detail offers "Consult Shirley" as the in-place form; closed does not.

    Since P-CS-5a the action posts to the chat surface (D-CS-B-110) rather
    than linking to ``/assistants?case=``.
    """
    actor_id, email, password = seeded_user
    open_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Open one")
    closed_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Shut one", closed=True)
    client, _core = await client_factory()
    await _login_and_csrf(client, email, password)

    open_detail = await client.get(f"/api/cases/{open_id}")
    assert "Consult Shirley" in open_detail.text
    assert f'hx-post="/chat/brief/case/{open_id}"' in open_detail.text
    assert f'href="/assistants?case={open_id}"' not in open_detail.text

    closed_detail = await client.get(f"/api/cases/{closed_id}")
    assert "Consult Shirley" not in closed_detail.text


# ---------------------------------------------------------------------------
# P-CS-5a — consult in place, the file as the brief, the dock on arrival
# ---------------------------------------------------------------------------

#: The brief's lead (D-CS-B-108, D-5a-1) — prompt text, pinned verbatim.
_CASE_BRIEF_LEAD = (
    "You are consulting on the case below — its working file as the PM sees it now. "
    "Ground your answer in it; cite the file's own figures and threads where they bear "
    "on the question. The conversation is ephemeral: the PM keeps what matters into the "
    "file with Keep, and nothing you say is written there by itself."
)


def _stash(client: AsyncClient) -> list[ActiveBrief]:
    """The app's consultation stash — one session per test, so at most one entry."""
    app = cast(ASGITransport, client._transport).app
    return list(getattr(app.state, "brief_stash", {}).values())


def _restash(client: AsyncClient, brief: ActiveBrief) -> None:
    """Replace the one session's stashed brief (a thread chosen in the picker)."""
    app = cast(ASGITransport, client._transport).app
    store = app.state.brief_stash
    (session_id,) = list(store)
    store[session_id] = brief


async def _consult(client: AsyncClient, csrf: str, marker: str) -> Any:
    """POST the case page's Consult Shirley for ``marker`` and return the response."""
    return await client.post(
        f"/chat/brief/case/{marker}", data={"csrf_token": csrf}, follow_redirects=False
    )


async def test_consult_in_place_sets_the_stash_and_opens_the_dock(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS5a-01: open and waiting cases are handed over in place; closed or unknown, nothing."""
    actor_id, email, password = seeded_user
    open_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Open one")
    waiting_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Waiting one")
    await _wait_case(app_engine, actor_id, waiting_id)
    closed_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Shut", closed=True)
    client, _core = await client_factory()
    csrf = await _login_and_csrf(client, email, password)

    handed = await _consult(client, csrf, str(open_id))
    assert handed.status_code == 200, handed.text
    assert handed.headers["HX-Trigger"] == "pf:brief-changed, pf:shirley-dock"
    # The action answers with itself — a quiet swap in place.
    assert 'data-pf-cs="consult-form"' in handed.text
    assert f'hx-post="/chat/brief/case/{open_id}"' in handed.text
    assert 'hx-swap="outerHTML"' in handed.text
    assert f'name="csrf_token" value="{csrf}"' in handed.text
    assert "Consult Shirley" in handed.text
    assert _stash(client) == [ActiveBrief(BRIEF_KIND_CASE, open_id, None)]

    handed = await _consult(client, csrf, str(waiting_id))
    assert handed.headers["HX-Trigger"] == "pf:brief-changed, pf:shirley-dock"
    assert _stash(client) == [ActiveBrief(BRIEF_KIND_CASE, waiting_id, None)]

    # Closed, unknown, malformed: answered, but no stash change and no trigger.
    for marker in (str(closed_id), str(uuid4()), "not-a-uuid"):
        dropped = await _consult(client, csrf, marker)
        assert dropped.status_code == 200, marker
        assert "HX-Trigger" not in dropped.headers, marker
        assert f'hx-post="/chat/brief/case/{marker}"' in dropped.text, marker
        assert _stash(client) == [ActiveBrief(BRIEF_KIND_CASE, waiting_id, None)], marker

    # The thread is kept when the stash already names the case; another case drops it.
    thread_id = await _add_thread(app_engine, actor_id, open_id, "Fee step-up")
    _restash(client, ActiveBrief(BRIEF_KIND_CASE, open_id, thread_id))
    await _consult(client, csrf, str(open_id))
    assert _stash(client) == [ActiveBrief(BRIEF_KIND_CASE, open_id, thread_id)]
    await _consult(client, csrf, str(waiting_id))
    assert _stash(client) == [ActiveBrief(BRIEF_KIND_CASE, waiting_id, None)]

    # CSRF is enforced: without the token nothing is set.
    refused = await client.post(f"/chat/brief/case/{open_id}", data={})
    assert refused.status_code == 403
    assert _stash(client) == [ActiveBrief(BRIEF_KIND_CASE, waiting_id, None)]


async def test_the_brief_is_the_working_file(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS5a-02: the stashed case's file reaches the prompt; waiting briefs, closed clears."""
    actor_id, email, password = seeded_user
    case_id, case_number = await _seed_case(app_engine, opened_by=actor_id, title="Fund II CV")
    await _add_thread(app_engine, actor_id, case_id, "Fee step-up")
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
        await CaseFigureRepository(session).add(
            case_id,
            label="Management fee",
            source="typed",
            value_text="1.5 %",
            now=datetime.now(UTC),
        )
    msg = Message(role=MessageRole.ASSISTANT, content="Understood.")
    client, core = await client_factory(_finish_events(msg), base_prompt="BASE")
    csrf = await _login_and_csrf(client, email, password)

    await _consult(client, csrf, str(case_id))
    await _drive_turn(client, csrf)
    briefed = core.last_system_prompt or ""
    assert briefed.startswith(f"BASE\n\n{_CASE_OPEN}\n{_CASE_BRIEF_LEAD}\nThe question\n")
    assert briefed.endswith("<<<END PORTFOLIFLOW CASE BRIEF>>>")
    lines = briefed.splitlines()
    assert f"  CASE-{case_number:04d} — Fund II CV" in lines
    for heading in ("Where we stand", "Threads", "Figures", "Record"):
        assert heading in lines, heading
    assert "  Fee step-up · Open" in lines
    assert "  Management fee · 1.5 % · typed" in lines
    assert "pins" not in briefed

    await _wait_case(app_engine, actor_id, case_id)
    await _drive_turn(client, csrf)
    briefed = core.last_system_prompt or ""
    assert _CASE_OPEN in briefed
    assert "  State: waiting until 01 Jan 2099" in briefed.splitlines()

    # A case that closed since it was handed over clears the stash (unchanged).
    other_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Closes later")
    await _consult(client, csrf, str(other_id))
    await _close_case(app_engine, actor_id, other_id)
    await _drive_turn(client, csrf)
    assert core.last_system_prompt == "BASE"
    assert _stash(client) == []


async def test_a_dock_opened_on_a_case_page_is_briefed_on_that_case(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS5a-03: ``/chat/dock?case=`` banners and stashes an open case; a closed one nothing."""
    actor_id, email, password = seeded_user
    open_id, open_number = await _seed_case(app_engine, opened_by=actor_id, title="Alpha")
    other_id, other_number = await _seed_case(app_engine, opened_by=actor_id, title="Bravo")
    closed_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Shut", closed=True)
    client, _core = await client_factory()
    csrf = await _login_and_csrf(client, email, password)
    assert _stash(client) == []

    # The case page writes its case into the dock host; another page keeps it bare.
    page = await client.get(f"/cases/{open_id}", follow_redirects=False)
    assert page.status_code == 200
    assert f'hx-get="/chat/dock?case={open_id}"' in page.text
    listing = await client.get("/cases", follow_redirects=False)
    assert 'hx-get="/chat/dock"' in listing.text
    assert "/chat/dock?case=" not in listing.text

    dock = await client.get(f"/chat/dock?case={open_id}")
    assert "Consulting for" in dock.text and f"CASE-{open_number:04d}" in dock.text
    assert _stash(client) == [ActiveBrief(BRIEF_KIND_CASE, open_id, None)]
    # The bare dock renders from the stash, unchanged.
    assert f"CASE-{open_number:04d}" in (await client.get("/chat/dock")).text
    # The marker wins over a different stashed case.
    dock = await client.get(f"/chat/dock?case={other_id}")
    assert f"CASE-{other_number:04d}" in dock.text and f"CASE-{open_number:04d}" not in dock.text
    assert _stash(client) == [ActiveBrief(BRIEF_KIND_CASE, other_id, None)]

    await client.post("/chat/brief/dismiss", data={"csrf_token": csrf})
    for marker in (str(closed_id), "not-a-uuid"):
        dock = await client.get(f"/chat/dock?case={marker}")
        assert dock.status_code == 200, marker
        assert "Consulting for" not in dock.text, marker
        assert _stash(client) == [], marker


# ---------------------------------------------------------------------------
# Test 7 — boundary: the briefless path is the plain system prompt
# ---------------------------------------------------------------------------


async def test_briefless_turn_passes_the_plain_system_prompt(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """No stash → the turn's prompt is exactly ``get_system_prompt()``.

    The narrowest observable proof that the brief is additive and opt-in: a path
    with no case brief (the shape the Telegram surface always has) reaches the
    model with the plain system prompt, unmodified. The Telegram bot assembles
    its prompt on its own seam and never traverses this route.
    """
    _actor_id, email, password = seeded_user
    msg = Message(role=MessageRole.ASSISTANT, content="hello")
    client, core = await client_factory(_finish_events(msg), base_prompt="SHIRLEY-SYSTEM-BASE")
    csrf = await _login_and_csrf(client, email, password)
    await _drive_turn(client, csrf)
    assert core.last_system_prompt == "SHIRLEY-SYSTEM-BASE"


# ---------------------------------------------------------------------------
# Keep from the dock (ADR-0138 §8 channel 2, §9; P-CS-4a) — CS4a-01…06
# ---------------------------------------------------------------------------

#: The retired pin routes, spelled in parts so the tree-wide retirement grep
#: (P-CS-4a gate G-6) finds no live reference — these tests only prove the
#: paths answer 404.
_RETIRED_PIN = "pin" + "-"


def _chart_events(msg: Message, spec: dict, caption: str = "NAV trajectory") -> list[StreamEvent]:
    """An event script that renders one Plotly figure and finishes with ``msg``."""
    return [
        StreamEvent(
            "chart_artifact",
            {
                "chart_format": "plotly",
                "image_base64": "",
                "spec": spec,
                "caption": caption,
            },
        ),
        StreamEvent("stream_finished", {"message": msg, "iterations": 1}),
    ]


def _artifact_id_from_stream(sse_body: str) -> str:
    """Read the archived figure's handle off the turn's ``chart`` frame."""
    match = re.search(r'"artifact_id":\s*"([0-9a-f]{12})"', sse_body)
    assert match is not None, sse_body[:500]
    return match.group(1)


_SPEC = {
    "data": [{"type": "scatter", "x": ["2026-01-31"], "y": [1.0], "name": "NAV"}],
    "layout": {"title": {"text": "Nordbank 2031 — NAV"}},
    "config": {"responsive": True},
}


async def test_history_bubbles_and_figures_carry_both_keep_gestures(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS4a-01: a reloaded answer and figure each carry Keep → and Keep to…; no pin remains."""
    actor_id, email, password = seeded_user
    await _seed_case(app_engine, opened_by=actor_id, title="Gesture case")
    msg = Message(role=MessageRole.ASSISTANT, content="Here is the trajectory.")
    client, _core = await client_factory(_chart_events(msg, _SPEC))
    csrf = await _login_and_csrf(client, email, password)
    artifact_id = _artifact_id_from_stream(await _drive_turn(client, csrf, "chart please"))

    body = (await client.get("/chat/history")).text
    for kind, handle in (("message", msg.id), ("chart", artifact_id)):
        match = re.search(
            rf'<div class="chat-keep" data-pf-keep="{kind}" data-pf-keep-ref="{handle}">.*?</div>',
            body,
            flags=re.DOTALL,
        )
        assert match is not None, kind
        gestures = match.group(0)
        assert 'hx-post="/api/chat/keep"' in gestures
        assert "Keep →" in gestures and "Keep to…" in gestures
        assert 'name="pick" value="1"' in gestures
        assert f'name="csrf_token" value="{csrf}"' in gestures
    assert f'name="message_id" value="{msg.id}"' in body
    assert f'name="artifact_id" value="{artifact_id}"' in body
    # Transport by reference: the gestures never carry the spec's data.
    assert body.count('"scatter"') == 1  # the figure's own spec block only
    assert re.search(r"\bpin\b", body, flags=re.IGNORECASE) is None
    assert "chat-pin" not in body

    dock = (await client.get("/chat/dock")).text
    assert 'id="chat-pin-dialog"' not in dock
    assert "chat-pin" not in dock

    # The live path fetches the same gestures from the actions endpoint.
    actions = await client.get(f"/api/chat/keep/actions?message_id={msg.id}")
    assert actions.status_code == 200
    assert 'data-pf-keep="message"' in actions.text and "Keep to…" in actions.text


async def test_keep_without_a_target_opens_the_picker_and_with_one_writes_the_row(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS4a-02: no target → the picker, nothing written; a stashed case → one kept row."""
    actor_id, email, password = seeded_user
    case_id, case_number = await _seed_case(app_engine, opened_by=actor_id, title="Keep target")
    first_line = "The Nordbank 2031 is senior non-preferred " + "and ranks below the AT1 " * 6
    answer = f"  \n{first_line}\nThe second line stays out of the caption."
    msg = Message(role=MessageRole.ASSISTANT, content=answer)
    client, _core = await client_factory(_finish_events(msg))
    csrf = await _login_and_csrf(client, email, password)
    await _drive_turn(client, csrf)

    # No stash, no posted target: "Keep →" opens the picker; nothing is written.
    picked = await _keep(client, csrf, message_id=msg.id, pick="")
    assert picked.status_code == 200
    assert 'data-pf-keep="picker"' in picked.text
    assert f"CASE-{case_number:04d}" in picked.text
    assert "HX-Trigger" not in picked.headers
    assert await _kept_rows(app_engine, actor_id, case_id) == []

    # A stashed case is the target: one click keeps into its file.
    await client.get(f"/assistants?case={case_id}")
    kept = await _keep(client, csrf, message_id=msg.id, pick="")
    assert kept.status_code == 200, kept.text
    (row,) = await _kept_rows(app_engine, actor_id, case_id)
    assert (row.artifact, row.via, row.thread_id) == ("consultation", "keep", None)
    assert row.payload == {"excerpt": answer}
    assert row.comment is None
    assert row.kept_by == actor_id
    stripped = first_line.strip()
    assert row.caption == stripped[:119].rstrip() + "…"
    assert row.caption is not None and len(row.caption) == 120

    line = kept.text
    assert 'data-pf-keep="done"' in line
    assert f'href="/cases/{case_id}#cs-threads">CASE-{case_number:04d}</a>' in line
    assert "· the file ·" in line
    assert f'data-pf-keep-ref="{msg.id}"' in line
    trigger = _trigger(kept)
    assert trigger["pf:case-kept"] == {"case_id": str(case_id)}
    assert "pf:brief-changed" not in trigger  # the default target already is the brief

    # "Keep to…" with a target still opens the picker.
    again = await _keep(client, csrf, message_id=msg.id, pick="1")
    assert 'data-pf-keep="picker"' in again.text
    assert len(await _kept_rows(app_engine, actor_id, case_id)) == 1


async def test_the_picker_offers_threads_the_file_and_other_cases_and_switches_the_brief(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS4a-03: threads + The file + other unclosed cases; a thread or a case becomes the brief."""
    actor_id, email, password = seeded_user
    case_a, number_a = await _seed_case(app_engine, opened_by=actor_id, title="Alpha")
    case_b, number_b = await _seed_case(app_engine, opened_by=actor_id, title="Bravo")
    closed_c, _ = await _seed_case(app_engine, opened_by=actor_id, title="Shut", closed=True)
    await _wait_case(app_engine, actor_id, case_b)
    live = await _add_thread(app_engine, actor_id, case_a, "Fee step-up")
    gone = await _add_thread(app_engine, actor_id, case_a, "Dropped idea")
    await _remove_thread(app_engine, actor_id, gone)

    msg = Message(role=MessageRole.ASSISTANT, content="The step-up applies from year three.")
    client, _core = await client_factory(_finish_events(msg))
    csrf = await _login_and_csrf(client, email, password)
    await _drive_turn(client, csrf)
    await client.get(f"/assistants?case={case_a}")

    picker = (await client.get(f"/api/chat/keep/picker?message_id={msg.id}")).text
    assert f"CASE-{number_a:04d}" in picker and "Keep this in…" in picker
    assert "The file" in picker
    assert f'name="thread_id" value="{live}"' in picker and "Fee step-up" in picker
    assert str(gone) not in picker and "Dropped idea" not in picker
    assert f"CASE-{number_b:04d} — Bravo · waiting" in picker
    assert str(closed_c) not in picker and "Shut" not in picker
    # The select re-renders the picker for another case.
    other = (await client.get(f"/api/chat/keep/picker?message_id={msg.id}&case_id={case_b}")).text
    assert f'Keep this in… <a href="/cases/{case_b}">CASE-{number_b:04d}</a>' in other
    assert "Fee step-up" not in other

    # Into the thread: the row lands there, the stash takes the thread.
    into_thread = await _keep(
        client, csrf, message_id=msg.id, case_id=str(case_a), thread_id=str(live)
    )
    (row,) = await _kept_rows(app_engine, actor_id, case_a)
    assert row.thread_id == live
    assert f'href="/cases/{case_a}#cs-thread-{live}"' in into_thread.text
    assert "pf:brief-changed" in _trigger(into_thread)
    banner = html.unescape((await client.get("/chat/brief/banner")).text)
    assert 'keeps to thread "Fee step-up"' in banner
    assert "data-pf-brief-target" in banner
    # The next one-click keep follows the stash into the thread.
    await _keep(client, csrf, message_id=msg.id, pick="")
    assert [r.thread_id for r in await _kept_rows(app_engine, actor_id, case_a)] == [live, live]

    # Into another case's file: the brief switches to that case.
    switched = await _keep(client, csrf, message_id=msg.id, case_id=str(case_b), thread_id="")
    trigger = _trigger(switched)
    assert trigger["pf:case-kept"] == {"case_id": str(case_b)}
    assert "pf:brief-changed" in trigger
    (row_b,) = await _kept_rows(app_engine, actor_id, case_b)
    assert row_b.thread_id is None
    banner = (await client.get("/chat/brief/banner")).text
    assert f"CASE-{number_b:04d}" in banner and "keeps to the file" in banner


async def test_a_chart_keeps_by_reference_and_unkeepable_figures_are_calm(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS4a-04: the server resolves the spec from the sidecar; stale and oversized write nothing."""
    actor_id, email, password = seeded_user
    case_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Chart case")
    msg = Message(role=MessageRole.ASSISTANT, content="Here is the trajectory.")
    client, core = await client_factory(_chart_events(msg, _SPEC))
    csrf = await _login_and_csrf(client, email, password)
    artifact_id = _artifact_id_from_stream(await _drive_turn(client, csrf, "chart please"))
    await client.get(f"/assistants?case={case_id}")

    stale = await _keep(client, csrf, artifact_id="deadbeef0000")
    assert 'data-pf-keep="unavailable"' in stale.text
    assert "no longer available to keep" in stale.text
    assert "HX-Trigger" not in stale.headers

    big = {"data": [], "layout": {"title": "x" * (_CHART_SPEC_BYTE_CAP + 10)}}
    core._events = _chart_events(Message(role=MessageRole.ASSISTANT, content="Big."), big)
    await _drive_turn(client, csrf, "the whole book")
    # An oversized figure streams with no handle, so no gesture is offered;
    # its sidecar record still has one, and a hand-built post meets the guard.
    app = cast(ASGITransport, client._transport).app
    (big_record,) = [
        record
        for records in app.state.chat_chart_artifacts.values()
        for record in records
        if record["oversized"]
    ]
    oversized = await _keep(client, csrf, artifact_id=big_record["artifact_id"])
    assert "too large to keep" in oversized.text
    assert await _kept_rows(app_engine, actor_id, case_id) == []

    kept = await _keep(client, csrf, artifact_id=artifact_id)
    assert 'data-pf-keep="done"' in kept.text
    (row,) = await _kept_rows(app_engine, actor_id, case_id)
    assert row.artifact == "chart_snapshot"
    assert row.payload == {"caption": "NAV trajectory", "spec": _SPEC}
    assert row.caption == "NAV trajectory"


async def test_a_target_gone_since_the_picker_is_refused_and_the_pin_routes_are_gone(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CS4a-05: a closed case or a removed thread re-renders the picker with the reason, zero rows."""
    actor_id, email, password = seeded_user
    case_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Moving target")
    other_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Still open")
    thread_id = await _add_thread(app_engine, actor_id, case_id, "Soon gone")
    msg = Message(role=MessageRole.ASSISTANT, content="An answer worth keeping.")
    client, _core = await client_factory(_finish_events(msg))
    csrf = await _login_and_csrf(client, email, password)
    await _drive_turn(client, csrf)

    await _remove_thread(app_engine, actor_id, thread_id)
    thread_gone = await _keep(
        client, csrf, message_id=msg.id, case_id=str(case_id), thread_id=str(thread_id)
    )
    assert thread_gone.status_code == 200
    assert "That thread is gone — pick another." in thread_gone.text
    assert 'data-pf-keep="picker"' in thread_gone.text
    assert "HX-Trigger" not in thread_gone.headers

    await _close_case(app_engine, actor_id, case_id)
    case_gone = await _keep(client, csrf, message_id=msg.id, case_id=str(case_id), thread_id="")
    assert "That case is closed or gone — pick another." in case_gone.text
    assert 'data-pf-keep="picker"' in case_gone.text

    # The repository is the last word: a case the route still read as open
    # but that closed before the write is refused there, nothing written.
    real_get = CaseRepository.get

    async def _reads_open(self: CaseRepository, wanted: UUID):
        case = await real_get(self, wanted)
        return replace(case, state="open") if case is not None else None

    monkeypatch.setattr(CaseRepository, "get", _reads_open)
    raced = await _keep(client, csrf, message_id=msg.id, case_id=str(case_id), thread_id="")
    monkeypatch.undo()
    assert "That case is closed or gone — pick another." in raced.text
    assert await _kept_rows(app_engine, actor_id, case_id) == []
    assert await _kept_rows(app_engine, actor_id, other_id) == []

    for path in (f"/api/chat/{_RETIRED_PIN}consultation", f"/api/chat/{_RETIRED_PIN}chart"):
        assert (await client.get(path)).status_code == 404, path
        assert (await client.post(path, data={"csrf_token": csrf})).status_code == 404, path


async def test_a_waiting_case_marker_banners_and_briefs(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS4a-06 (D-CS-B-93): a waiting case's Consult Shirley lands with its context."""
    actor_id, email, password = seeded_user
    case_id, case_number = await _seed_case(app_engine, opened_by=actor_id, title="On hold")
    await _wait_case(app_engine, actor_id, case_id)
    msg = Message(role=MessageRole.ASSISTANT, content="ok")
    client, core = await client_factory(_finish_events(msg), base_prompt="BASE")
    csrf = await _login_and_csrf(client, email, password)

    page = await client.get(f"/assistants?case={case_id}")
    assert page.status_code == 200
    dock = (await client.get("/chat/dock")).text
    assert "Consulting for" in dock and f"CASE-{case_number:04d}" in dock
    assert "keeps to the file" in dock
    await _drive_turn(client, csrf)
    briefed = core.last_system_prompt or ""
    assert "<<<PORTFOLIFLOW CASE BRIEF>>>" in briefed
    assert "State: waiting" in briefed


async def test_the_kept_line_undoes_the_keep(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS4b-11 (D-CS-B-99): Undo removes the row (removed_at), the gestures return; twice is calm."""
    actor_id, email, password = seeded_user
    case_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Undo target")
    msg = Message(role=MessageRole.ASSISTANT, content="An answer kept by mistake.")
    client, _core = await client_factory(_finish_events(msg))
    csrf = await _login_and_csrf(client, email, password)
    await _drive_turn(client, csrf)
    await client.get(f"/assistants?case={case_id}")

    kept = await _keep(client, csrf, message_id=msg.id, pick="")
    (row,) = await _kept_rows(app_engine, actor_id, case_id)
    undo = re.search(r'<button[^>]*data-pf-keep="undo"[^>]*>', kept.text, flags=re.DOTALL)
    assert undo is not None, kept.text
    raw_vals = re.search(r"hx-vals='([^']*)'", undo.group(0))
    assert raw_vals is not None
    vals = json.loads(html.unescape(raw_vals.group(1)))
    assert vals == {"kept_id": str(row.id), "message_id": msg.id, "artifact_id": ""}
    assert 'hx-post="/api/chat/keep/undo"' in undo.group(0)
    assert 'hx-target="closest .chat-keep"' in undo.group(0)

    undone = await client.post(
        "/api/chat/keep/undo",
        data={"kept_id": str(row.id), "message_id": msg.id},
        headers={"X-CSRF-Token": csrf},
    )
    assert undone.status_code == 200, undone.text
    assert _trigger(undone) == {"pf:case-kept": {"case_id": str(case_id)}}
    assert 'data-pf-keep="message"' in undone.text and "Keep to…" in undone.text
    assert f'data-pf-keep-ref="{msg.id}"' in undone.text
    assert await _kept_rows(app_engine, actor_id, case_id) == []  # removed, not deleted:
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
        gone = await CaseKeptRepository(session).get(row.id)
    assert gone is not None and gone.removed_at is not None

    again = await client.post(
        "/api/chat/keep/undo",
        data={"kept_id": str(row.id), "message_id": msg.id},
        headers={"X-CSRF-Token": csrf},
    )
    assert 'data-pf-keep="unavailable"' in again.text and "Already removed." in again.text
    assert "HX-Trigger" not in again.headers
    unknown = await client.post(
        "/api/chat/keep/undo",
        data={"kept_id": "not-an-id", "message_id": msg.id},
        headers={"X-CSRF-Token": csrf},
    )
    assert "Already removed." in unknown.text

    # A case closed since the keep: the calm state, the row stays.
    await _keep(client, csrf, message_id=msg.id, pick="")
    (second,) = await _kept_rows(app_engine, actor_id, case_id)
    await _close_case(app_engine, actor_id, case_id)
    closed = await client.post(
        "/api/chat/keep/undo",
        data={"kept_id": str(second.id), "message_id": msg.id},
        headers={"X-CSRF-Token": csrf},
    )
    assert "That case is closed." in closed.text
    assert [kept.id for kept in await _kept_rows(app_engine, actor_id, case_id)] == [second.id]


# ---------------------------------------------------------------------------
# Shirley's writes — the dock line and its Undo (ADR-0138 §10, P-CS-5b)
# ---------------------------------------------------------------------------


def _write_line_query(case_id: UUID, kind: str, row_id: UUID, **extra: str) -> dict[str, str]:
    """The fields chat.js reads off a ``case_write`` event for the line's GET."""
    return {
        "badge": "CASE-0001",
        "case_id": str(case_id),
        "kind": kind,
        "summary": "Ask the GP for the gate terms",
        "row_id": str(row_id),
        "appended": "",
        **extra,
    }


def _hidden(form: str, name: str) -> str | None:
    match = re.search(rf'<input type="hidden" name="{name}" value="([^"]*)">', form)
    return html.unescape(match.group(1)) if match is not None else None


async def test_the_write_line_renders_with_undo(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS5b-01 (D-CS-B-115): the line from the event's fields, its Undo form, the case-kept event."""
    actor_id, email, password = seeded_user
    case_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Write target")
    thread_id = await _add_thread(app_engine, actor_id, case_id, "Liquidity terms")
    client, _core = await client_factory()
    csrf = await _login_and_csrf(client, email, password)
    point_id = uuid4()

    line = await client.get(
        "/api/chat/case-write/line", params=_write_line_query(case_id, "point", point_id)
    )
    assert line.status_code == 200, line.text
    assert 'class="chat-write" role="status" data-pf-write="point"' in line.text
    assert f'Added to <a href="/cases/{case_id}#cs-points">CASE-0001</a>' in line.text
    assert "· open point: Ask the GP for the gate terms ·" in line.text
    form = re.search(r"<form[^>]*>.*?</form>", line.text, flags=re.DOTALL)
    assert form is not None, line.text
    assert 'hx-post="/api/chat/case-write/undo"' in form.group(0)
    assert 'hx-target="closest .chat-write"' in form.group(0)
    assert 'hx-swap="outerHTML"' in form.group(0)
    assert _hidden(form.group(0), "csrf_token") == csrf
    assert _hidden(form.group(0), "kind") == "point"
    assert _hidden(form.group(0), "row_id") == str(point_id)
    assert _hidden(form.group(0), "case_id") == str(case_id)
    assert _hidden(form.group(0), "appended") == ""
    assert 'data-pf-write="undo">Undo</button>' in form.group(0)
    # A case page on screen re-fetches its blocks and shows the new row.
    assert _trigger(line) == {"pf:case-kept": {"case_id": str(case_id)}}

    prose = await client.get(
        "/api/chat/case-write/line",
        params=_write_line_query(
            case_id, "prose", thread_id, summary="Fees step up.", appended="Fees step up."
        ),
    )
    text = html.unescape(prose.text)
    assert 'data-pf-write="prose"' in text
    assert f'href="/cases/{case_id}#cs-thread-{thread_id}"' in text
    assert '· text in thread "Liquidity terms": Fees step up. ·' in text
    assert 'name="appended" value="Fees step up."' in text

    for unusable in (
        _write_line_query(case_id, "decision", point_id),
        {**_write_line_query(case_id, "point", point_id), "case_id": "not-a-case"},
        {**_write_line_query(case_id, "point", point_id), "row_id": ""},
    ):
        empty = await client.get("/api/chat/case-write/line", params=unusable)
        assert empty.status_code == 200 and empty.text == ""
        assert "HX-Trigger" not in empty.headers

    # The dock line and the acknowledgement name a kind alike.
    assert CASE_WRITE_KIND_LABELS == case_tools._KIND_LABELS


async def _undo_write(client: AsyncClient, csrf: str, **fields: str) -> Any:
    """POST the write line's Undo form."""
    return await client.post(
        "/api/chat/case-write/undo",
        data={"csrf_token": csrf, "appended": "", **fields},
    )


async def test_undo_takes_shirleys_write_out_again_or_says_why_not(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """CS5b-02 (D-CS-B-115): every kind undone once; the calm lines write nothing."""
    actor_id, email, password = seeded_user
    case_id, number = await _seed_case(app_engine, opened_by=actor_id, title="Undo target")
    badge = f"CASE-{number:04d}"
    now = datetime.now(UTC)
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
        point = await CasePointRepository(session).add(case_id, text="Ask the GP", now=now)
        figure = await CaseFigureRepository(session).add(
            case_id, label="Fee", source="conversation", value_text="1.5 %", now=now
        )
        conversation = await CaseConversationRepository(session).add(
            case_id, held_on=now.date(), with_whom="GP", what_was_said="Gated.", now=now
        )
        threads = CaseThreadRepository(session)
        thread = await threads.add(case_id, title="Exit route", now=now)
        kept_prose = await threads.add(case_id, title="Liquidity", prose="Base.", now=now)
        await threads.update(kept_prose.id, prose="Base.\n\nThe GP confirmed.", now=now)
        moved_on = await threads.add(case_id, title="Fees", prose="Fees step up.", now=now)
    client, _core = await client_factory()
    csrf = await _login_and_csrf(client, email, password)

    undone = await _undo_write(
        client, csrf, kind="point", row_id=str(point.id), case_id=str(case_id)
    )
    assert undone.status_code == 200, undone.text
    assert 'data-pf-write="undone"' in undone.text
    assert f"Undone — removed from {badge}." in undone.text
    assert _trigger(undone) == {"pf:case-kept": {"case_id": str(case_id)}}
    again = await _undo_write(
        client, csrf, kind="point", row_id=str(point.id), case_id=str(case_id)
    )
    assert 'data-pf-write="unavailable"' in again.text and "Already removed." in again.text
    assert "HX-Trigger" not in again.headers

    for kind, row_id in (
        ("figure", figure.id),
        ("conversation", conversation.id),
        ("thread", thread.id),
    ):
        response = await _undo_write(
            client, csrf, kind=kind, row_id=str(row_id), case_id=str(case_id)
        )
        assert f"Undone — removed from {badge}." in response.text, kind

    # Prose: the paragraph comes off while the prose still ends with it …
    stripped = await _undo_write(
        client,
        csrf,
        kind="prose",
        row_id=str(kept_prose.id),
        case_id=str(case_id),
        appended="The GP confirmed.",
    )
    assert f"Undone — removed from {badge}." in stripped.text
    # … and stays when the PM has written on since.
    changed = await _undo_write(
        client,
        csrf,
        kind="prose",
        row_id=str(moved_on.id),
        case_id=str(case_id),
        appended="The GP confirmed.",
    )
    assert "The thread&#39;s text has changed since — edit it in the file." in changed.text
    assert "HX-Trigger" not in changed.headers

    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
        assert await CasePointRepository(session).get(point.id) is None
        assert await CaseFigureRepository(session).get(figure.id) is None
        assert await CaseConversationRepository(session).get(conversation.id) is None
        threads = CaseThreadRepository(session)
        removed = await threads.get(thread.id)
        assert removed is not None and removed.removed_at is not None
        assert (await threads.get(kept_prose.id)).prose == "Base."  # type: ignore[union-attr]
        assert (await threads.get(moved_on.id)).prose == "Fees step up."  # type: ignore[union-attr]
        # A row named under another case's id reads as gone.
        other_point = await CasePointRepository(session).add(case_id, text="Stays", now=now)
    other_case, _ = await _seed_case(app_engine, opened_by=actor_id, title="Another case")
    foreign = await _undo_write(
        client, csrf, kind="point", row_id=str(other_point.id), case_id=str(other_case)
    )
    assert "Already removed." in foreign.text
    unknown_kind = await _undo_write(
        client, csrf, kind="decision", row_id=str(other_point.id), case_id=str(case_id)
    )
    assert "Already removed." in unknown_kind.text

    # A case closed since the write: the calm line, the row stays.
    await _close_case(app_engine, actor_id, case_id)
    closed = await _undo_write(
        client, csrf, kind="point", row_id=str(other_point.id), case_id=str(case_id)
    )
    assert f"{badge} is closed." in closed.text
    assert "HX-Trigger" not in closed.headers
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=actor_id) as session:
        assert await CasePointRepository(session).get(other_point.id) is not None


# ---------------------------------------------------------------------------
# Watch Desk briefing hand-off (ADR-0133 §6, obligation 17)
# ---------------------------------------------------------------------------

_WATCH_OPEN = "<<<PORTFOLIFLOW WATCH DESK BRIEFING>>>"
_WATCH_CLOSE = "<<<END PORTFOLIFLOW WATCH DESK BRIEFING>>>"
_CASE_OPEN = "<<<PORTFOLIFLOW CASE BRIEF>>>"
_BEAT_AT = datetime(2026, 10, 7, 6, 2, tzinfo=UTC)
_TEXT_MARKETS = "Listed equities are down 1.8 % over five days; USD/EUR is flat on the week."
_TEXT_WATCH = "All nine AnlV quotas are calm; SAA real estate is approaching its ceiling."

#: Version-1 material in the shape ``services.irene.briefing_material``
#: composes: one instrument figure, one limit subject, one macro cluster, and
#: Currency moves switched off at the check (``families_on`` without ``fx``).
_MATERIAL: dict[str, Any] = {
    "version": MATERIAL_VERSION,
    "check_at": "2026-10-07T06:02:00+00:00",
    "markets": {
        "as_of": "2026-10-07",
        "window_start": "2026-10-02",
        "window_days": 5,
        "instruments": [
            {
                "id": "price:msci-world",
                "label": "MSCI World ETF",
                "value": "-1.8",
                "unit": "pct",
                "as_of": "2026-10-07",
                "window_start": "2026-10-02",
                "window_days": 5,
                "source": "instrument_prices",
            }
        ],
        "asset_classes": [],
        "currencies": [],
        "press_clusters": [
            {
                "id": "rss:macro:2026-10-07",
                "tag": "macro",
                "day": "2026-10-07",
                "source": "rss",
                "items": [
                    {
                        "title": "Spreads widen in high yield",
                        "source": "FT",
                        "url": "https://example.com/spreads",
                        "published_at": "2026-10-07T05:00:00+00:00",
                    }
                ],
            }
        ],
    },
    "watch": {
        "families_on": ["saa", "anlv", "rss", "price", "freshness", "liquidity"],
        "evaluated_at": "2026-10-07T06:02:00+00:00",
        "limits": [
            {
                "family": "anlv",
                "as_of": "2026-09-30",
                "subjects": [
                    {
                        "id": "anlv:real_estate",
                        "label": "real_estate",
                        "family": "anlv",
                        "value": "18.9",
                        "unit": "pct",
                        "max_pct": "25",
                        "utilisation_pct": "75.6",
                        "warn_threshold_pct": "90",
                        "status": "OK",
                        "muted": False,
                        "as_of": "2026-09-30",
                        "source": "limit_coverage",
                    }
                ],
            }
        ],
        "signals": [],
        "findings_this_check": [],
        "open_findings": {
            "saa": 0,
            "anlv": 0,
            "rss": 0,
            "price": 0,
            "freshness": 0,
            "liquidity": 0,
        },
        "press_tags": [{"tag": "macro", "muted": False}],
    },
}


async def _seed_briefing(
    app_engine: AsyncEngine,
    user_id: UUID,
    *,
    status: str = "written",
    beat_at: datetime = _BEAT_AT,
    inputs: dict[str, Any] | None = None,
    finding_ids: list[UUID] | None = None,
    text_watch: str = _TEXT_WATCH,
) -> UUID:
    """Append one briefing row for the primary tenant through the repository."""
    written = status == "written"
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
        row = await IreneBriefingRepository(session).append(
            beat_at=beat_at,
            written_at=beat_at + timedelta(seconds=30),
            status=status,
            skip_reason=None if written else "nothing_to_write",
            text_markets=_TEXT_MARKETS if written else None,
            text_watch=text_watch if written else None,
            inputs=inputs if inputs is not None else _MATERIAL,
            finding_ids=finding_ids or [],
            model_id="test-writer" if written else None,
        )
        return row.id


async def _seed_finding(app_engine: AsyncEngine, user_id: UUID) -> UUID:
    """Append one open noteworthy finding through the repository under RLS."""
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=user_id) as session:
        finding = await IreneFindingRepository(session).append(
            subject_key="anlv:real_estate",
            payload={"trigger": "Approaching", "finding": "Near the quota.", "basis": "92%."},
            urgency=5,
            band="noteworthy",
        )
        return finding.id


async def _seed_foreign_briefing(fresh_superuser_engine: AsyncEngine) -> UUID:
    """A second tenant with one written briefing of its own, by SQL.

    Seeded as superuser: under RLS the primary tenant has no view of it, so
    its id must read as unknown to the hand-off.
    """
    other_tenant = uuid4()
    briefing_id = uuid4()
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, 'Other', 'other-tenant')"),
            {"id": str(other_tenant)},
        )
        await conn.execute(
            text(
                """
                INSERT INTO irene_briefing
                    (id, tenant_id, beat_at, written_at, status, text_markets,
                     text_watch, inputs, finding_ids, model_id)
                VALUES
                    (:id, :tid, :beat_at, :written_at, 'written', 'theirs',
                     'theirs', '{}'::jsonb, '{}'::uuid[], 'test-writer')
                """
            ),
            {
                "id": str(briefing_id),
                "tid": str(other_tenant),
                "beat_at": _BEAT_AT,
                "written_at": _BEAT_AT + timedelta(seconds=30),
            },
        )
    return briefing_id


async def _hand_off(client: AsyncClient, csrf: str, marker: str) -> Any:
    """POST the Discuss action for ``marker`` and return the response."""
    return await client.post(
        f"/chat/brief/watch-briefing/{marker}",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )


async def test_a_written_briefing_sets_the_stash_opens_the_dock_and_briefs_the_turn(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """Obligation 17: stash + banner + trigger, and the block beneath the base prompt."""
    actor_id, email, password = seeded_user
    finding_id = await _seed_finding(app_engine, actor_id)
    briefing_id = await _seed_briefing(app_engine, actor_id, finding_ids=[finding_id])

    msg = Message(role=MessageRole.ASSISTANT, content="Understood.")
    client, core = await client_factory(_finish_events(msg), base_prompt="BASE")
    csrf = await _login_and_csrf(client, email, password)

    handed = await _hand_off(client, csrf, str(briefing_id))
    assert handed.status_code == 200, handed.text
    assert handed.headers["HX-Trigger"] == "pf:brief-changed, pf:shirley-dock"
    # The action answers with itself — a quiet swap in place.
    assert f'data-pf-discuss-briefing="{briefing_id}"' in handed.text
    assert f'hx-post="/chat/brief/watch-briefing/{briefing_id}"' in handed.text
    assert "Discuss briefing with Shirley" in handed.text

    # The banner names the check — persona-free — and links to the Journal.
    # No schedule row here, so the stamp is the UTC fallback with its suffix.
    dock = await client.get("/chat/dock")
    assert 'data-brief-kind="watch_briefing"' in dock.text
    assert "Discussing" in dock.text
    assert "the briefing of Wed 07 Oct 2026 06:02 UTC" in dock.text
    assert 'href="/watch-desk#journal"' in dock.text
    assert "Consulting for" not in dock.text
    assert "Irene" not in dock.text
    banner = await client.get("/chat/brief/banner")
    assert banner.status_code == 200
    assert "the briefing of Wed 07 Oct 2026 06:02 UTC" in banner.text
    assert "Dismiss" in banner.text

    # The turn: the base prompt byte-identical, the block beneath it.
    await _drive_turn(client, csrf)
    briefed = core.last_system_prompt or ""
    assert briefed.startswith(f"BASE\n\n{_WATCH_OPEN}\n")
    assert briefed.endswith(_WATCH_CLOSE)
    assert _CASE_OPEN not in briefed
    lines = briefed.removeprefix("BASE\n\n").splitlines()
    lead = lines[1]
    assert "written by Irene, the Watch Desk's background analyst" in lead
    assert "at the check of 2026-10-07T06:02:00+00:00" in lead
    assert "which is the check's figure and which is today's" in lead
    assert (
        "Check: 2026-10-07T06:02:00+00:00 (briefing written 2026-10-07T06:02:30+00:00, "
        "model test-writer)"
    ) in lines
    assert f"Markets and press: {_TEXT_MARKETS}" in lines
    assert f"Under observation: {_TEXT_WATCH}" in lines
    assert (
        "  - MSCI World ETF: -1.8 pct, 5-day window from 2026-10-02, as of 2026-10-07, "
        "source instrument_prices"
    ) in lines
    assert (
        "  - real_estate (anlv): 18.9 pct, ceiling 25 pct, utilisation 75.6 pct, status OK, "
        "as of 2026-09-30, source limit_coverage"
    ) in lines
    assert (
        '  - macro cluster of 2026-10-07: "Spreads widen in high yield" '
        "(FT, https://example.com/spreads)"
    ) in lines
    assert (
        "Watch points switched off at that check: Currency moves (fx) — the check did not "
        "evaluate them; never claim it watched them."
    ) in lines
    assert "Findings of that check:" in lines
    assert (
        "  - noteworthy · anlv:real_estate — Approaching — Near the quota. — resolution now: open"
    ) in lines


async def test_a_skipped_unknown_foreign_or_malformed_id_sets_nothing(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """Dropped silently: 200, no trigger, no banner, the turn runs on the base prompt."""
    actor_id, email, password = seeded_user
    skipped_id = await _seed_briefing(app_engine, actor_id, status="skipped")
    foreign_id = await _seed_foreign_briefing(fresh_superuser_engine)

    msg = Message(role=MessageRole.ASSISTANT, content="ok")
    client, core = await client_factory(_finish_events(msg), base_prompt="BASE")
    csrf = await _login_and_csrf(client, email, password)

    for marker in (str(skipped_id), str(uuid4()), str(foreign_id), "not-a-uuid"):
        handed = await _hand_off(client, csrf, marker)
        assert handed.status_code == 200, marker
        assert "HX-Trigger" not in handed.headers, marker
        assert f'data-pf-discuss-briefing="{marker}"' in handed.text, marker
        dock = await client.get("/chat/dock")
        assert "Discussing" not in dock.text, marker
        await _drive_turn(client, csrf)
        assert core.last_system_prompt == "BASE", marker

    # CSRF is enforced: without the token nothing is set.
    written_id = await _seed_briefing(app_engine, actor_id, beat_at=_BEAT_AT + timedelta(hours=6))
    refused = await client.post(
        f"/chat/brief/watch-briefing/{written_id}", data={"comment": "no token"}
    )
    assert refused.status_code == 403
    dock = await client.get("/chat/dock")
    assert "Discussing" not in dock.text


async def test_a_briefing_replaces_a_case_and_a_case_replaces_a_briefing_and_dismiss_clears(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """One active context per session, whichever kind came last; dismiss clears either."""
    actor_id, email, password = seeded_user
    case_id, case_number = await _seed_case(app_engine, opened_by=actor_id, title="Alpha")
    briefing_id = await _seed_briefing(app_engine, actor_id)

    msg = Message(role=MessageRole.ASSISTANT, content="ok")
    client, core = await client_factory(_finish_events(msg), base_prompt="BASE")
    csrf = await _login_and_csrf(client, email, password)

    await client.get(f"/assistants?case={case_id}")
    dock = await client.get("/chat/dock")
    assert "Consulting for" in dock.text and f"CASE-{case_number:04d}" in dock.text

    # The briefing replaces the case.
    handed = await _hand_off(client, csrf, str(briefing_id))
    assert handed.headers["HX-Trigger"] == "pf:brief-changed, pf:shirley-dock"
    dock = await client.get("/chat/dock")
    assert "Discussing" in dock.text
    assert "Consulting for" not in dock.text
    await _drive_turn(client, csrf)
    briefed = core.last_system_prompt or ""
    assert _WATCH_OPEN in briefed and _CASE_OPEN not in briefed

    # The case replaces the briefing.
    await client.get(f"/assistants?case={case_id}")
    dock = await client.get("/chat/dock")
    assert "Consulting for" in dock.text
    assert "Discussing" not in dock.text
    await _drive_turn(client, csrf)
    briefed = core.last_system_prompt or ""
    assert _CASE_OPEN in briefed and _WATCH_OPEN not in briefed

    # Dismiss clears whichever is active.
    await _hand_off(client, csrf, str(briefing_id))
    dismissed = await client.post("/chat/brief/dismiss", data={"csrf_token": csrf})
    assert dismissed.status_code == 200 and dismissed.text.strip() == ""
    dock = await client.get("/chat/dock")
    assert "Discussing" not in dock.text and "Consulting for" not in dock.text
    await _drive_turn(client, csrf)
    assert core.last_system_prompt == "BASE"


async def test_a_vanished_briefing_stash_clears_and_runs_unbriefed(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    fresh_superuser_engine: AsyncEngine,
) -> None:
    """A row that vanished behind the stash's back clears it; the turn is plain."""
    actor_id, email, password = seeded_user
    briefing_id = await _seed_briefing(app_engine, actor_id)

    msg = Message(role=MessageRole.ASSISTANT, content="ok")
    client, core = await client_factory(_finish_events(msg), base_prompt="BASE")
    csrf = await _login_and_csrf(client, email, password)
    await _hand_off(client, csrf, str(briefing_id))

    async with fresh_superuser_engine.begin() as conn:  # vanished behind its back
        await conn.execute(
            text("DELETE FROM irene_briefing WHERE id = :id"), {"id": str(briefing_id)}
        )
    await _drive_turn(client, csrf)
    assert core.last_system_prompt == "BASE"
    dock = await client.get("/chat/dock")
    assert "Discussing" not in dock.text


async def test_an_unknown_material_version_is_handed_over_as_persisted_json(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """Forward safety: a material shape this build does not know is quoted, not guessed."""
    actor_id, email, password = seeded_user
    future = MATERIAL_VERSION + 1
    briefing_id = await _seed_briefing(
        app_engine, actor_id, inputs={"version": future, "shape": "future"}, text_watch=""
    )

    msg = Message(role=MessageRole.ASSISTANT, content="ok")
    client, core = await client_factory(_finish_events(msg), base_prompt="BASE")
    csrf = await _login_and_csrf(client, email, password)
    await _hand_off(client, csrf, str(briefing_id))
    await _drive_turn(client, csrf)
    lines = (core.last_system_prompt or "").splitlines()

    assert "Under observation: (empty — no watch point was switched on at that check)" in lines
    assert (
        f"Material of that check (version {future}, a shape this build does not know), "
        "as persisted:"
    ) in lines
    assert f'  {{"shape": "future", "version": {future}}}' in lines
    assert "Watch points switched off at that check: not stated in this material version." in lines
    assert "Findings of that check: none" in lines


async def test_a_briefing_stash_yields_no_keep_target() -> None:
    """A briefing is not a case: no keep target, no case id — a case stash names its case."""
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
    case_id = uuid4()
    _set_active_brief(request, "session-a", ActiveBrief(BRIEF_KIND_CASE, case_id))
    assert _active_case_id(request, "session-a") == case_id
    briefing = ActiveBrief(BRIEF_KIND_WATCH_BRIEFING, uuid4())
    _set_active_brief(request, "session-a", briefing)
    assert _active_case_id(request, "session-a") is None
    assert _active_case_id(request, "session-b") is None
    # Resolved without a database read: nothing is kept into a briefing.
    assert await resolve_keep_target(cast(Any, None), brief=briefing) is None
    assert await resolve_keep_target(cast(Any, None), brief=None) is None
