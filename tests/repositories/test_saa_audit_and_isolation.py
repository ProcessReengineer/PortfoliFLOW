# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Audit-trail and cross-tenant isolation guards for the SAA tables.

These tests live in ``tests/repositories/`` because they exercise
the repository layer's interaction with the b005 schema:

* Every SAA write fires the audit trigger from b001 with
  ``tenant_id`` and ``user_id`` populated from the active session
  GUCs.
* Cross-tenant write attempts are blocked by RLS WITH CHECK at the
  database boundary, even if a route handler accidentally hands the
  repository a configuration id from another tenant.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    SAAConfigurationRepository,
    UserRepository,
    tenant_context,
)


# ---------------------------------------------------------------------------
# IS-01: audit_log captures SAA-configuration INSERT with tenant + user
# ---------------------------------------------------------------------------


async def test_is01_audit_log_captures_saa_config_insert(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="audit-saa@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        config = await SAAConfigurationRepository(session).create("Audited", 0.02, 100, actor.id)

    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(
            text(
                """
                SELECT tenant_id, user_id, table_name, operation, record_id
                FROM audit_log
                WHERE table_name = 'saa_configurations'
                  AND record_id  = :rid
                """
            ),
            {"rid": str(config.id)},
        )
        row = result.mappings().one()

    assert row["tenant_id"] == tenant_id
    assert row["user_id"] == actor.id
    assert row["operation"] == "INSERT"
    assert row["record_id"] == config.id


# ---------------------------------------------------------------------------
# IS-02: WITH CHECK on saa_configurations rejects writes for foreign tenant
# ---------------------------------------------------------------------------


async def test_is02_with_check_rejects_foreign_tenant_id(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A raw INSERT with a foreign ``tenant_id`` is rejected by RLS.

    Tenant A's session attempts to insert a row whose ``tenant_id``
    points at Tenant B. The standard ``apply_tenant_rls`` policy's
    ``WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid)``
    clause must reject the insert at the database boundary — even
    though the row would otherwise satisfy the unique / FK
    constraints. This is the audit-relevant invariant ADR-0035 §6
    calls "defence in depth": the application layer setting
    ``tenant_id`` from ``app.tenant_id`` is one safeguard; the WITH
    CHECK clause is the second.

    Note: this test does *not* exercise the FK-vs-RLS interaction
    (a row with the right ``tenant_id`` but pointing at another
    tenant's parent row is currently allowed by the schema; the
    application layer is responsible for ownership verification).
    A future ADR may add cross-table tenant-match CHECK constraints
    to close that hole.
    """
    from sqlalchemy.exc import ProgrammingError

    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")

    async with tenant_context(app_engine, tenant_a) as session:
        actor_a = await UserRepository(session).create(email="a@example.com", password_hash="x" * 8)

    # From tenant A's session, try to insert a saa_configurations row
    # with tenant_id pointing at B. The WITH CHECK clause must fire.
    with pytest.raises(ProgrammingError):
        async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
            await session.execute(
                text(
                    "INSERT INTO saa_configurations "
                    "(tenant_id, name, risk_free_rate, n_frontier_points, "
                    "is_active, created_by) "
                    "VALUES (:tid_b, 'Foreign', 0.01, 100, FALSE, :uid)"
                ),
                {"tid_b": str(tenant_b), "uid": str(actor_a.id)},
            )
