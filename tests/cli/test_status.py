# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the ``portfoliflow status`` subcommand.

Two test surfaces are covered:

- **Live-DB tests** that hit the compose Postgres exactly the same way
  the bootstrap and reset-dev tests do not — the live path is the only
  way to assert that the migration-tree, tenant, and user queries all
  read what they should from the real schema. These tests run when
  ``DATABASE_URL_SUPERUSER`` resolves; otherwise they skip cleanly.

- **Pure-Python tests** for the AIService section (env-var driven, no
  DB needed) and the JSON output shape.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from typer.testing import CliRunner

from cli import app
from cli.status import (
    _populate_ai_service_section,
    _StatusReport,
)
from core.tenant_constants import SENTINEL_TENANT_ID
from services.password_hashing import hash_password

runner = CliRunner()

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL_SUPERUSER not set; skipping live-DB status tests.",
            allow_module_level=False,
        )


@pytest_asyncio.fixture
async def superuser_engine_fixture() -> AsyncGenerator[AsyncEngine, None]:
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_then_seed_sentinel(
    superuser_engine_fixture: AsyncEngine,
) -> AsyncGenerator[tuple[str, str], None]:
    """Reset to a known clean state with sentinel tenant + owner.

    Yields ``(email, plaintext_password)`` for the seeded sentinel
    owner. Truncates again on teardown so the dev DB stays clean.
    """
    truncate_sql = text(
        "TRUNCATE TABLE data_upload_sheets, data_uploads, "
        "login_audit, sessions, audit_log, "
        "data_store_entries, users, tenants "
        "RESTART IDENTITY CASCADE"
    )
    email = "status-test@example.com"
    plaintext = "correct-horse-battery-staple"
    async with superuser_engine_fixture.begin() as conn:
        await conn.execute(truncate_sql)
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
                    (id, tenant_id, email, password_hash,
                     roles, is_active)
                VALUES
                    (:id, :tid, :email, :hash, ARRAY['owner']::text[], TRUE)
                """
            ),
            {
                "id": str(uuid4()),
                "tid": str(SENTINEL_TENANT_ID),
                "email": email,
                "hash": hash_password(plaintext),
            },
        )
    try:
        yield (email, plaintext)
    finally:
        async with superuser_engine_fixture.begin() as conn:
            await conn.execute(truncate_sql)
            # Re-bootstrap with the .env sentinel so subsequent manual
            # use of the dev DB is not broken by these tests.
            env_email = os.getenv("SENTINEL_EMAIL", "ops@example.com")
            env_password = os.getenv("SENTINEL_PASSWORD", "dev-only-sentinel-password-change-me")
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital') "
                    "ON CONFLICT DO NOTHING"
                ),
                {
                    "id": str(SENTINEL_TENANT_ID),
                    "name": "Sentinel Tenant",
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO users
                        (tenant_id, email, password_hash,
                         roles, is_active)
                    VALUES
                        (:tid, :email, :hash, ARRAY['owner']::text[], TRUE)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "tid": str(SENTINEL_TENANT_ID),
                    "email": env_email,
                    "hash": hash_password(env_password),
                },
            )


@pytest_asyncio.fixture
async def reset_only(
    superuser_engine_fixture: AsyncEngine,
) -> AsyncGenerator[None, None]:
    """Reset every domain table without seeding — for missing-sentinel tests."""
    truncate_sql = text(
        "TRUNCATE TABLE data_upload_sheets, data_uploads, "
        "login_audit, sessions, audit_log, "
        "data_store_entries, users, tenants "
        "RESTART IDENTITY CASCADE"
    )
    async with superuser_engine_fixture.begin() as conn:
        await conn.execute(truncate_sql)
    try:
        yield
    finally:
        async with superuser_engine_fixture.begin() as conn:
            await conn.execute(truncate_sql)
            env_email = os.getenv("SENTINEL_EMAIL", "ops@example.com")
            env_password = os.getenv("SENTINEL_PASSWORD", "dev-only-sentinel-password-change-me")
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, 'minathena-capital') "
                    "ON CONFLICT DO NOTHING"
                ),
                {"id": str(SENTINEL_TENANT_ID), "name": "Sentinel Tenant"},
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO users
                        (tenant_id, email, password_hash,
                         roles, is_active)
                    VALUES
                        (:tid, :email, :hash, ARRAY['owner']::text[], TRUE)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "tid": str(SENTINEL_TENANT_ID),
                    "email": env_email,
                    "hash": hash_password(env_password),
                },
            )


# ---------------------------------------------------------------------------
# Live-DB integration tests
# ---------------------------------------------------------------------------


def test_status_returns_zero_when_dev_db_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
    reset_then_seed_sentinel: tuple[str, str],
) -> None:
    """A bootstrapped DB plus configured AI service exits 0."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-key")
    monkeypatch.setenv("SHIRLEY_MODEL", "anthropic/claude-haiku-4.5")
    _require_db()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Sentinel exists  yes" in result.output
    assert "Pending Migrations  none" in result.output
    assert "Overall: OK" in result.output


