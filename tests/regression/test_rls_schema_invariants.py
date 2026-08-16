# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard for RLS schema invariants.

Walks ``pg_class`` and ``pg_policies`` after migrations have been
applied and asserts the invariants ADR-0035 requires:

- Every domain table (i.e. every ``relkind = 'r'`` entry in the
  ``public`` schema except Alembic's bookkeeping table) has
  ``relrowsecurity = true`` AND ``relforcerowsecurity = true``.
- Every such table has at least one policy attached.

A future migration that creates a domain table and forgets
``ENABLE / FORCE ROW LEVEL SECURITY`` or the standard policy will fail
this guard immediately, instead of silently producing a cross-tenant
data leak.

The companion guard ``test_portfoliflow_app_role_does_not_bypass_rls``
asserts that the application role cannot accidentally short-circuit
RLS via ``BYPASSRLS`` or ``SUPERUSER``.

These tests run against the live compose Postgres. If the DB is
unreachable, they skip rather than fail (so contributors without
Podman can still run the rest of the suite).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# .env may not yet be loaded in this collection path — load it
# explicitly here so DATABASE_URL_SUPERUSER is visible.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

# Tables created by tooling that are NOT subject to RLS by design.
# Alembic's bookkeeping has no tenant axis at all; ``countries`` is a
# global ISO 3166-1 alpha-2 stammtabelle (every tenant reads the same
# rows, see ADR-0045 §2); ``anlv_categories`` is the global AnlV
# regulatory catalogue (every tenant reads the same § 2 Abs. 1
# numbered categories, see ADR-0057 §Schema). All three are
# explicitly allow-listed so the RLS-presence guards below skip
# them. Adding a new entry here MUST be accompanied by an ADR that
# justifies the global semantics.
_NON_RLS_TABLES: frozenset[str] = frozenset({"alembic_version", "countries", "anlv_categories"})
# Backward-compatible alias retained for older tests / external readers.
NON_DOMAIN_TABLES: frozenset[str] = _NON_RLS_TABLES


@pytest_asyncio.fixture
async def superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    if not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL_SUPERUSER not set; cannot run RLS schema guards.",
            allow_module_level=False,
        )
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(
            f"Cannot reach Postgres at {DATABASE_URL_SUPERUSER!r}: {exc}.",
            allow_module_level=False,
        )
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_every_domain_table_has_rls_enabled(
    superuser_engine: AsyncEngine,
) -> None:
    """ADR-0035 §2: ENABLE + FORCE ROW LEVEL SECURITY on every domain table."""
    async with superuser_engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT c.relname,
                       c.relrowsecurity   AS rls_enabled,
                       c.relforcerowsecurity AS rls_forced
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND c.relkind = 'r'
                ORDER BY c.relname
                """
            )
        )
        rows = result.mappings().all()

    domain_tables = [r for r in rows if r["relname"] not in NON_DOMAIN_TABLES]
    assert domain_tables, "No domain tables found in public schema. Have migrations been applied?"

    missing_rls = [r["relname"] for r in domain_tables if not r["rls_enabled"]]
    assert not missing_rls, (
        f"Domain tables without RLS enabled: {missing_rls}. "
        f"Add `ALTER TABLE <name> ENABLE ROW LEVEL SECURITY` in the migration."
    )

    missing_force = [r["relname"] for r in domain_tables if not r["rls_forced"]]
    assert not missing_force, (
        f"Domain tables without FORCE RLS: {missing_force}. "
        f"Add `ALTER TABLE <name> FORCE ROW LEVEL SECURITY` in the migration."
    )


async def test_every_domain_table_has_at_least_one_policy(
    superuser_engine: AsyncEngine,
) -> None:
    """ADR-0035 §2: a forgotten policy would silently block all access."""
    async with superuser_engine.connect() as conn:
        tables_result = await conn.execute(
            text(
                """
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND c.relkind = 'r'
                """
            )
        )
        all_tables = {row[0] for row in tables_result.fetchall()}

        policy_result = await conn.execute(
            text(
                """
                SELECT tablename, COUNT(*) AS policy_count
                FROM pg_policies
                WHERE schemaname = 'public'
                GROUP BY tablename
                """
            )
        )
        policies_per_table = {row[0]: row[1] for row in policy_result.fetchall()}

    domain_tables = all_tables - NON_DOMAIN_TABLES
    tables_without_policy = sorted(t for t in domain_tables if t not in policies_per_table)
    assert not tables_without_policy, (
        f"Tables with RLS enabled but no policy: {tables_without_policy}. "
        f"A forgotten CREATE POLICY would silently block all access from "
        f"the application role. Use the apply_tenant_rls helper or add a "
        f"hand-written policy with an ADR."
    )


async def test_portfoliflow_app_role_does_not_bypass_rls(
    superuser_engine: AsyncEngine,
) -> None:
    """ADR-0035: the application role must NOT bypass RLS or be superuser."""
    async with superuser_engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT rolname, rolbypassrls, rolsuper
                FROM pg_roles
                WHERE rolname = 'portfoliflow_app'
                """
            )
        )
        role = result.mappings().one_or_none()

    assert role is not None, (
        "portfoliflow_app role is missing. db/init/01-create-app-role.sql "
        "should have created it on first container start. Run "
        "`podman compose down -v && podman compose up -d` to re-run init."
    )
    assert not role["rolbypassrls"], (
        "portfoliflow_app must NOT have BYPASSRLS — the whole point of RLS "
        "evaluating in tests is that the test role behaves like the prod role."
    )
    assert not role["rolsuper"], (
        "portfoliflow_app must NOT be a SUPERUSER — superusers also bypass RLS."
    )


