# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ASGI-level tests for the Shirley web voice surface (ADR-0076 Block 2).

These exercise ``POST /chat/voice`` (STT in) and ``POST /chat/tts``
(TTS out) plus the voice gating:

* a recorded question is transcribed and enters the same turn pipeline
  text does — the transcript becomes the user :class:`Message`'s content
  and the bootstrap fragment carries ``data-pf-voice="1"``;
* a voice turn may carry images for mixed mode (ADR-0075);
* an empty transcript / STT failure / unsupported audio format surfaces
  the inline ``chat_error`` fragment without appending a user message;
* ``/chat/tts`` returns MP3 bytes, 204 for empty text, or 502 on a
  synthesis failure;
* when voice is disabled the endpoints 404 and the composer hides the
  voice toggle; the text path is wholly unaffected (``data-pf-voice="0"``).

The scaffolding mirrors ``test_chat_image_upload.py``: live-DB fixtures
seed a sentinel-tenant owner and the chat route is wired to a scripted
``_FakeCore`` via ``app.state.ai_core``.

Voice reaches the route the way it does in production since ADR-0118 §4:
enablement walks the ``voice.enabled`` chain (here its ``VOICE_ENABLED``
tail) and the provider is **built per request** from a resolved
:class:`~services.voice.ResolvedVoice`. So the double moved one seam down —
``web.routes.chat.build_provider`` is replaced by a recorder that returns
the scripted ``_FakeVoiceProvider`` — and an autouse fixture pins every
``VOICE_*`` variable, because this module loads the developer's ``.env``
and an enabled deployment there would otherwise decide the gating tests.

Per-tenant resolution, chain precedence and the enabled-but-keyless 503
live in the sibling ``test_chat_voice_resolution.py``; what is pinned here
is the endpoint behaviour on either side of the gate.
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
from services.ai_models import ConnectionStatus, MessageRole
from services.ai_service_core import StreamEvent
from services.password_hashing import hash_password
from services.voice import (
    EmptyTranscriptError,
    ResolvedVoice,
    UnsupportedAudioFormatError,
    VoiceSynthesisError,
    VoiceTranscriptionError,
)
from web.main import create_app
from web.routes import chat as chat_routes
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

# A tiny stand-in PNG for the mixed-mode case. The route never decodes the
# bytes; only the MIME type and size are inspected.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

# A vision-capable model id (matches the allowlist) so mixed-mode image
# validation proceeds past the gate.
_VISION_MODEL = "anthropic/claude-sonnet-4.5"

# Arbitrary recorded-audio stand-in. The fake provider ignores it.
_AUDIO_BYTES = b"OggS" + b"\x00" * 32

#: Every environment variable the voice chains read. Pinned per test (set or
#: deleted) so the repository ``.env`` this module loads — an enabled voice
#: deployment on the maintainer's machine — cannot decide an outcome here.
_VOICE_ENV_VARS = (
    "VOICE_ENABLED",
    "VOICE_STT_PROVIDER",
    "VOICE_STT_MODEL",
    "VOICE_STT_API_KEY",
    "VOICE_STT_BASE_URL",
    "VOICE_TTS_PROVIDER",
    "VOICE_TTS_MODEL",
    "VOICE_TTS_VOICE",
    "VOICE_TTS_API_KEY",
)

_STT_KEY = "sk-env-stt"
_TTS_KEY = "sk-env-tts"


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB chat voice tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Fakes — AIServiceCore (CONNECTED, scripted) and VoiceProvider.
# ---------------------------------------------------------------------------


class _FakeCore:
    """Stand-in for :class:`AIServiceCore` with a configurable model id."""

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
    ) -> AsyncIterator[StreamEvent]:
        for event in self._events:
            yield event


class _FakeVoiceProvider:
    """Scripted :class:`VoiceProvider` honouring the Block-1 contract."""

    def __init__(
        self,
        transcript: str = "hello shirley",
        synth: bytes = b"MP3BYTES",
        stt_exc: Exception | None = None,
        tts_exc: Exception | None = None,
    ) -> None:
        self._transcript = transcript
        self._synth = synth
        self._stt_exc = stt_exc
        self._tts_exc = tts_exc

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        if self._stt_exc:
            raise self._stt_exc
        return self._transcript

    async def synthesize(self, text: str, *, fmt: str) -> tuple[bytes, str]:
        if self._tts_exc:
            raise self._tts_exc
        return self._synth, "audio/mpeg"


class _ProviderRecorder:
    """Stands in for :func:`build_provider`: records, then returns the fake.

    One seam below where the double used to sit. The route builds its
    provider from the turn's :class:`ResolvedVoice` (ADR-0118 §4), so the
    factory is the only place a test can both intercept the construction and
    read what was resolved.
    """

    def __init__(self) -> None:
        self.provider = _FakeVoiceProvider()
        self.resolved: ResolvedVoice | None = None
        self.calls = 0

    def __call__(self, resolved: ResolvedVoice) -> _FakeVoiceProvider:
        self.resolved = resolved
        self.calls += 1
        return self.provider


