# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the ``portfoliflow`` CLI bootstrap and set-password subcommands.

The Postgres connection is mocked: a small fake ``AsyncEngine`` whose
``begin()`` returns a context manager that records every ``execute``
call. Each test configures the responses returned by ``execute`` to
simulate the database state before bootstrap runs (sentinel tenant /
user present, missing, drifted, etc.).

The sub-stream 3b seed-installation step that ``bootstrap`` runs
after the user transaction needs a real :class:`AsyncEngine`. These
tests do not exercise it — they monkeypatch
``cli.bootstrap._run_seed_installation`` to a coroutine no-op so the
fake engine doesn't have to implement ``async_sessionmaker``. The
seed path is exercised end-to-end in
``tests/cli/test_bootstrap_seeds.py`` against the live compose DB.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from typer.testing import CliRunner

from cli import app
from cli.bootstrap import (
    _SEED_STEPS,
    _resolve_display_name,
    _resolve_email,
    _resolve_password,
    _run_default_asset_classes_installation,
    _run_unclassified_asset_class_installation,
)
from core.exceptions import ConfigurationError
from core.tenant_constants import SENTINEL_TENANT_ID

_SENTINEL_USER_ID = UUID("00000000-0000-0000-0000-000000000099")

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear bootstrap-relevant env vars so a developer's ``.env`` (auto-
    loaded by python-dotenv) cannot leak into these unit tests.

    Each test sets exactly the variables it needs; everything else
    starts unset, making the OWNER_*/SENTINEL_*/SUPER_ADMIN_* matrix
    deterministic regardless of the local environment.
    """
    for var in (
        "OWNER_EMAIL",
        "OWNER_PASSWORD",
        "OWNER_DISPLAY_NAME",
        "SENTINEL_EMAIL",
        "SENTINEL_PASSWORD",
        "SUPER_ADMIN_EMAIL",
        "SUPER_ADMIN_PASSWORD",
        "SUPER_ADMIN_DISPLAY_NAME",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Fake engine / connection
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        if not self._rows:
            raise RuntimeError("no rows")
        return self._rows[0]


class _Row:
    """Tiny attribute-bag stand-in for a SQLAlchemy row."""

    def __init__(self, **fields: Any) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


class _FakeConn:
    """Records every execute() call and returns scripted responses."""

    def __init__(self, responder: Callable[[str, dict[str, Any]], _FakeResult]) -> None:
        self._responder = responder
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(statement)
        bound = dict(params or {})
        self.calls.append((sql, bound))
        return self._responder(sql, bound)


class _FakeBeginCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _FakeEngine:
    def __init__(self, responder: Callable[[str, dict[str, Any]], _FakeResult]) -> None:
        self.conn = _FakeConn(responder)

    def begin(self) -> _FakeBeginCtx:
        return _FakeBeginCtx(self.conn)

    async def dispose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_engine(monkeypatch: pytest.MonkeyPatch, engine: _FakeEngine) -> None:
    """Patch ``superuser_engine`` and empty the canonical seed pipeline.

    Every seed step goes through :func:`tenant_context`, which requires a
    real :class:`AsyncEngine`. The fake engine cannot satisfy that, so the
    whole ``_SEED_STEPS`` pipeline is emptied wholesale for the bootstrap
    unit tests — no step list is mirrored here, so a newly added seed step
    needs no change to this helper. End-to-end coverage for the seed steps
    lives in ``test_bootstrap_seeds.py`` and its siblings; for the sector
    step in ``test_bootstrap_creates_unclassified_sector.py``.
    """
    monkeypatch.setattr("cli.bootstrap.superuser_engine", lambda: engine)
    monkeypatch.setattr("cli.bootstrap._SEED_STEPS", ())


def _scripted_responder(
    *,
    tenant_present: bool = False,
    tenant_name: str = "Minathena Capital",
    tenant_subdomain: str = "minathena-capital",
    user_present: bool = False,
    user_email: str = "ops@example.com",
    user_roles: tuple[str, ...] = ("owner",),
    user_is_active: bool = True,
) -> Callable[[str, dict[str, Any]], _FakeResult]:
    def _respond(sql: str, params: dict[str, Any]) -> _FakeResult:
        if "set_config" in sql:
            return _FakeResult([])
        if "INSERT INTO users" in sql:
            # The user insert returns the new row's id so the
            # bootstrap can pass it to the seed-installation step.
            return _FakeResult([_SENTINEL_USER_ID])
        if "FROM tenants" in sql:
            # The bootstrap only SELECTs the primary tenant by id;
            # the system-tenant upsert is INSERT ... ON CONFLICT.
            if tenant_present:
                return _FakeResult(
                    [
                        _Row(
                            id=str(SENTINEL_TENANT_ID),
                            name=tenant_name,
                            subdomain=tenant_subdomain,
                        )
                    ]
                )
            return _FakeResult([])
        if "FROM users" in sql:
            if user_present:
                return _FakeResult(
                    [
                        _Row(
                            id=_SENTINEL_USER_ID,
                            email=user_email,
                            roles=list(user_roles),
                            is_active=user_is_active,
                        )
                    ]
                )
            return _FakeResult([])
        # INSERTs / other writes: return empty result.
        return _FakeResult([])

    return _respond


# ---------------------------------------------------------------------------
# bootstrap tests
# ---------------------------------------------------------------------------


def test_bootstrap_creates_tenant_and_user_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "correct-horse-battery-staple")

    engine = _FakeEngine(_scripted_responder(tenant_present=False, user_present=False))
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["bootstrap"])

    assert result.exit_code == 0, result.output
    # Two tenant rows touched per run: the idempotent system-tenant
    # upsert and (when absent) the primary-tenant INSERT.
    insert_tenant = [c for c, _ in engine.conn.calls if "INSERT INTO tenants" in c]
    insert_user = [c for c, _ in engine.conn.calls if "INSERT INTO users" in c]
    assert len(insert_tenant) >= 1
    assert len(insert_user) == 1


def test_bootstrap_no_op_when_both_already_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    # Password not required when user already exists.
    monkeypatch.delenv("SENTINEL_PASSWORD", raising=False)

    engine = _FakeEngine(
        _scripted_responder(
            tenant_present=True,
            tenant_name="Minathena Capital",
            user_present=True,
            user_email="ops@example.com",
        )
    )
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["bootstrap"])

    assert result.exit_code == 0, result.output
    insert_user = [c for c, _ in engine.conn.calls if "INSERT INTO users" in c]
    # Primary-tenant row exists → no user INSERT. The system-tenant
    # upsert still fires (idempotent on conflict).
    assert insert_user == []


def test_bootstrap_exits_non_zero_when_email_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SENTINEL_EMAIL", raising=False)
    monkeypatch.setenv("SENTINEL_PASSWORD", "irrelevant")

    # Engine should not be touched, but install one anyway in case.
    engine = _FakeEngine(_scripted_responder())
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["bootstrap"])

    assert result.exit_code != 0


def test_bootstrap_exits_non_zero_when_user_missing_and_password_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.delenv("SENTINEL_PASSWORD", raising=False)

    engine = _FakeEngine(_scripted_responder(tenant_present=True, user_present=False))
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["bootstrap"])

    assert result.exit_code != 0


def test_bootstrap_exits_non_zero_on_tenant_name_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "correct-horse-battery-staple")

    engine = _FakeEngine(_scripted_responder(tenant_present=True, tenant_name="Wrong Tenant Name"))
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code != 0


def test_bootstrap_exits_non_zero_on_user_attribute_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing user with is_active=FALSE triggers drift detection."""
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")

    engine = _FakeEngine(
        _scripted_responder(
            tenant_present=True,
            user_present=True,
            user_email="ops@example.com",
            user_roles=("owner",),
            user_is_active=False,
        )
    )
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# bootstrap — super-admin creation
# ---------------------------------------------------------------------------