@pytest.mark.parametrize("table", ["sessions", "login_audit"])
async def test_phase_2b_auth_tables_have_rls(superuser_engine: AsyncEngine, table: str) -> None:
    """Sub-stream 2b: ``sessions`` and ``login_audit`` are RLS-policed.

    Both tables are created by migration b003. The standard
    ``apply_tenant_rls`` helper covers ``sessions`` directly;
    ``login_audit`` carries a custom policy that permits NULL-tenant
    inserts but the relrowsecurity / relforcerowsecurity flags are
    still ``true`` and the table has at least one policy attached.
    """
    async with superuser_engine.connect() as conn:
        flags_row = await conn.execute(
            text(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND c.relname  = :table
                """
            ),
            {"table": table},
        )
        flags = flags_row.mappings().one()

        policies_row = await conn.execute(
            text(
                """
                SELECT COUNT(*) FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename  = :table
                """
            ),
            {"table": table},
        )
        policy_count = int(policies_row.scalar_one())

    assert flags["relrowsecurity"], f"{table}: ENABLE ROW LEVEL SECURITY missing"
    assert flags["relforcerowsecurity"], f"{table}: FORCE ROW LEVEL SECURITY missing"
    assert policy_count >= 1, f"{table}: no RLS policy attached"


@pytest.mark.parametrize("table", ["data_uploads", "data_upload_sheets"])
async def test_phase_2d_data_upload_tables_have_rls(
    superuser_engine: AsyncEngine, table: str
) -> None:
    """Sub-stream 2d: ``data_uploads`` / ``data_upload_sheets`` are RLS-policed.

    Both tables are created by migration b004 and apply the standard
    ``apply_tenant_rls`` helper. ``data_uploads`` additionally carries
    a restrictive ``uploaded_by``-self policy; the per-table policy
    count is therefore ≥ 2 for ``data_uploads`` and ≥ 1 for
    ``data_upload_sheets``.
    """
    async with superuser_engine.connect() as conn:
        flags_row = await conn.execute(
            text(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND c.relname  = :table
                """
            ),
            {"table": table},
        )
        flags = flags_row.mappings().one()

        policies_row = await conn.execute(
            text(
                """
                SELECT COUNT(*) FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename  = :table
                """
            ),
            {"table": table},
        )
        policy_count = int(policies_row.scalar_one())

    assert flags["relrowsecurity"], f"{table}: ENABLE ROW LEVEL SECURITY missing"
    assert flags["relforcerowsecurity"], f"{table}: FORCE ROW LEVEL SECURITY missing"
    assert policy_count >= 1, f"{table}: no RLS policy attached"


