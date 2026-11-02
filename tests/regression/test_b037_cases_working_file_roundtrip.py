# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for b037 (ADR-0138, the case working file).

Exercises the b037 migration's reversibility: from ``head`` it downgrades to
b036 (dropping the eight working-file tables, the two new ``cases`` indexes
and the six new ``cases`` columns), asserts all of it is gone, then
``upgrade head`` again and asserts it is back in the shape ADR-0138's Object
model specifies — the column sets, ``owner_user_id`` / ``updated_at`` NOT
NULL, RLS enabled + forced with the standard ``tenant_isolation`` policy on
every new table, the pair-unique constraint of ``case_investments``, the
one-draft partial unique index of ``case_decisions`` and the partial
``ix_cases_wait_until``.

A ``cases`` row inserted in the downgraded state proves the backfill: after
the upgrade it carries ``owner_user_id = opened_by`` and
``updated_at = opened_at``.

Three absences get their own assertions, because an absence is exactly what
a well-meaning later migration "completes":

1. **No audit trigger on any working-file table.** The record is the audit
   layer; the working file is work in progress (the b031 idiom).
2. **No ``updated_at`` on any working-file table.** The working-file stamp
   lives on ``cases`` (D-CS-B-5).
3. **No ``kind`` / ``type`` column on ``cases``.** There are no case kinds;
   ``framing`` is the one presentation flag (ADR-0138 §3).

The downgrade names its **target revision** rather than using
``downgrade -1``, so the guard stays honest for every future head. Runs
against a **per-test scratch database** (``tests/regression/conftest.py``);
if the server is unreachable the test skips.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.regression.conftest import ScratchDatabase

#: The revision immediately below b037 — i.e. b037's own ``down_revision``.
_BELOW = "b036_briefing_and_family_scope"

_COMMON: frozenset[str] = frozenset({"id", "tenant_id", "case_id", "created_at"})

_EXPECTED_COLUMNS: dict[str, frozenset[str]] = {
    "case_threads": _COMMON
    | {"position", "title", "status", "prose", "conclusion", "concluded_at", "removed_at"},
    "case_kept": _COMMON
    | {
        "thread_id",
        "position",
        "artifact",
        "caption",
        "comment",
        "payload",
        "payload_sha256",
        "attachment_id",
        "kept_by",
        "via",
        "kept_at",
        "removed_at",
    },
    "case_figures": _COMMON
    | {"position", "label", "source", "value_text", "source_ref", "as_of", "note"},
    "case_conversations": _COMMON
    | {
        "position",
        "held_on",
        "channel",
        "with_whom",
        "what_was_said",
        "what_was_promised",
        "duration_minutes",
    },
    "case_points": _COMMON
    | {"position", "text", "owner_user_id", "due_on", "done_at", "ticket_id"},
    "case_decisions": _COMMON
    | {
        "options",
        "chosen_key",
        "rationale",
        "decided_by",
        "decided_at",
        "recorded_by",
        "recorded_at",
        "record_entry_id",
    },
    "case_investments": _COMMON | {"investment_id", "linked_by", "linked_at"},
    "case_proposals": _COMMON
    | {
        "requested_by",
        "requested_at",
        "request_text",
        "landed_at",
        "items",
        "resolved_at",
    },
}
_NEW_TABLES: tuple[str, ...] = tuple(_EXPECTED_COLUMNS)

_NEW_CASE_COLUMNS: tuple[str, ...] = (
    "wait_until",
    "owner_user_id",
    "counterparty",
    "due_on",
    "framing",
    "updated_at",
)
_NEW_CASE_INDEXES: tuple[str, ...] = ("ix_cases_tenant_owner", "ix_cases_wait_until")


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


async def _column_nullability(engine: AsyncEngine, table: str) -> dict[str, str]:
    """Return ``{column_name: is_nullable}`` for ``table``."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        )
        return {row[0]: row[1] for row in result.fetchall()}


async def _index_defs(engine: AsyncEngine, table: str) -> dict[str, str]:
    """Return ``{indexname: indexdef}`` for ``table``."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = :t"
            ),
            {"t": table},
        )
        return {row[0]: row[1] for row in result.fetchall()}


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


async def _seed_legacy_case(engine: AsyncEngine) -> dict[str, str]:
    """Insert a tenant, a user and a b036-shaped ``cases`` row; return the ids."""
    ids = {k: str(uuid4()) for k in ("tenant", "user", "case")}
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, 'b037 RT', :sub)"),
            {"id": ids["tenant"], "sub": f"b037-rt-{ids['tenant'][:12]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash) "
                "VALUES (:id, :t, :email, :pw)"
            ),
            {
                "id": ids["user"],
                "t": ids["tenant"],
                "email": f"b037-{ids['user'][:8]}@example.com",
                "pw": "x" * 16,
            },
        )
        # NOTE: no owner_user_id / updated_at — they do not exist at b036.
        await conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, case_number, title, opened_by, opened_at) "
                "VALUES (:id, :t, 1, 'Legacy case', :u, "
                "TIMESTAMPTZ '2026-07-22 09:00:00+00')"
            ),
            {"id": ids["case"], "t": ids["tenant"], "u": ids["user"]},
        )
    return ids


