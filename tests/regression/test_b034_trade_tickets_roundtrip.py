# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for b034 (ADR-0128 §1–§3).

Exercises the b034 migration's reversibility: from ``head`` it downgrades
to b033 (dropping ``trade_tickets`` and ``trade_ticket_effects``), asserts
both tables are gone, then ``upgrade head`` again and asserts they are back
in the full shape the ADR specifies — the lifecycle vocabulary CHECK
carrying all eight states, the three attribution CHECKs that make a station
unreachable without its actor, the ``commitment`` shape rule, the two
unique constraints, RLS enabled + forced with the standard
``tenant_isolation`` policy, and the generic audit trigger on **both**
tables (operator decision D-4, the b033 contrast).

Two invariants get their own assertions because they are load-bearing
*absences*, and an absence is exactly what a well-meaning later migration
"completes":

1. **``effect_id`` carries no foreign key** (ADR-0128 §2). The ledger stays
   ignorant of the layer above it, and the referenced row may legitimately
   be deleted by a reversal or an ADR-0097 §7 CRUD correction. Adding the
   FK that looks missing would both invert the dependency and make the
   effect record die with the row it documents.
2. **``sent`` / ``acknowledged`` / ``executed`` are in the status CHECK**
   even though no v1 transition writes them (ADR-0128 §3). Trimming the
   vocabulary to what is reachable would turn arming the ADR-0129 channel
   from a rule change back into a migration.

The downgrade names its **target revision** rather than using
``downgrade -1``: ``-1`` is relative to whatever the DB's current head is,
so a relative downgrade silently stops undoing *this* migration the moment
a newer one lands. Naming b034's own ``down_revision`` keeps the guard
honest for every future head.

Runs against a **per-test scratch database** (``tests/regression/conftest.py``)
like the other migration guards, so no downgrade ever touches the shared
dev database. If the server is unreachable the test skips.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.regression.conftest import ScratchDatabase

_TICKETS = "trade_tickets"
_EFFECTS = "trade_ticket_effects"
#: The revision immediately below b034 — i.e. b034's own ``down_revision``.
_BELOW = "b033_add_watchpoints"

_EXPECTED_TICKET_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "tenant_id",
        "ticket_number",
        "kind",
        "direction",
        "status",
        "investment_id",
        "cash_investment_id",
        "trade_date",
        "settlement_date",
        "units",
        "price_per_unit",
        "gross_amount",
        "fees",
        "taxes",
        "net_amount",
        "currency",
        "commitment_amount",
        "master_data",
        "set_inactive",
        "note",
        "source",
        "cancel_reason",
        "case_id",
        "proposed_by",
        "proposed_at",
        "approved_by",
        "approved_at",
        "booked_by",
        "booked_at",
        "cancelled_at",
        "created_by",
        "created_at",
        "updated_at",
    }
)

_EXPECTED_EFFECT_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "tenant_id",
        "ticket_id",
        "effect_type",
        "effect_id",
        "prior_state",
        "emitted_at",
    }
)

_EXPECTED_TICKET_CHECKS = (
    "ck_trade_tickets_kind",
    "ck_trade_tickets_direction",
    "ck_trade_tickets_status",
    "ck_trade_tickets_commitment_shape",
    "ck_trade_tickets_units_positive",
    "ck_trade_tickets_price_positive",
    "ck_trade_tickets_proposed_attribution",
    "ck_trade_tickets_approved_attribution",
    "ck_trade_tickets_booked_attribution",
    "ck_trade_tickets_cancelled_timestamp",
)

_EXPECTED_EFFECT_CHECKS = ("ck_trade_ticket_effects_effect_type",)

_TICKET_UNIQUE = "uq_trade_tickets_tenant_ticket_number"
_EFFECT_UNIQUE = "uq_trade_ticket_effects_ticket_effect"

_EXPECTED_TICKET_INDEXES = (
    "ix_trade_tickets_tenant_id",
    "ix_trade_tickets_tenant_status",
    "ix_trade_tickets_investment_id",
    "ix_trade_tickets_case_id",
)