def test_bootstrap_skips_super_admin_when_env_vars_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No SUPER_ADMIN_EMAIL / SUPER_ADMIN_PASSWORD → super-admin skipped."""
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "owner-pw")
    monkeypatch.delenv("SUPER_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("SUPER_ADMIN_PASSWORD", raising=False)

    engine = _FakeEngine(_scripted_responder(tenant_present=False, user_present=False))
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["bootstrap"])

    assert result.exit_code == 0, result.output
    # Exactly one INSERT INTO users — the primary owner. No super-admin
    # insert, no super_admin_audit row.
    insert_user = [c for c, _ in engine.conn.calls if "INSERT INTO users" in c]
    audit_insert = [c for c, _ in engine.conn.calls if "INSERT INTO super_admin_audit" in c]
    assert len(insert_user) == 1
    assert audit_insert == []


def test_bootstrap_creates_super_admin_when_env_vars_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SUPER_ADMIN_EMAIL + SUPER_ADMIN_PASSWORD set → super-admin created."""
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "owner-pw")
    monkeypatch.setenv("SUPER_ADMIN_EMAIL", "admin@example.com")
    # Must satisfy the set-time password policy (>=12 chars, >=2 classes).
    monkeypatch.setenv("SUPER_ADMIN_PASSWORD", "sa-pw-Str0ng-123")

    engine = _FakeEngine(_scripted_responder(tenant_present=False, user_present=False))
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["bootstrap"])

    assert result.exit_code == 0, result.output
    # Two INSERT INTO users — primary owner + super-admin. One audit
    # row written for 'create_super_admin'.
    insert_user = [c for c, _ in engine.conn.calls if "INSERT INTO users" in c]
    audit_insert = [c for c, _ in engine.conn.calls if "INSERT INTO super_admin_audit" in c]
    assert len(insert_user) == 2
    assert len(audit_insert) == 1


