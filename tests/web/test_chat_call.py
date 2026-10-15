# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The voice call surface on the web (ADR-0137).

The call twin of the gating section of ``test_chat_voice_resolution.py``,
and built the same way: live Postgres, real vault rows written through
``ScopedSettingRepository``, a real :class:`CredentialResolver` over the
tenant session.

What is pinned here:

* **The switch is the tenant row, and only the tenant row.** The
  ``voice_call`` declaration has no environment link (ADR-0137 §7), so a
  ``VOICE_CALL_ENABLED`` variable switches nothing on; the row's ``true``
  (case-insensitive) is on and every other value is off.
* **The switch alone offers the call.** No key is probed to decide whether
  the call is offered — the ADR-0118 §5 posture the voice controls already
  follow; a missing key is judged when a call is started (ADR-0137 §4).
* **The call chain resolves end to end.** With the switch on, the key comes
  from the tenant's vault row and from nowhere else — a ``VOICE_CALL_API_KEY``
  variable supplies nothing — the defaults of ``services.voice_call`` fill
  the empty fields, the persona voice ``voice_tts.voice`` stands in for an
  empty call voice, and an incomplete call fails naming the card, not
  ``.env`` (ADR-0137 §7).
* **The ``ask_shirley`` job runs the typed turn's machinery** (ADR-0137 §3).
  Through the real login flow and a scripted core: the job answers 202 at
  once, waits for the turn, runs it with the tool allowlist, the turn's LLM
  resolution and tool context, appends only what the turn produced — marked
  ``channel="call"``, the question itself never stored — binds its charts,
  rewrites the answer for the ear (falling back to the language's sentence
  when the rewrite fails) and outlives the poll. A cancel discards the turn
  and closes its generator; a cancel after the end changes nothing; an error
  event fails the job with nothing appended.
* **The relay and the end line** (ADR-0137 §5). Transcript lines land in
  ``seq`` order, each once, however often and in whatever order the
  browser sends them; the end line renders in the history fragment and
  forgets the call's cursor. With the switch off the call's write
  endpoints answer 404.
* **The session start** (ADR-0137 §4, §8, §10). With the vendor answered by
  ``pytest-httpx``: off is 404 and incomplete is 503 naming the card, both
  without a vendor request; a fresh tenant's agent is created and its id
  written back; a known agent is read once per process and definition and
  PATCHed only on a changed hash; a vanished one is re-created; a credential
  is minted on every start; a refusal is a 502 that never quotes the key and
  leaves no row, no memo entry and no line behind.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories import ScopedSettingRepository, tenant_context
from core.tenant_constants import SENTINEL_TENANT_ID
from services.ai_models import ConnectionStatus, Conversation, Message, MessageRole
from services.ai_service_core import StreamEvent, SynthesisResult
from services.credential_vault import MASTER_KEY_ENV_VAR, VaultCipher
from services.investments.credential_resolver import CredentialResolver
from services.password_hashing import hash_password
from services.voice_call import (
    CALL_TOOL_ALLOWLIST,
    DEFAULT_BASE_URL,
    DEFAULT_CONVERSATION_MODEL,
    DEFAULT_LANGUAGE,
    AgentDefinition,
    CallConfigurationError,
    ResolvedCall,
    build_agent_definition,
    fallback_for,
)
from web.main import create_app
from web.routes import chat as chat_routes
from web.settings import WebSettings

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_MASTER_KEY = Fernet.generate_key().decode()
_CALL_KEY = "xi-call-tenant"


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB voice-call tests.",
            allow_module_level=False,
        )


