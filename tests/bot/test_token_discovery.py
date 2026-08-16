# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Bot-token discovery across tenants (ADR-0112 §5, strand F5).

:func:`bot.token_discovery.discover_bot_tokens` decides the dispatcher
set: which tenants get a bot, and which token each one polls with. It is a
plain async function over an engine plus two settings values, so these
tests drive it with a fake engine and a real Fernet key — no aiogram, no
database, no network.

Five groups:

* **Opt-out semantics** — an absent ``enabled`` row means enabled, a
  ``"false"`` one means skip, and a disabled ``bot_token`` row is a
  suspended credential.
* **Failure isolation** — a row that will not decrypt skips *that* tenant
  with an ERROR naming the row and the tenant, and never the value; the
  others start regardless.
* **Environment fallback** — additive, and it stands down for a tenant
  that stores its own token (scope precedence, ADR-0112 §1).
* **Vault unconfigured** — nothing decrypts, one WARNING, the environment
  can still serve.
* **Log hygiene** — no token value reaches a log line or a ``repr``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet

from bot.token_discovery import (
    SOURCE_ENV_FALLBACK,
    SOURCE_VAULT,
    DiscoveredBot,
    discover_bot_tokens,
)
from services.credential_vault import MASTER_KEY_ENV_VAR, VaultCipher

_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def master_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Seat a real, per-test Fernet key so the vault is configured."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, key)
    return key


@pytest.fixture
def no_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)


class _FakeConnection:
    """Answers the one SELECT discovery makes."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.statements: list[str] = []

    async def execute(self, statement: Any, params: Any = None) -> list[Any]:
        self.statements.append(str(statement))
        return self._rows

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeEngine:
    """Minimal stand-in for the superuser :class:`AsyncEngine`."""

    def __init__(self, rows: list[Any]) -> None:
        self.connection = _FakeConnection(rows)

    def connect(self) -> _FakeConnection:
        return self.connection


def _token_row(
    tenant_id: UUID,
    ciphertext: bytes | None,
    *,
    enabled: bool = True,
    row_id: UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=row_id or uuid4(),
        tenant_id=tenant_id,
        key="bot_token",
        value_ciphertext=ciphertext,
        value_plain=None,
        enabled=enabled,
    )


def _enabled_row(tenant_id: UUID, value: str, *, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        key="enabled",
        value_ciphertext=None,
        value_plain=value,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# Opt-out semantics
# ---------------------------------------------------------------------------


async def test_a_stored_token_without_an_enabled_row_starts(master_key: str) -> None:
    """An absent ``enabled`` row means enabled — opt-out, not opt-in."""
    cipher = VaultCipher(master_key)
    engine = _FakeEngine([_token_row(_TENANT_A, cipher.encrypt("token-a"))])

    discovered = await discover_bot_tokens(engine)  # type: ignore[arg-type]

    assert discovered == [DiscoveredBot(tenant_id=_TENANT_A, token="token-a", source=SOURCE_VAULT)]


async def test_an_explicit_true_enabled_row_starts(master_key: str) -> None:
    cipher = VaultCipher(master_key)
    engine = _FakeEngine(
        [
            _token_row(_TENANT_A, cipher.encrypt("token-a")),
            _enabled_row(_TENANT_A, "true"),
        ]
    )

    discovered = await discover_bot_tokens(engine)  # type: ignore[arg-type]

    assert [bot.tenant_id for bot in discovered] == [_TENANT_A]


async def test_enabled_false_skips_the_tenant_with_an_info(
    master_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    """``enabled = "false"`` is the per-tenant off switch, and it is visible."""
    cipher = VaultCipher(master_key)
    engine = _FakeEngine(
        [
            _token_row(_TENANT_A, cipher.encrypt("token-a")),
            _enabled_row(_TENANT_A, "false"),
        ]
    )

    with caplog.at_level(logging.INFO, logger="bot.token_discovery"):
        discovered = await discover_bot_tokens(engine)  # type: ignore[arg-type]

    assert discovered == []
    assert any("enabled is false" in r.getMessage() for r in caplog.records)


async def test_a_disabled_token_row_is_a_suspended_credential(master_key: str) -> None:
    """The row's own ``enabled`` column suspends it, exactly as the resolver reads it."""
    cipher = VaultCipher(master_key)
    engine = _FakeEngine([_token_row(_TENANT_A, cipher.encrypt("token-a"), enabled=False)])

    assert await discover_bot_tokens(engine) == []  # type: ignore[arg-type]


async def test_a_disabled_enabled_row_does_not_switch_the_bot_off(master_key: str) -> None:
    """A *disabled* ``enabled`` row is an absent one — the switch itself is off."""
    cipher = VaultCipher(master_key)
    engine = _FakeEngine(
        [
            _token_row(_TENANT_A, cipher.encrypt("token-a")),
            _enabled_row(_TENANT_A, "false", enabled=False),
        ]
    )

    assert [bot.tenant_id for bot in await discover_bot_tokens(engine)] == [  # type: ignore[arg-type]
        _TENANT_A
    ]


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


