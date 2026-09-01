# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AuditLogRepository tests against the live compose Postgres.

The repository has one method and it answers one question — "has this row
been UPDATEd since?" — but the question is load-bearing: trade-ticket
reversal (ADR-0128 §6) refuses when the answer is yes, and would otherwise
delete a row somebody had corrected. So these tests are less about the SQL
than about the *premises* the SQL rests on, every one of which lives in the
database rather than in the code:

* the audit trigger fires on paths that do not maintain ``updated_at``
  (TA-01) — which is precisely why the check reads the log and not the row;
* an INSERT is not an UPDATE (TA-02), so a booking does not report itself
  as a modification;
* ``NOW()`` is the *transaction* timestamp, so a write and the effect row
  enumerating it tie rather than differ, and the strict comparison excludes
  the booking's own writes (TA-03);
* ``audit_log`` is RLS-policed, so one tenant's edits are invisible to
  another (TA-04).

Coverage
--------
* TA-01: an UPDATE after the cutoff is detected — on ``position_transactions``,
  whose ``updated_at`` is *not* maintained.
* TA-02: the INSERT that created the row is not reported.
* TA-03: the strict boundary — an UPDATE inside the reference transaction
  ties and is excluded; a later one is not.
* TA-04: table, record and operation all discriminate.
* TA-05: cross-tenant invisibility.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    AuditLogRepository,
    InvestmentRepository,
    PositionTransactionRepository,
    UserRepository,
    tenant_context,
)

_TRADE_DATE = date(2026, 8, 31)
_LEDGER_TABLE = "position_transactions"


async def _seed(app_engine: AsyncEngine, tenant_id, *, email: str):
    """A user, a unitised instrument, and one ``opening`` row on its ledger."""
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="listed_class", display_name="Listed Class"
        )
        instrument = await InvestmentRepository(session).create(
            name="Listed Fund",
            investment_type="listed_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
            valuation_mode="unitised",
        )
        row = await PositionTransactionRepository(session).add(
            investment_id=instrument.id,
            txn_type="opening",
            trade_date=_TRADE_DATE,
            units=Decimal("100"),
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )
    return actor, instrument, row


async def _transaction_now(app_engine: AsyncEngine, tenant_id, actor_id) -> datetime:
    """Read the database's own clock, so the cutoff is comparable to the log's."""
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        return (await session.execute(text("SELECT NOW()"))).scalar_one()


# ---------------------------------------------------------------------------
# TA-01: an edit is detected — on a table whose updated_at is not maintained
# ---------------------------------------------------------------------------