def test_bootstrap_raises_on_partial_super_admin_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting only SUPER_ADMIN_EMAIL (not password) is a config error."""
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "owner-pw")
    monkeypatch.setenv("SUPER_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.delenv("SUPER_ADMIN_PASSWORD", raising=False)

    engine = _FakeEngine(_scripted_responder(tenant_present=False, user_present=False))
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code != 0
    # No super-admin user insert should have happened.
    insert_user = [c for c, _ in engine.conn.calls if "INSERT INTO users" in c]
    # Only the primary owner insert (which runs before the partial-config
    # check), or none at all if the txn rolls back. Either way: no
    # super-admin row, no audit row.
    audit_insert = [c for c, _ in engine.conn.calls if "INSERT INTO super_admin_audit" in c]
    assert audit_insert == []
    assert len(insert_user) <= 1


# ---------------------------------------------------------------------------
# bootstrap — owner display_name (ADR-0068)
# ---------------------------------------------------------------------------


def test_bootstrap_owner_insert_includes_display_name_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OWNER_DISPLAY_NAME is bound into the owner INSERT (ADR-0068)."""
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("OWNER_DISPLAY_NAME", "Alex")

    engine = _FakeEngine(_scripted_responder(tenant_present=False, user_present=False))
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code == 0, result.output

    user_inserts = [
        (sql, params) for sql, params in engine.conn.calls if "INSERT INTO users" in sql
    ]
    assert len(user_inserts) == 1
    sql, params = user_inserts[0]
    assert "display_name" in sql
    assert params.get("display_name") == "Alex"


def test_bootstrap_owner_insert_display_name_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without OWNER_DISPLAY_NAME the owner insert binds display_name=None."""
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.delenv("OWNER_DISPLAY_NAME", raising=False)

    engine = _FakeEngine(_scripted_responder(tenant_present=False, user_present=False))
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code == 0, result.output

    user_inserts = [params for sql, params in engine.conn.calls if "INSERT INTO users" in sql]
    assert len(user_inserts) == 1
    assert user_inserts[0].get("display_name") is None


# ---------------------------------------------------------------------------
# set-password tests
# ---------------------------------------------------------------------------


def test_set_password_exits_non_zero_when_user_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "nobody@example.com")

    engine = _FakeEngine(_scripted_responder(user_present=False))
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["set-password", "--password-stdin"], input="newpw-Str0ng-123\n")

    assert result.exit_code != 0


def test_set_password_updates_hash_and_deletes_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful rotation issues UPDATE users + DELETE sessions."""
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")

    engine = _FakeEngine(_scripted_responder(user_present=True, user_email="ops@example.com"))
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["set-password", "--password-stdin"], input="newpw-Str0ng-123\n")

    assert result.exit_code == 0, result.output
    update_users = [c for c, _ in engine.conn.calls if "UPDATE users" in c]
    delete_sessions = [c for c, _ in engine.conn.calls if "DELETE FROM sessions" in c]
    assert len(update_users) == 1
    assert len(delete_sessions) == 1