@pytest.mark.parametrize(
    "table",
    [
        "asset_classes",
        "saa_configurations",
        "saa_asset_class_inputs",
        "saa_correlations",
    ],
)
async def test_phase_3b_saa_tables_have_rls(superuser_engine: AsyncEngine, table: str) -> None:
    """Sub-stream 3b: the four SAA tables are RLS-policed.

    All four tables are created by migration b005 (per ADR-0042 §1)
    and apply the standard ``apply_tenant_rls`` helper. Each must
    carry both row-security flags and at least one policy. The
    audit trigger is verified separately by service tests; here we
    focus on the row-locality invariant ADR-0035 §2 mandates.
    """
    async with superuser_engine.connect() as conn:
        flags_row = await conn.execute(
            text(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND c.relname  = :table
                """
            ),
            {"table": table},
        )
        flags = flags_row.mappings().one()

        policies_row = await conn.execute(
            text(
                """
                SELECT COUNT(*) FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename  = :table
                """
            ),
            {"table": table},
        )
        policy_count = int(policies_row.scalar_one())

    assert flags["relrowsecurity"], f"{table}: ENABLE ROW LEVEL SECURITY missing"
    assert flags["relforcerowsecurity"], f"{table}: FORCE ROW LEVEL SECURITY missing"
    assert policy_count >= 1, f"{table}: no RLS policy attached"


@pytest.mark.parametrize(
    "table",
    [
        "sectors",
        "investment_country_weights",
        "investment_sector_weights",
        "regions",
        "region_country_memberships",
        "investment_region_weights",
    ],
)
async def test_phase_5a_sector_country_tables_have_rls(
    superuser_engine: AsyncEngine, table: str
) -> None:
    """Sub-stream 5a + Phase-6 region-model tables are RLS-policed.

    The ``countries`` table — intentionally global — is covered by
    ``test_countries_table_is_globally_readable_no_rls`` below. The
    three Phase-5a and three Phase-6 tenant-scoped tables apply the
    standard ``apply_tenant_rls`` helper.
    """
    async with superuser_engine.connect() as conn:
        flags_row = await conn.execute(
            text(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND c.relname  = :table
                """
            ),
            {"table": table},
        )
        flags = flags_row.mappings().one()

        policies_row = await conn.execute(
            text(
                """
                SELECT COUNT(*) FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename  = :table
                """
            ),
            {"table": table},
        )
        policy_count = int(policies_row.scalar_one())

    assert flags["relrowsecurity"], f"{table}: ENABLE ROW LEVEL SECURITY missing"
    assert flags["relforcerowsecurity"], f"{table}: FORCE ROW LEVEL SECURITY missing"
    assert policy_count >= 1, f"{table}: no RLS policy attached"


async def test_countries_table_is_globally_readable_no_rls(
    superuser_engine: AsyncEngine,
) -> None:
    """ADR-0045 §2: ``countries`` is a global lookup table without RLS.

    The table appears in :data:`_NON_RLS_TABLES` and the row-security
    flags MUST be ``FALSE`` (otherwise the application role would
    get filtered to nothing because the rows carry no ``tenant_id``).
    No tenant_isolation policy may be attached.
    """
    async with superuser_engine.connect() as conn:
        flags_row = await conn.execute(
            text(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND c.relname  = 'countries'
                """
            )
        )
        flags = flags_row.mappings().one_or_none()

        policies_row = await conn.execute(
            text(
                """
                SELECT COUNT(*) FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename  = 'countries'
                """
            )
        )
        policy_count = int(policies_row.scalar_one())

    assert flags is not None, "countries table is missing — has migration b007 been applied?"
    assert not flags["relrowsecurity"], (
        "countries: ENABLE ROW LEVEL SECURITY must be OFF for a global "
        "lookup table (would filter every read to empty since the rows "
        "have no tenant_id)."
    )
    assert not flags["relforcerowsecurity"], (
        "countries: FORCE ROW LEVEL SECURITY must be OFF for a global lookup table."
    )
    assert policy_count == 0, (
        f"countries: expected 0 policies, found {policy_count}. The "
        "table is global; no tenant_isolation policy applies."
    )
    assert "countries" in _NON_RLS_TABLES, (
        "_NON_RLS_TABLES allow-list must contain 'countries' so the RLS-presence guards skip it."
    )


@pytest.mark.parametrize(
    "table",
    [
        "limit_sets",
        "limits",
    ],
)
async def test_phase_7_anlagegrenzen_tables_have_rls(
    superuser_engine: AsyncEngine, table: str
) -> None:
    """Phase-7 Anlagegrenzen tenant-scoped tables are RLS-policed (b010).

    Per ADR-0055, ADR-0056, ADR-0035 §3 the three new tables apply
    ``apply_tenant_rls`` and the audit trigger. ``limits.tenant_id``
    is denormalised from ``limit_sets.tenant_id`` for row-local RLS
    evaluation.
    """
    async with superuser_engine.connect() as conn:
        flags_row = await conn.execute(
            text(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND c.relname  = :table
                """
            ),
            {"table": table},
        )
        flags = flags_row.mappings().one()

        policies_row = await conn.execute(
            text(
                """
                SELECT COUNT(*) FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename  = :table
                """
            ),
            {"table": table},
        )
        policy_count = int(policies_row.scalar_one())

    assert flags["relrowsecurity"], f"{table}: ENABLE ROW LEVEL SECURITY missing"
    assert flags["relforcerowsecurity"], f"{table}: FORCE ROW LEVEL SECURITY missing"
    assert policy_count >= 1, f"{table}: no RLS policy attached"