async def test_a_decrypt_failure_skips_only_that_tenant(
    master_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    """One mis-rotated row must not take the other tenants' bots down."""
    cipher = VaultCipher(master_key)
    bad_row_id = uuid4()
    foreign = VaultCipher(Fernet.generate_key().decode()).encrypt("token-a")
    engine = _FakeEngine(
        [
            _token_row(_TENANT_A, foreign, row_id=bad_row_id),
            _token_row(_TENANT_B, cipher.encrypt("token-b")),
        ]
    )

    with caplog.at_level(logging.ERROR, logger="bot.token_discovery"):
        discovered = await discover_bot_tokens(engine)  # type: ignore[arg-type]

    assert discovered == [DiscoveredBot(tenant_id=_TENANT_B, token="token-b", source=SOURCE_VAULT)]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert str(bad_row_id) in errors[0]
    assert str(_TENANT_A) in errors[0]
    assert "token-a" not in errors[0]


async def test_an_empty_decrypted_token_is_refused(
    master_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A blank token would be a dispatcher that can never poll."""
    cipher = VaultCipher(master_key)
    engine = _FakeEngine([_token_row(_TENANT_A, cipher.encrypt("   "))])

    with caplog.at_level(logging.ERROR, logger="bot.token_discovery"):
        assert await discover_bot_tokens(engine) == []  # type: ignore[arg-type]

    assert any("empty value" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# The environment fallback is additive
# ---------------------------------------------------------------------------


async def test_the_env_token_adds_a_dispatcher(master_key: str) -> None:
    """Additive, not modal: the stored bot and the env bot both run (D3)."""
    cipher = VaultCipher(master_key)
    engine = _FakeEngine([_token_row(_TENANT_A, cipher.encrypt("token-a"))])

    discovered = await discover_bot_tokens(  # type: ignore[arg-type]
        engine,
        env_token="env-token",
        env_tenant_id=_TENANT_B,
    )

    assert discovered == [
        DiscoveredBot(tenant_id=_TENANT_A, token="token-a", source=SOURCE_VAULT),
        DiscoveredBot(tenant_id=_TENANT_B, token="env-token", source=SOURCE_ENV_FALLBACK),
    ]


async def test_a_tenants_own_token_outranks_the_env_token(
    master_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Scope precedence (ADR-0112 §1): the stored row wins, and says so."""
    cipher = VaultCipher(master_key)
    engine = _FakeEngine([_token_row(_TENANT_A, cipher.encrypt("token-a"))])

    with caplog.at_level(logging.INFO, logger="bot.token_discovery"):
        discovered = await discover_bot_tokens(  # type: ignore[arg-type]
            engine,
            env_token="env-token",
            env_tenant_id=_TENANT_A,
        )

    assert discovered == [DiscoveredBot(tenant_id=_TENANT_A, token="token-a", source=SOURCE_VAULT)]
    assert any("its own bot token" in r.getMessage() for r in caplog.records)


async def test_a_disabled_tenant_falls_back_to_the_env_token(master_key: str) -> None:
    """A tenant that switched its own bot off is not "carrying a token"."""
    cipher = VaultCipher(master_key)
    engine = _FakeEngine(
        [
            _token_row(_TENANT_A, cipher.encrypt("token-a")),
            _enabled_row(_TENANT_A, "false"),
        ]
    )

    discovered = await discover_bot_tokens(  # type: ignore[arg-type]
        engine,
        env_token="env-token",
        env_tenant_id=_TENANT_A,
    )

    assert discovered == [
        DiscoveredBot(tenant_id=_TENANT_A, token="env-token", source=SOURCE_ENV_FALLBACK)
    ]


async def test_no_rows_and_no_env_token_discovers_nothing(master_key: str) -> None:
    assert await discover_bot_tokens(_FakeEngine([])) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Vault unconfigured
# ---------------------------------------------------------------------------


async def test_without_a_master_key_stored_tokens_cannot_serve(
    no_master_key: None, caplog: pytest.LogCaptureFixture
) -> None:
    """No key, no decrypt — one WARNING, and the environment still serves."""
    engine = _FakeEngine([_token_row(_TENANT_A, b"whatever")])

    with caplog.at_level(logging.WARNING, logger="bot.token_discovery"):
        discovered = await discover_bot_tokens(  # type: ignore[arg-type]
            engine,
            env_token="env-token",
            env_tenant_id=_TENANT_B,
        )

    assert discovered == [
        DiscoveredBot(tenant_id=_TENANT_B, token="env-token", source=SOURCE_ENV_FALLBACK)
    ]
    assert any(MASTER_KEY_ENV_VAR in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Log hygiene
# ---------------------------------------------------------------------------


def test_the_discovered_bot_never_renders_its_token() -> None:
    """``repr`` is what a log line, an f-string and a traceback all reach for."""
    bot = DiscoveredBot(tenant_id=_TENANT_A, token="super-secret-token", source=SOURCE_VAULT)

    rendered = f"{bot!r} {bot} {[bot]}"

    assert "super-secret-token" not in rendered
    assert "masked" in rendered
    assert str(_TENANT_A) in rendered


async def test_the_scan_reads_scoped_settings_only(master_key: str) -> None:
    """The engine bypasses RLS, so the statement it runs is worth pinning."""
    cipher = VaultCipher(master_key)
    engine = _FakeEngine([_token_row(_TENANT_A, cipher.encrypt("token-a"))])

    await discover_bot_tokens(engine)  # type: ignore[arg-type]

    assert len(engine.connection.statements) == 1
    statement = engine.connection.statements[0]
    assert "FROM scoped_settings" in statement
    assert "scope = 'tenant'" in statement
