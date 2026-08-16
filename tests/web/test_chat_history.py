# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the per-session chat-history surface (ADR-0050, ADR-0114).

Four groups:

* Unit tests for ``_trim_history`` exercising the role-safe FIFO
  eviction policy without involving the HTTP surface.
* Unit tests for ``_TurnRecorder`` driving it with the same event
  sequences the SSE handler observes.
* Route-level tests for the full surface — POST appends a user
  message, SSE completion appends assistant + tool messages, error
  path leaves the assistant side empty, ``/chat/new`` clears the
  store, ``/chat/history`` rehydrates the prose, logout drops the
  history, two-turn continuity, two-session isolation.
* The chart-artefact sidecar (ADR-0114) — capture, its shared
  lifecycle with the history (new chat, logout, session LRU), the
  turn-group trim coupling, and chart rehydration through
  ``/chat/history`` including the over-the-cap placeholder.

The route-level tests reuse the live-DB fixture pattern from
``test_chat_sse.py``; the conftest seeds the sentinel tenant + a
sentinel-tenant user and the SSE core is stubbed with a scripted
``_FakeCore``.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.tenant_constants import SENTINEL_TENANT_ID
from services.ai_models import (
    ConnectionStatus,
    Conversation,
    Message,
    MessageRole,
    ToolCall,
)
from services.ai_service_core import StreamEvent
from services.password_hashing import hash_password
from web.main import create_app
from web.routes.chat import (
    _CHAT_HISTORIES_LIMIT,
    _CHART_SPEC_BYTE_CAP,
    _HISTORY_MAX_CHARS,
    _HISTORY_MAX_MESSAGES,
    _TurnRecorder,
    _chart_artifacts,
    _get_or_create_history,
    _history_items,
    _trim_history,
)
from web.settings import WebSettings
import functools
import operator

if TYPE_CHECKING:
    from services.tools._tool_context import ToolExecutionContext

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB chat-history tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Unit tests for _trim_history
# ---------------------------------------------------------------------------


def _conv_with_messages(items: list[tuple[MessageRole, str]]) -> Conversation:
    conv = Conversation()
    for role, content in items:
        conv.messages.append(Message(role=role, content=content))
    return conv


def test_trim_history_noop_when_within_bounds() -> None:
    """A short, well-formed history is not modified by ``_trim_history``."""
    conv = _conv_with_messages(
        [
            (MessageRole.USER, "hello"),
            (MessageRole.ASSISTANT, "hi"),
        ]
    )
    _trim_history(conv)
    assert len(conv.messages) == 2


def test_trim_history_drops_oldest_turn_group_on_count_overflow() -> None:
    """21 paired messages collapse to a history starting at a user turn."""
    conv = Conversation()
    # 11 user + 11 assistant = 22 messages (over the 20-cap by 2).
    for i in range(11):
        conv.messages.append(Message(role=MessageRole.USER, content=f"u{i}"))
        conv.messages.append(Message(role=MessageRole.ASSISTANT, content=f"a{i}"))

    _trim_history(conv)

    assert len(conv.messages) <= _HISTORY_MAX_MESSAGES
    # First message after trim must be a user message — role-safety
    # invariant.
    assert conv.messages[0].role == MessageRole.USER


def test_trim_history_drops_by_char_count() -> None:
    """A history whose contents sum to >24 000 chars trims by group."""
    conv = Conversation()
    # Three turn-groups: each ~12 000 chars, total 36 000.
    for i in range(3):
        conv.messages.append(Message(role=MessageRole.USER, content=f"u{i} " + "x" * 1000))
        conv.messages.append(
            Message(
                role=MessageRole.ASSISTANT,
                content=f"a{i} " + "y" * 11000,
            )
        )

    _trim_history(conv)

    total_chars = sum(len(m.content or "") for m in conv.messages)
    assert total_chars <= _HISTORY_MAX_CHARS
    assert conv.messages[0].role == MessageRole.USER


