# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the ``portfoliflow reset-dev`` subcommand.

Mirrors the mock-engine pattern from ``test_bootstrap.py``: a small
fake ``AsyncEngine`` whose ``begin()`` returns a context manager that
records every ``execute`` call. Each test inspects the recorded calls
to assert the expected SQL and re-bootstrap behaviour without touching
a real Postgres instance.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from typer.testing import CliRunner

from cli import app
from core.tenant_constants import SENTINEL_TENANT_ID

_SENTINEL_USER_ID = UUID("00000000-0000-0000-0000-000000000099")

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear bootstrap-relevant env vars so a developer's ``.env`` (auto-
    loaded by python-dotenv via ``cli._db`` at import time) cannot leak
    into these unit tests.

    Without this, a populated ``SUPER_ADMIN_EMAIL`` / ``SUPER_ADMIN_PASSWORD``
    pair triggers an extra super-admin ``INSERT INTO users`` during the
    post-truncate bootstrap, skewing the recorded-call assertions. Each
    test sets exactly the variables it needs via :func:`_set_dev_env`.
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
# Fake engine / connection (copied from test_bootstrap.py — same shape)
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
    def __init__(self, **fields: Any) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


class _FakeConn:
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


def _empty_responder() -> Callable[[str, dict[str, Any]], _FakeResult]:
    """Responder for a fully cleared post-truncate database.

    Every SELECT returns no rows so ``_run_bootstrap`` follows the
    "create from scratch" path. The ``INSERT INTO users ... RETURNING
    id`` introduced in sub-stream 3b returns a synthetic id so the
    seed-installation step (mocked by ``_install_engine``) gets a
    plausible UUID to work with.
    """

    def _respond(sql: str, params: dict[str, Any]) -> _FakeResult:
        if "INSERT INTO users" in sql:
            return _FakeResult([_SENTINEL_USER_ID])
        return _FakeResult([])

    return _respond


def _install_engine(monkeypatch: pytest.MonkeyPatch, engine: _FakeEngine) -> None:
    """Patch the ``superuser_engine`` factory and empty the seed pipeline.

    ``reset-dev`` runs the canonical ``_SEED_STEPS`` pipeline, every step of
    which uses :func:`tenant_context` and therefore a real
    :class:`AsyncEngine`. The fake engine in this file cannot satisfy that,
    so the pipeline is emptied wholesale. It is patched at its single
    definition site in ``cli.bootstrap`` — ``cli.reset_dev`` reads it through
    a module reference, so no step list is mirrored here. End-to-end coverage
    for the seed steps lives in ``test_bootstrap_seeds.py`` and its siblings.
    """
    monkeypatch.setattr("cli.reset_dev.superuser_engine", lambda: engine)
    monkeypatch.setattr("cli.bootstrap._SEED_STEPS", ())


def _set_dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the env vars reset-dev expects in a happy-path run."""
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv(
        "DATABASE_URL_SUPERUSER",
        "postgresql+asyncpg://postgres:pw@localhost/portfoliflow_dev",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reset_dev_without_confirm_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--confirm`` the command refuses and exits non-zero."""
    _set_dev_env(monkeypatch)
    engine = _FakeEngine(_empty_responder())
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["reset-dev"])

    assert result.exit_code != 0
    # Engine must not have been touched.
    assert engine.conn.calls == []


def test_reset_dev_with_confirm_truncates_then_bootstraps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: truncate every domain table, then re-bootstrap."""
    _set_dev_env(monkeypatch)
    engine = _FakeEngine(_empty_responder())
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["reset-dev", "--confirm"])

    assert result.exit_code == 0, result.output

    truncates = [c for c, _ in engine.conn.calls if "TRUNCATE TABLE" in c]
    assert len(truncates) == 1, f"expected exactly one TRUNCATE call, got {len(truncates)}"
    truncate_sql = truncates[0]
    # Domain tables must all be in the TRUNCATE statement.
    for table in (
        "investment_region_weights",
        "region_country_memberships",
        "regions",
        "investment_country_weights",
        "investment_sector_weights",
        "sectors",
        "investment_cashflows",
        "investment_navs",
        "investments",
        "saa_correlations",
        "saa_asset_class_inputs",
        "saa_configurations",
        "asset_classes",
        "data_upload_sheets",
        "data_uploads",
        "login_audit",
        "sessions",
        "audit_log",
        "data_store_entries",
        "users",
        "tenants",
    ):
        assert table in truncate_sql, f"{table} missing from TRUNCATE"
    # alembic_version must NOT be touched.
    assert "alembic_version" not in truncate_sql

    # After truncate, _run_bootstrap re-creates the tenants and owner.
    insert_tenants = [c for c, _ in engine.conn.calls if "INSERT INTO tenants" in c]
    insert_users = [c for c, _ in engine.conn.calls if "INSERT INTO users" in c]
    # Two tenant rows: the idempotent system-tenant upsert
    # (INSERT ... ON CONFLICT) and the primary-tenant INSERT (absent on
    # the freshly-truncated DB). See cli.bootstrap._run_bootstrap.
    assert len(insert_tenants) == 2
    # One user row: the primary owner. The super-admin step is skipped
    # because the isolation fixture clears SUPER_ADMIN_*.
    assert len(insert_users) == 1


def test_reset_dev_refuses_when_url_does_not_target_dev_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production-guard refuses to run against a non-dev database URL."""
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv(
        "DATABASE_URL_SUPERUSER",
        "postgresql+asyncpg://postgres:pw@prod.example.com/portfoliflow_prod",
    )
    engine = _FakeEngine(_empty_responder())
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["reset-dev", "--confirm"])

    assert result.exit_code != 0
    # Engine must not have been touched.
    assert engine.conn.calls == []


def test_reset_dev_bootstrap_uses_sentinel_tenant_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-truncate bootstrap binds to ``SENTINEL_TENANT_ID``."""
    _set_dev_env(monkeypatch)
    engine = _FakeEngine(_empty_responder())
    _install_engine(monkeypatch, engine)

    result = runner.invoke(app, ["reset-dev", "--confirm"])

    assert result.exit_code == 0, result.output
    # The set_config call binds the tenant context to the sentinel ID
    # before any insert. Mirrors the bootstrap-test discipline.
    set_config_calls = [params for sql, params in engine.conn.calls if "set_config" in sql]
    assert any(params.get("tid") == str(SENTINEL_TENANT_ID) for params in set_config_calls), (
        "reset-dev did not bind to the sentinel tenant"
    )
