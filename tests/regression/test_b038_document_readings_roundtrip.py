# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip guard for b038 (ADR-0139, ADR-0140, the Reading Desk store).

Exercises the b038 migration's reversibility: from ``head`` it downgrades
to b037 (dropping ``document_readings``, ``reading_purges`` and
``audit_trigger_function_excluding``), asserts all of it is gone, then
``upgrade head`` again and asserts it is back in the shape of ADR-0139 §2 /
§5: the column lists and their nullability, RLS enabled + forced with the
standard ``tenant_isolation`` policy, the unique constraint, the two shape
CHECKs, the three indexes, the two delete rules and the three defaults.

The audit side is ADR-0140's:

* ``document_readings`` carries the excluding function with exactly the
  three payload columns as arguments, ``reading_purges`` the generic one,
  and no other table uses the excluding function (Decision 5).
* The generic function is untouched in both directions, and the excluding
  one mirrors it: without the inserted block the two sources are equal. A
  later change to the generic function has to be mirrored; this is the
  assertion that notices.

Absences get their own assertions, because an absence is exactly what a
well-meaning later migration "completes":

1. **No ``created_at`` / ``updated_at`` on ``document_readings``.** The
   ADR's column list is the contract.
2. **No ``content`` / ``text`` / ``pdf`` column.** The document passes
   through and no text layer is stored (RD-D-2, RD-D-13).

The downgrade names its **target revision** rather than using
``downgrade -1``, so the guard stays honest for every future head. Runs
against a **per-test scratch database** (``tests/regression/conftest.py``);
if the server is unreachable the test skips.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.regression.conftest import ScratchDatabase

#: The revision immediately below b038 — i.e. b038's own ``down_revision``.
_BELOW = "b037_add_case_working_file"

_GENERIC = "audit_trigger_function"
_EXCLUDING = "audit_trigger_function_excluding"

#: The block b038 inserts into the generic function's body: eight lines,
#: the last of them empty.
_INSERTED_BLOCK = """\
    -- The one difference from audit_trigger_function() (ADR-0140): the column
    -- names passed as trigger arguments are subtracted from both row images.
    -- A NULL image stays NULL.
    FOR i IN 0 .. TG_NARGS - 1 LOOP
        old_payload := old_payload - TG_ARGV[i];
        new_payload := new_payload - TG_ARGV[i];
    END LOOP;

"""

#: ``{table: [(column, is_nullable), ...]}`` in schema order.
_EXPECTED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "reading_purges": [
        ("id", "NO"),
        ("tenant_id", "NO"),
        ("criterion", "NO"),
        ("years", "NO"),
        ("cutoff", "NO"),
        ("reading_count", "NO"),
        ("run_by", "NO"),
        ("run_at", "NO"),
    ],
    "document_readings": [
        ("id", "NO"),
        ("tenant_id", "NO"),
        ("reading_number", "NO"),
        ("filename", "NO"),
        ("sha256", "NO"),
        ("size_bytes", "NO"),
        ("page_count", "YES"),
        ("read_at", "NO"),
        ("read_by", "NO"),
        ("origin", "NO"),
        ("case_attachment_id", "YES"),
        ("document_type", "NO"),
        ("profile_version", "NO"),
        ("language", "YES"),
        ("fund_name_read", "YES"),
        ("manager_name_read", "YES"),
        ("document_date", "YES"),
        ("period_label", "YES"),
        ("currency", "YES"),
        ("investment_id", "YES"),
        ("manager_name", "YES"),
        ("assigned_by", "YES"),
        ("assigned_at", "YES"),
        ("findings", "YES"),
        ("note", "YES"),
        ("model", "NO"),
        ("prompt_version", "NO"),
        ("adhoc_keywords", "NO"),
        ("trust", "NO"),
        ("search_text", "YES"),
        ("removed_at", "YES"),
        ("removed_by", "YES"),
        ("removal_reason", "YES"),
        ("purge_id", "YES"),
    ],
}
_NEW_TABLES: tuple[str, ...] = tuple(_EXPECTED_COLUMNS)