@pytest.fixture(autouse=True)
def silent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the variables a ``voice_call`` environment link would read, were there one."""
    monkeypatch.delenv("VOICE_CALL_ENABLED", raising=False)
    monkeypatch.delenv("VOICE_CALL_API_KEY", raising=False)
    # The persona-voice fallback reads ``voice_tts.voice``, which has an env link.
    monkeypatch.delenv("VOICE_TTS_VOICE", raising=False)


@pytest.fixture
def vault_key(monkeypatch: pytest.MonkeyPatch) -> VaultCipher:
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, _MASTER_KEY)
    return VaultCipher(_MASTER_KEY)


@pytest_asyncio.fixture
async def fresh_superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    assert DATABASE_URL_SUPERUSER is not None
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_schema(fresh_superuser_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    truncate_sql = text(
        "TRUNCATE TABLE scoped_settings, data_upload_sheets, data_uploads, "
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
async def seeded_tenant(fresh_superuser_engine: AsyncEngine, reset_schema: None) -> None:
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )


@pytest_asyncio.fixture
async def app_engine(seeded_tenant: None) -> AsyncGenerator[AsyncEngine, None]:
    """The application-role engine, so row-level security applies as in a request."""
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _write_setting(
    engine: AsyncEngine,
    *,
    provider: str,
    key: str,
    value: str,
    cipher: VaultCipher | None = None,
) -> None:
    """Write one tenant-scope row exactly as the admin surface would."""
    async with tenant_context(engine, SENTINEL_TENANT_ID) as session:
        repo = ScopedSettingRepository(session)
        if cipher is not None:
            await repo.upsert(
                scope="tenant",
                provider=provider,
                key=key,
                is_secret=True,
                value_ciphertext=cipher.encrypt(value),
                secret_hint=value[-4:],
            )
        else:
            await repo.upsert(
                scope="tenant",
                provider=provider,
                key=key,
                is_secret=False,
                value_plain=value,
            )


async def _switch(engine: AsyncEngine) -> bool:
    """Read the call switch the way the dock does, inside the tenant's session."""
    async with tenant_context(engine, SENTINEL_TENANT_ID) as db:
        return await chat_routes._call_switch(CredentialResolver(session=db))


async def _call(engine: AsyncEngine) -> ResolvedCall | None:
    """Resolve the call chain the way a call start does, inside the tenant's session."""
    async with tenant_context(engine, SENTINEL_TENANT_ID) as db:
        return await chat_routes._call_values(
            CredentialResolver(session=db), tenant_id=SENTINEL_TENANT_ID
        )


async def _enable_with_key(engine: AsyncEngine, cipher: VaultCipher) -> None:
    """Switch the call on and store its key in the tenant's vault row."""
    await _write_setting(engine, provider="voice_call", key="enabled", value="true")
    await _write_setting(
        engine, provider="voice_call", key="api_key", value=_CALL_KEY, cipher=cipher
    )


# ---------------------------------------------------------------------------
# The switch (ADR-0137 §4, §7)
# ---------------------------------------------------------------------------


