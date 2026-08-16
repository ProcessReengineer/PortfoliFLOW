# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.tools.investment_tools`.

Two layers, mirroring the structure ADR-0047 §Tests prescribes:

1. **Context-not-set path** — no database needed. With the
   tool-execution context cleared, each of the three tools must
   return its graceful explanatory string rather than raising. This
   pins the GUI-degradation path and runs in any environment.
2. **Happy path** — DB-backed. Seeds one tenant with three
   investments (each with NAV and cashflow rows), points a
   ``ToolExecutionContext`` at the test database URL + seeded tenant,
   and asserts the three tools return correctly-formatted data. Each
   tool builds its own loop-local engine from that URL — exactly the
   production path (ADR-0047, amended) — so these tests genuinely
   connect. Unknown names and an invalid ``nav_kind`` return clear
   guidance messages. Skips cleanly when no test database is
   configured.

The DB fixtures are defined locally (not imported from
``tests._db_fixtures``) so the autouse ``reset_schema`` there does not
leak into the context-not-set tests above — they must run without a
database. This mirrors the local-fixture pattern in
``tests/web/test_chat_routes.py``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories import (
    AssetClassRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from services.tools._tool_context import (
    ToolExecutionContext,
    clear_tool_context,
    set_tool_context,
)
from services.tools.investment_tools import (
    get_investment_detail,
    get_investment_nav_history,
    list_investments,
)

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB investment-tool tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Shared: context teardown so a turn's context never leaks into the next test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_context() -> Generator[None, None, None]:
    """Clear the tool-execution context before and after every test."""
    clear_tool_context()
    yield
    clear_tool_context()


# ---------------------------------------------------------------------------
# Layer 1 — context-not-set path (no database needed)
# ---------------------------------------------------------------------------


def test_list_investments_without_context_degrades_gracefully() -> None:
    """``list_investments`` returns the explanatory string, does not raise."""
    result = list_investments()
    assert "context was not set" in result
    assert "web chat surface" in result


def test_get_investment_detail_without_context_degrades_gracefully() -> None:
    """``get_investment_detail`` returns the explanatory string, does not raise."""
    result = get_investment_detail("Anything")
    assert "context was not set" in result


def test_get_investment_nav_history_without_context_degrades_gracefully() -> None:
    """``get_investment_nav_history`` returns the explanatory string, does not raise."""
    result = get_investment_nav_history("Anything")
    assert "context was not set" in result


def test_invalid_nav_kind_without_context_still_degrades_gracefully() -> None:
    """The context check fires before the nav_kind validation."""
    result = get_investment_nav_history("Anything", nav_kind="garbage")
    assert "context was not set" in result


# ---------------------------------------------------------------------------
# Layer 2 — DB-backed happy path
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Seeded:
    """Handle returned by the ``seeded`` fixture for the DB-backed tests.

    Carries the database **URL** (not an engine) — the tools build
    their own loop-local engine from it, mirroring production.
    """

    database_url: str
    tenant_id: UUID


@pytest_asyncio.fixture
async def superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine bound to the Postgres superuser — fixture-only, for seeding."""
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine bound to the unprivileged ``portfoliflow_app`` role.

    Fixture-only — the ``seeded`` fixture uses it to insert the test
    rows. The investment tools under test do *not* use this engine:
    each builds its own short-lived, loop-local engine from the
    database URL inside the fresh ``run_async_in_fresh_loop`` event
    loop (ADR-0047, amended). NullPool so every checkout creates a
    fresh connection, keeping the seeding path clean.
    """
    _require_db()
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def reset_schema(
    superuser_engine: AsyncEngine,
) -> AsyncGenerator[None, None]:
    """Truncate the domain tables before AND after every DB-backed test."""
    truncate_sql = text(
        "TRUNCATE TABLE investment_region_weights, "
        "region_country_memberships, regions, "
        "investment_country_weights, "
        "investment_sector_weights, sectors, "
        "investment_cashflows, investment_navs, investments, "
        "saa_correlations, saa_asset_class_inputs, "
        "saa_configurations, asset_classes, "
        "data_upload_sheets, data_uploads, "
        "login_audit, sessions, audit_log, "
        "data_store_entries, users, tenants "
        "RESTART IDENTITY CASCADE"
    )
    async with superuser_engine.begin() as conn:
        await conn.execute(truncate_sql)
    try:
        yield
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(truncate_sql)


async def _seed_tenant(superuser_engine: AsyncEngine, name: str) -> UUID:
    """Insert a tenant row (superuser path) and return its id."""
    from uuid import uuid4

    new_id = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES (:id, :name, gen_random_uuid()::text)"
            ),
            {"id": str(new_id), "name": name},
        )
    return new_id