def test_trim_history_preserves_role_safety_under_tool_messages() -> None:
    """Pathological case: middle turn-group has assistant+tool messages.

    Naïve single-message FIFO eviction would orphan tool messages
    (assistant dropped, tool messages stranded). Turn-group eviction
    keeps the API invariant: every tool message is preceded by an
    assistant entry with the matching tool_call_id, or both are gone.
    """
    conv = Conversation()
    # First turn-group: 18 plain messages so we definitely overflow.
    for i in range(9):
        conv.messages.append(Message(role=MessageRole.USER, content=f"u{i}"))
        conv.messages.append(Message(role=MessageRole.ASSISTANT, content=f"a{i}"))
    # Second turn-group: assistant-with-tool-calls + two tool results.
    conv.messages.append(Message(role=MessageRole.USER, content="please look up A and B"))
    conv.messages.append(
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(id="call_1", name="lookup", arguments={"x": 1}),
                ToolCall(id="call_2", name="lookup", arguments={"x": 2}),
            ],
        )
    )
    conv.messages.append(
        Message(
            role=MessageRole.TOOL,
            content="result A",
            tool_call_id="call_1",
        )
    )
    conv.messages.append(
        Message(
            role=MessageRole.TOOL,
            content="result B",
            tool_call_id="call_2",
        )
    )
    conv.messages.append(Message(role=MessageRole.ASSISTANT, content="Found A and B."))
    # Latest user message — the one we must never evict.
    conv.messages.append(Message(role=MessageRole.USER, content="thanks"))

    _trim_history(conv)

    # Walk the result and assert every tool message has a preceding
    # assistant entry with the matching tool_call_id earlier in the
    # array.
    seen_call_ids: set[str] = set()
    for msg in conv.messages:
        if msg.role == MessageRole.ASSISTANT:
            for tc in msg.tool_calls:
                seen_call_ids.add(tc.id)
        elif msg.role == MessageRole.TOOL:
            assert msg.tool_call_id in seen_call_ids, (
                f"orphan tool message {msg.tool_call_id!r} survived trim"
            )
    # The result starts on a user message.
    assert conv.messages[0].role == MessageRole.USER
    # The most recent user message survived.
    assert conv.messages[-1].content == "thanks"


def test_trim_history_preserves_most_recent_user_even_when_oversized() -> None:
    """A single oversized user message must not be evicted."""
    conv = Conversation()
    conv.messages.append(Message(role=MessageRole.USER, content="X" * (_HISTORY_MAX_CHARS + 100)))
    _trim_history(conv)
    assert len(conv.messages) == 1
    assert conv.messages[0].role == MessageRole.USER


# ---------------------------------------------------------------------------
# Unit tests for _TurnRecorder
# ---------------------------------------------------------------------------


def test_turn_recorder_reconstructs_assistant_tool_assistant_sequence() -> None:
    """A single round of tool calls produces assistant→tool→assistant."""
    recorder = _TurnRecorder()
    final_msg = Message(role=MessageRole.ASSISTANT, content="Found it.", tool_calls=[])

    recorder.observe(
        "tool_called",
        {
            "name": "lookup",
            "arguments": '{"x": 1}',
            "tool_call_id": "call_1",
        },
    )
    recorder.observe(
        "tool_completed",
        {"name": "lookup", "result": "the answer", "tool_call_id": "call_1"},
    )
    recorder.observe("stream_finished", {"message": final_msg, "iterations": 1})

    collected = recorder.collected()
    assert len(collected) == 3
    assert collected[0].role == MessageRole.ASSISTANT
    assert collected[0].tool_calls[0].id == "call_1"
    assert collected[1].role == MessageRole.TOOL
    assert collected[1].tool_call_id == "call_1"
    assert collected[1].content == "the answer"
    assert collected[2] is final_msg