async def test_call_switch_is_off_with_no_row_even_when_the_environment_says_on(
    app_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No tenant row: off, whatever the environment holds — there is no env link."""
    monkeypatch.setenv("VOICE_CALL_ENABLED", "true")

    assert await _switch(app_engine) is False


async def test_call_switch_follows_the_tenant_row(app_engine: AsyncEngine) -> None:
    """``true`` in any case is on; anything else is off; an upsert applies at the next read."""
    for value, expected in (("true", True), ("TRUE", True), ("yes", False), ("false", False)):
        await _write_setting(app_engine, provider="voice_call", key="enabled", value=value)
        assert await _switch(app_engine) is expected, value


async def test_call_switch_offers_the_call_without_a_key(app_engine: AsyncEngine) -> None:
    """The switch alone offers the call; no ``api_key`` row is stored."""
    await _write_setting(app_engine, provider="voice_call", key="enabled", value="true")

    # The key is judged when a call is started, not here (ADR-0118 §5 posture).
    assert await _switch(app_engine) is True


# ---------------------------------------------------------------------------
# The call chain (ADR-0137 §7)
# ---------------------------------------------------------------------------


async def test_call_values_are_none_when_off_whatever_else_is_set(
    app_engine: AsyncEngine, vault_key: VaultCipher
) -> None:
    """Switched off: inactive, even with a key and every config row stored."""
    await _write_setting(app_engine, provider="voice_call", key="enabled", value="false")
    await _write_setting(
        app_engine, provider="voice_call", key="api_key", value=_CALL_KEY, cipher=vault_key
    )
    for key, value in (("voice", "v1"), ("model", "m-x"), ("agent_id", "agent_x")):
        await _write_setting(app_engine, provider="voice_call", key=key, value=value)

    assert await _call(app_engine) is None


async def test_call_values_fill_the_package_defaults(
    app_engine: AsyncEngine, vault_key: VaultCipher
) -> None:
    """On, a key and a voice, nothing else: every default from ``services.voice_call``."""
    await _enable_with_key(app_engine, vault_key)
    await _write_setting(app_engine, provider="voice_call", key="voice", value="v1")

    call = await _call(app_engine)

    assert call == ResolvedCall(
        api_key=_CALL_KEY,
        agent_id=None,
        model=DEFAULT_CONVERSATION_MODEL,
        voice="v1",
        language=DEFAULT_LANGUAGE,
        base_url=DEFAULT_BASE_URL,
    )


async def test_call_values_fall_back_to_the_persona_voice(
    app_engine: AsyncEngine, vault_key: VaultCipher
) -> None:
    """No call voice: the tenant's ``voice_tts.voice`` row is the call's voice."""
    await _enable_with_key(app_engine, vault_key)
    await _write_setting(app_engine, provider="voice_tts", key="voice", value="v_tts")

    call = await _call(app_engine)

    assert call is not None
    assert call.voice == "v_tts"


async def test_call_values_without_a_key_name_the_card(
    app_engine: AsyncEngine, vault_key: VaultCipher
) -> None:
    """On without a key: a configuration error naming the card, never ``.env``."""
    await _write_setting(app_engine, provider="voice_call", key="enabled", value="true")
    await _write_setting(app_engine, provider="voice_call", key="voice", value="v1")

    with pytest.raises(CallConfigurationError) as excinfo:
        await _call(app_engine)

    assert "Voice call" in str(excinfo.value)
    assert ".env" not in str(excinfo.value)


async def test_call_values_ignore_a_key_in_the_environment(
    app_engine: AsyncEngine,
    vault_key: VaultCipher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``VOICE_CALL_API_KEY`` supplies nothing: there is no env link, end to end."""
    monkeypatch.setenv("VOICE_CALL_API_KEY", "xi-call-env")
    await _write_setting(app_engine, provider="voice_call", key="enabled", value="true")
    await _write_setting(app_engine, provider="voice_call", key="voice", value="v1")

    with pytest.raises(CallConfigurationError):
        await _call(app_engine)


async def test_call_values_read_the_tenant_rows(
    app_engine: AsyncEngine, vault_key: VaultCipher
) -> None:
    """Agent id, model and base URL come from their rows; the trailing slash is dropped."""
    await _enable_with_key(app_engine, vault_key)
    for key, value in (
        ("voice", "v1"),
        ("agent_id", "agent_x"),
        ("model", "m-x"),
        ("base_url", "https://eu.invalid/"),
    ):
        await _write_setting(app_engine, provider="voice_call", key=key, value=value)

    call = await _call(app_engine)

    assert call is not None
    assert (call.agent_id, call.model, call.base_url) == ("agent_x", "m-x", "https://eu.invalid")


# ---------------------------------------------------------------------------
# The ask_shirley job, the relay and the end line (ADR-0137 §3–§5)
# ---------------------------------------------------------------------------

_LLM_MODEL = "anthropic/claude-sonnet-4.5"
_SPOKEN = "Das Buch steht gut."


class _FakeCallCore:
    """A scripted :class:`AIServiceCore` for the job: a turn and a rewrite.

    ``stream_response`` yields ``events`` in order and, before the event at
    an index in ``gates``, waits for that gate — so a test can hold the turn
    before its first event (still waiting for the lock) or mid-stream. It
    records what each turn was handed and whether the turn's generator was
    closed early. ``run_synthesis`` returns ``synthesis`` or raises it.
    """

    def __init__(self) -> None:
        self.events: list[StreamEvent] = []
        self.gates: dict[int, asyncio.Event] = {}
        self.turns: list[dict[str, Any]] = []
        self.closed = False
        self.synthesis: SynthesisResult | Exception = SynthesisResult(
            tool_calls=[{"name": "speak", "arguments": {"text": _SPOKEN}, "id": "c1"}],
            raw_text="",
        )
        self.synthesis_calls: list[dict[str, Any]] = []

    def get_status(self) -> ConnectionStatus:
        return ConnectionStatus.CONNECTED

    def get_model(self) -> str:
        return _LLM_MODEL

    def get_system_prompt(self, prompt_name: str = "shirley") -> str:
        return ""

    async def stream_response(
        self,
        conversation: Conversation,
        system_prompt: str = "",
        temperature: float = 0.7,
        tool_context: object = None,
        *,
        llm: object = None,
        tool_names: frozenset[str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.turns.append(
            {
                "messages": list(conversation.messages),
                "tool_names": tool_names,
                "llm": llm,
                "tool_context": tool_context,
            }
        )
        try:
            for index, event in enumerate(self.events):
                gate = self.gates.get(index)
                if gate is not None:
                    await gate.wait()
                yield event
        except GeneratorExit:
            self.closed = True
            raise

    async def run_synthesis(self, **kwargs: Any) -> SynthesisResult:
        self.synthesis_calls.append(kwargs)
        if isinstance(self.synthesis, Exception):
            raise self.synthesis
        return self.synthesis


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin Shirley's LLM chain, so a job is never refused for want of one."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-openrouter")
    monkeypatch.setenv("SHIRLEY_MODEL", _LLM_MODEL)


@pytest.fixture
def fake_core() -> _FakeCallCore:
    return _FakeCallCore()


@pytest_asyncio.fixture
async def seeded_user(
    fresh_superuser_engine: AsyncEngine,
    reset_schema: None,
) -> tuple[UUID, str, str]:
    plaintext = "correct-horse-battery-staple"
    user_id = uuid4()
    email = "voice-call@example.com"
    async with fresh_superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, 'minathena-capital') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
        )
        await conn.execute(
            text(
                """
                INSERT INTO users
                    (id, tenant_id, email, password_hash, roles, is_active)
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
async def app_client(
    seeded_user: tuple[UUID, str, str],
    fake_core: _FakeCallCore,
    llm_env: None,
) -> AsyncGenerator[Any, None]:
    """Yield ``(client, app)`` for a fully wired app with the scripted core."""
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
    try:
        app = create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        app.state.ai_core = fake_core
        transport = ASGITransport(app=app)
        client = AsyncClient(transport=transport, base_url="http://testserver")
        await stack.enter_async_context(client)
        yield client, app
    finally:
        await stack.aclose()


@pytest_asyncio.fixture
async def call_on(app_client: Any) -> None:
    """Switch the call on as a tenant row — no key: the switch alone offers the call."""
    _client, app = app_client
    await _write_setting(app.state.engine, provider="voice_call", key="enabled", value="true")


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """Log in and return the session CSRF token from the dock's composer form."""
    pre = await client.get("/login")
    csrf = pre.cookies.get("portfoliflow_csrf_pre_session")
    assert csrf is not None
    await client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )
    page = await client.get("/chat/dock", follow_redirects=False)
    assert page.status_code == 200
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


async def _ask(
    client: AsyncClient,
    csrf: str,
    question: str = "Wie steht das Buch?",
    *,
    events: list[dict[str, Any]] | None = None,
    language: str | None = None,
) -> Any:
    body: dict[str, Any] = {"call_id": "call1", "question": question, "events": events or []}
    if language is not None:
        body["language"] = language
    return await client.post("/chat/call/ask", json=body, headers={"X-CSRF-Token": csrf})


async def _relay(client: AsyncClient, csrf: str, seqs: list[int], call_id: str = "call1") -> Any:
    events = [{"seq": seq, "role": "user", "text": f"line {seq}"} for seq in seqs]
    return await client.post(
        "/chat/call/events",
        json={"call_id": call_id, "events": events},
        headers={"X-CSRF-Token": csrf},
    )


async def _job_state(client: AsyncClient, job_id: str) -> dict[str, Any]:
    response = await client.get(f"/chat/call/jobs/{job_id}")
    assert response.status_code == 200
    return response.json()


async def _poll_until(client: AsyncClient, job_id: str, state: str) -> dict[str, Any]:
    """Poll the job until it reports ``state``; bounded, never a fixed sleep."""
    result: dict[str, Any] = {}
    for _ in range(50):
        result = await _job_state(client, job_id)
        if result["state"] == state:
            return result
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {state}: {result}")


async def _finish(app: Any, job_id: str) -> None:
    """Await the job's task, if it is still running."""
    for (_session_id, running_id), task in list(app.state.call_tasks.items()):
        if running_id == job_id:
            await asyncio.wait_for(task, timeout=5)


def _history(app: Any) -> Conversation:
    store = app.state.chat_histories
    assert len(store) == 1, store
    return next(iter(store.values()))


def _finished(message: Message) -> StreamEvent:
    return StreamEvent("stream_finished", {"message": message, "iterations": 1})


async def test_call_endpoints_answer_404_when_the_switch_is_off(
    app_client: Any, seeded_user: tuple[UUID, str, str]
) -> None:
    """No tenant row: ``ask``, ``events`` and ``end`` are not there."""
    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)
    headers = {"X-CSRF-Token": csrf}

    ask = await _ask(client, csrf)
    events = await _relay(client, csrf, [1])
    end = await client.post("/chat/call/end", json={"call_id": "call1"}, headers=headers)

    assert [ask.status_code, events.status_code, end.status_code] == [404, 404, 404]


async def test_a_job_completes_with_the_typed_turns_machinery(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    fake_core: _FakeCallCore,
    call_on: None,
) -> None:
    """202, then ``done``; the answer is stored, the question is not."""
    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)
    answer = Message(role=MessageRole.ASSISTANT, content="Das Buch steht bei 3,5 Mrd. EUR.")
    fake_core.events = [StreamEvent("chunk", {"text": "Das"}), _finished(answer)]

    response = await _ask(client, csrf, "Wie steht das Buch?")
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    result = await _poll_until(client, job_id, "done")

    assert result["speakable"] == _SPOKEN
    assert result["prose"] == answer.content
    assert result["message_id"] == answer.id
    history = _history(app)
    assert [m.id for m in history.messages] == [answer.id]
    assert history.messages[0].channel == "call"
    assert all(m.content != "Wie steht das Buch?" for m in history.messages)
    turn = fake_core.turns[0]
    asked = turn["messages"][-1]
    assert (asked.role, asked.content, asked.channel) == (
        MessageRole.USER,
        "Wie steht das Buch?",
        "call",
    )
    assert turn["tool_names"] == CALL_TOOL_ALLOWLIST
    assert turn["llm"].model == _LLM_MODEL
    assert turn["tool_context"].tenant_id == SENTINEL_TENANT_ID


