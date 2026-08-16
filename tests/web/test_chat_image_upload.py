# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ASGI-level tests for Shirley image input on the web chat surface (ADR-0075).

These exercise ``POST /chat/messages`` with multipart image uploads:

* a valid PNG plus text builds an ``image/png`` :class:`Attachment` on the
  turn's user message;
* an image-only send (no text) gets the default instruction;
* oversize and unsupported-MIME uploads return the inline error fragment
  without appending a user message;
* an image attached while a non-vision model is active returns the
  vision-gate error fragment;
* after a finished SSE turn the persisted user message's ``attachments``
  are stripped (single-turn vision contract).

The scaffolding mirrors ``test_chat_sse.py``: live-DB fixtures seed a
sentinel-tenant owner, the chat route is wired to a scripted
``_FakeCore`` via ``app.state.ai_core``, and the per-test client is bound
to the FastAPI app via ``ASGITransport``. A vision-capable model id is
used by default so validation proceeds; the vision-gate test overrides it.
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
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.tenant_constants import SENTINEL_TENANT_ID
from services.ai_models import ConnectionStatus, Message, MessageRole
from services.ai_service_core import StreamEvent
from services.password_hashing import hash_password
from web.main import create_app
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

# A tiny stand-in PNG. The route never decodes the bytes; only the MIME
# type and size are inspected, so the header is enough to be realistic.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

# A vision-capable model id (matches the allowlist) so image validation
# proceeds past the gate in the happy-path tests.
_VISION_MODEL = "anthropic/claude-sonnet-4.5"


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB chat image-upload tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fake AIServiceCore — configurable model, scripted event sequence.
# ---------------------------------------------------------------------------


