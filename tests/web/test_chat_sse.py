# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the Shirley-Chat Server-Sent Events endpoint.

The SSE handler reads the user's stashed message, drives
``AIServiceCore.stream_response`` (mocked), and translates each
``StreamEvent`` to an SSE frame. These tests assert the wire shape
without needing a real LLM endpoint:

* The happy-path frame sequence (``message``, …, ``done``).
* The error path (an ``error`` event closes the stream).
* The chart-artefact path — a ``render_chart`` Plotly artefact
  (``chart_format="plotly"``) becomes a ``chart`` event carrying the
  figure spec; a legacy ``generate_chart`` PNG artefact still becomes
  a ``chart`` event carrying the ``data:`` src (ADR-0048).
* The auth gating (a request for someone else's turn returns 404).

The stop-token regression test exercises the ``_StopTokenStripper``
end-to-end: a fake stream that emits ``<|eom|>`` mid-response must not
leak the control token into the rendered SSE chunks.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.tenant_constants import SENTINEL_TENANT_ID
from services.ai_models import ConnectionStatus, Message, MessageRole
from services.ai_service_core import StreamEvent
from services.password_hashing import hash_password
from web.main import create_app
from web.routes.chat import _CHART_SPEC_BYTE_CAP
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; skipping live-DB SSE chat tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fake AIServiceCore — yields a scripted event sequence.
# ---------------------------------------------------------------------------


class _FakeCore:
    """Minimal stand-in for :class:`AIServiceCore` used by the SSE handler.

    The handler calls four methods: ``get_status()``, ``get_model()``,
    ``get_system_prompt()``, ``stream_response()``. Everything else
    that the AI-settings UI uses is absent from the read path the
    handler exercises.
    """

    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = list(events)
        self._status = ConnectionStatus.CONNECTED

    def get_status(self) -> ConnectionStatus:
        return self._status

    def set_status(self, status: ConnectionStatus) -> None:
        self._status = status

    def get_model(self) -> str:
        return "fake/model"

    def get_system_prompt(self, prompt_name: str = "shirley") -> str:
        return ""

    async def stream_response(
        self,
        conversation: object,
        system_prompt: str = "",
        temperature: float = 0.7,
        tool_context: object = None,
        llm: object = None,
    ) -> AsyncIterator[StreamEvent]:
        # Mirror the real signature: the chat route now hands the
        # per-turn context to the core (ADR-0063). Record it so a test
        # can assert the route built it from the session.
        self.last_tool_context = tool_context
        # ADR-0112 §4b: the route resolves per turn and hands the
        # resolution in. Captured so tests can assert what drove it.
        self.last_llm = llm
        for event in self._events:
            yield event


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
    """Truncate every domain table before AND after each test.

    The trailing TRUNCATE prevents ``sse@example.com`` from persisting
    in the dev database after the last test (sub-stream 3a, Task 2
    fix).
    """
    truncate_sql = text(
        "TRUNCATE TABLE data_upload_sheets, data_uploads, "
        "login_audit, sessions, audit_log, "
        "data_store_entries, users, tenants "
        "RESTART IDENTITY CASCADE"
    )
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(truncate_sql)
    try:
        yield
    finally:
        async with fresh_superuser_engine.begin() as conn:
            await conn.execute(truncate_sql)


@pytest_asyncio.fixture
async def seeded_user(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "sse@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(user_id),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
            },
        )
    return user_id, email, plaintext