# ---------------------------------------------------------------------------
# _resolve_email / _resolve_password — OWNER_* with SENTINEL_* legacy alias
# ---------------------------------------------------------------------------


def test_resolve_email_prefers_owner_email_without_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.delenv("SENTINEL_EMAIL", raising=False)

    with caplog.at_level(logging.WARNING, logger="portfoliflow.cli"):
        resolved = _resolve_email(None)

    assert resolved == "owner@example.com"
    assert not any("deprecated" in r.message for r in caplog.records)


def test_resolve_email_falls_back_to_sentinel_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("OWNER_EMAIL", raising=False)
    monkeypatch.setenv("SENTINEL_EMAIL", "legacy@example.com")

    with caplog.at_level(logging.WARNING, logger="portfoliflow.cli"):
        resolved = _resolve_email(None)

    assert resolved == "legacy@example.com"
    assert any("SENTINEL_EMAIL is deprecated" in r.message for r in caplog.records)


def test_resolve_email_cli_flag_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("SENTINEL_EMAIL", "legacy@example.com")

    assert _resolve_email("flag@example.com") == "flag@example.com"


def test_resolve_email_raises_when_all_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OWNER_EMAIL", raising=False)
    monkeypatch.delenv("SENTINEL_EMAIL", raising=False)

    with pytest.raises(ConfigurationError):
        _resolve_email(None)


def test_resolve_password_prefers_owner_password_without_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("OWNER_PASSWORD", "owner-pw")
    monkeypatch.delenv("SENTINEL_PASSWORD", raising=False)

    with caplog.at_level(logging.WARNING, logger="portfoliflow.cli"):
        resolved = _resolve_password(use_stdin=False)

    assert resolved == "owner-pw"
    assert not any("deprecated" in r.message for r in caplog.records)


def test_resolve_password_falls_back_to_sentinel_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("OWNER_PASSWORD", raising=False)
    monkeypatch.setenv("SENTINEL_PASSWORD", "legacy-pw")

    with caplog.at_level(logging.WARNING, logger="portfoliflow.cli"):
        resolved = _resolve_password(use_stdin=False)

    assert resolved == "legacy-pw"
    assert any("SENTINEL_PASSWORD is deprecated" in r.message for r in caplog.records)


def test_resolve_password_returns_none_when_all_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OWNER_PASSWORD", raising=False)
    monkeypatch.delenv("SENTINEL_PASSWORD", raising=False)

    assert _resolve_password(use_stdin=False) is None


# ---------------------------------------------------------------------------
# _resolve_display_name — OWNER_DISPLAY_NAME (ADR-0068)
# ---------------------------------------------------------------------------


def test_resolve_display_name_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_DISPLAY_NAME", "Alex")
    assert _resolve_display_name() == "Alex"


def test_resolve_display_name_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OWNER_DISPLAY_NAME", raising=False)
    assert _resolve_display_name() is None


def test_resolve_display_name_blank_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OWNER_DISPLAY_NAME", "   ")
    assert _resolve_display_name() is None


# ---------------------------------------------------------------------------
# _SEED_STEPS — the canonical seed pipeline
# ---------------------------------------------------------------------------


def test_seed_steps_order_contract() -> None:
    """The unclassified asset class must be seeded before the defaults.

    ``_run_default_asset_classes_installation`` documents that it runs
    after ``_run_unclassified_asset_class_installation``; this test pins
    that ordering in the canonical ``_SEED_STEPS`` pipeline.
    """
    assert len(set(_SEED_STEPS)) == len(_SEED_STEPS)
    assert _SEED_STEPS.index(_run_unclassified_asset_class_installation) < _SEED_STEPS.index(
        _run_default_asset_classes_installation
    )
