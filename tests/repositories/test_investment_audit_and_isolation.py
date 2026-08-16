# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Audit-trail and cross-tenant isolation guards for the Investment-domain tables.

These tests live in ``tests/repositories/`` because they exercise
the repository layer's interaction with the b006 schema:

* Every Investment-domain write (investments, NAVs, cashflows) fires
  the audit trigger from b001 with ``tenant_id`` and ``user_id``
  populated from the active session GUCs.
* Cross-tenant write attempts are blocked by RLS WITH CHECK at the
  database boundary, even if a route handler accidentally hands the
  repository an investment id from another tenant.

Mirrors ``tests/repositories/test_saa_audit_and_isolation.py`` so
the same IS-01 / IS-02 invariants are demonstrably enforced for the
new Phase-4 tables.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)


async def _seed_investment(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    investment_name: str = "Audit Fund",
):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="default_class", display_name="Default Class"
        )
        investment = await InvestmentRepository(session).create(
            name=investment_name,
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
    return actor, investment


# ---------------------------------------------------------------------------
# IS-01a: audit_log captures investments INSERT with tenant + user
# ---------------------------------------------------------------------------


async def test_is01a_audit_log_captures_investment_insert(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="audit-inv@example.com", password_hash="x" * 8
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(code="ac", display_name="AC")
        investment = await InvestmentRepository(session).create(
            name="Audited Fund",
            investment_type="private_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(
            text(
                """
                SELECT tenant_id, user_id, table_name, operation, record_id
                FROM audit_log
                WHERE table_name = 'investments'
                  AND record_id  = :rid
                """
            ),
            {"rid": str(investment.id)},
        )
        row = result.mappings().one()

    assert row["tenant_id"] == tenant_id
    assert row["user_id"] == actor.id
    assert row["operation"] == "INSERT"
    assert row["record_id"] == investment.id


# ---------------------------------------------------------------------------
# IS-01b: audit_log captures investment_navs INSERT with tenant + user
# ---------------------------------------------------------------------------


async def test_is01b_audit_log_captures_nav_insert(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="audit-nav@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        nav = await InvestmentNavRepository(session).upsert(
            investment_id=investment.id,
            as_of_date=date(2025, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("1"),
            currency="EUR",
            source=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(
            text(
                """
                SELECT tenant_id, user_id, table_name, operation, record_id
                FROM audit_log
                WHERE table_name = 'investment_navs'
                  AND record_id  = :rid
                """
            ),
            {"rid": str(nav.id)},
        )
        row = result.mappings().one()

    assert row["tenant_id"] == tenant_id
    assert row["user_id"] == actor.id
    assert row["operation"] == "INSERT"
    assert row["record_id"] == nav.id


# ---------------------------------------------------------------------------
# IS-01c: audit_log captures investment_cashflows INSERT with tenant + user
# ---------------------------------------------------------------------------


async def test_is01c_audit_log_captures_cashflow_insert(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, investment = await _seed_investment(app_engine, tenant_id, email="audit-cf@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        cashflow = await InvestmentCashflowRepository(session).create(
            investment_id=investment.id,
            flow_timestamp=datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc),
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-1"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        result = await session.execute(
            text(
                """
                SELECT tenant_id, user_id, table_name, operation, record_id
                FROM audit_log
                WHERE table_name = 'investment_cashflows'
                  AND record_id  = :rid
                """
            ),
            {"rid": str(cashflow.id)},
        )
        row = result.mappings().one()

    assert row["tenant_id"] == tenant_id
    assert row["user_id"] == actor.id
    assert row["operation"] == "INSERT"
    assert row["record_id"] == cashflow.id


# ---------------------------------------------------------------------------
# IS-02a: WITH CHECK on investments rejects writes for foreign tenant
# ---------------------------------------------------------------------------


async def test_is02a_investments_with_check_rejects_foreign_tenant(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A raw INSERT into ``investments`` with a foreign ``tenant_id`` is rejected.

    Mirrors the SAA test (IS-02) for the Phase-4 tables. The standard
    ``apply_tenant_rls`` policy's
    ``WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid)``
    must reject the insert at the database boundary even though the
    row would otherwise satisfy structural constraints.
    """
    from sqlalchemy.exc import ProgrammingError

    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")

    async with tenant_context(app_engine, tenant_a) as session:
        actor_a = await UserRepository(session).create(email="a@example.com", password_hash="x" * 8)
        ac_a = await AssetClassRepository(session).create(code="ac", display_name="AC")

    with pytest.raises(ProgrammingError):
        async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
            await session.execute(
                text(
                    "INSERT INTO investments "
                    "(tenant_id, name, investment_type, asset_class_id, "
                    "currency, created_by) "
                    "VALUES (:tid_b, 'Foreign', 'private_equity', :ac, "
                    "'EUR', :uid)"
                ),
                {
                    "tid_b": str(tenant_b),
                    "ac": str(ac_a.id),
                    "uid": str(actor_a.id),
                },
            )


# ---------------------------------------------------------------------------
# IS-02b: WITH CHECK on investment_navs rejects writes for foreign tenant
# ---------------------------------------------------------------------------


async def test_is02b_investment_navs_with_check_rejects_foreign_tenant(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    from sqlalchemy.exc import ProgrammingError

    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")

    actor_a, inv_a = await _seed_investment(app_engine, tenant_a, email="a@example.com")

    with pytest.raises(ProgrammingError):
        async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
            await session.execute(
                text(
                    "INSERT INTO investment_navs "
                    "(tenant_id, investment_id, as_of_date, nav_value, "
                    "currency, nav_kind, created_by) "
                    "VALUES (:tid_b, :iid, '2025-12-31', 1.0, "
                    "'EUR', 'actual', :uid)"
                ),
                {
                    "tid_b": str(tenant_b),
                    "iid": str(inv_a.id),
                    "uid": str(actor_a.id),
                },
            )


# ---------------------------------------------------------------------------
# IS-02c: WITH CHECK on investment_cashflows rejects writes for foreign tenant
# ---------------------------------------------------------------------------


async def test_is02c_investment_cashflows_with_check_rejects_foreign_tenant(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    from sqlalchemy.exc import ProgrammingError

    tenant_a = await seed_tenant(name="A")
    tenant_b = await seed_tenant(name="B")

    actor_a, inv_a = await _seed_investment(app_engine, tenant_a, email="a@example.com")

    with pytest.raises(ProgrammingError):
        async with tenant_context(app_engine, tenant_a, user_id=actor_a.id) as session:
            await session.execute(
                text(
                    "INSERT INTO investment_cashflows "
                    "(tenant_id, investment_id, flow_timestamp, "
                    "flow_type, flow_kind, amount, currency, created_by) "
                    "VALUES (:tid_b, :iid, '2025-06-15 12:00:00+00', "
                    "'capital_call', 'actual', -1.0, 'EUR', :uid)"
                ),
                {
                    "tid_b": str(tenant_b),
                    "iid": str(inv_a.id),
                    "uid": str(actor_a.id),
                },
            )