@pytest_asyncio.fixture
async def web_client_factory(
    seeded_user: tuple[UUID, str, str],
):
    """Factory that yields a configured ``httpx.AsyncClient`` whose
    chat route is wired to a scripted ``_FakeCore``.

    Lifespans are stacked on a single ``AsyncExitStack`` so all
    resources tear down cleanly when the test ends, even when the
    test creates multiple clients.
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

    async def _make(events: list[StreamEvent]) -> AsyncClient:
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        # Lifespan does not own the fake core — attach the override
        # after startup so the chat route picks it up.
        app.state.ai_core = _FakeCore(events)
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://testserver")
        await stack.enter_async_context(client)
        return client

    try:
        yield _make
    finally:
        await stack.aclose()


async def _login_and_get_csrf(client: AsyncClient, email: str, password: str) -> str:
    get_response = await client.get("/login")
    csrf = get_response.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    # ADR-0051 retired ``GET /chat``; the embedded Shirley section on
    # ``/assistants`` now carries the session CSRF token in its
    # composer form.
    page = await client.get("/assistants", follow_redirects=False)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


def _parse_sse_frames(text: str) -> list[tuple[str, str]]:
    """Decode an SSE response body into ``(event, data)`` pairs."""
    frames: list[tuple[str, str]] = []
    for raw in text.split("\n\n"):
        raw = raw.strip("\n")
        if not raw:
            continue
        event = "message"
        data_lines: list[str] = []
        for line in raw.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
        frames.append((event, "\n".join(data_lines)))
    return frames


async def _open_turn(client: AsyncClient, csrf: str, message: str) -> str:
    """POST a chat message and return the freshly issued ``turn_id``."""
    response = await client.post(
        "/chat/messages",
        data={"message": message, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    match = re.search(r'data-pf-sse-url="/chat/stream/([0-9a-f]+)"', response.text)
    assert match is not None, response.text
    return match.group(1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_sse_happy_path_yields_message_then_done(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    fake_msg = Message(role=MessageRole.ASSISTANT, content="Hi there")
    events: list[StreamEvent] = [
        StreamEvent("chunk", {"text": "Hi "}),
        StreamEvent("chunk", {"text": "there"}),
        StreamEvent("stream_finished", {"message": fake_msg, "iterations": 0}),
    ]
    client = await web_client_factory(events)
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "hello")
    response = await client.get(f"/chat/stream/{turn_id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse_frames(response.text)
    events_seen = [e for e, _ in frames]
    assert events_seen[:2] == ["message", "message"]
    assert "done" in events_seen
    assert "Hi " in response.text
    assert "there" in response.text


async def test_sse_strips_stop_tokens_from_chunks(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A stream that emits ``<|eom|>`` must not leak the control token
    into rendered SSE chunks (sub-stream 2c, Task 4 — Option A).
    """
    _id, email, password = seeded_user
    fake_msg = Message(role=MessageRole.ASSISTANT, content="hello world")
    events: list[StreamEvent] = [
        StreamEvent("chunk", {"text": "hello"}),
        StreamEvent("chunk", {"text": "<|eom|>"}),
        StreamEvent("chunk", {"text": " world"}),
        StreamEvent("stream_finished", {"message": fake_msg, "iterations": 0}),
    ]
    client = await web_client_factory(events)
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "hello")
    response = await client.get(f"/chat/stream/{turn_id}")
    assert response.status_code == 200
    # Neither the raw nor the html-escaped form may appear.
    assert "<|eom|>" not in response.text
    assert "&lt;|eom|&gt;" not in response.text
    # The benign payload survives.
    assert "hello" in response.text
    assert "world" in response.text


async def test_sse_error_event_closes_stream(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    events: list[StreamEvent] = [
        StreamEvent("error", {"message": "Boom!"}),
    ]
    client = await web_client_factory(events)
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "hello")
    response = await client.get(f"/chat/stream/{turn_id}")
    assert response.status_code == 200
    frames = _parse_sse_frames(response.text)
    events_seen = [e for e, _ in frames]
    assert "error" in events_seen
    assert events_seen[-1] == "done"
    assert "Boom!" in response.text


