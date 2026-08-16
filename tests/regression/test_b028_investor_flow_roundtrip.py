# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for b028 (ADR-0103 §5).

Exercises the b028 migration's reversibility: from ``head`` it downgrades to
b027 (narrowing ``ck_investment_cashflows_flow_type`` back to seven values),
asserts the constraint no longer lists ``'investor_flow'``, then
``upgrade head`` again and asserts the eighth value is live. The test always
restores the DB to ``head``.

The downgrade names its **target revision** (b027) rather than using
``downgrade -1``: ``-1`` is relative to whatever the DB's current head is, so
a relative downgrade silently stops undoing *this* migration the moment a
newer one lands. Naming ``b027_add_cash_investment_type`` keeps the guard
asserting what its title claims for every future head.

Because the narrower (seven-value) CHECK revalidates existing rows on
recreation, downgrading with an ``'investor_flow'`` row present fails by
design (b028 docstring: no lossy back-cast). This guard therefore probes the
constraint's *definition* via ``pg_get_constraintdef`` — which needs no
domain rows at all and cannot leave a poison row behind — and proves the
widened CHECK **accepts** an investor-flow row separately, in a transaction
it rolls back.

Runs against a **per-test scratch database**: the ``scratch_db`` fixture
creates one, migrates it to ``head`` and drops it again afterwards. That is
what makes the paragraph above hold in practice as well as in principle — the
shared dev database permanently carries ``'investor_flow'`` rows after any
real import, so this downgrade would fail there for reasons that have nothing
to do with b028 (see ``tests/regression/conftest.py``). The Alembic CLI still
runs in a fresh subprocess, so it does not contend with this test's own
connection. If the server is unreachable the test skips, matching the other
live-DB regression guards.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.regression.conftest import ScratchDatabase

_CONSTRAINT = "ck_investment_cashflows_flow_type"
#: The revision immediately below b028 — i.e. b028's own ``down_revision``.
_BELOW = "b027_add_cash_investment_type"


async def _constraint_def(engine: AsyncEngine) -> str:
    """Return the human-readable definition of the flow-type CHECK."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
            {"name": _CONSTRAINT},
        )
        value = result.scalar_one_or_none()
    assert value is not None, f"{_CONSTRAINT} not found — is the DB migrated?"
    return value


async def test_b028_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the scratch database is at head, where the CHECK already
    # lists 'investor_flow'.
    assert "investor_flow" in await _constraint_def(scratch_superuser_engine), (
        f"{_CONSTRAINT} does not list 'investor_flow' before round-trip — DB at head?"
    )

    try:
        # 1) downgrade to b027: the narrower CHECK no longer lists the
        #    eighth value.
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
        assert "investor_flow" not in await _constraint_def(scratch_superuser_engine), (
            f"{_CONSTRAINT} still lists 'investor_flow' after downgrade to {_BELOW}"
        )

        # 2) upgrade head → the eighth value is back.
        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"
        assert "investor_flow" in await _constraint_def(scratch_superuser_engine), (
            f"{_CONSTRAINT} missing 'investor_flow' after re-upgrade to head"
        )
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")


async def test_widened_check_accepts_an_investor_flow_row(
    scratch_superuser_engine: AsyncEngine,
) -> None:
    """At head, the CHECK itself admits ``'investor_flow'``.

    The definition assertion above proves the literal is *listed*; this
    proves Postgres actually **accepts** a row carrying it. The probe runs
    inside a transaction that is always rolled back, so it leaves no row
    behind — in particular none that would poison the b028 downgrade.

    Only the CHECK is under test, so the probe writes into a throwaway table
    created **LIKE** the real one — structure, defaults and CHECK constraints,
    but none of the FKs — which isolates the constraint under test from the
    domain fixtures entirely. ``INCLUDING DEFAULTS`` is load-bearing:
    ``LIKE`` does not copy column defaults on its own, and without it the
    ``gen_random_uuid()`` default on ``id`` is absent, so every INSERT below
    would fail on a NOT NULL violation — including the one that is *supposed*
    to fail, which would then pass for the wrong reason.

    The rejection half therefore asserts the error is specifically a **check**
    violation, not merely "some exception".
    """
    async with scratch_superuser_engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "CREATE TEMP TABLE probe_cashflows "
                    "(LIKE investment_cashflows "
                    " INCLUDING DEFAULTS INCLUDING CONSTRAINTS) "
                    "ON COMMIT DROP"
                )
            )
            # The eighth value is accepted...
            await conn.execute(
                text(
                    "INSERT INTO probe_cashflows "
                    "(tenant_id, investment_id, flow_timestamp, flow_type, "
                    " flow_kind, amount, currency, ingest_origin, created_by) "
                    "VALUES (gen_random_uuid(), gen_random_uuid(), NOW(), "
                    " 'investor_flow', 'plan', 1000.0000, 'EUR', 'manual', "
                    " gen_random_uuid())"
                )
            )
            count = await conn.execute(
                text("SELECT count(*) FROM probe_cashflows WHERE flow_type = 'investor_flow'")
            )
            assert count.scalar_one() == 1

            # ...and a value outside the eight-member set is still rejected,
            # proving the CHECK was widened rather than dropped. The failing
            # statement poisons the transaction, so it runs inside a
            # SAVEPOINT the assertion can unwind to.
            nested = await conn.begin_nested()
            with pytest.raises(IntegrityError) as excinfo:
                await conn.execute(
                    text(
                        "INSERT INTO probe_cashflows "
                        "(tenant_id, investment_id, flow_timestamp, "
                        " flow_type, flow_kind, amount, currency, "
                        " ingest_origin, created_by) "
                        "VALUES (gen_random_uuid(), gen_random_uuid(), NOW(), "
                        " 'not_a_flow_type', 'plan', 1.0, 'EUR', 'manual', "
                        " gen_random_uuid())"
                    )
                )
            await nested.rollback()
            # Specifically a CHECK violation — not a NOT NULL / FK one, which
            # would mean the probe rejected the row for an unrelated reason.
            # asyncpg's DBAPI adapter re-wraps the driver error, so the
            # violation class shows up in the message rather than in the type.
            message = str(excinfo.value)
            assert "CheckViolationError" in message, (
                "expected the widened flow_type CHECK to reject the row; got "
                f"a different integrity error: {message}"
            )
            assert "violates check constraint" in message
        finally:
            await trans.rollback()