class _FakeCore:
    """Stand-in for :class:`AIServiceCore` with a configurable model id.

    The image-upload route reads ``get_status()`` and ``get_model()``;
    the SSE handler additionally drives ``stream_response()``. The model
    id is configurable so the vision-gate path can be exercised.
    """

    def __init__(self, events: list[StreamEvent], model: str) -> None:
        self._events = list(events)
        self._status = ConnectionStatus.CONNECTED
        self._model = model

    def get_status(self) -> ConnectionStatus:
        return self._status

    def get_model(self) -> str:
        return self._model

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
    email = "imgupload@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES "
                "(:id, :name, 'minathena-capital') "
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
    monkeypatch: pytest.MonkeyPatch,
):
    """Factory yielding ``(client, app)`` wired to a scripted ``_FakeCore``.

    The app is returned alongside the client so a test can inspect the
    in-memory chat history on ``app.state.chat_histories``.

    The factory's ``model`` argument sets ``SHIRLEY_MODEL`` as well as the
    fake core's model: since ADR-0112 §4b the vision gate asks about the
    model the *turn resolved*, not the core's, because the model is the
    tenant's now and the singleton's could belong to nobody. With no vault
    rows the chain ends at the environment, which is what is set here.
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
        events: list[StreamEvent], model: str = _VISION_MODEL
    ) -> tuple[AsyncClient, FastAPI]:
        monkeypatch.setenv("SHIRLEY_MODEL", model)
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        app.state.ai_core = _FakeCore(events, model)
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://testserver")
        await stack.enter_async_context(client)
        return client, app

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
    page = await client.get("/assistants", follow_redirects=False)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


def _only_history(app: FastAPI):
    """Return the single in-memory conversation, or ``None`` if absent."""
    store = getattr(app.state, "chat_histories", None)
    if not store:
        return None
    assert len(store) == 1, f"expected one session history, got {len(store)}"
    return next(iter(store.values()))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_multipart_png_with_text_builds_attachment(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A valid PNG + text → 200, turn fragment, and an ``image/png`` attachment."""
    _id, email, password = seeded_user
    client, app = await web_client_factory([])
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/messages",
        data={"message": "What does this fact sheet show?", "csrf_token": csrf},
        files={"images": ("sheet.png", _PNG_BYTES, "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert 'data-pf-sse-url="/chat/stream/' in response.text
    # The thumbnail is rendered inline in the user bubble.
    assert "chat-attachment-thumb" in response.text
    assert "data:image/png;base64," in response.text

    conv = _only_history(app)
    assert conv is not None
    user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "What does this fact sheet show?"
    assert len(user_msgs[0].attachments) == 1
    assert user_msgs[0].attachments[0].mime_type == "image/png"
    assert user_msgs[0].attachments[0].data == _PNG_BYTES


async def test_image_only_send_uses_default_instruction(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An image with no text → 200 and the default instruction as content."""
    _id, email, password = seeded_user
    client, app = await web_client_factory([])
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/messages",
        data={"message": "", "csrf_token": csrf},
        files={"images": ("sheet.png", _PNG_BYTES, "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 200

    conv = _only_history(app)
    assert conv is not None
    user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == (
        "Please analyse the attached image in the context of my portfolio."
    )
    assert len(user_msgs[0].attachments) == 1


async def test_oversize_image_returns_error_fragment(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An image above the ceiling → the size error fragment, no history."""
    # Patch the ceiling on the route's bound name so the test stays fast
    # (no need to upload a real 8 MiB payload).
    monkeypatch.setattr("web.routes.chat.MAX_IMAGE_BYTES", 8)

    _id, email, password = seeded_user
    client, app = await web_client_factory([])
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/messages",
        data={"message": "too big", "csrf_token": csrf},
        files={"images": ("big.png", _PNG_BYTES, "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "too large" in response.text
    assert "chat-error" in response.text
    # No user message with bytes was appended.
    conv = _only_history(app)
    if conv is not None:
        assert all(not m.attachments for m in conv.messages)


async def test_unsupported_mime_returns_error_fragment(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An unsupported image type → the type error fragment, no history."""
    _id, email, password = seeded_user
    client, app = await web_client_factory([])
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/messages",
        data={"message": "weird format", "csrf_token": csrf},
        files={"images": ("weird.bmp", _PNG_BYTES, "image/bmp")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Unsupported image type" in response.text
    assert "chat-error" in response.text
    conv = _only_history(app)
    if conv is not None:
        assert all(not m.attachments for m in conv.messages)


async def test_non_vision_model_returns_vision_gate_fragment(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An image with a non-vision model active → the vision-gate fragment."""
    _id, email, password = seeded_user
    client, app = await web_client_factory([], model="mistralai/mistral-7b-instruct")
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/messages",
        data={"message": "read this", "csrf_token": csrf},
        files={"images": ("sheet.png", _PNG_BYTES, "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    # The apostrophe in "can't" is HTML-escaped by Jinja, so match a
    # punctuation-free substring of the gate message instead.
    assert "vision-capable model" in response.text
    assert "chat-error" in response.text
    conv = _only_history(app)
    if conv is not None:
        assert all(not m.attachments for m in conv.messages)


async def test_text_only_send_still_works(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A plain text message (no image, urlencoded) is unaffected by the change."""
    _id, email, password = seeded_user
    client, app = await web_client_factory([])
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/messages",
        data={"message": "Hello Shirley", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Hello Shirley" in response.text
    conv = _only_history(app)
    assert conv is not None
    user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
    assert len(user_msgs) == 1
    assert not user_msgs[0].attachments


async def test_attachments_stripped_after_finished_turn(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """After a full SSE turn, the user message's image bytes are dropped."""
    _id, email, password = seeded_user
    fake_msg = Message(role=MessageRole.ASSISTANT, content="It shows the NAV.")
    events: list[StreamEvent] = [
        StreamEvent("chunk", {"text": "It shows the NAV."}),
        StreamEvent("stream_finished", {"message": fake_msg, "iterations": 0}),
    ]
    client, app = await web_client_factory(events)
    csrf = await _login_and_get_csrf(client, email, password)

    post = await client.post(
        "/chat/messages",
        data={"message": "What does this show?", "csrf_token": csrf},
        files={"images": ("sheet.png", _PNG_BYTES, "image/png")},
        follow_redirects=False,
    )
    assert post.status_code == 200
    match = re.search(r'data-pf-sse-url="/chat/stream/([0-9a-f]+)"', post.text)
    assert match is not None, post.text
    turn_id = match.group(1)

    # Before the stream is drained the bytes are still present.
    conv = _only_history(app)
    assert conv is not None
    user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
    assert user_msgs[0].attachments, "bytes should be present until the turn finishes"

    stream = await client.get(f"/chat/stream/{turn_id}")
    assert stream.status_code == 200

    # After stream_finished the single-turn contract strips the bytes,
    # and the assistant reply has been appended to history.
    user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
    assert user_msgs[0].attachments == []
    assistant_msgs = [m for m in conv.messages if m.role == MessageRole.ASSISTANT and m.content]
    assert any("NAV" in m.content for m in assistant_msgs)