async def test_sse_plotly_chart_artifact_yields_chart_event(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A ``render_chart`` Plotly artefact streams the figure spec (ADR-0048)."""
    import json as _json

    _id, email, password = seeded_user
    fake_msg = Message(role=MessageRole.ASSISTANT, content="Done")
    spec = {
        "data": [{"type": "scatter", "x": [1], "y": [2]}],
        "layout": {"title": {"text": "NAV"}},
        "config": {"responsive": True},
    }
    events: list[StreamEvent] = [
        StreamEvent(
            "chart_artifact",
            {
                "chart_format": "plotly",
                "image_base64": "",
                "spec": spec,
                "caption": "NAV chart",
            },
        ),
        StreamEvent("stream_finished", {"message": fake_msg, "iterations": 0}),
    ]
    client = await web_client_factory(events)
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "chart please")
    response = await client.get(f"/chat/stream/{turn_id}")
    assert response.status_code == 200
    frames = _parse_sse_frames(response.text)
    chart_frames = [f for f in frames if f[0] == "chart"]
    assert len(chart_frames) == 1
    payload = _json.loads(chart_frames[0][1])
    assert payload["chart_format"] == "plotly"
    assert payload["spec"] == spec
    assert payload["caption"] == "NAV chart"
    # The interactive spec never reaches the wire as a PNG data URI.
    assert "data:image/png" not in chart_frames[0][1]
    # ADR-0114: the archived spec's handle rides along, so the live figure can
    # offer "Pin to case…". Same ``chart`` event — one additive payload key.
    assert len(payload["artifact_id"]) == 12


async def test_sse_png_chart_artifact_still_yields_chart_event(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A legacy ``generate_chart`` PNG artefact still streams a ``data:`` src.

    Defensive coexistence path: the web assistant uses ``render_chart``,
    but a GUI-shaped PNG envelope must never silently break the stream.
    """
    import json as _json

    _id, email, password = seeded_user
    fake_msg = Message(role=MessageRole.ASSISTANT, content="Done")
    events: list[StreamEvent] = [
        StreamEvent(
            "chart_artifact",
            {
                "chart_format": "png",
                "image_base64": "AAAA",
                "spec": None,
                "caption": "Test chart",
            },
        ),
        StreamEvent("stream_finished", {"message": fake_msg, "iterations": 0}),
    ]
    client = await web_client_factory(events)
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "chart please")
    response = await client.get(f"/chat/stream/{turn_id}")
    assert response.status_code == 200
    frames = _parse_sse_frames(response.text)
    chart_frames = [f for f in frames if f[0] == "chart"]
    assert len(chart_frames) == 1
    payload = _json.loads(chart_frames[0][1])
    assert payload["chart_format"] == "png"
    assert payload["src"] == "data:image/png;base64,AAAA"
    assert payload["caption"] == "Test chart"
    # The PNG branch is not archived (ADR-0114 §Follow-ups), so it carries no
    # handle and the browser offers no pin for it.
    assert "artifact_id" not in payload


async def test_sse_oversized_plotly_spec_streams_without_a_pin_handle(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Over the archival cap: the figure still streams, but carries no handle.

    ADR-0114 §3 — degrade, never refuse the live render. The empty
    ``artifact_id`` is what suppresses the pin affordance client-side.
    """
    import json as _json

    _id, email, password = seeded_user
    fake_msg = Message(role=MessageRole.ASSISTANT, content="A big one.")
    spec = {"data": [], "layout": {"title": "x" * (_CHART_SPEC_BYTE_CAP + 10)}}
    events: list[StreamEvent] = [
        StreamEvent(
            "chart_artifact",
            {
                "chart_format": "plotly",
                "image_base64": "",
                "spec": spec,
                "caption": "Everything, all at once",
            },
        ),
        StreamEvent("stream_finished", {"message": fake_msg, "iterations": 0}),
    ]
    client = await web_client_factory(events)
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "the whole book please")
    response = await client.get(f"/chat/stream/{turn_id}")
    assert response.status_code == 200
    frames = _parse_sse_frames(response.text)
    chart_frames = [f for f in frames if f[0] == "chart"]
    assert len(chart_frames) == 1
    payload = _json.loads(chart_frames[0][1])
    assert payload["spec"] == spec  # unchanged on the wire
    assert payload["artifact_id"] == ""


async def test_sse_unknown_turn_returns_404(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client = await web_client_factory([])
    await _login_and_get_csrf(client, email, password)
    response = await client.get("/chat/stream/" + "0" * 32)
    assert response.status_code == 404


async def test_sse_unauthenticated_redirects(
    web_client_factory: Any,
) -> None:
    client = await web_client_factory([])
    response = await client.get("/chat/stream/" + "0" * 32, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