_EXPECTED_TRIGGER_FUNCTIONS: dict[str, str] = {
    "document_readings": _EXCLUDING,
    "reading_purges": _GENERIC,
}


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


async def _columns(engine: AsyncEngine, table: str) -> list[tuple[str, str]]:
    """Return ``[(column_name, is_nullable), ...]`` in schema order."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "ORDER BY ordinal_position"
            ),
            {"t": table},
        )
        return [(row[0], row[1]) for row in result.fetchall()]


async def _column_defaults(engine: AsyncEngine, table: str) -> dict[str, str | None]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        )
        return {row[0]: row[1] for row in result.fetchall()}


async def _index_names(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t"),
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


async def _fk_delete_rule(engine: AsyncEngine, table: str, column: str) -> str:
    """Return ``confdeltype`` of the one FK on ``table.column``."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT c.confdeltype::text
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = c.conkey[1]
                WHERE n.nspname = 'public'
                  AND t.relname = :t
                  AND c.contype = 'f'
                  AND a.attname = :c
                """
            ),
            {"t": table, "c": column},
        )
        return result.scalar_one()


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


async def _function_source(engine: AsyncEngine, name: str) -> str | None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT prosrc FROM pg_proc "
                "WHERE proname = :name AND pronamespace = 'public'::regnamespace"
            ),
            {"name": name},
        )
        return result.scalar_one_or_none()


async def _triggers(engine: AsyncEngine) -> list[tuple[str, str, str, str]]:
    """Return ``(table, trigger, function, definition)`` of every non-internal trigger."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT t.relname, tg.tgname, p.proname, pg_get_triggerdef(tg.oid)
                FROM pg_trigger tg
                JOIN pg_class t ON tg.tgrelid = t.oid
                JOIN pg_proc p ON tg.tgfoid = p.oid
                WHERE NOT tg.tgisinternal
                """
            )
        )
        return [(row[0], row[1], row[2], row[3]) for row in result.fetchall()]


def _excluded_arguments(definition: str) -> list[str]:
    match = re.search(rf"EXECUTE FUNCTION {_EXCLUDING}\((.*)\)$", definition)
    assert match is not None, f"not an excluding trigger: {definition!r}"
    return re.findall(r"'([^']*)'", match.group(1))


async def _assert_full_shape(engine: AsyncEngine) -> None:
    """Assert both tables carry the ADR-0139 shape."""
    for table, expected in _EXPECTED_COLUMNS.items():
        actual = await _columns(engine, table)
        names = [name for name, _ in actual]
        expected_names = [name for name, _ in expected]
        assert set(names) == set(expected_names), (
            f"{table} column set drifted from ADR-0139: "
            f"missing={sorted(set(expected_names) - set(names))}, "
            f"extra={sorted(set(names) - set(expected_names))}"
        )
        assert names == expected_names, f"{table} column order drifted: {names}"
        assert dict(actual) == dict(expected), (
            f"{table} nullability drifted: {sorted(set(actual) ^ set(expected))}"
        )

        enabled, forced = await _rls_enabled_and_forced(engine, table)
        assert enabled and forced, (
            f"{table} must have RLS enabled AND forced after apply_tenant_rls."
        )
        assert await _has_tenant_isolation_policy(engine, table), (
            f"{table} is missing the standard tenant_isolation policy."
        )

    assert "uq_document_readings_tenant_reading_number" in await _constraint_names(
        engine, "document_readings", "u"
    ), "document_readings is missing uq_document_readings_tenant_reading_number."
    checks = await _constraint_names(engine, "document_readings", "c")
    for check in ("ck_document_readings_one_assignment", "ck_document_readings_tombstone_shape"):
        assert check in checks, f"document_readings is missing {check}."
    indexes = await _index_names(engine, "document_readings")
    for index in (
        "ix_document_readings_tenant_sha256",
        "ix_document_readings_tenant_investment",
        "ix_document_readings_tenant_document_date",
    ):
        assert index in indexes, f"document_readings is missing {index}."

    assert await _fk_delete_rule(engine, "document_readings", "investment_id") == "n", (
        "document_readings.investment_id must be ON DELETE SET NULL (RD-D-29)."
    )
    assert await _fk_delete_rule(engine, "document_readings", "purge_id") == "r", (
        "document_readings.purge_id must be ON DELETE RESTRICT."
    )

    defaults = await _column_defaults(engine, "document_readings")
    assert defaults["origin"] == "'upload'::text"
    assert defaults["trust"] == "'untrusted'::text"
    assert defaults["adhoc_keywords"] == "'[]'::jsonb"

    # Absence 1: the ADR's column list is the contract.
    for column in ("created_at", "updated_at"):
        assert column not in defaults, (
            f"document_readings must carry no {column!r}; the ADR-0139 §2 list is the contract."
        )
    # Absence 2: the document is not kept.
    for column in ("content", "text", "pdf"):
        assert column not in defaults, (
            f"document_readings must carry no {column!r}: no document and no text layer "
            "is stored (RD-D-2, RD-D-13)."
        )


async def _assert_audit_shape(engine: AsyncEngine, generic_source: str) -> None:
    """Assert the two triggers and the two functions are ADR-0140's."""
    triggers = await _triggers(engine)
    for table, function in _EXPECTED_TRIGGER_FUNCTIONS.items():
        found = [(name, fn) for tbl, name, fn, _ in triggers if tbl == table]
        assert found == [(f"{table}_audit_trigger", function)], (
            f"{table} must carry exactly one trigger, on {function}; found {found}."
        )

    excluding = [(tbl, definition) for tbl, _, fn, definition in triggers if fn == _EXCLUDING]
    assert [tbl for tbl, _ in excluding] == ["document_readings"], (
        "Only document_readings uses the excluding function (ADR-0140 Decision 5)."
    )
    assert _excluded_arguments(excluding[0][1]) == ["findings", "note", "search_text"]

    assert await _function_source(engine, _GENERIC) == generic_source, (
        "b038 must not touch audit_trigger_function() (ADR-0140 Decision 5)."
    )
    mirrored = await _function_source(engine, _EXCLUDING)
    assert mirrored is not None, f"{_EXCLUDING} is missing."
    assert mirrored.count(_INSERTED_BLOCK) == 1, "The inserted block drifted."
    assert mirrored.replace(_INSERTED_BLOCK, "") == generic_source, (
        f"{_EXCLUDING} no longer mirrors {_GENERIC}; a change to the generic "
        "function must be mirrored (ADR-0140)."
    )


