# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pairing-code binding and per-dispatcher authorisation (ADR-0112 §5, F5).

Two seams, tested together because they are two halves of one contract:

* :mod:`services.telegram_pairing` — the in-process code store the web app
  issues into and the bot redeems from (D4): single use, five-minute TTL,
  one live code per user, tenant-matched, throttled per chat, and mute
  about *why* a redeem failed.
* :func:`bot.telegram_bot._handle_pair_command` /
  :func:`bot.telegram_bot._authorise` — the bot half (D5): ``/pair`` writes
  the user-scope ``telegram.chat_id`` row in the dispatcher's tenant, a
  paired chat runs the turn **as that user**, an unpaired chat is dropped
  silently, and the deprecated whitelist still admits on the environment
  dispatcher alone.

The database is doubled, not run: the repository call *shape* is what
matters here (which tenant, which user, which row), and the live
round-trip through Postgres is covered by the surface tests in
``tests/web/test_provider_credentials.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from services import telegram_pairing
from services.ai_service_core import AIServiceCore, StreamEvent
from services.ai_models import Message, MessageRole
from services.investments.credential_resolver import ProviderCredential

_BOT_ENV_VARS = (
    "TELEGRAM_BOT_ENABLED",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_IDS",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_API_KEY",
    "SHIRLEY_MODEL",
    "DATABASE_URL",
    "SHIRLEY_BOT_TENANT_SUBDOMAIN",
)

