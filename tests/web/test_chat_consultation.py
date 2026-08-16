# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Shirley case brief and the consultation pin (ADR-0107 C6).

ASGI-level tests over a live Postgres. Two surfaces are exercised:

* the **case brief** — a per-session stash the assistants surface sets from a
  ``?case=<id>`` marker, appended to Shirley's system prompt at the SSE call
  site (never an ``ai_service_core`` change). A scripted ``_FakeCore`` records
  the system prompt it was handed, so the brief is observable without a live
  model;
* the **consultation pin** — a completed assistant answer, curated and pinned to
  a case as the third pin artifact class (``consultation``).

The Telegram surface is untouched: the brief and pin presume the web UI's banner
and pin loop, so the boundary test asserts the briefless path still yields the
plain system prompt.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories._session import tenant_context
from core.repositories.case_repository import CaseRepository
from core.tenant_constants import SENTINEL_TENANT_ID
from services.ai_models import ConnectionStatus, Message, MessageRole
from services.ai_service_core import StreamEvent
from services.password_hashing import hash_password
from web.main import create_app
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
    """Log in and return the session CSRF token (read off the composer form)."""
    get_response = await client.get("/login")
    pre_csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert pre_csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": pre_csrf},
        follow_redirects=False,
    )
    page = await client.get("/assistants", follow_redirects=False)
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


async def _append_entry(
    app_engine: AsyncEngine,
    *,
    opened_by: UUID,
    case_id: UUID,
    kind: str,
    actor: str,
    payload: dict,
    actor_user_id: UUID | None = None,
) -> None:
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        await CaseRepository(session).append_entry(
            case_id,
            kind=kind,
            actor=actor,
            actor_user_id=actor_user_id,
            payload=payload,
            now=datetime.now(UTC),
        )


async def _close_case(app_engine: AsyncEngine, opened_by: UUID, case_id: UUID) -> None:
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        await CaseRepository(session).close(
            case_id,
            closed_by=opened_by,
            closing_note="closed mid-test",
            now=datetime.now(UTC),
        )


