# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""End-to-end test for the super-admin bootstrap step.

Mirrors :mod:`test_bootstrap_seeds` — synchronous tests that drive
the CLI via :class:`CliRunner` and verify state against the live
compose Postgres.

Coverage:

* First ``bootstrap`` with ``SUPER_ADMIN_EMAIL`` / ``SUPER_ADMIN_PASSWORD``
  set creates exactly one super-admin and one ``create_super_admin``
  audit row.
* Re-running ``bootstrap`` is idempotent on the super-admin row —
  no duplicate user, no duplicate audit row.
* Without the env vars, ``bootstrap`` finishes cleanly and no
  super-admin is created.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from cli import app
from core.tenant_constants import SYSTEM_TENANT_ID

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

runner = CliRunner()

_TRUNCATE_SQL = (
    "TRUNCATE TABLE investment_region_weights, "
    "region_country_memberships, regions, "
    "investment_country_weights, "
    "investment_sector_weights, sectors, "
    "investment_cashflows, investment_navs, investments, "
    "saa_correlations, saa_asset_class_inputs, "
    "saa_configurations, asset_classes, "
    "data_upload_sheets, data_uploads, "
    "super_admin_audit, "
    "login_audit, sessions, audit_log, "
    "data_store_entries, users, tenants "
    "RESTART IDENTITY CASCADE"
)


def _require_db() -> None:
    if not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL_SUPERUSER not set; cannot run live-DB tests.",
            allow_module_level=False,
        )


async def _truncate_async() -> None:
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_TRUNCATE_SQL))
    finally:
        await engine.dispose()


def _truncate() -> None:
    asyncio.run(_truncate_async())


async def _count_super_admins_async() -> int:
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT COUNT(*) FROM users WHERE tenant_id = :sys AND is_super_admin = TRUE"),
                {"sys": str(SYSTEM_TENANT_ID)},
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


def _count_super_admins() -> int:
    return asyncio.run(_count_super_admins_async())


async def _count_super_admin_audit_async(action: str) -> int:
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT COUNT(*) FROM super_admin_audit WHERE action = :action"),
                {"action": action},
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


def _count_super_admin_audit(action: str) -> int:
    return asyncio.run(_count_super_admin_audit_async(action))


@pytest.fixture
def clean_db() -> Iterator[None]:
    _require_db()
    _truncate()
    try:
        yield
    finally:
        _truncate()


def test_bootstrap_creates_super_admin_when_env_set(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "owner@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "owner-pw")
    monkeypatch.setenv("SUPER_ADMIN_EMAIL", "first@super.example")
    monkeypatch.setenv("SUPER_ADMIN_PASSWORD", "sa-pw-Str0ng-123")

    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code == 0, result.output

    assert _count_super_admins() == 1
    assert _count_super_admin_audit("create_super_admin") == 1


def test_bootstrap_super_admin_is_idempotent(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "owner@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "owner-pw")
    monkeypatch.setenv("SUPER_ADMIN_EMAIL", "first@super.example")
    monkeypatch.setenv("SUPER_ADMIN_PASSWORD", "sa-pw-Str0ng-123")

    first = runner.invoke(app, ["bootstrap"])
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, ["bootstrap"])
    assert second.exit_code == 0, second.output

    # Still exactly one super-admin and one audit row — idempotent on
    # email at both the user and audit layers.
    assert _count_super_admins() == 1
    assert _count_super_admin_audit("create_super_admin") == 1


def test_bootstrap_skips_super_admin_without_env(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "owner@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "owner-pw")
    monkeypatch.delenv("SUPER_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("SUPER_ADMIN_PASSWORD", raising=False)

    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code == 0, result.output

    assert _count_super_admins() == 0
    assert _count_super_admin_audit("create_super_admin") == 0


def test_bootstrap_rejects_partial_super_admin_env(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "owner@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "owner-pw")
    monkeypatch.setenv("SUPER_ADMIN_EMAIL", "first@super.example")
    monkeypatch.delenv("SUPER_ADMIN_PASSWORD", raising=False)

    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code != 0
    # Transaction rolled back — no primary owner, no super-admin.
    assert _count_super_admins() == 0
    assert _count_super_admin_audit("create_super_admin") == 0