@pytest.fixture(autouse=True)
def pinned_voice_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every ``VOICE_*`` variable; each test sets what it needs."""
    for var in _VOICE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def voice_build(monkeypatch: pytest.MonkeyPatch) -> _ProviderRecorder:
    """Replace ``web.routes.chat.build_provider`` with the recorder."""
    recorder = _ProviderRecorder()
    monkeypatch.setattr(chat_routes, "build_provider", recorder)
    return recorder


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
    email = "voice@example.com"
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
    voice_build: _ProviderRecorder,
):
    """Factory yielding ``(client, app)`` wired to a scripted ``_FakeCore``.

    Passing ``voice_provider`` enables voice the way a deployment does — the
    ``VOICE_ENABLED`` and key variables the chains end at — and hands the
    scripted fake to the ``build_provider`` recorder. Omitting it leaves
    ``VOICE_ENABLED`` unset, which is the off state (ADR-0118 §5).

    The app is returned alongside the client so a test can inspect the
    in-memory chat history on ``app.state.chat_histories``.

    The factory's ``model`` argument sets ``SHIRLEY_MODEL`` as well as the
    fake core's model, mirroring ``test_chat_image_upload.py``: since
    ADR-0112 §4b the vision gate asks about the model the *turn resolved*,
    not the core's, because the model is the tenant's now and the
    singleton's could belong to nobody. With no vault rows the chain ends
    at the environment, which is what is set here.
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
        events: list[StreamEvent],
        model: str = _VISION_MODEL,
        *,
        voice_provider: _FakeVoiceProvider | None = None,
    ) -> tuple[AsyncClient, FastAPI]:
        monkeypatch.setenv("SHIRLEY_MODEL", model)
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        app.state.ai_core = _FakeCore(events, model)
        if voice_provider is not None:
            voice_build.provider = voice_provider
            monkeypatch.setenv("VOICE_ENABLED", "true")
            monkeypatch.setenv("VOICE_STT_API_KEY", _STT_KEY)
            monkeypatch.setenv("VOICE_TTS_API_KEY", _TTS_KEY)
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
# STT — POST /chat/voice
# ---------------------------------------------------------------------------