_EXPECTED_EFFECT_INDEXES = (
    "ix_trade_ticket_effects_tenant_id",
    "ix_trade_ticket_effects_effect",
)

#: The ADR-0129 channel states — defined from day one, unreachable in v1.
_CHANNEL_STATES = ("sent", "acknowledged", "executed")


async def _table_exists(engine: AsyncEngine, table: str) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        )
        return result.scalar_one_or_none() is not None


async def _columns(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        )
        return {row[0] for row in result.fetchall()}


async def _constraint_names(engine: AsyncEngine, table: str, contype: str) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT c.conname
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND t.relname = :t
                  AND c.contype::text = :contype
                """
            ),
            {"t": table, "contype": contype},
        )
        return {row[0] for row in result.fetchall()}


async def _check_clause(engine: AsyncEngine, table: str, name: str) -> str:
    """Return the rendered CHECK expression of ``name`` on ``table``."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT pg_get_constraintdef(c.oid)
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                WHERE n.nspname = 'public' AND t.relname = :t AND c.conname = :n
                """
            ),
            {"t": table, "n": name},
        )
        return result.scalar_one()


async def _fk_columns(engine: AsyncEngine, table: str) -> set[str]:
    """Return the local column names covered by a FOREIGN KEY on ``table``."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT a.attname
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                JOIN unnest(c.conkey) AS k(attnum) ON TRUE
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                WHERE n.nspname = 'public'
                  AND t.relname = :t
                  AND c.contype = 'f'
                """
            ),
            {"t": table},
        )
        return {row[0] for row in result.fetchall()}


async def _index_names(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t"),
            {"t": table},
        )
        return {row[0] for row in result.fetchall()}


async def _rls_enabled_and_forced(engine: AsyncEngine, table: str) -> tuple[bool, bool]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = :t AND relnamespace = 'public'::regnamespace"
                ),
                {"t": table},
            )
        ).first()
    assert row is not None
    return bool(row.relrowsecurity), bool(row.relforcerowsecurity)


async def _has_tenant_isolation_policy(engine: AsyncEngine, table: str) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT 1 FROM pg_policies "
                "WHERE schemaname = 'public' AND tablename = :t "
                "AND policyname = 'tenant_isolation'"
            ),
            {"t": table},
        )
        return result.scalar_one_or_none() is not None


async def _audit_trigger_names(engine: AsyncEngine, table: str) -> set[str]:
    """Return the table's non-internal triggers bound to the audit function."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT tg.tgname
                FROM pg_trigger tg
                JOIN pg_class t ON tg.tgrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                JOIN pg_proc p ON tg.tgfoid = p.oid
                WHERE n.nspname = 'public'
                  AND t.relname = :t
                  AND NOT tg.tgisinternal
                  AND p.proname = 'audit_trigger_function'
                """
            ),
            {"t": table},
        )
        return {row[0] for row in result.fetchall()}


async def _assert_full_shape(engine: AsyncEngine) -> None:
    """Assert both tables carry the ADR-0128 shape."""
    ticket_columns = await _columns(engine, _TICKETS)
    assert ticket_columns == _EXPECTED_TICKET_COLUMNS, (
        "trade_tickets column set drifted from the ADR-0128 §1 shape: "
        f"missing={sorted(_EXPECTED_TICKET_COLUMNS - ticket_columns)}, "
        f"extra={sorted(ticket_columns - _EXPECTED_TICKET_COLUMNS)}"
    )

    effect_columns = await _columns(engine, _EFFECTS)
    assert effect_columns == _EXPECTED_EFFECT_COLUMNS, (
        "trade_ticket_effects column set drifted from the ADR-0128 §2 shape: "
        f"missing={sorted(_EXPECTED_EFFECT_COLUMNS - effect_columns)}, "
        f"extra={sorted(effect_columns - _EXPECTED_EFFECT_COLUMNS)}"
    )

    ticket_checks = await _constraint_names(engine, _TICKETS, "c")
    for name in _EXPECTED_TICKET_CHECKS:
        assert name in ticket_checks, (
            f"trade_tickets is missing the {name} CHECK — the lifecycle's "
            "sign, shape and attribution rules are schema facts (ADR-0128 §3)."
        )

    effect_checks = await _constraint_names(engine, _EFFECTS, "c")
    for name in _EXPECTED_EFFECT_CHECKS:
        assert name in effect_checks, f"trade_ticket_effects is missing the {name} CHECK."

    # ADR-0128 §3: the full vocabulary from day one. Trimming it to the
    # states v1 can reach would turn arming ADR-0129 into a migration.
    status_clause = await _check_clause(engine, _TICKETS, "ck_trade_tickets_status")
    for state in _CHANNEL_STATES:
        assert f"'{state}'" in status_clause, (
            f"ck_trade_tickets_status must list {state!r} even though no v1 "
            "transition writes it — ADR-0129 arms it as a rule change, not a "
            f"migration. Found: {status_clause}"
        )

    # ADR-0128 §2: effect_id is deliberately unconstrained. The only FKs on
    # this table are the two that point *downwards* — to its tenant and to
    # its ticket.
    effect_fk_columns = await _fk_columns(engine, _EFFECTS)
    assert effect_fk_columns == {"tenant_id", "ticket_id"}, (
        "trade_ticket_effects must carry foreign keys on tenant_id and "
        f"ticket_id only; found {sorted(effect_fk_columns)}. In particular "
        "effect_id must have none: the ledger stays ignorant of the layer "
        "above it, and the referenced row may legitimately be deleted by a "
        "reversal or an ADR-0097 §7 CRUD correction (ADR-0128 §2)."
    )

    assert _TICKET_UNIQUE in await _constraint_names(engine, _TICKETS, "u"), (
        f"trade_tickets is missing {_TICKET_UNIQUE}; without it two concurrent "
        "allocations could burn the same tenant ticket number."
    )
    assert _EFFECT_UNIQUE in await _constraint_names(engine, _EFFECTS, "u"), (
        f"trade_ticket_effects is missing {_EFFECT_UNIQUE}; without it one "
        "ticket could record the same emitted row twice and a reversal would "
        "read a doubled linkage."
    )

    ticket_indexes = await _index_names(engine, _TICKETS)
    for name in _EXPECTED_TICKET_INDEXES:
        assert name in ticket_indexes, f"trade_tickets is missing index {name}."

    effect_indexes = await _index_names(engine, _EFFECTS)
    for name in _EXPECTED_EFFECT_INDEXES:
        assert name in effect_indexes, f"trade_ticket_effects is missing index {name}."

    for table in (_TICKETS, _EFFECTS):
        enabled, forced = await _rls_enabled_and_forced(engine, table)
        assert enabled and forced, (
            f"{table} must have RLS enabled AND forced after apply_tenant_rls."
        )
        assert await _has_tenant_isolation_policy(engine, table), (
            f"{table} is missing the standard tenant_isolation policy."
        )
        triggers = await _audit_trigger_names(engine, table)
        assert triggers == {f"{table}_audit_trigger"}, (
            f"{table} must carry exactly the generic audit trigger (D-4): who "
            "proposed, approved and booked a portfolio change is precisely "
            "what BAIT/VAIT-grade explainability must capture, and a ticket "
            f"carries no secrets to keep out of audit_log. Found {sorted(triggers)}."
        )


async def test_b034_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the fixture built the scratch database at head.
    for table in (_TICKETS, _EFFECTS):
        assert await _table_exists(scratch_superuser_engine, table), (
            f"{table} missing before round-trip — is the DB at head?"
        )

    try:
        # 1) downgrade to b033: both tables are gone.
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
        for table in (_TICKETS, _EFFECTS):
            assert not await _table_exists(scratch_superuser_engine, table), (
                f"{table} still present after downgrade to {_BELOW}"
            )

        # 2) upgrade head → both are back, in full shape.
        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"
        for table in (_TICKETS, _EFFECTS):
            assert await _table_exists(scratch_superuser_engine, table), (
                f"{table} missing after re-upgrade to head"
            )

        await _assert_full_shape(scratch_superuser_engine)
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")