@pytest_asyncio.fixture
async def seeded(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> _Seeded:
    """Seed one tenant with three investments, NAVs, and cashflows.

    - ``Alpha Fund`` — active private_equity, full metadata, three
      actual NAVs + one plan NAV, one capital call + one distribution.
    - ``Beta Fund`` — active real_estate, sparse metadata, one actual
      NAV, no cashflows.
    - ``Gamma Fund`` — inactive listed_equity, one actual NAV.
    """
    tenant_id = await _seed_tenant(superuser_engine, "Investment-Tools Tenant")

    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="invtools@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="default_class", display_name="Default Class"
        )
        inv_repo = InvestmentRepository(session)
        nav_repo = InvestmentNavRepository(session)
        cf_repo = InvestmentCashflowRepository(session)

        alpha = await inv_repo.create(
            name="Alpha Fund",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
            manager_name="Alpha Capital",
            vintage_year=2020,
            commitment_amount=Decimal("10000000"),
        )
        beta = await inv_repo.create(
            name="Beta Fund",
            investment_type="real_estate",
            asset_class_id=asset_class.id,
            currency="USD",
            created_by=actor.id,
        )
        gamma = await inv_repo.create(
            name="Gamma Fund",
            investment_type="listed_equity",
            asset_class_id=asset_class.id,
            currency="GBP",
            created_by=actor.id,
            is_active=False,
        )

        # Alpha: three actual NAVs + one plan NAV.
        for d, value in (
            (date(2021, 12, 31), Decimal("3000000")),
            (date(2022, 12, 31), Decimal("6000000")),
            (date(2023, 12, 31), Decimal("8500000")),
        ):
            await nav_repo.upsert(
                investment_id=alpha.id,
                as_of_date=d,
                nav_kind="actual",
                nav_value=value,
                currency="EUR",
                source="test-seed",
                created_by=actor.id,
            )
        await nav_repo.upsert(
            investment_id=alpha.id,
            as_of_date=date(2024, 12, 31),
            nav_kind="plan",
            nav_value=Decimal("11000000"),
            currency="EUR",
            source="test-seed",
            created_by=actor.id,
        )

        # Alpha: one capital call (negative) + one distribution (positive).
        await cf_repo.create(
            investment_id=alpha.id,
            flow_timestamp=datetime(2021, 3, 15, 12, 0, tzinfo=timezone.utc),
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-2000000"),
            currency="EUR",
            description="First drawdown",
            created_by=actor.id,
        )
        await cf_repo.create(
            investment_id=alpha.id,
            flow_timestamp=datetime(2023, 6, 30, 12, 0, tzinfo=timezone.utc),
            flow_type="distribution",
            flow_kind="actual",
            amount=Decimal("1500000"),
            currency="EUR",
            description="Partial realisation",
            created_by=actor.id,
        )

        # Beta: a single actual NAV, no cashflows.
        await nav_repo.upsert(
            investment_id=beta.id,
            as_of_date=date(2023, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("4200000"),
            currency="USD",
            source="test-seed",
            created_by=actor.id,
        )

        # Gamma (inactive): a single actual NAV.
        await nav_repo.upsert(
            investment_id=gamma.id,
            as_of_date=date(2023, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("750000"),
            currency="GBP",
            source="test-seed",
            created_by=actor.id,
        )

    return _Seeded(database_url=DATABASE_URL, tenant_id=tenant_id)


def _set_context(seeded: _Seeded) -> None:
    """Point the module-level tool-execution context at the seeded tenant."""
    set_tool_context(
        ToolExecutionContext(tenant_id=seeded.tenant_id, database_url=seeded.database_url)
    )


async def test_list_investments_returns_all_seeded(seeded: _Seeded) -> None:
    """All three seeded investments appear, with the count header and flags."""
    _set_context(seeded)
    result = list_investments()

    assert "3 investment(s)" in result
    assert "Alpha Fund" in result
    assert "Beta Fund" in result
    assert "Gamma Fund" in result
    # Catalogue fields surfaced.
    assert "private_equity" in result
    assert "Alpha Capital" in result
    assert "vintage=2020" in result
    assert "10,000,000.00" in result
    # The inactive investment is flagged; the active ones are not.
    assert "Gamma Fund (inactive)" in result
    assert "Alpha Fund (inactive)" not in result
    # Sparse metadata renders as em-dashes rather than blowing up.
    assert "manager=—" in result


async def test_tool_succeeds_when_called_twice_in_a_row(
    seeded: _Seeded,
) -> None:
    """Calling a tool twice in one session must both succeed.

    Regression guard for the cross-loop engine bug (ADR-0047,
    amended): each call builds and disposes its own loop-local engine
    inside the fresh ``run_async_in_fresh_loop`` event loop. A leaked
    or undisposed engine, or an engine shared across the thread/loop
    boundary, would surface as a ``RuntimeError`` ("got Future
    attached to a different loop") on the second call.
    """
    _set_context(seeded)
    first = list_investments()
    second = list_investments()

    assert "3 investment(s)" in first
    assert "3 investment(s)" in second


async def test_get_investment_detail_returns_catalogue_and_summaries(
    seeded: _Seeded,
) -> None:
    """The detail block carries catalogue fields + NAV + cashflow summaries."""
    _set_context(seeded)
    result = get_investment_detail("Alpha Fund")

    assert "Investment: Alpha Fund" in result
    assert "Type: private_equity" in result
    assert "Manager: Alpha Capital" in result
    assert "Vintage year: 2020" in result
    # NAV summary: 4 rows (3 actual + 1 plan), latest actual is the 2023 one.
    assert "NAVs: 4 row(s)" in result
    assert "Latest actual NAV: 8,500,000.00 EUR on 2023-12-31" in result
    # Cashflow summary: 2 rows, signed actual totals.
    assert "Cashflows: 2 row(s)" in result
    assert "Actual inflows: 1,500,000.00" in result
    assert "actual outflows: -2,000,000.00" in result


async def test_get_investment_detail_handles_no_nav_no_cashflow(
    seeded: _Seeded,
) -> None:
    """Beta has one NAV and no cashflows — the summaries say so plainly."""
    _set_context(seeded)
    result = get_investment_detail("Beta Fund")

    assert "Investment: Beta Fund" in result
    assert "Manager: —" in result
    assert "NAVs: 1 row(s)" in result
    assert "Cashflows: none recorded" in result


async def test_get_investment_detail_unknown_name(seeded: _Seeded) -> None:
    """An unknown name returns clear guidance, not an error."""
    _set_context(seeded)
    result = get_investment_detail("Nonexistent Fund")

    assert "No investment named 'Nonexistent Fund'" in result
    assert "list_investments" in result


async def test_get_investment_nav_history_returns_all_kinds(
    seeded: _Seeded,
) -> None:
    """Without a filter, both actual and plan NAV rows are returned, sorted."""
    _set_context(seeded)
    result = get_investment_nav_history("Alpha Fund")

    assert "NAV history for 'Alpha Fund' (all kinds) — 4 row(s)" in result
    assert "2021-12-31" in result
    assert "2024-12-31" in result
    assert "plan" in result
    assert "actual" in result
    # Ascending by date — the earliest row precedes the latest.
    assert result.index("2021-12-31") < result.index("2024-12-31")


async def test_get_investment_nav_history_filters_by_kind(
    seeded: _Seeded,
) -> None:
    """``nav_kind='actual'`` excludes the plan row."""
    _set_context(seeded)
    result = get_investment_nav_history("Alpha Fund", nav_kind="actual")

    assert "(actual) — 3 row(s)" in result
    assert "2024-12-31" not in result  # the plan row is excluded


async def test_get_investment_nav_history_invalid_kind(
    seeded: _Seeded,
) -> None:
    """An invalid ``nav_kind`` returns the validation message."""
    _set_context(seeded)
    result = get_investment_nav_history("Alpha Fund", nav_kind="garbage")

    assert "Invalid nav_kind 'garbage'" in result
    assert "'actual'" in result and "'plan'" in result


async def test_get_investment_nav_history_unknown_name(
    seeded: _Seeded,
) -> None:
    """An unknown name returns clear guidance, not an error."""
    _set_context(seeded)
    result = get_investment_nav_history("Nonexistent Fund")

    assert "No investment named 'Nonexistent Fund'" in result


async def test_list_investments_empty_tenant(
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> None:
    """A tenant with no investments gets the explanatory empty message.

    No ``app_engine`` fixture needed — ``list_investments`` builds its
    own loop-local engine from the URL in the context (ADR-0047,
    amended).
    """
    empty_tenant = await _seed_tenant(superuser_engine, "Empty Tenant")
    set_tool_context(ToolExecutionContext(tenant_id=empty_tenant, database_url=DATABASE_URL))
    result = list_investments()

    assert "No investments are present" in result
    assert "Data Import" in result