async def test_ta01_an_update_after_the_cutoff_is_detected(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The premise of D-Y, stated as a test rather than as a claim.

    The same edit is asserted twice: the audit log sees it, and
    ``updated_at`` does not. If ``position_transactions`` ever grows an
    ``onupdate`` the second assertion fails — and the reversal check would
    then have a second witness available, which is worth being told about.
    """
    tenant_id = await seed_tenant("TA-01")
    actor, instrument, row = await _seed(app_engine, tenant_id, email="pm@ta01.example")
    cutoff = await _transaction_now(app_engine, tenant_id, actor.id)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await PositionTransactionRepository(session).update(
            row.id,
            trade_date=_TRADE_DATE,
            units=Decimal("120"),
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        detected = await AuditLogRepository(session).has_update_since(
            _LEDGER_TABLE, row.id, after=cutoff
        )
        edited = await PositionTransactionRepository(session).get_by_id(row.id)

    assert detected is True
    assert edited is not None and edited.units == Decimal("120.00000000")
    # The reason the log has to be asked: the row itself does not say.
    assert edited.updated_at == row.updated_at
    assert instrument.id == edited.investment_id


# ---------------------------------------------------------------------------
# TA-02: the row's own INSERT is not an UPDATE
# ---------------------------------------------------------------------------


async def test_ta02_the_creating_insert_is_not_reported(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A booking must not report itself as having modified what it wrote."""
    tenant_id = await seed_tenant("TA-02")
    actor, _, row = await _seed(app_engine, tenant_id, email="pm@ta02.example")
    long_before = datetime(2020, 1, 1, tzinfo=timezone.utc)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        audit = AuditLogRepository(session)
        # Even with a cutoff before the row existed, the INSERT does not count.
        assert await audit.has_update_since(_LEDGER_TABLE, row.id, after=long_before) is False
        # And there *is* an audit row for it — the trigger did fire.
        recorded = await session.execute(
            text("SELECT operation FROM audit_log WHERE table_name = :t AND record_id = :r"),
            {"t": _LEDGER_TABLE, "r": str(row.id)},
        )
        assert [value for (value,) in recorded.all()] == ["INSERT"]


# ---------------------------------------------------------------------------
# TA-03: the strict boundary
# ---------------------------------------------------------------------------


async def test_ta03_an_update_inside_the_reference_transaction_ties_and_is_excluded(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The property the reversal check is built on.

    ``audit_log.created_at`` and ``trade_ticket_effects.emitted_at`` are both
    ``NOW()``, which in Postgres is the transaction timestamp — so a write
    made in the same transaction as the cutoff is read ties with it, and the
    strict ``>`` excludes it. Without that, every booking that deactivated an
    investment would refuse its own reversal.
    """
    tenant_id = await seed_tenant("TA-03")
    actor, _, row = await _seed(app_engine, tenant_id, email="pm@ta03.example")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        stamp = (await session.execute(text("SELECT NOW()"))).scalar_one()
        await PositionTransactionRepository(session).update(
            row.id,
            trade_date=_TRADE_DATE,
            units=Decimal("110"),
        )
        same_transaction = await AuditLogRepository(session).has_update_since(
            _LEDGER_TABLE, row.id, after=stamp
        )

    assert same_transaction is False

    # A later transaction's edit is a different matter.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await PositionTransactionRepository(session).update(
            row.id,
            trade_date=_TRADE_DATE,
            units=Decimal("130"),
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        assert (
            await AuditLogRepository(session).has_update_since(_LEDGER_TABLE, row.id, after=stamp)
            is True
        )


# ---------------------------------------------------------------------------
# TA-04: table, record and operation all discriminate
# ---------------------------------------------------------------------------


async def test_ta04_the_probe_is_scoped_to_one_table_and_one_row(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An edit elsewhere is not an edit here."""
    tenant_id = await seed_tenant("TA-04")
    actor, instrument, row = await _seed(app_engine, tenant_id, email="pm@ta04.example")
    cutoff = await _transaction_now(app_engine, tenant_id, actor.id)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        # Edit the *investment*, not the ledger row.
        await InvestmentRepository(session).set_active(instrument.id, False)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        audit = AuditLogRepository(session)
        assert await audit.has_update_since("investments", instrument.id, after=cutoff) is True
        # Same id, wrong table.
        assert await audit.has_update_since(_LEDGER_TABLE, instrument.id, after=cutoff) is False
        # Right table, untouched row.
        assert await audit.has_update_since(_LEDGER_TABLE, row.id, after=cutoff) is False
        # A row that never existed.
        assert await audit.has_update_since("investments", uuid4(), after=cutoff) is False


# ---------------------------------------------------------------------------
# TA-05: cross-tenant invisibility
# ---------------------------------------------------------------------------


async def test_ta05_one_tenants_edits_are_invisible_to_another(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """``audit_log`` is RLS-policed (ADR-0035 §7), and the repository relies on it.

    The reversal check runs inside a tenant context and never filters by
    tenant itself. If the policy were ever dropped, a reversal in one tenant
    could be blocked by an edit in another — invisibly, since the two rows
    would share nothing but a UUID.
    """
    owner = await seed_tenant("TA-05")
    other = await seed_tenant("TA-05b")
    actor, _, row = await _seed(app_engine, owner, email="pm@ta05.example")
    cutoff = await _transaction_now(app_engine, owner, actor.id)

    async with tenant_context(app_engine, owner, user_id=actor.id) as session:
        await PositionTransactionRepository(session).update(
            row.id,
            trade_date=_TRADE_DATE,
            units=Decimal("140"),
        )

    async with tenant_context(app_engine, owner, user_id=actor.id) as session:
        assert (
            await AuditLogRepository(session).has_update_since(_LEDGER_TABLE, row.id, after=cutoff)
            is True
        )

    async with tenant_context(app_engine, other) as session:
        assert (
            await AuditLogRepository(session).has_update_since(_LEDGER_TABLE, row.id, after=cutoff)
            is False
        )
