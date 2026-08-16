# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migration round-trip + backfill guard for b021 (ADR-0092).

Exercises the b021 migration's reversibility and its definite backfill:

* from ``head`` it downgrades to b020 (dropping ``ingest_origin`` from all
  seven ingested tables and ``source`` from ``investment_cashflows``),
  asserts those artefacts are gone, then ``upgrade head`` again and asserts
  they are back;
* **backfill proof:** a row inserted while the DB is at b020 (before the
  column existed) reads ``ingest_origin = 'excel'`` after the re-upgrade —
  the definite backfill ADR-0092 mandates.

The downgrade names its **target revision** rather than using
``downgrade -1``: ``-1`` is relative to whatever the DB's current head is,
so a relative downgrade silently stops undoing *this* migration the moment a
newer one lands — it then asserts against someone else's schema. Naming
b021's own ``down_revision`` keeps the guard honest for every future head.

The test always restores the DB to ``head`` and removes the rows it seeded.

Runs against a **per-test scratch database**: the ``scratch_db`` fixture
creates one, migrates it to ``head`` and drops it again afterwards, so this
guard's downgrade never reaches the shared dev database (see
``tests/regression/conftest.py`` for why that matters). The Alembic CLI still
runs in a fresh subprocess, so it does not contend with this test's own
connection. If the server is unreachable the test skips, matching the other
live-DB regression guards.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.regression.conftest import ScratchDatabase

#: The revision immediately below b021 — i.e. b021's own ``down_revision``.
_BELOW = "b020_add_investment_identifiers"

_INGEST_ORIGIN_TABLES = (
    "investment_navs",
    "investment_cashflows",
    "investment_region_weights",
    "investment_country_weights",
    "investment_sector_weights",
    "investment_rating_weight",
    "investment_maturity_weight",
)


async def _column_exists(engine: AsyncEngine, table: str, column: str) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = :table
                  AND column_name = :column
                """
            ),
            {"table": table, "column": column},
        )
        return result.scalar_one_or_none() is not None


async def _seed_nav_chain_at_b020(engine: AsyncEngine) -> dict[str, str]:
    """Insert a minimal tenant→…→NAV chain while at b020.

    At b020 the ``ingest_origin`` column does not exist, so the NAV insert
    omits it — exactly the pre-b021 state whose backfill we assert. Runs as
    superuser (RLS bypassed). Returns the ids for later cleanup.
    """
    ids = {k: str(uuid4()) for k in ("tenant", "user", "ac", "inv", "nav")}
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants (id, name, subdomain) VALUES (:id, 'b021 RT', :sub)"),
            {"id": ids["tenant"], "sub": f"b021-rt-{ids['tenant'][:12]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash) "
                "VALUES (:id, :t, :email, :pw)"
            ),
            {
                "id": ids["user"],
                "t": ids["tenant"],
                "email": f"b021-{ids['user'][:8]}@example.com",
                "pw": "x" * 16,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO asset_classes (id, tenant_id, code, display_name) "
                "VALUES (:id, :t, 'rt_class', 'RT Class')"
            ),
            {"id": ids["ac"], "t": ids["tenant"]},
        )
        await conn.execute(
            text(
                "INSERT INTO investments "
                "(id, tenant_id, name, investment_type, asset_class_id, "
                " currency, created_by) "
                "VALUES (:id, :t, 'RT Fund', 'private_equity', :ac, "
                "'EUR', :u)"
            ),
            {
                "id": ids["inv"],
                "t": ids["tenant"],
                "ac": ids["ac"],
                "u": ids["user"],
            },
        )
        # NOTE: no ingest_origin column — it does not exist at b020.
        await conn.execute(
            text(
                "INSERT INTO investment_navs "
                "(id, tenant_id, investment_id, as_of_date, nav_value, "
                " currency, nav_kind, created_by) "
                "VALUES (:id, :t, :inv, DATE '2024-12-31', 100, 'EUR', "
                "'actual', :u)"
            ),
            {
                "id": ids["nav"],
                "t": ids["tenant"],
                "inv": ids["inv"],
                "u": ids["user"],
            },
        )
    return ids


async def _cleanup_chain(engine: AsyncEngine, ids: dict[str, str]) -> None:
    async with engine.begin() as conn:
        # FK order: nav → investment → asset_class → user, then the
        # audit_log rows the INSERT/DELETE triggers wrote (they FK the
        # tenant), then the tenant itself last.
        await conn.execute(
            text("DELETE FROM investment_navs WHERE id = :id"),
            {"id": ids["nav"]},
        )
        await conn.execute(text("DELETE FROM investments WHERE id = :id"), {"id": ids["inv"]})
        await conn.execute(text("DELETE FROM asset_classes WHERE id = :id"), {"id": ids["ac"]})
        await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": ids["user"]})
        # Clear the audit trail this seed generated (references the tenant).
        await conn.execute(
            text("DELETE FROM audit_log WHERE tenant_id = :t"),
            {"t": ids["tenant"]},
        )
        await conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": ids["tenant"]})


async def test_b021_round_trip_and_excel_backfill(
    scratch_db: ScratchDatabase,
    scratch_superuser_engine: AsyncEngine,
) -> None:
    # Precondition: the fixture built the scratch database at head.
    for table in _INGEST_ORIGIN_TABLES:
        assert await _column_exists(scratch_superuser_engine, table, "ingest_origin"), (
            f"{table}.ingest_origin missing before round-trip — is the DB at head?"
        )
    assert await _column_exists(scratch_superuser_engine, "investment_cashflows", "source"), (
        "investment_cashflows.source missing before round-trip."
    )

    seeded: dict[str, str] | None = None
    try:
        # 1) downgrade to b020: the columns are gone.
        down = scratch_db.alembic("downgrade", _BELOW)
        assert down.returncode == 0, f"downgrade failed:\n{down.stderr}"
        for table in _INGEST_ORIGIN_TABLES:
            assert not await _column_exists(scratch_superuser_engine, table, "ingest_origin"), (
                f"{table}.ingest_origin still present after downgrade to {_BELOW}"
            )
        assert not await _column_exists(
            scratch_superuser_engine, "investment_cashflows", "source"
        ), f"investment_cashflows.source still present after downgrade to {_BELOW}"

        # 2) Seed a NAV row while the column does not exist (pre-b021 state).
        seeded = await _seed_nav_chain_at_b020(scratch_superuser_engine)

        # 3) upgrade head → the columns are back and the pre-existing row
        #    backfilled to 'excel'.
        up = scratch_db.alembic("upgrade", "head")
        assert up.returncode == 0, f"re-upgrade failed:\n{up.stderr}"
        for table in _INGEST_ORIGIN_TABLES:
            assert await _column_exists(scratch_superuser_engine, table, "ingest_origin"), (
                f"{table}.ingest_origin missing after re-upgrade to head"
            )
        assert await _column_exists(scratch_superuser_engine, "investment_cashflows", "source"), (
            "investment_cashflows.source missing after re-upgrade to head"
        )

        async with scratch_superuser_engine.connect() as conn:
            origin = await conn.execute(
                text("SELECT ingest_origin FROM investment_navs WHERE id = :id"),
                {"id": seeded["nav"]},
            )
            assert origin.scalar_one() == "excel", (
                "b021 backfill: a row that existed before the migration must "
                "read ingest_origin='excel'."
            )
    finally:
        # Always restore head (so a mid-test failure does not leave the schema
        # downgraded under the assertions above) and remove seeded rows.
        scratch_db.alembic("upgrade", "head")
        if seeded is not None:
            await _cleanup_chain(scratch_superuser_engine, seeded)