async def test_b038_round_trip(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    engine = scratch_superuser_engine
    # Precondition: the fixture built the scratch database at head.
    for table in _NEW_TABLES:
        assert await _table_exists(engine, table), (
            f"{table} missing before round-trip — is the DB at head?"
        )
    assert await _function_source(engine, _EXCLUDING) is not None, f"{_EXCLUDING} missing at head."
    generic_source = await _function_source(engine, _GENERIC)
    assert generic_source is not None

    try:
        # 1) downgrade to b037: tables and excluding function gone, generic untouched.
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
        for table in _NEW_TABLES:
            assert not await _table_exists(engine, table), (
                f"{table} still present after downgrade to {_BELOW}"
            )
        assert await _function_source(engine, _EXCLUDING) is None, (
            f"{_EXCLUDING} still present after downgrade to {_BELOW}"
        )
        assert await _function_source(engine, _GENERIC) == generic_source, (
            "The downgrade must leave audit_trigger_function() as it was."
        )

        # 2) upgrade head → all of it is back, in full shape.
        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"
        await _assert_full_shape(engine)
        await _assert_audit_shape(engine, generic_source)
    finally:
        # Always restore head so a mid-test failure does not leave the schema
        # downgraded under the assertions above.
        scratch_db.alembic("upgrade", "head")