async def test_events_in_the_ask_body_land_first_in_seq_order(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    fake_core: _FakeCallCore,
    call_on: None,
) -> None:
    """The lines ride the ask body and are in the history before the answer."""
    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)
    answer = Message(role=MessageRole.ASSISTANT, content="Gut.")
    fake_core.events = [_finished(answer)]
    events = [
        {"seq": 1, "role": "assistant", "text": "Moment, ich schaue nach."},
        {"seq": 0, "role": "user", "text": "Wie steht das Buch?"},
    ]

    job_id = (await _ask(client, csrf, events=events)).json()["job_id"]
    await _poll_until(client, job_id, "done")

    messages = _history(app).messages
    assert [(m.role, m.content) for m in messages] == [
        (MessageRole.USER, "Wie steht das Buch?"),
        (MessageRole.ASSISTANT, "Moment, ich schaue nach."),
        (MessageRole.ASSISTANT, "Gut."),
    ]
    assert [m.channel for m in messages[:2]] == ["call", "call"]
    assert [m.content for m in fake_core.turns[0]["messages"][:2]] == [
        "Wie steht das Buch?",
        "Moment, ich schaue nach.",
    ]


async def test_the_relay_appends_each_line_once_in_order(
    app_client: Any, seeded_user: tuple[UUID, str, str], call_on: None
) -> None:
    """A repeated batch appends nothing; a late, out-of-order line lands in its place."""
    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    first = await _relay(client, csrf, [1, 2])
    again = await _relay(client, csrf, [1, 2])
    late = await _relay(client, csrf, [3, 2])

    assert [r.json()["accepted"] for r in (first, again, late)] == [2, 0, 1]
    assert [m.content for m in _history(app).messages] == ["line 1", "line 2", "line 3"]
    assert all(m.channel == "call" for m in _history(app).messages)