async def test_anlv_categories_table_is_globally_readable_no_rls(
    superuser_engine: AsyncEngine,
) -> None:
    """ADR-0057 §Schema: ``anlv_categories`` is a global lookup table.

    Same exception as ``countries``: every tenant reads the same
    AnlV § 2 Abs. 1 categories. RLS flags must be ``FALSE`` (otherwise
    the application role would get filtered to empty) and no policy
    may be attached.
    """
    async with superuser_engine.connect() as conn:
        flags_row = await conn.execute(
            text(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND c.relname  = 'anlv_categories'
                """
            )
        )
        flags = flags_row.mappings().one_or_none()

        policies_row = await conn.execute(
            text(
                """
                SELECT COUNT(*) FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename  = 'anlv_categories'
                """
            )
        )
        policy_count = int(policies_row.scalar_one())

    assert flags is not None, "anlv_categories table is missing — has migration b010 been applied?"
    assert not flags["relrowsecurity"], (
        "anlv_categories: ENABLE ROW LEVEL SECURITY must be OFF (global table)."
    )
    assert not flags["relforcerowsecurity"], (
        "anlv_categories: FORCE ROW LEVEL SECURITY must be OFF (global table)."
    )
    assert policy_count == 0, f"anlv_categories: expected 0 policies, found {policy_count}."
    assert "anlv_categories" in _NON_RLS_TABLES, (
        "_NON_RLS_TABLES allow-list must contain 'anlv_categories'."
    )


@pytest.mark.parametrize(
    "table",
    [
        "investment_bond_analytics",
        "investment_rating_weight",
        "investment_maturity_weight",
    ],
)
async def test_adr_0079_liquid_archetype_tables_have_rls(
    superuser_engine: AsyncEngine, table: str
) -> None:
    """ADR-0079 §2: the three liquid-archetype tables are RLS-policed.

    All three tables are created by migration b016 and apply the
    standard ``apply_tenant_rls`` helper. Each must carry both
    row-security flags and at least one policy. They denormalise
    ``tenant_id`` from ``investments`` for row-local RLS evaluation
    per ADR-0035 §3.
    """
    async with superuser_engine.connect() as conn:
        flags_row = await conn.execute(
            text(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND c.relname  = :table
                """
            ),
            {"table": table},
        )
        flags = flags_row.mappings().one()

        policies_row = await conn.execute(
            text(
                """
                SELECT COUNT(*) FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename  = :table
                """
            ),
            {"table": table},
        )
        policy_count = int(policies_row.scalar_one())

    assert flags["relrowsecurity"], f"{table}: ENABLE ROW LEVEL SECURITY missing"
    assert flags["relforcerowsecurity"], f"{table}: FORCE ROW LEVEL SECURITY missing"
    assert policy_count >= 1, f"{table}: no RLS policy attached"