def test_turn_recorder_handles_two_rounds_of_tool_calls() -> None:
    """The model can dispatch a second round of tools after the first."""
    recorder = _TurnRecorder()
    final_msg = Message(role=MessageRole.ASSISTANT, content="Done.", tool_calls=[])

    # Round 1: one tool call.
    recorder.observe(
        "tool_called",
        {"name": "lookup", "arguments": "{}", "tool_call_id": "call_1"},
    )
    recorder.observe(
        "tool_completed",
        {"name": "lookup", "result": "r1", "tool_call_id": "call_1"},
    )
    # Round 2: another tool call.
    recorder.observe(
        "tool_called",
        {"name": "lookup", "arguments": "{}", "tool_call_id": "call_2"},
    )
    recorder.observe(
        "tool_completed",
        {"name": "lookup", "result": "r2", "tool_call_id": "call_2"},
    )
    recorder.observe("stream_finished", {"message": final_msg, "iterations": 2})

    collected = recorder.collected()
    # Expected shape: A(tc1) T1 A(tc2) T2 A(final)
    assert len(collected) == 5
    assert collected[0].role == MessageRole.ASSISTANT
    assert collected[0].tool_calls[0].id == "call_1"
    assert collected[1].role == MessageRole.TOOL
    assert collected[1].tool_call_id == "call_1"
    assert collected[2].role == MessageRole.ASSISTANT
    assert collected[2].tool_calls[0].id == "call_2"
    assert collected[3].role == MessageRole.TOOL
    assert collected[3].tool_call_id == "call_2"
    assert collected[4] is final_msg


def test_turn_recorder_handles_parallel_tool_calls_in_one_round() -> None:
    """Two tool calls in the same round share one assistant record."""
    recorder = _TurnRecorder()
    final_msg = Message(role=MessageRole.ASSISTANT, content="Done.", tool_calls=[])

    recorder.observe(
        "tool_called",
        {"name": "a", "arguments": "{}", "tool_call_id": "call_1"},
    )
    recorder.observe(
        "tool_called",
        {"name": "b", "arguments": "{}", "tool_call_id": "call_2"},
    )
    recorder.observe(
        "tool_completed",
        {"name": "a", "result": "r1", "tool_call_id": "call_1"},
    )
    recorder.observe(
        "tool_completed",
        {"name": "b", "result": "r2", "tool_call_id": "call_2"},
    )
    recorder.observe("stream_finished", {"message": final_msg, "iterations": 1})

    collected = recorder.collected()
    # Expected: one assistant(tc1, tc2), two tools, one final assistant.
    assert len(collected) == 4
    assert collected[0].role == MessageRole.ASSISTANT
    assert [tc.id for tc in collected[0].tool_calls] == ["call_1", "call_2"]


# ---------------------------------------------------------------------------
# Route-level tests — live DB + scripted SSE core
# ---------------------------------------------------------------------------


class _FakeCore:
    """Minimal stand-in for :class:`AIServiceCore`."""

    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = list(events)
        self._status = ConnectionStatus.CONNECTED
        # ``last_messages`` is a snapshot, not a reference — the SSE
        # handler mutates the conversation in place after the call, so
        # a reference would observe post-turn state.
        self.last_messages: list[Message] = []

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
        conversation: Conversation,
        system_prompt: str = "",
        temperature: float = 0.7,
        tool_context: ToolExecutionContext | None = None,
        llm: object = None,
    ) -> AsyncIterator[StreamEvent]:
        # Snapshot the conversation messages at call time; the SSE
        # handler mutates the live conversation after this generator
        # yields, so storing a reference would observe post-turn state.
        self.last_messages = list(conversation.messages)
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
    email = "history@example.com"
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
async def second_seeded_user(
    fresh_superuser_engine: AsyncEngine,
    seeded_user: tuple[UUID, str, str],
) -> tuple[UUID, str, str]:
    plaintext = "another-secret-phrase-2026"
    user_id = uuid4()
    email = "history2@example.com"
    async with fresh_superuser_engine.begin() as conn:
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
    """Factory yielding a client whose chat is wired to a scripted core."""
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
        events: list[StreamEvent],
    ) -> tuple[AsyncClient, Any, _FakeCore]:
        """Return (client, app, fake_core) so tests can inspect state."""
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        fake = _FakeCore(events)
        app.state.ai_core = fake
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://testserver")
        await stack.enter_async_context(client)
        return client, app, fake

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
    # ``/assistants`` is now the canonical surface that carries the
    # session CSRF token in its composer form.
    page = await client.get("/assistants", follow_redirects=False)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