async def test_a_job_waits_for_the_turn_until_its_first_event(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    fake_core: _FakeCallCore,
    call_on: None,
) -> None:
    """``waiting_for_turn`` until the first event, ``running`` after it, then ``done``."""
    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)
    before_first, before_finish = asyncio.Event(), asyncio.Event()
    fake_core.gates = {0: before_first, 1: before_finish}
    fake_core.events = [
        StreamEvent("chunk", {"text": "Gut"}),
        _finished(Message(role=MessageRole.ASSISTANT, content="Gut.")),
    ]

    job_id = (await _ask(client, csrf)).json()["job_id"]
    await asyncio.sleep(0)
    assert (await _job_state(client, job_id))["state"] == "waiting_for_turn"

    before_first.set()
    await _poll_until(client, job_id, "running")

    before_finish.set()
    await _poll_until(client, job_id, "done")


async def test_a_cancel_discards_the_turn_and_closes_its_generator(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    fake_core: _FakeCallCore,
    call_on: None,
) -> None:
    """Barge-in mid-stream: cancelled, nothing in the history, the generator closed."""
    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)
    held = asyncio.Event()
    fake_core.gates = {1: held}
    fake_core.events = [
        StreamEvent("chunk", {"text": "Gut"}),
        _finished(Message(role=MessageRole.ASSISTANT, content="Gut.")),
    ]

    job_id = (await _ask(client, csrf)).json()["job_id"]
    await _poll_until(client, job_id, "running")
    response = await client.post(f"/chat/call/jobs/{job_id}/cancel", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert response.json() == {"job_id": job_id, "state": "cancelled"}

    held.set()
    await _finish(app, job_id)

    assert _history(app).messages == []
    assert fake_core.closed is True
    assert (await _job_state(client, job_id))["state"] == "cancelled"


async def test_a_cancel_after_the_end_changes_nothing_and_unknown_jobs_are_404(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    fake_core: _FakeCallCore,
    call_on: None,
) -> None:
    """A late cancel answers the ``done`` state; a stranger's id resolves to nothing."""
    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)
    fake_core.events = [_finished(Message(role=MessageRole.ASSISTANT, content="Gut."))]
    headers = {"X-CSRF-Token": csrf}

    job_id = (await _ask(client, csrf)).json()["job_id"]
    await _poll_until(client, job_id, "done")
    late = await client.post(f"/chat/call/jobs/{job_id}/cancel", headers=headers)
    unknown_get = await client.get("/chat/call/jobs/000000000000")
    unknown_cancel = await client.post("/chat/call/jobs/000000000000/cancel", headers=headers)

    assert late.status_code == 200
    assert late.json() == {"job_id": job_id, "state": "done"}
    assert [unknown_get.status_code, unknown_cancel.status_code] == [404, 404]


