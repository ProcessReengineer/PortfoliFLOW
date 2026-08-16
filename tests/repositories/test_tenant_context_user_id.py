# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Verify ``tenant_context(user_id=...)`` populates the audit-log actor.

The b001 audit trigger reads ``app.user_id`` via
``current_setting('app.user_id', true)`` — NULL when unset. Phase 2's
auth middleware sets the GUC via the optional ``user_id`` parameter
on :func:`tenant_context`. These tests confirm the wiring works
end-to-end against the live compose Postgres.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import UserRepository, tenant_context


async def test_audit_log_user_id_populated_when_user_id_passed(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A write inside ``tenant_context(..., user_id=u)`` records ``user_id=u``."""
    tenant_id = await seed_tenant()

    # First create the user so we have a UUID to act as.
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="actor@example.com", password_hash="x" * 8
        )

    # Now do a second write inside a session that names the actor.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        target = await UserRepository(session).create(
            email="target@example.com", password_hash="y" * 8
        )

    # The audit-log row for the second insert must carry actor.id as user_id.
    async with tenant_context(app_engine, tenant_id) as session:
        row = await session.execute(
            text(
                """
                SELECT user_id FROM audit_log
                WHERE table_name = 'users'
                  AND record_id  = :rid
                """
            ),
            {"rid": str(target.id)},
        )
        result = row.mappings().one()

    assert result["user_id"] == actor.id


async def test_audit_log_user_id_null_when_user_id_omitted(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A write inside ``tenant_context(...)`` (no user) records ``user_id IS NULL``.

    This matches the Phase-1 behaviour and confirms the new parameter
    is genuinely optional.
    """
    tenant_id = await seed_tenant()

    async with tenant_context(app_engine, tenant_id) as session:
        created = await UserRepository(session).create(
            email="anon@example.com", password_hash="z" * 8
        )

    async with tenant_context(app_engine, tenant_id) as session:
        row = await session.execute(
            text(
                """
                SELECT user_id FROM audit_log
                WHERE table_name = 'users'
                  AND record_id  = :rid
                """
            ),
            {"rid": str(created.id)},
        )
        result = row.mappings().one()

    assert result["user_id"] is None