async def test_voice_happy_path_transcribes_and_begins_turn(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A recorded question → 200, voice-flagged fragment, transcript as content."""
    _id, email, password = seeded_user
    provider = _FakeVoiceProvider(transcript="how is my portfolio doing")
    client, app = await web_client_factory([], voice_provider=provider)
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/voice",
        data={"csrf_token": csrf},
        files={"audio": ("voice.webm", _AUDIO_BYTES, "audio/webm")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert 'data-pf-voice="1"' in response.text
    assert 'data-pf-sse-url="/chat/stream/' in response.text
    # The transcript shows in the user bubble.
    assert "how is my portfolio doing" in response.text

    conv = _only_history(app)
    assert conv is not None
    user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "how is my portfolio doing"
    assert not user_msgs[0].attachments


async def test_voice_mixed_mode_carries_image(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Voice + a valid PNG → one image/png attachment, transcript as content."""
    _id, email, password = seeded_user
    provider = _FakeVoiceProvider(transcript="what does this chart show")
    client, app = await web_client_factory([], voice_provider=provider)
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/voice",
        data={"csrf_token": csrf},
        files={
            "audio": ("voice.webm", _AUDIO_BYTES, "audio/webm"),
            "images": ("sheet.png", _PNG_BYTES, "image/png"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert 'data-pf-voice="1"' in response.text

    conv = _only_history(app)
    assert conv is not None
    user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "what does this chart show"
    assert len(user_msgs[0].attachments) == 1
    assert user_msgs[0].attachments[0].mime_type == "image/png"


async def test_voice_empty_transcript_returns_error_fragment(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An empty transcript → the empty-speech fragment, no user message."""
    _id, email, password = seeded_user
    provider = _FakeVoiceProvider(stt_exc=EmptyTranscriptError("no speech"))
    client, app = await web_client_factory([], voice_provider=provider)
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/voice",
        data={"csrf_token": csrf},
        files={"audio": ("voice.webm", _AUDIO_BYTES, "audio/webm")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    # The apostrophe in "didn't" is HTML-escaped by Jinja (&#39;), so
    # match a punctuation-free substring of the empty-speech copy.
    assert "catch any speech" in response.text
    assert "chat-error" in response.text
    # No user message was appended.
    conv = _only_history(app)
    if conv is not None:
        user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
        assert user_msgs == []


async def test_voice_stt_failure_returns_error_fragment(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A transcription failure → the STT-failure fragment, no user message."""
    _id, email, password = seeded_user
    provider = _FakeVoiceProvider(stt_exc=VoiceTranscriptionError("upstream 500"))
    client, app = await web_client_factory([], voice_provider=provider)
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/voice",
        data={"csrf_token": csrf},
        files={"audio": ("voice.webm", _AUDIO_BYTES, "audio/webm")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Transcription failed" in response.text
    assert "chat-error" in response.text
    conv = _only_history(app)
    if conv is not None:
        user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
        assert user_msgs == []


async def test_voice_unsupported_format_returns_error_fragment(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """An unsupported audio format → the format fragment, no user message."""
    _id, email, password = seeded_user
    provider = _FakeVoiceProvider(stt_exc=UnsupportedAudioFormatError("audio/x-weird"))
    client, app = await web_client_factory([], voice_provider=provider)
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/voice",
        data={"csrf_token": csrf},
        files={"audio": ("voice.bin", _AUDIO_BYTES, "audio/x-weird")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    # The apostrophe in "isn't" is HTML-escaped by Jinja (&#39;), so match a
    # punctuation-free substring of the format-error copy.
    assert "audio format" in response.text
    assert "chat-error" in response.text
    conv = _only_history(app)
    if conv is not None:
        user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
        assert user_msgs == []


# ---------------------------------------------------------------------------
# TTS — POST /chat/tts
# ---------------------------------------------------------------------------


async def test_tts_happy_path_returns_audio(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Prose → 200 with MP3 bytes and an audio/mpeg content type."""
    _id, email, password = seeded_user
    provider = _FakeVoiceProvider(synth=b"MP3BYTES")
    client, _app = await web_client_factory([], voice_provider=provider)
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/tts",
        data={"text": "Here is your answer.", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.content == b"MP3BYTES"
    assert response.headers["content-type"].startswith("audio/mpeg")


async def test_tts_empty_text_returns_204(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """Empty prose (a chart-only turn) → 204 with no body."""
    _id, email, password = seeded_user
    provider = _FakeVoiceProvider()
    client, _app = await web_client_factory([], voice_provider=provider)
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/tts",
        data={"text": "", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 204
    assert response.content == b""


async def test_tts_synthesis_failure_returns_502(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A synthesis failure → 502 (the text answer is already rendered)."""
    _id, email, password = seeded_user
    provider = _FakeVoiceProvider(tts_exc=VoiceSynthesisError("tts down"))
    client, _app = await web_client_factory([], voice_provider=provider)
    csrf = await _login_and_get_csrf(client, email, password)

    response = await client.post(
        "/chat/tts",
        data={"text": "Speak this.", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 502


# ---------------------------------------------------------------------------
# Gating — disabled service and the text-path regression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", [None, "false"])
async def test_disabled_service_404s_and_hides_controls(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
    monkeypatch: pytest.MonkeyPatch,
    flag: str | None,
) -> None:
    """Voice disabled → 404 on both endpoints; the toggle is not rendered.

    Both off states are pinned: the variable unset (the default the chain
    ends at) and set to ``"false"`` — only ``"true"`` enables.
    """
    _id, email, password = seeded_user
    if flag is not None:
        monkeypatch.setenv("VOICE_ENABLED", flag)
    client, _app = await web_client_factory([])  # voice disabled (no provider)
    csrf = await _login_and_get_csrf(client, email, password)

    voice = await client.post(
        "/chat/voice",
        data={"csrf_token": csrf},
        files={"audio": ("voice.webm", _AUDIO_BYTES, "audio/webm")},
        follow_redirects=False,
    )
    assert voice.status_code == 404

    tts = await client.post(
        "/chat/tts",
        data={"text": "anything", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert tts.status_code == 404

    page = await client.get("/assistants", follow_redirects=False)
    assert "data-pf-voice-toggle" not in page.text


async def test_enabled_service_renders_controls(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """``VOICE_ENABLED=true`` → the composer renders the voice toggle marker."""
    _id, email, password = seeded_user
    provider = _FakeVoiceProvider()
    client, _app = await web_client_factory([], voice_provider=provider)
    csrf = await _login_and_get_csrf(client, email, password)
    del csrf  # only needed to confirm login succeeded

    page = await client.get("/assistants", follow_redirects=False)
    assert "data-pf-voice-toggle" in page.text


async def test_text_path_is_voice_zero_regression(
    web_client_factory: Any,
    seeded_user: tuple[UUID, str, str],
) -> None:
    """A text-only send still works and is flagged ``data-pf-voice="0"``."""
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
    assert 'data-pf-voice="0"' in response.text

    conv = _only_history(app)
    assert conv is not None
    user_msgs = [m for m in conv.messages if m.role == MessageRole.USER]
    assert len(user_msgs) == 1
    assert not user_msgs[0].attachments