async def test_an_error_event_fails_the_job_with_nothing_appended(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    fake_core: _FakeCallCore,
    call_on: None,
) -> None:
    """ADR-0050's error-path rule: the message is reported, the history untouched."""
    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)
    fake_core.events = [
        StreamEvent("chunk", {"text": "partial"}),
        StreamEvent("error", {"message": "The model refused."}),
    ]

    job_id = (await _ask(client, csrf)).json()["job_id"]
    result = await _poll_until(client, job_id, "failed")

    assert result["error"] == "The model refused."
    assert _history(app).messages == []


async def test_the_turns_charts_are_bound_and_reported(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    fake_core: _FakeCallCore,
    call_on: None,
) -> None:
    """A Plotly artefact is archived against the answer and rides the ``done`` result."""
    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)
    answer = Message(role=MessageRole.ASSISTANT, content="Hier ist der Verlauf.")
    spec = {"data": [{"type": "scatter", "x": [1], "y": [2]}], "layout": {}}
    fake_core.events = [
        StreamEvent(
            "chart_artifact",
            {"chart_format": "plotly", "image_base64": "", "spec": spec, "caption": "NAV"},
        ),
        _finished(answer),
    ]

    job_id = (await _ask(client, csrf)).json()["job_id"]
    result = await _poll_until(client, job_id, "done")

    (records,) = app.state.chat_chart_artifacts.values()
    assert [r["message_id"] for r in records] == [answer.id]
    assert [(c["artifact_id"], c["spec"]) for c in result["charts"]] == [
        (records[0]["artifact_id"], spec)
    ]
    context = fake_core.synthesis_calls[0]["context_messages"]
    assert "One chart accompanies" in context[0]["content"]


async def test_a_failed_rewrite_speaks_the_fallback_sentence(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    fake_core: _FakeCallCore,
    call_on: None,
) -> None:
    """The job is still ``done``; the sentence follows the ask's language, else the default."""
    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)
    fake_core.synthesis = RuntimeError("rewrite down")
    spoken: list[str] = []
    for language in ("de", "en", None):
        fake_core.events = [_finished(Message(role=MessageRole.ASSISTANT, content="Gut."))]
        job_id = (await _ask(client, csrf, language=language)).json()["job_id"]
        spoken.append((await _poll_until(client, job_id, "done"))["speakable"])

    assert spoken == [fallback_for("de"), fallback_for("en"), fallback_for(DEFAULT_LANGUAGE)]


async def test_the_job_outlives_the_poll(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    fake_core: _FakeCallCore,
    call_on: None,
) -> None:
    """Nobody polls; the answer still lands in the history and a late poll says ``done``.

    The browser's 75-second fallback promises exactly this.
    """
    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)
    answer = Message(role=MessageRole.ASSISTANT, content="Spät, aber da.")
    fake_core.events = [_finished(answer)]

    job_id = (await _ask(client, csrf)).json()["job_id"]
    await _finish(app, job_id)

    assert [m.id for m in _history(app).messages] == [answer.id]
    assert (await _job_state(client, job_id))["state"] == "done"