async def _consultation_pins(app_engine: AsyncEngine, opened_by: UUID, case_id: UUID):
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        entries = await CaseRepository(session).list_entries(case_id)
    return [
        e
        for e in entries
        if e.kind == "pin" and (e.payload or {}).get("artifact") == "consultation"
    ]


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
    """A stashed case briefs the turn; without a stash the prompt is unchanged."""
    actor_id, email, password = seeded_user
    case_id, case_number = await _seed_case(
        app_engine,
        opened_by=actor_id,
        title="PE ceiling review",
        opened_payload={
            "materiality_at_opening": {"lines": ["PE at 27.4% vs a 25% ceiling — 2.4 pts over."]}
        },
    )
    await _append_entry(
        app_engine,
        opened_by=actor_id,
        case_id=case_id,
        kind="note",
        actor="pm",
        actor_user_id=actor_id,
        payload={"text": "Waiting on the Q2 statement before acting."},
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
    assert "PE at 27.4% vs a 25% ceiling" in briefed  # frozen materiality line
    assert "Waiting on the Q2 statement" in briefed  # timeline digest


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

    # The stash is gone: the banner no longer renders.
    page = await client.get("/assistants")
    assert "Consulting for" not in page.text


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

    # Malformed, unknown, closed — no banner, no error.
    for marker in ("not-a-uuid", str(uuid4()), str(closed_id)):
        page = await client.get(f"/assistants?case={marker}")
        assert page.status_code == 200
        assert "Consulting for" not in page.text

    # A valid open case → banner with badge + title.
    page = await client.get(f"/assistants?case={open_a}")
    assert "Consulting for" in page.text
    assert f"CASE-{num_a:04d}" in page.text
    assert "Alpha" in page.text

    # A second valid marker replaces the first.
    page = await client.get(f"/assistants?case={open_b}")
    assert f"CASE-{num_b:04d}" in page.text
    assert f"CASE-{num_a:04d}" not in page.text

    # Dismiss clears the stash — the banner is gone on the next render.
    csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert csrf_match is not None
    csrf = csrf_match.group(1)
    dismissed = await client.post("/chat/brief/dismiss", data={"csrf_token": csrf})
    assert dismissed.status_code == 200
    assert dismissed.text.strip() == ""
    page = await client.get("/assistants")
    assert "Consulting for" not in page.text


# ---------------------------------------------------------------------------
# Test 3 — prefill
# ---------------------------------------------------------------------------


async def test_dialog_prefills_the_message_text_and_flags_the_unavailable(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """The dialog prefills exactly the identified message; a stale id is calm."""
    actor_id, email, password = seeded_user
    await _seed_case(app_engine, opened_by=actor_id, title="Prefill case")

    answer = "The Nordbank 2031 is senior non-preferred — anlv:15, not anlv:17."
    msg = Message(role=MessageRole.ASSISTANT, content=answer)
    client, _core = await client_factory(_finish_events(msg))
    csrf = await _login_and_csrf(client, email, password)
    await _drive_turn(client, csrf)

    # The real message id prefills exactly that message's text.
    dialog = await client.get(f"/api/chat/pin-consultation?message_id={msg.id}")
    assert dialog.status_code == 200
    assert answer in dialog.text
    assert 'name="excerpt"' in dialog.text

    # A stale/unknown id → the calm "no longer available" state, no prefill.
    stale = await client.get("/api/chat/pin-consultation?message_id=deadbeef00")
    assert "no longer available" in stale.text
    assert answer not in stale.text


# ---------------------------------------------------------------------------
# Test 4 — pin gates
# ---------------------------------------------------------------------------


async def test_pin_gates_write_nothing_and_preserve_inputs(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """Empty comment / emptied excerpt / missing / closed case each write nothing."""
    actor_id, email, password = seeded_user
    open_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Gate case")
    closed_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Closed", closed=True)
    client, _core = await client_factory()
    csrf = await _login_and_csrf(client, email, password)

    # No apostrophe/quote — the textarea autoescapes, so plain text round-trips.
    good_excerpt = "The finding, kept verbatim for the record."

    # Gate 1 — empty comment.
    r = await client.post(
        "/api/chat/pin-consultation",
        data={
            "csrf_token": csrf,
            "case_id": str(open_id),
            "comment": "  ",
            "excerpt": good_excerpt,
        },
    )
    assert r.status_code == 200
    assert "curation comment is required" in r.text
    assert good_excerpt in r.text  # the edited excerpt survives

    # Gate 2 — emptied excerpt.
    r = await client.post(
        "/api/chat/pin-consultation",
        data={
            "csrf_token": csrf,
            "case_id": str(open_id),
            "comment": "why it matters",
            "excerpt": "   ",
        },
    )
    assert "excerpt is empty" in r.text

    # Gate 3 — the case does not exist.
    r = await client.post(
        "/api/chat/pin-consultation",
        data={
            "csrf_token": csrf,
            "case_id": str(uuid4()),
            "comment": "x",
            "excerpt": good_excerpt,
        },
    )
    assert "could not be found" in r.text

    # Gate 4 — the case is closed.
    r = await client.post(
        "/api/chat/pin-consultation",
        data={
            "csrf_token": csrf,
            "case_id": str(closed_id),
            "comment": "x",
            "excerpt": good_excerpt,
        },
    )
    assert "closed" in r.text.lower()

    assert await _consultation_pins(app_engine, actor_id, open_id) == []
    assert await _consultation_pins(app_engine, actor_id, closed_id) == []


# ---------------------------------------------------------------------------
# Test 5 — pin write + anatomy
# ---------------------------------------------------------------------------


async def test_a_successful_pin_writes_one_entry_and_renders_the_anatomy(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """A pin appends one consultation entry (actor pm) and renders quoted."""
    actor_id, email, password = seeded_user
    case_id, case_number = await _seed_case(app_engine, opened_by=actor_id, title="Anatomy case")
    client, _core = await client_factory()
    csrf = await _login_and_csrf(client, email, password)

    excerpt = "The Hansestadt AT1 is a contingent convertible — anlv:17 is correct."
    comment = "Ranking analysis — the Nordbank classification looks wrong."
    r = await client.post(
        "/api/chat/pin-consultation",
        data={
            "csrf_token": csrf,
            "case_id": str(case_id),
            "comment": comment,
            "excerpt": excerpt,
        },
    )
    assert r.status_code == 200
    assert "Excerpt pinned to" in r.text
    assert f"CASE-{case_number:04d}" in r.text

    pins = await _consultation_pins(app_engine, actor_id, case_id)
    assert len(pins) == 1
    entry = pins[0]
    assert entry.actor == "pm"
    assert entry.actor_user_id == actor_id
    assert entry.payload["comment"] == comment
    assert entry.payload["excerpt"] == excerpt

    # The timeline renders the quoted excerpt + comment under Shirley's name.
    detail = await client.get(f"/cases/{case_id}")
    assert detail.status_code == 200
    assert "Consultation pinned" in detail.text
    assert excerpt in detail.text
    assert comment in detail.text
    assert "pf-artifact--consultation" in detail.text

    # An unknown artifact still falls to the calm fallback (no leak, no break).
    await _append_entry(
        app_engine,
        opened_by=actor_id,
        case_id=case_id,
        kind="pin",
        actor="pm",
        actor_user_id=actor_id,
        payload={"artifact": "mystery", "secret": "SHOULD-NOT-LEAK"},
    )
    detail = await client.get(f"/cases/{case_id}")
    assert detail.status_code == 200
    assert "SHOULD-NOT-LEAK" not in detail.text


# ---------------------------------------------------------------------------
# Test 6 — the case-side affordance
# ---------------------------------------------------------------------------


async def test_consult_shirley_action_open_only(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """Open detail offers "Consult Shirley" with the marker URL; closed does not."""
    actor_id, email, password = seeded_user
    open_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Open one")
    closed_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Shut one", closed=True)
    client, _core = await client_factory()
    await _login_and_csrf(client, email, password)

    open_detail = await client.get(f"/cases/{open_id}")
    assert "Consult Shirley" in open_detail.text
    assert f'href="/assistants?case={open_id}"' in open_detail.text

    closed_detail = await client.get(f"/cases/{closed_id}")
    assert "Consult Shirley" not in closed_detail.text


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
# Test 8 — the chart pin (ADR-0114): dialog, gates, payload, timeline
# ---------------------------------------------------------------------------


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


async def _chart_pins(app_engine: AsyncEngine, opened_by: UUID, case_id: UUID):
    async with tenant_context(app_engine, SENTINEL_TENANT_ID, user_id=opened_by) as session:
        entries = await CaseRepository(session).list_entries(case_id)
    return [
        e
        for e in entries
        if e.kind == "pin" and (e.payload or {}).get("artifact") == "chart_snapshot"
    ]


_SPEC = {
    "data": [{"type": "scatter", "x": ["2026-01-31"], "y": [1.0], "name": "NAV"}],
    "layout": {"title": {"text": "Nordbank 2031 — NAV"}},
    "config": {"responsive": True},
}


async def test_chart_pin_dialog_names_the_figure_and_a_stale_handle_is_calm(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """The dialog identifies the chart by caption; an unknown handle is calm.

    A frozen figure has nothing to trim, so there is no excerpt field — and the
    form never carries the spec: only the sidecar handle (ADR-0114 §2).
    """
    actor_id, email, password = seeded_user
    await _seed_case(app_engine, opened_by=actor_id, title="Chart case")

    msg = Message(role=MessageRole.ASSISTANT, content="Here is the trajectory.")
    client, _core = await client_factory(_chart_events(msg, _SPEC))
    csrf = await _login_and_csrf(client, email, password)
    artifact_id = _artifact_id_from_stream(await _drive_turn(client, csrf, "chart please"))

    dialog = await client.get(f"/api/chat/pin-chart?artifact_id={artifact_id}")
    assert dialog.status_code == 200
    assert "Pin this chart to a case" in dialog.text
    assert "NAV trajectory" in dialog.text
    assert f'name="artifact_id" value="{artifact_id}"' in dialog.text
    assert 'name="excerpt"' not in dialog.text
    # Transport by reference: the spec is never put in front of the client.
    assert "scatter" not in dialog.text

    # A stale/unknown handle → the calm "no longer available" state, no pin.
    stale = await client.get("/api/chat/pin-chart?artifact_id=deadbeef0000")
    assert "no longer available" in stale.text
    assert 'name="case_id"' not in stale.text


async def test_chart_pin_gates_write_nothing(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """Empty comment / stale handle / missing case / closed case each write nothing."""
    actor_id, email, password = seeded_user
    open_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Gate chart case")
    closed_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Shut", closed=True)

    msg = Message(role=MessageRole.ASSISTANT, content="Here it is.")
    client, _core = await client_factory(_chart_events(msg, _SPEC))
    csrf = await _login_and_csrf(client, email, password)
    artifact_id = _artifact_id_from_stream(await _drive_turn(client, csrf, "chart please"))

    # Gate 1 — empty comment (the artefact is fine).
    r = await client.post(
        "/api/chat/pin-chart",
        data={
            "csrf_token": csrf,
            "artifact_id": artifact_id,
            "case_id": str(open_id),
            "comment": "   ",
        },
    )
    assert r.status_code == 200
    assert "curation comment is required" in r.text

    # Gate 2 — the handle no longer resolves in this session's sidecar.
    r = await client.post(
        "/api/chat/pin-chart",
        data={
            "csrf_token": csrf,
            "artifact_id": "deadbeef0000",
            "case_id": str(open_id),
            "comment": "why it matters",
        },
    )
    assert "no longer available" in r.text

    # Gate 3 — the case does not exist.
    r = await client.post(
        "/api/chat/pin-chart",
        data={
            "csrf_token": csrf,
            "artifact_id": artifact_id,
            "case_id": str(uuid4()),
            "comment": "why it matters",
        },
    )
    assert "could not be found" in r.text

    # Gate 4 — the case is closed (ADR-0107 §4 immutability).
    r = await client.post(
        "/api/chat/pin-chart",
        data={
            "csrf_token": csrf,
            "artifact_id": artifact_id,
            "case_id": str(closed_id),
            "comment": "why it matters",
        },
    )
    assert "closed" in r.text.lower()

    assert await _chart_pins(app_engine, actor_id, open_id) == []
    assert await _chart_pins(app_engine, actor_id, closed_id) == []


async def test_chart_pin_closed_race_is_refused_by_the_repository(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A case that closes between the open-gate and the write writes nothing.

    The race is simulated where it actually happens: the route's open-gate read
    sees ``open``, and by the time ``append_entry`` re-reads the state it is
    closed. Only the repository's refusal stands between the pin and the
    record — the C6 handler shape, exercised for the chart class.
    """
    actor_id, email, password = seeded_user
    case_id, _ = await _seed_case(app_engine, opened_by=actor_id, title="Racing", closed=True)

    real_get = CaseRepository.get
    reads = {"n": 0}

    async def _open_on_first_read(self: CaseRepository, wanted: UUID):
        case = await real_get(self, wanted)
        reads["n"] += 1
        # First read = the route's gate (still open); every later read — the
        # one inside append_entry — sees the case as it now is: closed.
        if reads["n"] == 1 and case is not None:
            return replace(case, state="open")
        return case

    monkeypatch.setattr(CaseRepository, "get", _open_on_first_read)

    msg = Message(role=MessageRole.ASSISTANT, content="Here it is.")
    client, _core = await client_factory(_chart_events(msg, _SPEC))
    csrf = await _login_and_csrf(client, email, password)
    artifact_id = _artifact_id_from_stream(await _drive_turn(client, csrf, "chart please"))

    r = await client.post(
        "/api/chat/pin-chart",
        data={
            "csrf_token": csrf,
            "artifact_id": artifact_id,
            "case_id": str(case_id),
            "comment": "raced in",
        },
    )
    assert r.status_code == 200
    assert "closed" in r.text.lower()
    monkeypatch.undo()
    assert await _chart_pins(app_engine, actor_id, case_id) == []


async def test_a_successful_chart_pin_freezes_the_spec_and_renders_it(
    client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    app_engine: AsyncEngine,
) -> None:
    """One pin entry carrying artifact/comment/caption/spec, rendered verbatim.

    The client posted a handle; the *server* resolved the spec — which is what
    makes the record trustworthy — and embedded it, so the case is
    self-contained and survives the session that produced it.
    """
    actor_id, email, password = seeded_user
    case_id, case_number = await _seed_case(app_engine, opened_by=actor_id, title="Anatomy chart")

    msg = Message(role=MessageRole.ASSISTANT, content="Here is the trajectory.")
    client, _core = await client_factory(_chart_events(msg, _SPEC))
    csrf = await _login_and_csrf(client, email, password)
    artifact_id = _artifact_id_from_stream(await _drive_turn(client, csrf, "chart please"))

    comment = "The drawdown the committee asked about — kept for the record."
    r = await client.post(
        "/api/chat/pin-chart",
        data={
            "csrf_token": csrf,
            "artifact_id": artifact_id,
            "case_id": str(case_id),
            "comment": comment,
        },
    )
    assert r.status_code == 200
    assert "Chart pinned to" in r.text
    assert f"CASE-{case_number:04d}" in r.text

    pins = await _chart_pins(app_engine, actor_id, case_id)
    assert len(pins) == 1
    entry = pins[0]
    assert entry.actor == "pm"
    assert entry.actor_user_id == actor_id
    assert entry.payload["comment"] == comment
    assert entry.payload["caption"] == "NAV trajectory"
    assert entry.payload["spec"] == _SPEC  # the frozen figure, byte for byte

    # The timeline re-plots it from the payload — nothing recomputed.
    detail = await client.get(f"/cases/{case_id}")
    assert detail.status_code == 200
    assert "Chart snapshot pinned" in detail.text
    assert comment in detail.text
    assert "pf-artifact--chart" in detail.text
    assert "data-pf-chart-plot" in detail.text
    assert '"scatter"' in detail.text

    # A second pin of the same figure is a second entry — the sidecar record
    # is not consumed by pinning.
    again = await client.post(
        "/api/chat/pin-chart",
        data={
            "csrf_token": csrf,
            "artifact_id": artifact_id,
            "case_id": str(case_id),
            "comment": "and once more, for the annex",
        },
    )
    assert "Chart pinned to" in again.text
    assert len(await _chart_pins(app_engine, actor_id, case_id)) == 2