@pytest.mark.parametrize(
    ("table", "new_uq", "old_uq"),
    [
        (
            "investment_sector_weights",
            "uq_investment_sector_weights_investment_date_sector",
            "uq_investment_sector_weights_investment_sector",
        ),
        (
            "investment_region_weights",
            "uq_investment_region_weights_investment_date_region",
            "uq_investment_region_weights_inv_region_unique",
        ),
        (
            "investment_country_weights",
            "uq_investment_country_weights_investment_date_country",
            "uq_investment_country_weights_investment_country",
        ),
    ],
)
async def test_adr_0080_composition_weights_have_historised_unique_key(
    superuser_engine: AsyncEngine, table: str, new_uq: str, old_uq: str
) -> None:
    """ADR-0080 §1: the three-column natural key replaced the old one.

    Each composition-weight table must now carry the new
    ``(investment_id, as_of_date, <dim>)`` unique constraint and must
    NOT carry its old point-in-time two-column constraint. The
    ``basis`` column must also be NOT NULL (no NULL-as-reported
    fallback — every weight row states its provenance).
    """
    async with superuser_engine.connect() as conn:
        constraints_row = await conn.execute(
            text(
                """
                SELECT c.conname
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN pg_namespace n ON t.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND t.relname = :table
                  AND c.contype = 'u'
                """
            ),
            {"table": table},
        )
        unique_constraints = {row[0] for row in constraints_row.fetchall()}

        basis_nullable_row = await conn.execute(
            text(
                """
                SELECT is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = :table
                  AND column_name = 'basis'
                """
            ),
            {"table": table},
        )
        basis_nullable = basis_nullable_row.scalar_one_or_none()

    assert new_uq in unique_constraints, (
        f"{table}: the historised unique constraint {new_uq!r} is missing. "
        "Has migration b017 been applied?"
    )
    assert old_uq not in unique_constraints, (
        f"{table}: the old point-in-time unique constraint {old_uq!r} must "
        "be dropped by migration b017."
    )
    assert basis_nullable == "NO", f"{table}: the basis column must be NOT NULL per ADR-0080 §1."


@pytest.mark.parametrize(
    "table",
    [
        "position_transactions",
        "instrument_prices",
    ],
)
async def test_adr_0097_position_model_tables_have_rls(
    superuser_engine: AsyncEngine, table: str
) -> None:
    """ADR-0097 §2/§3: the two position-model tables are RLS-policed (b024).

    Both tables are created by migration b024 and apply the standard
    ``apply_tenant_rls`` helper. Each must carry both row-security flags
    and at least one policy. They denormalise ``tenant_id`` from
    ``investments`` for row-local RLS evaluation per ADR-0035 §3.
    """
    async with superuser_engine.connect() as conn:
        flags_row = await conn.execute(
            text(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND c.relname  = :table
                """
            ),
            {"table": table},
        )
        flags = flags_row.mappings().one()

        policies_row = await conn.execute(
            text(
                """
                SELECT COUNT(*) FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename  = :table
                """
            ),
            {"table": table},
        )
        policy_count = int(policies_row.scalar_one())

    assert flags["relrowsecurity"], f"{table}: ENABLE ROW LEVEL SECURITY missing"
    assert flags["relforcerowsecurity"], f"{table}: FORCE ROW LEVEL SECURITY missing"
    assert policy_count >= 1, f"{table}: no RLS policy attached"


@pytest.mark.parametrize(
    "table",
    [
        "investments",
        "investment_navs",
        "investment_cashflows",
    ],
)
async def test_phase_4a_investment_tables_have_rls(
    superuser_engine: AsyncEngine, table: str
) -> None:
    """Sub-stream 4a: the three Investment-domain tables are RLS-policed.

    All three tables are created by migration b006 (per ADR-0043 §1)
    and apply the standard ``apply_tenant_rls`` helper. Each must
    carry both row-security flags and at least one policy. The
    audit-trigger fires-with-tenant-and-user invariant is verified
    in ``tests/repositories/test_investment_audit_and_isolation.py``;
    here we focus on the row-locality invariant ADR-0035 §2 mandates.
    """
    async with superuser_engine.connect() as conn:
        flags_row = await conn.execute(
            text(
                """
                SELECT c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON c.relnamespace = n.oid
                WHERE n.nspname = 'public'
                  AND c.relname  = :table
                """
            ),
            {"table": table},
        )
        flags = flags_row.mappings().one()

        policies_row = await conn.execute(
            text(
                """
                SELECT COUNT(*) FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename  = :table
                """
            ),
            {"table": table},
        )
        policy_count = int(policies_row.scalar_one())

    assert flags["relrowsecurity"], f"{table}: ENABLE ROW LEVEL SECURITY missing"
    assert flags["relforcerowsecurity"], f"{table}: FORCE ROW LEVEL SECURITY missing"
    assert policy_count >= 1, f"{table}: no RLS policy attached"