def test_status_returns_one_when_sentinel_missing(
    reset_only: None,
) -> None:
    """A truncated DB (no sentinel) exits 1 with a clear note."""
    _require_db()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1, result.output
    assert "Sentinel exists  no" in result.output
    assert "Sentinel tenant is missing" in result.output
    assert "Overall: ATTENTION REQUIRED" in result.output


def test_status_json_output_is_valid_json(
    monkeypatch: pytest.MonkeyPatch,
    reset_then_seed_sentinel: tuple[str, str],
) -> None:
    """The ``--json`` flag emits a parseable JSON document."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-key")
    monkeypatch.setenv("SHIRLEY_MODEL", "anthropic/claude-haiku-4.5")
    _require_db()
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["healthy"] is True
    assert "database" in parsed
    assert "tenants" in parsed
    assert "users" in parsed
    assert "ai_service" in parsed
    assert "web_application" in parsed
    # Items are serialised as [label, value] pairs.
    db_items = parsed["database"]["items"]
    assert any(item[0] == "Connection" for item in db_items)


# ---------------------------------------------------------------------------
# AIService section — pure unit tests (no DB needed)
# ---------------------------------------------------------------------------


def test_ai_section_skipped_when_no_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--check-ai`` without an API key reports SKIPPED, not OK."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("SHIRLEY_MODEL", "anthropic/claude-haiku-4.5")
    report = _StatusReport()
    _populate_ai_service_section(report, check_ai=True)
    items = dict(report.ai_service.items)
    assert items["API Key configured"] == "no"
    assert items["Reachability"] == "SKIPPED (no API key)"
    assert report.ai_service.ok is False


def test_ai_section_skipped_without_check_ai_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--check-ai`` reachability is always SKIPPED — even with key."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-key")
    monkeypatch.setenv("SHIRLEY_MODEL", "anthropic/claude-haiku-4.5")
    report = _StatusReport()
    _populate_ai_service_section(report, check_ai=False)
    items = dict(report.ai_service.items)
    assert items["API Key configured"] == "yes"
    assert items["Reachability"] == "SKIPPED (use --check-ai)"
    assert report.ai_service.ok is True


def test_ai_section_reachability_ok_with_mocked_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response from the probe sets Reachability OK."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-key")
    monkeypatch.setenv("SHIRLEY_MODEL", "anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    class _FakeClient:
        def get(self, url: str, headers: dict[str, str], timeout: float) -> Any:
            assert "/models" in url
            assert headers["Authorization"] == "Bearer sk-or-fake-key"
            return httpx.Response(200, request=httpx.Request("GET", url))

    report = _StatusReport()
    _populate_ai_service_section(report, check_ai=True, reachability_client=_FakeClient())
    items = dict(report.ai_service.items)
    assert items["Reachability"] == "OK (HTTP 200)"
    assert report.ai_service.ok is True


def test_ai_section_reachability_failure_sets_section_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-2xx response flags the section as failing."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-key")
    monkeypatch.setenv("SHIRLEY_MODEL", "anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    class _FakeClient:
        def get(self, url: str, headers: dict[str, str], timeout: float) -> Any:
            return httpx.Response(401, request=httpx.Request("GET", url))

    report = _StatusReport()
    _populate_ai_service_section(report, check_ai=True, reachability_client=_FakeClient())
    items = dict(report.ai_service.items)
    assert items["Reachability"] == "FAILED (HTTP 401)"
    assert report.ai_service.ok is False
