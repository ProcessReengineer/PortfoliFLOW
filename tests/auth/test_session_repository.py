# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for ``SessionRepository`` against the live compose Postgres.

Covers:

- create + get_by_token round-trip.
- Idle-timeout: a session whose ``last_seen_at`` is older than
  :data:`IDLE_TIMEOUT` is not returned.
- Absolute-timeout: a session past ``expires_at`` is not returned.
- ``touch`` bumps ``last_seen_at`` to NOW().
- ``delete`` and ``delete_all_for_user``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import UserRepository, tenant_context
from services.auth.session import (
    IDLE_TIMEOUT,
    SessionRepository,
)


async def test_create_and_retrieve_session(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()

    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(
            email="alice@example.com", password_hash="x" * 8
        )
        repo = SessionRepository(session)
        created = await repo.create_session(user, ip="127.0.0.1", ua="pytest")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = SessionRepository(session)
        fetched = await repo.get_by_token(created.session_token)

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.user_id == user.id
    assert fetched.tenant_id == tenant_id


async def test_idle_timeout_hides_session(
    app_engine: AsyncEngine, seed_tenant, superuser_engine: AsyncEngine
) -> None:
    tenant_id = await seed_tenant()

    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(email="idle@example.com", password_hash="x" * 8)
        repo = SessionRepository(session)
        created = await repo.create_session(user, ip=None, ua=None)

    # Roll last_seen_at back further than IDLE_TIMEOUT. Computing the
    # target timestamp in Python avoids asyncpg's interval-inference
    # quirk on `NOW() - $1`.
    backdated_at = datetime.now(timezone.utc) - (IDLE_TIMEOUT + timedelta(minutes=1))
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET last_seen_at = :ts WHERE id = :id"),
            {"ts": backdated_at, "id": str(created.id)},
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = SessionRepository(session)
        result = await repo.get_by_token(created.session_token)
    assert result is None


async def test_absolute_timeout_hides_session(
    app_engine: AsyncEngine, seed_tenant, superuser_engine: AsyncEngine
) -> None:
    tenant_id = await seed_tenant()

    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(
            email="absolute@example.com", password_hash="x" * 8
        )
        repo = SessionRepository(session)
        created = await repo.create_session(user, ip=None, ua=None)

    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET expires_at = NOW() - INTERVAL '1 minute' WHERE id = :id"),
            {"id": str(created.id)},
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = SessionRepository(session)
        result = await repo.get_by_token(created.session_token)
    assert result is None


async def test_touch_bumps_last_seen_at(
    app_engine: AsyncEngine, seed_tenant, superuser_engine: AsyncEngine
) -> None:
    tenant_id = await seed_tenant()

    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(
            email="touch@example.com", password_hash="x" * 8
        )
        repo = SessionRepository(session)
        created = await repo.create_session(user, ip=None, ua=None)

    # Backdate slightly so we can observe the bump.
    backdated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET last_seen_at = :ts WHERE id = :id"),
            {"ts": backdated_at, "id": str(created.id)},
        )

    async with tenant_context(app_engine, tenant_id) as session:
        await SessionRepository(session).touch(created.id)

    async with superuser_engine.connect() as conn:
        row = await conn.execute(
            text("SELECT NOW() - last_seen_at AS delta FROM sessions WHERE id = :id"),
            {"id": str(created.id)},
        )
        delta = row.scalar_one()
    assert delta < timedelta(seconds=5)


async def test_delete_removes_only_target_session(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()

    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(email="del@example.com", password_hash="x" * 8)
        repo = SessionRepository(session)
        a = await repo.create_session(user, ip=None, ua=None)
        b = await repo.create_session(user, ip=None, ua=None)

    async with tenant_context(app_engine, tenant_id) as session:
        await SessionRepository(session).delete(a.id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = SessionRepository(session)
        assert await repo.get_by_token(a.session_token) is None
        assert await repo.get_by_token(b.session_token) is not None


async def test_delete_all_for_user_clears_every_session(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()

    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(
            email="logout@example.com", password_hash="x" * 8
        )
        repo = SessionRepository(session)
        a = await repo.create_session(user, ip=None, ua=None)
        b = await repo.create_session(user, ip=None, ua=None)

    async with tenant_context(app_engine, tenant_id) as session:
        await SessionRepository(session).delete_all_for_user(user.id)

    async with tenant_context(app_engine, tenant_id) as session:
        repo = SessionRepository(session)
        assert await repo.get_by_token(a.session_token) is None
        assert await repo.get_by_token(b.session_token) is None