async def _open_turn(client: AsyncClient, csrf: str, message: str) -> str:
    response = await client.post(
        "/chat/messages",
        data={"message": message, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    match = re.search(r'data-pf-sse-url="/chat/stream/([0-9a-f]+)"', response.text)
    assert match is not None, response.text
    return match.group(1)


def _get_history_for_only_session(app: Any) -> Conversation | None:
    """Return the sole session's history from the app store."""
    store = getattr(app.state, "chat_histories", None)
    if store is None or not store:
        return None
    assert len(store) == 1, store
    return next(iter(store.values()))


# ---------- Test 1: POST appends to history ---------------------------------


async def test_post_messages_appends_user_to_history(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, app, _fake = await web_client_factory([])
    csrf = await _login_and_get_csrf(client, email, password)

    await client.post(
        "/chat/messages",
        data={"message": "Hello Shirley", "csrf_token": csrf},
        follow_redirects=False,
    )

    history = _get_history_for_only_session(app)
    assert history is not None
    assert len(history.messages) == 1
    assert history.messages[0].role == MessageRole.USER
    assert history.messages[0].content == "Hello Shirley"


# ---------- Test 2: SSE completion appends assistant + tool messages ---------


async def test_sse_completion_appends_assistant_and_tool_messages(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    final_msg = Message(role=MessageRole.ASSISTANT, content="Found it.", tool_calls=[])
    events: list[StreamEvent] = [
        StreamEvent(
            "tool_called",
            {
                "name": "lookup",
                "arguments": '{"x": 1}',
                "tool_call_id": "call_1",
            },
        ),
        StreamEvent(
            "tool_completed",
            {
                "name": "lookup",
                "result": "the answer",
                "tool_call_id": "call_1",
            },
        ),
        StreamEvent("stream_finished", {"message": final_msg, "iterations": 1}),
    ]
    client, app, _fake = await web_client_factory(events)
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "find x")
    response = await client.get(f"/chat/stream/{turn_id}")
    assert response.status_code == 200

    history = _get_history_for_only_session(app)
    assert history is not None
    # user → assistant(tool_calls) → tool → final-assistant
    assert [m.role for m in history.messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.ASSISTANT,
    ]
    assert history.messages[1].tool_calls[0].id == "call_1"
    assert history.messages[2].tool_call_id == "call_1"
    assert history.messages[3].content == "Found it."


# ---------- Test 3: SSE error path does not append assistant content ---------


async def test_sse_error_does_not_append_assistant(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    events: list[StreamEvent] = [
        StreamEvent("error", {"message": "boom"}),
    ]
    client, app, _fake = await web_client_factory(events)
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "ask something")
    response = await client.get(f"/chat/stream/{turn_id}")
    assert response.status_code == 200

    history = _get_history_for_only_session(app)
    assert history is not None
    # The user message persists; nothing on the assistant side.
    assert len(history.messages) == 1
    assert history.messages[0].role == MessageRole.USER


# ---------- Test 7: POST /chat/new clears history ---------------------------


async def test_new_chat_clears_history_and_returns_empty_fragment(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, app, _fake = await web_client_factory([])
    csrf = await _login_and_get_csrf(client, email, password)
    # Prime the history with one user message.
    await client.post(
        "/chat/messages",
        data={"message": "first", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert _get_history_for_only_session(app) is not None

    response = await client.post(
        "/chat/new",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert "chat-empty" in response.text
    # History dropped server-side.
    assert _get_history_for_only_session(app) is None


# ---------- Test 8: GET /chat/history with no history -----------------------


async def test_get_chat_history_empty_returns_placeholder(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, _app, _fake = await web_client_factory([])
    await _login_and_get_csrf(client, email, password)

    response = await client.get("/chat/history")
    assert response.status_code == 200
    assert "chat-empty" in response.text


# ---------- Test 9: GET /chat/history renders user + final assistant only ---


async def test_get_chat_history_renders_visible_messages_only(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    final_msg = Message(role=MessageRole.ASSISTANT, content="Final reply.", tool_calls=[])
    events: list[StreamEvent] = [
        StreamEvent(
            "tool_called",
            {
                "name": "lookup",
                "arguments": "{}",
                "tool_call_id": "call_1",
            },
        ),
        StreamEvent(
            "tool_completed",
            {
                "name": "lookup",
                "result": "intermediate result text",
                "tool_call_id": "call_1",
            },
        ),
        StreamEvent("stream_finished", {"message": final_msg, "iterations": 1}),
    ]
    client, _app, _fake = await web_client_factory(events)
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "the question")
    await client.get(f"/chat/stream/{turn_id}")

    response = await client.get("/chat/history")
    assert response.status_code == 200
    body = response.text
    assert "the question" in body
    assert "Final reply." in body
    # Tool messages and tool-call assistant placeholders are not
    # rendered — the user does not see those in the live stream
    # either.
    assert "intermediate result text" not in body


# ---------- Test 10: Logout drops the history ------------------------------


async def test_logout_drops_chat_history(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    _id, email, password = seeded_user
    client, app, _fake = await web_client_factory([])
    csrf = await _login_and_get_csrf(client, email, password)
    await client.post(
        "/chat/messages",
        data={"message": "first", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert _get_history_for_only_session(app) is not None

    response = await client.post(
        "/logout",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _get_history_for_only_session(app) is None


# ---------- Test 11: Conversation continuity end-to-end --------------------


async def test_two_turns_share_history(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Turn 2 sees turn 1's messages in its outgoing conversation."""
    _id, email, password = seeded_user
    final_msg_1 = Message(role=MessageRole.ASSISTANT, content="reply1", tool_calls=[])
    final_msg_2 = Message(role=MessageRole.ASSISTANT, content="reply2", tool_calls=[])
    events: list[StreamEvent] = [
        StreamEvent("stream_finished", {"message": final_msg_1, "iterations": 0}),
    ]
    client, _app, fake = await web_client_factory(events)
    csrf = await _login_and_get_csrf(client, email, password)

    # Turn 1.
    turn_id = await _open_turn(client, csrf, "first question")
    await client.get(f"/chat/stream/{turn_id}")
    # Turn 1's outgoing conversation (captured at stream_response
    # call time) had exactly the one user message.
    roles_seen_turn1 = [m.role for m in fake.last_messages]
    assert roles_seen_turn1 == [MessageRole.USER]
    assert fake.last_messages[0].content == "first question"

    # Rewire the fake for turn 2.
    fake._events = [
        StreamEvent("stream_finished", {"message": final_msg_2, "iterations": 0}),
    ]

    turn_id = await _open_turn(client, csrf, "second question")
    await client.get(f"/chat/stream/{turn_id}")

    # Turn 2's outgoing conversation must include turn 1's user line
    # and turn 1's final assistant reply.
    contents = [m.content for m in fake.last_messages]
    assert "first question" in contents
    assert "reply1" in contents
    assert "second question" in contents
    # Ordering invariant: roles alternate user→assistant→user.
    assert [m.role for m in fake.last_messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.USER,
    ]


# ---------- Test 12: Two sessions, two histories ---------------------------


async def test_two_sessions_have_isolated_histories(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    second_seeded_user: tuple[UUID, str, str],
) -> None:
    """Turns posted under session A do not appear in session B's history."""
    _id_a, email_a, password_a = seeded_user
    _id_b, email_b, password_b = second_seeded_user

    # The factory yields one client/app per call. To get two distinct
    # *sessions* against the same FastAPI app we open two clients
    # against one app; the shared store on app.state isolates by
    # session id (cookies are per-client).
    settings = WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        csrf_cookie_name="portfoliflow_csrf_pre_session",
        database_url=DATABASE_URL,
        database_url_superuser=DATABASE_URL_SUPERUSER,
        session_cookie_secure=False,
    )
    app = create_app(settings)

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        app.state.ai_core = _FakeCore([])

        transport = ASGITransport(app=app)
        client_a = await stack.enter_async_context(
            AsyncClient(transport=transport, base_url="http://testserver")
        )
        client_b = await stack.enter_async_context(
            AsyncClient(transport=transport, base_url="http://testserver")
        )

        csrf_a = await _login_and_get_csrf(client_a, email_a, password_a)
        csrf_b = await _login_and_get_csrf(client_b, email_b, password_b)

        await client_a.post(
            "/chat/messages",
            data={"message": "session A message", "csrf_token": csrf_a},
        )
        await client_b.post(
            "/chat/messages",
            data={"message": "session B message", "csrf_token": csrf_b},
        )

        store = app.state.chat_histories
        assert len(store) == 2
        contents_per_session = {
            sid: [m.content for m in conv.messages] for sid, conv in store.items()
        }
        all_msgs = functools.reduce(operator.iadd, contents_per_session.values(), [])
        assert "session A message" in all_msgs
        assert "session B message" in all_msgs
        # No session sees the other's message.
        for messages in contents_per_session.values():
            assert not ("session A message" in messages and "session B message" in messages)


# ---------------------------------------------------------------------------
# Chart-artefact sidecar (ADR-0114) — unit tests
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal stand-in for the store helpers, which touch only ``app.state``."""

    class _App:
        def __init__(self) -> None:
            self.state = type("_State", (), {})()

    def __init__(self) -> None:
        self.app = _FakeRequest._App()


def _artifact(message_id: str, artifact_id: str) -> dict[str, Any]:
    """One sidecar record in the shape the SSE capture point writes."""
    return {
        "artifact_id": artifact_id,
        "message_id": message_id,
        "spec": {"data": [], "layout": {}},
        "caption": f"caption {artifact_id}",
        "created_at": 0.0,
        "oversized": False,
    }


def test_trim_history_evicts_the_artifacts_of_the_evicted_turn_group() -> None:
    """A dropped turn-group takes its archived specs with it (ADR-0114).

    The whole point of the coupling: an orphaned spec would outlive the message
    it belongs to and could never be rendered, pinned or garbage-collected.
    """
    conv = Conversation()
    for i in range(11):
        conv.messages.append(Message(role=MessageRole.USER, content=f"u{i}"))
        conv.messages.append(Message(role=MessageRole.ASSISTANT, content=f"a{i}"))

    oldest_assistant = conv.messages[1].id
    newest_assistant = conv.messages[-1].id
    artifacts = [
        _artifact(oldest_assistant, "aaaaaaaaaaaa"),
        _artifact(newest_assistant, "bbbbbbbbbbbb"),
    ]

    _trim_history(conv, artifacts)

    surviving_ids = {m.id for m in conv.messages}
    assert oldest_assistant not in surviving_ids  # the group was evicted
    assert [a["artifact_id"] for a in artifacts] == ["bbbbbbbbbbbb"]
    assert all(a["message_id"] in surviving_ids for a in artifacts)


def test_trim_history_without_a_sidecar_is_unchanged() -> None:
    """The sidecar argument is optional — history-only trimming still works."""
    conv = Conversation()
    for i in range(11):
        conv.messages.append(Message(role=MessageRole.USER, content=f"u{i}"))
        conv.messages.append(Message(role=MessageRole.ASSISTANT, content=f"a{i}"))
    _trim_history(conv)
    assert len(conv.messages) <= _HISTORY_MAX_MESSAGES
    assert conv.messages[0].role == MessageRole.USER


def test_session_lru_eviction_drops_the_sidecar_with_the_history() -> None:
    """One lifecycle, two stores: the LRU drop takes the artefacts too."""
    request = cast(Any, _FakeRequest())
    for index in range(_CHAT_HISTORIES_LIMIT):
        session_id = f"session-{index}"
        _get_or_create_history(request, session_id)
        _chart_artifacts(request)[session_id] = [_artifact("m", f"{index:012d}")]

    oldest = "session-0"
    assert oldest in _chart_artifacts(request)

    _get_or_create_history(request, "session-new")  # trips the cap

    assert oldest not in request.app.state.chat_histories
    assert oldest not in _chart_artifacts(request)
    assert "session-1" in _chart_artifacts(request)  # only the oldest went


def test_history_items_interleave_charts_at_their_message_positions() -> None:
    """Charts follow the message they belong to; a chart-only turn still shows.

    The prose rule is ADR-0050's, unchanged (no tool internals, no empty
    bubbles) — so an assistant message with no prose contributes its figure
    and nothing else.
    """
    conv = Conversation()
    user = Message(role=MessageRole.USER, content="chart please")
    with_prose = Message(role=MessageRole.ASSISTANT, content="Here it is.")
    chart_only = Message(role=MessageRole.ASSISTANT, content="")
    conv.messages.extend([user, with_prose, chart_only])

    items = _history_items(
        conv,
        [
            _artifact(with_prose.id, "aaaaaaaaaaaa"),
            _artifact(chart_only.id, "bbbbbbbbbbbb"),
        ],
    )

    assert [i["kind"] for i in items] == ["message", "message", "chart", "chart"]
    assert items[0]["content"] == "chart please"
    assert items[1]["content"] == "Here it is."
    assert items[2]["artifact_id"] == "aaaaaaaaaaaa"
    assert items[3]["artifact_id"] == "bbbbbbbbbbbb"


# ---------------------------------------------------------------------------
# Chart-artefact sidecar (ADR-0114) — route-level tests
# ---------------------------------------------------------------------------


def _sidecar_for_only_session(app: Any) -> list[dict[str, Any]]:
    """Return the sole session's artefact records (``[]`` when absent)."""
    store = getattr(app.state, "chat_chart_artifacts", None)
    if not store:
        return []
    assert len(store) == 1, store
    return next(iter(store.values()))


def _plotly_events(message: Message, spec: dict[str, Any]) -> list[StreamEvent]:
    """A turn that renders one Plotly artefact and finishes with ``message``."""
    return [
        StreamEvent(
            "chart_artifact",
            {
                "chart_format": "plotly",
                "image_base64": "",
                "spec": spec,
                "caption": "NAV trajectory",
            },
        ),
        StreamEvent("stream_finished", {"message": message, "iterations": 1}),
    ]


async def test_a_plotly_artifact_is_archived_against_its_assistant_message(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The capture point archives the spec and anchors it to the turn's message."""
    _id, email, password = seeded_user
    final_msg = Message(role=MessageRole.ASSISTANT, content="Here it is.")
    spec = {"data": [{"type": "scatter", "x": [1], "y": [2]}], "layout": {"title": "NAV"}}
    client, app, _fake = await web_client_factory(_plotly_events(final_msg, spec))
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "chart please")
    await client.get(f"/chat/stream/{turn_id}")

    records = _sidecar_for_only_session(app)
    assert len(records) == 1
    assert records[0]["spec"] == spec
    assert records[0]["caption"] == "NAV trajectory"
    assert records[0]["oversized"] is False
    assert len(records[0]["artifact_id"]) == 12
    # Anchored to the very message the recorder appended to the history.
    assert records[0]["message_id"] == final_msg.id
    history = _get_history_for_only_session(app)
    assert history is not None
    assert records[0]["message_id"] in {m.id for m in history.messages}
    # The spec stays out of the LLM-bound record.
    assert all("scatter" not in (m.content or "") for m in history.messages)


async def test_a_png_artifact_is_not_archived(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Only ``chart_format == "plotly"`` is captured (ADR-0114 §Follow-ups)."""
    _id, email, password = seeded_user
    final_msg = Message(role=MessageRole.ASSISTANT, content="Done")
    events: list[StreamEvent] = [
        StreamEvent(
            "chart_artifact",
            {
                "chart_format": "png",
                "image_base64": "AAAA",
                "spec": None,
                "caption": "Legacy chart",
            },
        ),
        StreamEvent("stream_finished", {"message": final_msg, "iterations": 1}),
    ]
    client, app, _fake = await web_client_factory(events)
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "chart please")
    await client.get(f"/chat/stream/{turn_id}")

    assert _sidecar_for_only_session(app) == []


async def test_an_oversized_spec_streams_live_but_is_not_archived(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Over the cap: the user still sees the chart, the sidecar keeps a marker."""
    _id, email, password = seeded_user
    final_msg = Message(role=MessageRole.ASSISTANT, content="A big one.")
    spec = {"data": [], "layout": {"title": "x" * (_CHART_SPEC_BYTE_CAP + 10)}}
    client, app, _fake = await web_client_factory(_plotly_events(final_msg, spec))
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "the whole book please")
    stream = await client.get(f"/chat/stream/{turn_id}")

    # The live render is never refused — the spec still reaches the browser.
    assert "event: chart" in stream.text
    records = _sidecar_for_only_session(app)
    assert len(records) == 1
    assert records[0]["oversized"] is True
    assert records[0]["spec"] is None


async def test_an_error_turn_archives_nothing(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """No assistant message, no anchor — the turn's charts are dropped.

    Mirrors ADR-0050's error-path rule for assistant content: nothing partial
    is recorded.
    """
    _id, email, password = seeded_user
    events: list[StreamEvent] = [
        StreamEvent(
            "chart_artifact",
            {
                "chart_format": "plotly",
                "image_base64": "",
                "spec": {"data": [], "layout": {}},
                "caption": "doomed",
            },
        ),
        StreamEvent("error", {"message": "Boom!"}),
    ]
    client, app, _fake = await web_client_factory(events)
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "chart please")
    await client.get(f"/chat/stream/{turn_id}")

    assert _sidecar_for_only_session(app) == []


async def test_chat_history_rehydrates_the_chart_at_its_message_position(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A reload restores the figure itself, not a description of it (ADR-0114).

    The spec rides in an inert ``application/json`` block — never interpolated
    into JavaScript — and the figure carries the pin affordance with its
    sidecar handle.
    """
    _id, email, password = seeded_user
    final_msg = Message(role=MessageRole.ASSISTANT, content="Here it is.")
    spec = {"data": [{"type": "scatter", "x": [1], "y": [2]}], "layout": {"title": "NAV"}}
    client, app, _fake = await web_client_factory(_plotly_events(final_msg, spec))
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "chart please")
    await client.get(f"/chat/stream/{turn_id}")

    body = (await client.get("/chat/history")).text
    assert "chart please" in body  # the user line
    assert "Here it is." in body  # the assistant prose
    assert "data-pf-chart-plot" in body  # the figure container
    assert '<script type="application/json" data-pf-chart-spec>' in body
    assert '"scatter"' in body  # the frozen spec itself
    assert "NAV trajectory" in body  # the caption
    artifact_id = _sidecar_for_only_session(app)[0]["artifact_id"]
    assert f"/api/chat/pin-chart?artifact_id={artifact_id}" in body
    # Order: the figure follows the message it belongs to.
    assert body.index("Here it is.") < body.index("data-pf-chart-plot")


async def test_chat_history_renders_the_placeholder_for_an_unarchived_chart(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An over-the-cap figure rehydrates as a calm placeholder, not a gap."""
    _id, email, password = seeded_user
    final_msg = Message(role=MessageRole.ASSISTANT, content="A big one.")
    spec = {"data": [], "layout": {"title": "x" * (_CHART_SPEC_BYTE_CAP + 10)}}
    client, _app, _fake = await web_client_factory(_plotly_events(final_msg, spec))
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "the whole book please")
    await client.get(f"/chat/stream/{turn_id}")

    body = (await client.get("/chat/history")).text
    assert "too large to restore" in body
    assert "data-pf-chart-spec" not in body
    # No pin affordance for a figure the server never archived.
    assert "/api/chat/pin-chart" not in body


async def test_new_chat_and_logout_drop_the_sidecar_with_the_history(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """The two stores share one lifecycle — neither outlives the other."""
    _id, email, password = seeded_user
    final_msg = Message(role=MessageRole.ASSISTANT, content="Here it is.")
    spec = {"data": [], "layout": {}}
    client, app, fake = await web_client_factory(_plotly_events(final_msg, spec))
    csrf = await _login_and_get_csrf(client, email, password)
    turn_id = await _open_turn(client, csrf, "chart please")
    await client.get(f"/chat/stream/{turn_id}")
    assert len(_sidecar_for_only_session(app)) == 1

    # POST /chat/new drops both.
    await client.post("/chat/new", data={"csrf_token": csrf})
    assert _sidecar_for_only_session(app) == []
    assert _get_history_for_only_session(app) is None

    # And so does logout, from a fresh chart.
    fake._events = _plotly_events(Message(role=MessageRole.ASSISTANT, content="again"), spec)
    turn_id = await _open_turn(client, csrf, "chart again")
    await client.get(f"/chat/stream/{turn_id}")
    assert len(_sidecar_for_only_session(app)) == 1

    response = await client.post(
        "/logout",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _sidecar_for_only_session(app) == []