_CHAT_ID = 4242
_TELEGRAM_USER_ID = 12345
_TENANT_A = UUID("aaaaaaaa-1111-1111-1111-111111111111")
_TENANT_B = UUID("bbbbbbbb-2222-2222-2222-222222222222")
_USER_A = UUID("11111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_USER_B = UUID("22222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(autouse=True)
def reset_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Clear bot env vars, module handles and the pairing store per test."""
    for var in _BOT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    import bot.config
    import bot.telegram_bot as tb

    def _reset() -> None:
        bot.config._instance = None
        tb._bot_thread = None
        tb._bot_loop = None
        tb._bot_core = None
        tb._bot_engine = None
        tb._bot_tenant_id = None
        tb._bot_database_url = ""
        tb._bot_superuser_url = ""
        tb._whitelist_deprecation_warned = False
        tb._chat_histories.clear()
        telegram_pairing.reset_store()

    _reset()
    yield
    _reset()


@pytest.fixture
def bot_config(monkeypatch: pytest.MonkeyPatch) -> Any:
    """An enabled configuration whose whitelist carries the test user."""
    monkeypatch.setenv("TELEGRAM_BOT_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", str(_TELEGRAM_USER_ID))
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x/y")

    from bot.config import BotSettings

    return BotSettings()


@pytest.fixture
def aiobot_mock() -> MagicMock:
    aiobot = MagicMock()
    aiobot.send_chat_action = AsyncMock()
    aiobot.send_message = AsyncMock()
    aiobot.send_photo = AsyncMock()
    aiobot.delete_message = AsyncMock()
    return aiobot


def _binding(tenant_id: UUID | None, source: str = "vault") -> Any:
    import bot.telegram_bot as tb

    return tb._BotBinding(
        tenant_id=tenant_id,
        source=source,
        label=tb._binding_label(tenant_id, source),
    )


def _message(text: str, *, chat_id: int = _CHAT_ID) -> Any:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=_TELEGRAM_USER_ID),
        chat=SimpleNamespace(id=chat_id),
        photo=None,
        document=None,
        caption=None,
    )


# ---------------------------------------------------------------------------
# Database doubles
# ---------------------------------------------------------------------------


class _NullAsyncContext:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeDatabase:
    """Records what the handlers ask the repositories for, and answers.

    Stands in for ``tenant_context`` plus the two repositories the pairing
    path uses. One instance per test, installed over the three names
    :mod:`bot.telegram_bot` imported.
    """

    def __init__(self, *, users: dict[UUID, list[UUID]], bindings: dict[UUID, str]) -> None:
        #: tenant id → the users RLS would show inside that tenant.
        self.users = users
        #: user id → the chat id stored in that user's ``telegram.chat_id`` row.
        self.bindings = bindings
        self.contexts: list[dict[str, Any]] = []
        self.upserts: list[dict[str, Any]] = []
        self._active_tenant: UUID | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import bot.telegram_bot as tb

        monkeypatch.setattr(tb, "tenant_context", self._tenant_context)
        monkeypatch.setattr(tb, "UserRepository", self._user_repository)
        monkeypatch.setattr(tb, "ScopedSettingRepository", self._settings_repository)
        # Any non-None object will do: every use of the engine is doubled.
        tb._bot_engine = SimpleNamespace(name="fake-engine")

    def _tenant_context(
        self, engine: Any, tenant_id: UUID, user_id: UUID | None = None, **kwargs: Any
    ) -> _NullAsyncContext:
        self.contexts.append({"tenant_id": tenant_id, "user_id": user_id})
        self._active_tenant = tenant_id
        return _NullAsyncContext(SimpleNamespace(name="fake-session"))

    def _user_repository(self, session: Any) -> Any:
        tenant_id = self._active_tenant
        users = [SimpleNamespace(id=uid) for uid in self.users.get(tenant_id, [])]

        async def _list_all() -> list[Any]:
            return users

        return SimpleNamespace(list_all=_list_all)

    def _settings_repository(self, session: Any) -> Any:
        database = self

        class _Repository:
            async def get(
                self, scope: str, provider: str, key: str, user_id: UUID | None = None
            ) -> Any:
                value = database.bindings.get(user_id) if user_id is not None else None
                if value is None:
                    return None
                return SimpleNamespace(enabled=True, is_secret=False, value_plain=value, id=uuid4())

            async def upsert(self, **kwargs: Any) -> Any:
                database.upserts.append({**kwargs, "tenant_id": database._active_tenant})
                database.bindings[kwargs["user_id"]] = kwargs["value_plain"]
                return SimpleNamespace(id=uuid4())

        return _Repository()


class _FakeResolver:
    """Records the identity every resolution was made for."""

    def __init__(self) -> None:
        self.resolve_calls: list[dict[str, Any]] = []
        self.config_calls: list[dict[str, Any]] = []

    async def resolve(self, provider: str, **kwargs: Any) -> Any:
        self.resolve_calls.append({"provider": provider, **kwargs})
        return ProviderCredential(provider=provider, payload={"api_key": "sk-test"})

    async def resolve_config(
        self, provider: str, key: str, *, user_id: Any = None, scopes: Any = None
    ) -> str | None:
        self.config_calls.append({"key": key, "user_id": user_id})
        return "test/model" if key == "model" else None


def _recording_stream() -> Any:
    calls: list[Any] = []

    async def fake_stream(
        self: AIServiceCore,
        conversation: Any,
        system_prompt: str = "",
        temperature: float = 0.7,
        tool_context: Any = None,
        llm: Any = None,
    ):
        calls.append({"conversation": conversation, "llm": llm})
        final = Message(role=MessageRole.ASSISTANT, content="reply")
        yield StreamEvent("chunk", {"text": "reply"})
        yield StreamEvent("stream_finished", {"message": final, "iterations": 0})

    fake_stream.calls = calls  # type: ignore[attr-defined]
    return fake_stream


# ---------------------------------------------------------------------------
# The pairing store (D4)
# ---------------------------------------------------------------------------


def test_a_code_is_redeemed_once() -> None:
    issued = telegram_pairing.issue_code(_TENANT_A, _USER_A)

    assert telegram_pairing.redeem_code(issued.code, tenant_id=_TENANT_A) == _USER_A
    assert telegram_pairing.redeem_code(issued.code, tenant_id=_TENANT_A) is None


def test_a_code_is_normalised_on_redeem() -> None:
    """Users retype codes off a screen: case, spaces and dashes are noise."""
    issued = telegram_pairing.issue_code(_TENANT_A, _USER_A)
    typed = f" {issued.code[:4].lower()}-{issued.code[4:].lower()} "

    assert telegram_pairing.redeem_code(typed, tenant_id=_TENANT_A) == _USER_A


def test_an_expired_code_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    issued = telegram_pairing.issue_code(_TENANT_A, _USER_A)
    later = datetime.now(timezone.utc) + telegram_pairing.CODE_TTL + timedelta(seconds=1)
    monkeypatch.setattr(telegram_pairing, "_now", lambda: later)

    assert telegram_pairing.redeem_code(issued.code, tenant_id=_TENANT_A) is None


def test_a_code_cannot_cross_tenants() -> None:
    """A code minted in one tenant must never bind a chat in another."""
    issued = telegram_pairing.issue_code(_TENANT_A, _USER_A)

    assert telegram_pairing.redeem_code(issued.code, tenant_id=_TENANT_B) is None
    # And it is still usable in its own tenant — a wrong-tenant attempt is
    # not a consumption.
    assert telegram_pairing.redeem_code(issued.code, tenant_id=_TENANT_A) == _USER_A


def test_a_code_with_no_dispatcher_tenant_is_refused() -> None:
    """The desktop dispatcher has no tenant, so it can bind nothing."""
    issued = telegram_pairing.issue_code(_TENANT_A, _USER_A)

    assert telegram_pairing.redeem_code(issued.code, tenant_id=None) is None


def test_reissuing_invalidates_the_previous_code() -> None:
    """One live code per user: a second click must not leave two open doors."""
    first = telegram_pairing.issue_code(_TENANT_A, _USER_A)
    second = telegram_pairing.issue_code(_TENANT_A, _USER_A)

    assert telegram_pairing.redeem_code(first.code, tenant_id=_TENANT_A) is None
    assert telegram_pairing.redeem_code(second.code, tenant_id=_TENANT_A) == _USER_A


def test_reissuing_does_not_touch_another_users_code() -> None:
    other = telegram_pairing.issue_code(_TENANT_A, _USER_B)
    telegram_pairing.issue_code(_TENANT_A, _USER_A)

    assert telegram_pairing.redeem_code(other.code, tenant_id=_TENANT_A) == _USER_B


def test_revoking_drops_the_users_pending_codes() -> None:
    """A revoke must not leave an outstanding code able to re-bind the chat."""
    issued = telegram_pairing.issue_code(_TENANT_A, _USER_A)

    assert telegram_pairing.revoke_codes_for_user(_USER_A) == 1
    assert telegram_pairing.redeem_code(issued.code, tenant_id=_TENANT_A) is None


def test_attempts_are_throttled_per_chat() -> None:
    for _ in range(telegram_pairing.THROTTLE_MAX_ATTEMPTS):
        assert telegram_pairing.note_attempt(_CHAT_ID) is True
    assert telegram_pairing.note_attempt(_CHAT_ID) is False
    # A different chat has its own budget.
    assert telegram_pairing.note_attempt(_CHAT_ID + 1) is True


def test_the_throttle_window_slides(monkeypatch: pytest.MonkeyPatch) -> None:
    for _ in range(telegram_pairing.THROTTLE_MAX_ATTEMPTS):
        telegram_pairing.note_attempt(_CHAT_ID)
    assert telegram_pairing.note_attempt(_CHAT_ID) is False

    later = datetime.now(timezone.utc) + telegram_pairing.THROTTLE_WINDOW + timedelta(seconds=1)
    monkeypatch.setattr(telegram_pairing, "_now", lambda: later)

    assert telegram_pairing.note_attempt(_CHAT_ID) is True


def test_a_pending_code_never_renders_in_a_repr() -> None:
    telegram_pairing.issue_code(_TENANT_A, _USER_A)
    pending = next(iter(telegram_pairing._PENDING.values()))

    assert "code" not in repr(pending).lower()
    assert str(_USER_A) in repr(pending)


# ---------------------------------------------------------------------------
# /pair — the bot half (D5)
# ---------------------------------------------------------------------------


async def test_pair_writes_the_chat_binding_in_the_dispatchers_tenant(
    bot_config: Any,
    aiobot_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bot.telegram_bot as tb

    database = _FakeDatabase(users={_TENANT_A: [_USER_A]}, bindings={})
    database.install(monkeypatch)
    issued = telegram_pairing.issue_code(_TENANT_A, _USER_A)

    await tb._handle_pair_command(
        aiobot_mock,
        _message(f"/pair {issued.code}"),
        bot_config,
        binding=_binding(_TENANT_A),
    )

    assert database.upserts == [
        {
            "scope": "user",
            "provider": "telegram",
            "key": "chat_id",
            "is_secret": False,
            "value_plain": str(_CHAT_ID),
            "user_id": _USER_A,
            "tenant_id": _TENANT_A,
        }
    ]
    # Written inside the dispatcher's tenant, as the paired user.
    assert database.contexts[-1] == {"tenant_id": _TENANT_A, "user_id": _USER_A}
    sent = [call.kwargs["text"] for call in aiobot_mock.send_message.call_args_list]
    assert len(sent) == 1
    assert "now linked" in sent[0]


async def test_a_foreign_tenants_code_is_refused_without_an_oracle(
    bot_config: Any,
    aiobot_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reply must not reveal that the code exists — only in another tenant."""
    import bot.telegram_bot as tb

    database = _FakeDatabase(users={_TENANT_B: [_USER_B]}, bindings={})
    database.install(monkeypatch)
    issued = telegram_pairing.issue_code(_TENANT_A, _USER_A)

    await tb._handle_pair_command(
        aiobot_mock,
        _message(f"/pair {issued.code}"),
        bot_config,
        binding=_binding(_TENANT_B),
    )

    assert database.upserts == []
    sent = [call.kwargs["text"] for call in aiobot_mock.send_message.call_args_list]
    assert sent == [tb._PAIR_FAILED_MESSAGE]


async def test_an_unknown_code_gets_the_same_reply(
    bot_config: Any,
    aiobot_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bot.telegram_bot as tb

    _FakeDatabase(users={}, bindings={}).install(monkeypatch)

    await tb._handle_pair_command(
        aiobot_mock,
        _message("/pair ZZZZZZZZ"),
        bot_config,
        binding=_binding(_TENANT_A),
    )

    sent = [call.kwargs["text"] for call in aiobot_mock.send_message.call_args_list]
    assert sent == [tb._PAIR_FAILED_MESSAGE]


async def test_pair_without_a_code_explains_the_usage(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bot.telegram_bot as tb

    _FakeDatabase(users={}, bindings={}).install(monkeypatch)

    await tb._handle_pair_command(
        aiobot_mock, _message("/pair"), bot_config, binding=_binding(_TENANT_A)
    )

    sent = [call.kwargs["text"] for call in aiobot_mock.send_message.call_args_list]
    assert sent == [tb._PAIR_USAGE_MESSAGE]


async def test_pair_attempts_are_throttled(
    bot_config: Any, aiobot_mock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guessing is bounded in practice, not only in theory."""
    import bot.telegram_bot as tb

    database = _FakeDatabase(users={_TENANT_A: [_USER_A]}, bindings={})
    database.install(monkeypatch)
    binding = _binding(_TENANT_A)

    for _ in range(telegram_pairing.THROTTLE_MAX_ATTEMPTS):
        await tb._handle_pair_command(
            aiobot_mock, _message("/pair WRONGONE"), bot_config, binding=binding
        )

    # The next attempt is refused before the code is even looked at — so a
    # *valid* code presented now does not bind either.
    issued = telegram_pairing.issue_code(_TENANT_A, _USER_A)
    await tb._handle_pair_command(
        aiobot_mock, _message(f"/pair {issued.code}"), bot_config, binding=binding
    )

    assert database.upserts == []
    sent = [call.kwargs["text"] for call in aiobot_mock.send_message.call_args_list]
    assert sent[-1] == tb._PAIR_FAILED_MESSAGE
    # The code survived the throttled attempt — it was never consumed.
    assert telegram_pairing.redeem_code(issued.code, tenant_id=_TENANT_A) == _USER_A


async def test_the_code_never_reaches_a_log_line(
    bot_config: Any,
    aiobot_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """For five minutes the code is a bearer token for the account."""
    import bot.telegram_bot as tb

    database = _FakeDatabase(users={_TENANT_A: [_USER_A]}, bindings={})
    database.install(monkeypatch)
    issued = telegram_pairing.issue_code(_TENANT_A, _USER_A)

    with caplog.at_level(logging.DEBUG):
        await tb._handle_pair_command(
            aiobot_mock,
            _message(f"/pair {issued.code}"),
            bot_config,
            binding=_binding(_TENANT_A),
        )
        # …and a failing attempt logs nothing either.
        await tb._handle_pair_command(
            aiobot_mock,
            _message("/pair OTHERCOD"),
            bot_config,
            binding=_binding(_TENANT_A),
        )

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert issued.code not in logged
    assert "OTHERCOD" not in logged


# ---------------------------------------------------------------------------
# Authorisation (D5)
# ---------------------------------------------------------------------------


async def test_a_paired_chat_runs_the_turn_as_the_paired_user(
    bot_config: Any,
    aiobot_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completion of §4b: user-scope rows now apply to a bot turn."""
    import bot.telegram_bot as tb

    database = _FakeDatabase(
        users={_TENANT_A: [_USER_B, _USER_A]},
        bindings={_USER_A: str(_CHAT_ID)},
    )
    database.install(monkeypatch)
    resolver = _FakeResolver()
    monkeypatch.setattr(tb, "CredentialResolver", lambda **kwargs: resolver)
    stream = _recording_stream()

    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_text_message(
            aiobot_mock, _message("hallo"), bot_config, binding=_binding(_TENANT_A)
        )

    assert len(stream.calls) == 1
    assert resolver.resolve_calls == [
        {"provider": "openrouter", "tenant_id": _TENANT_A, "user_id": _USER_A}
    ]
    # The model lookup carries the same identity; the base URL is a tenant
    # setting and deliberately does not.
    assert {"key": "model", "user_id": _USER_A} in resolver.config_calls
    assert {"key": "base_url", "user_id": None} in resolver.config_calls


async def test_an_unpaired_chat_is_dropped_silently_on_a_vault_dispatcher(
    bot_config: Any,
    aiobot_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant that stores its own token is pairing-only — whitelist or not."""
    import bot.telegram_bot as tb

    database = _FakeDatabase(users={_TENANT_A: [_USER_A]}, bindings={})
    database.install(monkeypatch)
    stream = _recording_stream()

    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_text_message(
            aiobot_mock, _message("hallo"), bot_config, binding=_binding(_TENANT_A)
        )

    assert stream.calls == []
    aiobot_mock.send_message.assert_not_awaited()


async def test_a_binding_from_another_tenant_does_not_authorise(
    bot_config: Any,
    aiobot_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lookup is tenant-scoped: tenant B's dispatcher cannot see A's row."""
    import bot.telegram_bot as tb

    database = _FakeDatabase(
        users={_TENANT_A: [_USER_A], _TENANT_B: []},
        bindings={_USER_A: str(_CHAT_ID)},
    )
    database.install(monkeypatch)
    stream = _recording_stream()

    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_text_message(
            aiobot_mock, _message("hallo"), bot_config, binding=_binding(_TENANT_B)
        )

    assert stream.calls == []


async def test_the_whitelist_still_admits_on_the_env_dispatcher(
    bot_config: Any,
    aiobot_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The deprecated fallback works, and says loudly that it is deprecated."""
    import bot.telegram_bot as tb

    database = _FakeDatabase(users={_TENANT_A: [_USER_A]}, bindings={})
    database.install(monkeypatch)
    resolver = _FakeResolver()
    monkeypatch.setattr(tb, "CredentialResolver", lambda **kwargs: resolver)
    stream = _recording_stream()

    with (
        caplog.at_level(logging.WARNING, logger="bot.telegram_bot"),
        patch.object(AIServiceCore, "stream_response", stream),
    ):
        await tb._handle_text_message(
            aiobot_mock,
            _message("hallo"),
            bot_config,
            binding=_binding(_TENANT_A, source="env-fallback"),
        )

    assert len(stream.calls) == 1
    # Admitted, but with no user identity: it authorises a Telegram account,
    # not a PortfoliFLOW user.
    assert resolver.resolve_calls == [
        {"provider": "openrouter", "tenant_id": _TENANT_A, "user_id": None}
    ]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("TELEGRAM_ALLOWED_USER_IDS" in w and "deprecated" in w for w in warnings)


async def test_a_lookup_failure_fails_closed(
    bot_config: Any,
    aiobot_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database outage must drop the message, never admit it."""
    import bot.telegram_bot as tb

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database is down")

    monkeypatch.setattr(tb, "tenant_context", _explode)
    tb._bot_engine = SimpleNamespace(name="fake-engine")
    stream = _recording_stream()

    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_text_message(
            aiobot_mock, _message("hallo"), bot_config, binding=_binding(_TENANT_A)
        )

    assert stream.calls == []


# ---------------------------------------------------------------------------
# Per-tenant state keying (D6)
# ---------------------------------------------------------------------------


async def test_two_tenants_keep_separate_histories_for_one_chat_id(
    bot_config: Any,
    aiobot_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A private chat id is the same for every bot — the tenant is the key.

    Without the tenant in the key, tenant B's turn would replay tenant A's
    conversation: a cross-tenant leak through a dict.
    """
    import bot.telegram_bot as tb

    database = _FakeDatabase(
        users={_TENANT_A: [_USER_A], _TENANT_B: [_USER_B]},
        bindings={_USER_A: str(_CHAT_ID), _USER_B: str(_CHAT_ID)},
    )
    database.install(monkeypatch)
    monkeypatch.setattr(tb, "CredentialResolver", lambda **kwargs: _FakeResolver())
    stream = _recording_stream()

    with patch.object(AIServiceCore, "stream_response", stream):
        await tb._handle_text_message(
            aiobot_mock, _message("nur für A"), bot_config, binding=_binding(_TENANT_A)
        )
        await tb._handle_text_message(
            aiobot_mock, _message("nur für B"), bot_config, binding=_binding(_TENANT_B)
        )

    assert set(tb._chat_histories) == {(_TENANT_A, _CHAT_ID), (_TENANT_B, _CHAT_ID)}
    assert [m.content for m in tb._chat_histories[(_TENANT_A, _CHAT_ID)]] == [
        "nur für A",
        "reply",
    ]
    # Tenant B's turn saw only its own history — one user message, not two.
    second_conversation = stream.calls[1]["conversation"]
    assert [m.content for m in second_conversation.messages] == ["nur für B"]


async def test_reset_clears_only_the_callers_tenant_entry(
    bot_config: Any,
    aiobot_mock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bot.telegram_bot as tb

    database = _FakeDatabase(
        users={_TENANT_A: [_USER_A], _TENANT_B: [_USER_B]},
        bindings={_USER_A: str(_CHAT_ID), _USER_B: str(_CHAT_ID)},
    )
    database.install(monkeypatch)
    monkeypatch.setattr(tb, "CredentialResolver", lambda **kwargs: _FakeResolver())

    with patch.object(AIServiceCore, "stream_response", _recording_stream()):
        await tb._handle_text_message(
            aiobot_mock, _message("für A"), bot_config, binding=_binding(_TENANT_A)
        )
        await tb._handle_text_message(
            aiobot_mock, _message("für B"), bot_config, binding=_binding(_TENANT_B)
        )
        await tb._handle_text_message(
            aiobot_mock, _message("/reset"), bot_config, binding=_binding(_TENANT_A)
        )

    assert (_TENANT_A, _CHAT_ID) not in tb._chat_histories
    assert (_TENANT_B, _CHAT_ID) in tb._chat_histories