async def _assert_full_shape(engine: AsyncEngine) -> None:
    """Assert the eight tables and the six cases columns carry the ADR-0138 shape."""
    for table, expected in _EXPECTED_COLUMNS.items():
        columns = set(await _column_nullability(engine, table))
        assert columns == expected, (
            f"{table} column set drifted from the ADR-0138 Object model: "
            f"missing={sorted(expected - columns)}, extra={sorted(columns - expected)}"
        )
        # Absence 2: the working-file stamp lives on cases, not on the rows.
        assert "updated_at" not in columns, (
            f"{table} must carry no updated_at; cases.updated_at is the working-file "
            "stamp (D-CS-B-5)."
        )

        enabled, forced = await _rls_enabled_and_forced(engine, table)
        assert enabled and forced, (
            f"{table} must have RLS enabled AND forced after apply_tenant_rls."
        )
        assert await _has_tenant_isolation_policy(engine, table), (
            f"{table} is missing the standard tenant_isolation policy."
        )
        # Absence 1: the record is the audit layer.
        triggers = await _audit_trigger_names(engine, table)
        assert triggers == set(), (
            f"{table} must carry no audit trigger (the b031 idiom); found {sorted(triggers)}."
        )
        assert f"ix_{table}_case_id" in await _index_defs(engine, table), (
            f"{table} is missing ix_{table}_case_id."
        )

    case_columns = await _column_nullability(engine, "cases")
    for column in _NEW_CASE_COLUMNS:
        assert column in case_columns, f"cases.{column} missing after upgrade."
    assert case_columns["owner_user_id"] == "NO", "cases.owner_user_id must be NOT NULL."
    assert case_columns["updated_at"] == "NO", "cases.updated_at must be NOT NULL."
    for column in ("wait_until", "counterparty", "due_on", "framing"):
        assert case_columns[column] == "YES", f"cases.{column} must be nullable."
    # Absence 3: no case kinds — framing is the one presentation flag (§3).
    for column in ("kind", "type"):
        assert column not in case_columns, (
            f"cases must carry no {column!r} column: there are no case kinds (ADR-0138 §3)."
        )

    assert "uq_case_investments_case_investment" in await _constraint_names(
        engine, "case_investments", "u"
    ), "case_investments is missing uq_case_investments_case_investment."

    decision_indexes = await _index_defs(engine, "case_decisions")
    one_draft = decision_indexes.get("uq_case_decisions_one_draft")
    assert one_draft is not None, "case_decisions is missing uq_case_decisions_one_draft."
    assert "UNIQUE" in one_draft and "WHERE (recorded_at IS NULL)" in one_draft, (
        f"uq_case_decisions_one_draft must be a unique partial index; found {one_draft!r}."
    )

    case_indexes = await _index_defs(engine, "cases")
    assert "ix_cases_tenant_owner" in case_indexes, "cases is missing ix_cases_tenant_owner."
    wait_until = case_indexes.get("ix_cases_wait_until")
    assert wait_until is not None, "cases is missing ix_cases_wait_until."
    assert "WHERE (state = 'waiting'::text)" in wait_until, (
        f"ix_cases_wait_until must be partial on waiting cases; found {wait_until!r}."
    )
    assert "ix_cases_tenant_state" in case_indexes, "ix_cases_tenant_state must stay."


async def test_b037_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the fixture built the scratch database at head.
    for table in _NEW_TABLES:
        assert await _table_exists(scratch_superuser_engine, table), (
            f"{table} missing before round-trip — is the DB at head?"
        )

    try:
        # 1) downgrade to b036: the tables, indexes and columns are gone.
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
        for table in _NEW_TABLES:
            assert not await _table_exists(scratch_superuser_engine, table), (
                f"{table} still present after downgrade to {_BELOW}"
            )
        leftover = set(_NEW_CASE_COLUMNS) & set(
            await _column_nullability(scratch_superuser_engine, "cases")
        )
        assert not leftover, f"cases columns still present after downgrade: {sorted(leftover)}"
        leftover_indexes = set(_NEW_CASE_INDEXES) & set(
            await _index_defs(scratch_superuser_engine, "cases")
        )
        assert not leftover_indexes, (
            f"cases indexes still present after downgrade: {sorted(leftover_indexes)}"
        )

        # A legacy row in the b036 shape, for the backfill proof.
        ids = await _seed_legacy_case(scratch_superuser_engine)

        # 2) upgrade head → all of it is back, in full shape, and backfilled.
        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"
        for table in _NEW_TABLES:
            assert await _table_exists(scratch_superuser_engine, table), (
                f"{table} missing after re-upgrade to head"
            )

        await _assert_full_shape(scratch_superuser_engine)

        async with scratch_superuser_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT owner_user_id, opened_by, updated_at, opened_at "
                        "FROM cases WHERE id = :id"
                    ),
                    {"id": ids["case"]},
                )
            ).one()
        assert row.owner_user_id == row.opened_by, (
            "owner_user_id must be backfilled from opened_by."
        )
        assert row.updated_at == row.opened_at, (
            "updated_at must be backfilled as COALESCE(closed_at, opened_at)."
        )
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")