async def test_end_appends_the_line_and_forgets_the_cursor(
    app_client: Any, seeded_user: tuple[UUID, str, str], call_on: None
) -> None:
    """The fragment carries the end line; the call's relay starts afresh."""
    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)
    assert (await _relay(client, csrf, [1])).json()["accepted"] == 1

    response = await client.post(
        "/chat/call/end",
        json={"call_id": "call1", "at": "12:11"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert "chat-call-line" in response.text
    assert "Call ended 12:11" in response.text
    assert (await _relay(client, csrf, [1])).json()["accepted"] == 1


# ---------------------------------------------------------------------------
# The session start (ADR-0137 §4, §8, §10)
# ---------------------------------------------------------------------------

_VENDOR = "https://vendor.invalid"
_TOOLS_URL = f"{_VENDOR}/v1/convai/tools"
_CREATE_URL = f"{_VENDOR}/v1/convai/agents/create"
_TOOL_URL = f"{_VENDOR}/v1/convai/tools/tool_1"


def _agent_url(agent_id: str) -> str:
    return f"{_VENDOR}/v1/convai/agents/{agent_id}"


def _token_url(agent_id: str) -> str:
    return f"{_VENDOR}/v1/convai/conversation/token?agent_id={agent_id}"


def _expected_definition(voice: str) -> AgentDefinition:
    """The definition the endpoint builds for this module's tenant rows."""
    return build_agent_definition(
        ResolvedCall(
            api_key=_CALL_KEY,
            agent_id=None,
            model=DEFAULT_CONVERSATION_MODEL,
            voice=voice,
            language=DEFAULT_LANGUAGE,
            base_url=_VENDOR,
        )
    )


@pytest_asyncio.fixture
async def call_configured(app_client: Any, vault_key: VaultCipher) -> None:
    """On, a key, the vendor host pinned to ``vendor.invalid`` and a call voice."""
    _client, app = app_client
    engine = app.state.engine
    await _enable_with_key(engine, vault_key)
    await _write_setting(engine, provider="voice_call", key="base_url", value=_VENDOR)
    await _write_setting(engine, provider="voice_call", key="voice", value="v_call")


async def _start(client: AsyncClient, csrf: str, at: str | None = None) -> Any:
    body = {"at": at} if at is not None else None
    return await client.post("/chat/call/session", json=body, headers={"X-CSRF-Token": csrf})


async def _agent_row(engine: AsyncEngine) -> str | None:
    """Read the tenant's ``voice_call.agent_id`` row, as the Admin card does."""
    async with tenant_context(engine, SENTINEL_TENANT_ID) as db:
        return await CredentialResolver(session=db).resolve_config(
            "voice_call", "agent_id", scopes=("tenant",)
        )


def _token_reply(token: str) -> dict[str, str]:
    return {"token": token, "conversation_id": "conv_1"}


async def test_session_answers_404_when_the_call_is_off(
    app_client: Any, seeded_user: tuple[UUID, str, str], httpx_mock: Any
) -> None:
    """No tenant row: 404, and the vendor is never asked."""
    client, _app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    response = await _start(client, csrf)

    assert response.status_code == 404
    assert httpx_mock.get_requests() == []


async def test_session_answers_503_naming_the_card_when_incomplete(
    app_client: Any, seeded_user: tuple[UUID, str, str], call_on: None, httpx_mock: Any
) -> None:
    """On without a key: 503 naming the card; no vendor request, no history line."""
    client, app = app_client
    _uid, email, password = seeded_user
    csrf = await _login(client, email, password)

    response = await _start(client, csrf)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "Voice call" in detail
    assert "Admin → Providers & Credentials" in detail
    assert httpx_mock.get_requests() == []
    assert not getattr(app.state, "chat_call_lines", None)


async def test_the_first_call_provisions_the_agent_and_writes_its_id_back(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    vault_key: VaultCipher,
    httpx_mock: Any,
) -> None:
    """Fresh tenant: tool, agent, token; the id lands in the row and the memo."""
    client, app = app_client
    _uid, email, password = seeded_user
    engine = app.state.engine
    await _enable_with_key(engine, vault_key)
    await _write_setting(engine, provider="voice_call", key="base_url", value=_VENDOR)
    await _write_setting(engine, provider="voice_tts", key="voice", value="v_tts")
    httpx_mock.add_response(method="POST", url=_TOOLS_URL, json={"id": "tool_new"})
    httpx_mock.add_response(method="POST", url=_CREATE_URL, json={"agent_id": "agent_new"})
    httpx_mock.add_response(method="GET", url=_token_url("agent_new"), json=_token_reply("tok-1"))
    csrf = await _login(client, email, password)

    response = await _start(client, csrf, at="12:03")

    assert response.status_code == 200
    payload = response.json()
    assert payload["vendor"] == "elevenlabs"
    assert payload["token"] == "tok-1"
    assert payload["conversation_id"] == "conv_1"
    assert re.fullmatch(r"[0-9a-f]{12}", payload["call_id"])
    assert payload["max_duration_seconds"] == 1200
    assert payload["language"] == "de"
    assert await _agent_row(engine) == "agent_new"
    definition = _expected_definition("v_tts")
    assert app.state.call_agent_memo == {
        SENTINEL_TENANT_ID: ("agent_new", definition.definition_hash)
    }
    history = await client.get("/chat/history")
    assert history.text.count('class="chat-call-line"') == 1
    assert "Call started 12:03" in history.text


async def test_a_known_agent_is_read_once_and_the_credential_minted_per_start(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    call_configured: None,
    httpx_mock: Any,
) -> None:
    """Fresh process: one GET, one token; then a memo hit: a token alone, a new one."""
    client, app = app_client
    _uid, email, password = seeded_user
    await _write_setting(app.state.engine, provider="voice_call", key="agent_id", value="agent_1")
    definition = _expected_definition("v_call")
    agent = {"agent_id": "agent_1", "name": definition.name}
    httpx_mock.add_response(method="GET", url=_agent_url("agent_1"), json=agent)
    httpx_mock.add_response(method="GET", url=_token_url("agent_1"), json=_token_reply("tok-a"))
    httpx_mock.add_response(method="GET", url=_token_url("agent_1"), json=_token_reply("tok-b"))
    csrf = await _login(client, email, password)

    second = await _start(client, csrf)
    third = await _start(client, csrf)

    assert [second.status_code, third.status_code] == [200, 200]
    assert [second.json()["token"], third.json()["token"]] == ["tok-a", "tok-b"]
    assert [(r.method, str(r.url)) for r in httpx_mock.get_requests()] == [
        ("GET", _agent_url("agent_1")),
        ("GET", _token_url("agent_1")),
        ("GET", _token_url("agent_1")),
    ]


async def test_a_changed_definition_patches_the_agent(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    call_configured: None,
    httpx_mock: Any,
) -> None:
    """Another hash at the vendor: PATCH tool, PATCH agent, token; the row stays."""
    client, app = app_client
    _uid, email, password = seeded_user
    engine = app.state.engine
    await _write_setting(engine, provider="voice_call", key="agent_id", value="agent_1")
    stale = {
        "agent_id": "agent_1",
        "name": "PortfoliFLOW Shirley call · def 00000000",
        "conversation_config": {"agent": {"prompt": {"tool_ids": ["tool_1"]}}},
    }
    httpx_mock.add_response(method="GET", url=_agent_url("agent_1"), json=stale)
    httpx_mock.add_response(method="PATCH", url=_TOOL_URL, json={"id": "tool_1"})
    httpx_mock.add_response(method="PATCH", url=_agent_url("agent_1"), json={"agent_id": "agent_1"})
    httpx_mock.add_response(method="GET", url=_token_url("agent_1"), json=_token_reply("tok-1"))
    csrf = await _login(client, email, password)

    response = await _start(client, csrf)

    assert response.status_code == 200
    assert await _agent_row(engine) == "agent_1"
    definition = _expected_definition("v_call")
    assert app.state.call_agent_memo == {
        SENTINEL_TENANT_ID: ("agent_1", definition.definition_hash)
    }


async def test_a_vanished_agent_is_recreated_and_the_row_rewritten(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    call_configured: None,
    httpx_mock: Any,
) -> None:
    """The vendor answers 404 for the stored id: created afresh, the new id stored."""
    client, app = app_client
    _uid, email, password = seeded_user
    engine = app.state.engine
    await _write_setting(engine, provider="voice_call", key="agent_id", value="agent_gone")
    httpx_mock.add_response(method="GET", url=_agent_url("agent_gone"), status_code=404)
    httpx_mock.add_response(method="POST", url=_TOOLS_URL, json={"id": "tool_new"})
    httpx_mock.add_response(method="POST", url=_CREATE_URL, json={"agent_id": "agent_new"})
    httpx_mock.add_response(method="GET", url=_token_url("agent_new"), json=_token_reply("tok-1"))
    csrf = await _login(client, email, password)

    response = await _start(client, csrf)

    assert response.status_code == 200
    assert await _agent_row(engine) == "agent_new"


async def test_a_vendor_refusal_is_a_502_that_never_quotes_the_key(
    app_client: Any,
    seeded_user: tuple[UUID, str, str],
    call_configured: None,
    httpx_mock: Any,
) -> None:
    """401 quoting the key: 502 with ``<key>`` in its place; no row, no memo, no line."""
    client, app = app_client
    _uid, email, password = seeded_user
    httpx_mock.add_response(
        method="POST",
        url=_TOOLS_URL,
        status_code=401,
        text=f'{{"detail": "invalid api key {_CALL_KEY}"}}',
    )
    csrf = await _login(client, email, password)

    response = await _start(client, csrf)

    assert response.status_code == 502
    assert "HTTP 401" in response.text
    assert "<key>" in response.text
    assert _CALL_KEY not in response.text
    assert await _agent_row(app.state.engine) is None
    assert not getattr(app.state, "call_agent_memo", None)
    assert not getattr(app.state, "chat_call_lines", None)
