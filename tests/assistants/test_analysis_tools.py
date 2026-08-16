# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`services.tools.analysis_tools` (ADR-0069).

Layers, mirroring ``tests/assistants/test_investment_tools.py``:

1. **Context-not-set path** — no database. With the tool-execution
   context cleared, each of the three tools returns its graceful
   explanatory string rather than raising (the GUI-degradation path).
2. **Registration / contract** — the three tools register as
   ``READ_INTERNAL`` and appear in ``get_tool_definitions``; the
   regression guard pins that ``get_limit_coverage`` exposes **no
   projection / call-amount / horizon parameter** (ADR-0069 Non-Goals).
3. **SAA chart envelope** — the by-handle contract (ADR-0048): the SAA
   tool, given a series, appends a data handle whose stashed envelope
   carries ``"__data__": "saa_hypothetical"`` and the three
   ``series_name`` values. Exercised without a DB by monkeypatching the
   async bridge to return a synthetic bundle, plus a pure-function test
   of the envelope builder.
4. **DB-backed happy path** — seeds a tenant and asserts each tool
   reads real data end-to-end (building its own loop-local engine from
   the context URL, exactly the production path). Skips cleanly when no
   test database is configured.

The DB fixtures are defined locally (not imported from
``tests._db_fixtures``) so the autouse ``reset_schema`` there does not
leak into the no-database tests above. This mirrors the local-fixture
pattern in ``tests/assistants/test_investment_tools.py``.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pandas as pd
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
    LimitsRepository,
    SAAAssetClassInputRepository,
    SAAConfigurationRepository,
    SAACorrelationRepository,
    UserRepository,
    tenant_context,
)
from services.analytics.benchmark_comparison import SAAHypotheticalSeries
from services.benchmark_comparison import (
    SAAConfigurationOptionDTO,
    SAAHypotheticalBundle,
    SAAHypotheticalEffects,
    WeightSetOptionDTO,
)
from services.saa import (
    SAAAssetClassInputSpec,
    SAACorrelationSpec,
    SAAService,
)
from services.tool_classes import ToolClass
from services.tool_registry import get_tool_registry
from services.tools import analysis_tools
from services.tools._tool_context import (
    ToolExecutionContext,
    clear_tool_context,
    clear_tool_data,
    get_tool_data,
    set_tool_context,
)
from services.tools.analysis_tools import (
    _build_saa_chart_envelope,
    get_limit_coverage,
    get_portfolio_overview,
    get_portfolio_statistics,
    get_saa_configuration,
    get_saa_hypothetical_comparison,
)

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

_TOOL_NAMES = (
    "get_limit_coverage",
    "get_saa_hypothetical_comparison",
    "get_portfolio_statistics",
)


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB analysis-tool tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Shared teardown — no context or cached envelope leaks across tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_context() -> Generator[None, None, None]:
    """Clear the tool-execution context and data cache around every test."""
    clear_tool_context()
    clear_tool_data()
    yield
    clear_tool_context()
    clear_tool_data()


# ---------------------------------------------------------------------------
# Layer 1 — context-not-set path (no database needed)
# ---------------------------------------------------------------------------


def test_get_limit_coverage_without_context_degrades_gracefully() -> None:
    """``get_limit_coverage`` returns the explanatory string, does not raise."""
    result = get_limit_coverage()
    assert "context was not set" in result
    assert "web chat surface" in result


def test_get_saa_hypothetical_without_context_degrades_gracefully() -> None:
    """``get_saa_hypothetical_comparison`` returns the explanatory string."""
    result = get_saa_hypothetical_comparison()
    assert "context was not set" in result


def test_get_portfolio_statistics_without_context_degrades_gracefully() -> None:
    """``get_portfolio_statistics`` returns the explanatory string."""
    result = get_portfolio_statistics()
    assert "context was not set" in result


def test_get_portfolio_overview_without_context_degrades_gracefully() -> None:
    """``get_portfolio_overview`` returns the explanatory string, not a raise."""
    result = get_portfolio_overview()
    assert "context was not set" in result
    assert "web chat surface" in result


def test_get_saa_configuration_without_context_degrades_gracefully() -> None:
    """``get_saa_configuration`` returns the explanatory string, not a raise."""
    result = get_saa_configuration()
    assert "context was not set" in result


def test_context_check_fires_before_argument_validation() -> None:
    """The context guard runs before any argument validation."""
    assert "context was not set" in get_limit_coverage(from_date="not-a-date")
    assert "context was not set" in get_saa_hypothetical_comparison(weight_set="garbage")
    # The two ADR-0070 tools share the same ordering.
    assert "context was not set" in get_portfolio_overview(as_of_date="nope")


# ---------------------------------------------------------------------------
# Layer 2 — registration / contract (no database needed)
# ---------------------------------------------------------------------------


def test_three_tools_register_as_read_internal() -> None:
    """All three tools appear in get_tool_definitions as READ_INTERNAL."""
    reg = get_tool_registry()
    names = {d["function"]["name"] for d in reg.get_tool_definitions()}
    for name in _TOOL_NAMES:
        assert name in names, f"{name} not registered"
        assert reg.get_tool_class(name) is ToolClass.READ_INTERNAL


def test_get_limit_coverage_exposes_no_projection_parameter() -> None:
    """Regression: the limit tool has no forward-projection parameter.

    ADR-0069 deliberately scopes ``get_limit_coverage`` to present /
    historical coverage. The parameter surface must therefore carry
    *only* the three pass-through range parameters and nothing that
    invites a forward model (a call amount, a horizon, a scenario).
    """
    reg = get_tool_registry()
    definition = next(
        d for d in reg.get_tool_definitions() if d["function"]["name"] == "get_limit_coverage"
    )
    properties = definition["function"]["parameters"]["properties"]
    assert set(properties) == {"from_date", "to_date", "cut_over"}

    forbidden = (
        "call",
        "amount",
        "horizon",
        "project",
        "forecast",
        "scenario",
        "overlay",
        "future",
        "what_if",
    )
    param_names = " ".join(properties).lower()
    for token in forbidden:
        assert token not in param_names, (
            f"projection-style parameter token '{token}' leaked into get_limit_coverage"
        )


# ---------------------------------------------------------------------------
# Layer 3 — SAA chart envelope (by-handle contract, no database needed)
# ---------------------------------------------------------------------------


def _make_saa_series() -> SAAHypotheticalSeries:
    idx = pd.to_datetime(["2022-01-31", "2022-02-28", "2022-03-31"])
    return SAAHypotheticalSeries(
        saa_label="Tangency — Standard 2026",
        saa_weights={"pe": 0.5, "re": 0.5},
        saa_x_benchmark=pd.Series([0.01, 0.02, -0.01], index=idx),
        saa_x_composite=pd.Series([0.015, 0.01, 0.0], index=idx),
        actual_portfolio_returns=pd.Series([0.02, 0.0, 0.01], index=idx),
        period_start=date(2022, 1, 31),
        period_end=date(2022, 3, 31),
    )


def _make_saa_bundle(
    *,
    series: SAAHypotheticalSeries | None,
    config_id: UUID | None,
    config_name: str = "Standard 2026",
    effects: SAAHypotheticalEffects | None = None,
    weight_set_options: list[WeightSetOptionDTO] | None = None,
) -> SAAHypotheticalBundle:
    options = (
        [
            SAAConfigurationOptionDTO(
                saa_configuration_id=config_id,
                name=config_name,
                is_active=True,
            )
        ]
        if config_id is not None
        else []
    )
    return SAAHypotheticalBundle(
        saa_configuration_options=options,
        weight_set_options=weight_set_options or [],
        selected_configuration_id=config_id,
        selected_weight_set="tangency",
        series=series,
        effects=effects,
    )


def test_build_saa_chart_envelope_long_form_three_series() -> None:
    """The envelope builder produces tidy long-form rows, one block per series."""
    bundle = _make_saa_bundle(series=_make_saa_series(), config_id=uuid4())
    handle = _build_saa_chart_envelope(bundle)
    assert handle is not None

    envelope = get_tool_data(handle)
    assert envelope is not None
    assert envelope["__data__"] == "saa_hypothetical"
    assert envelope["columns"] == [
        "as_of_date",
        "cumulative_index",
        "series_name",
    ]
    series_names = {row[2] for row in envelope["rows"]}
    assert series_names == {"SAA × Benchmark", "SAA × Composite", "Actual"}
    # Rebased to a cumulative index: first value of each series is 1 + r0.
    actual_rows = [r for r in envelope["rows"] if r[2] == "Actual"]
    assert actual_rows[0][1] == pytest.approx(1.02)
    assert envelope["meta"]["unit"] == "index"
    assert envelope["meta"]["base"] == 1.0


def test_build_saa_chart_envelope_returns_none_without_series() -> None:
    """No series → no envelope, no handle."""
    bundle = _make_saa_bundle(series=None, config_id=uuid4())
    assert _build_saa_chart_envelope(bundle) is None


def _set_dummy_context() -> None:
    """A context whose URL is never used (the async bridge is patched)."""
    set_tool_context(
        ToolExecutionContext(
            tenant_id=uuid4(),
            database_url="postgresql+asyncpg://unused/db",
        )
    )


def test_saa_tool_appends_handle_and_stashes_envelope(monkeypatch) -> None:
    """The success path appends a data handle resolving to the SAA envelope."""
    effects = SAAHypotheticalEffects(
        actual_cumulative_endpoint=0.03,
        saa_x_benchmark_cumulative_endpoint=0.02,
        saa_x_composite_cumulative_endpoint=0.025,
        allocation_effect_pp=1.0,
        selection_effect_pp=0.5,
    )
    bundle = _make_saa_bundle(
        series=_make_saa_series(),
        config_id=uuid4(),
        config_name="Standard 2026",
        effects=effects,
    )
    monkeypatch.setattr(analysis_tools, "run_async_in_fresh_loop", lambda _factory: bundle)
    _set_dummy_context()

    result = get_saa_hypothetical_comparison(weight_set="tangency")

    assert "Standard 2026" in result
    assert "Allocation effect" in result
    assert "+1.00 pp" in result
    assert "data_handle:" in result
    assert 'series_column="series_name"' in result

    handle = next(
        line.split(":", 1)[1].strip()
        for line in result.splitlines()
        if line.startswith("data_handle:")
    )
    envelope = get_tool_data(handle)
    assert envelope is not None
    assert envelope["__data__"] == "saa_hypothetical"
    assert {row[2] for row in envelope["rows"]} == {
        "SAA × Benchmark",
        "SAA × Composite",
        "Actual",
    }


def test_saa_tool_no_configuration_message(monkeypatch) -> None:
    """No SAA configuration → a clear explanatory string, no handle."""
    bundle = _make_saa_bundle(series=None, config_id=None)
    monkeypatch.setattr(analysis_tools, "run_async_in_fresh_loop", lambda _factory: bundle)
    _set_dummy_context()

    result = get_saa_hypothetical_comparison()
    assert "no SAA configuration" in result
    assert "data_handle:" not in result


def test_saa_tool_unavailable_optimisation_surfaces_reason(monkeypatch) -> None:
    """A config that cannot optimise surfaces the hint, no handle."""
    bundle = _make_saa_bundle(
        series=None,
        config_id=uuid4(),
        config_name="Standard 2026",
        weight_set_options=[
            WeightSetOptionDTO(
                code="tangency",
                display_name="Tangency (max Sharpe)",
                available=False,
                unavailable_hint="Need at least two asset classes.",
            )
        ],
    )
    monkeypatch.setattr(analysis_tools, "run_async_in_fresh_loop", lambda _factory: bundle)
    _set_dummy_context()

    result = get_saa_hypothetical_comparison()
    assert "produced no result" in result
    assert "Need at least two asset classes." in result
    assert "data_handle:" not in result


def test_saa_tool_rejects_invalid_weight_set() -> None:
    """An invalid weight_set is rejected before any DB work."""
    _set_dummy_context()
    result = get_saa_hypothetical_comparison(weight_set="garbage")
    assert "Invalid weight_set 'garbage'" in result


# ---------------------------------------------------------------------------
# Layer 4 — DB-backed fixtures and happy paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Seeded:
    """Handle returned by the DB-seeding fixtures.

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
    """Engine bound to the unprivileged ``portfoliflow_app`` role (seeding)."""
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
        "TRUNCATE TABLE limits, limit_sets, "
        "asset_class_benchmark_mapping, benchmark_observations, benchmarks, "
        "investment_region_weights, region_country_memberships, regions, "
        "investment_country_weights, investment_sector_weights, sectors, "
        "investment_cashflows, investment_navs, investments, "
        "saa_correlations, saa_asset_class_inputs, saa_configurations, "
        "asset_classes, data_upload_sheets, data_uploads, "
        "login_audit, sessions, audit_log, data_store_entries, "
        "users, tenants RESTART IDENTITY CASCADE"
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
    new_id = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) VALUES "
                "(:id, :name, gen_random_uuid()::text)"
            ),
            {"id": str(new_id), "name": name},
        )
    return new_id


def _set_context(seeded: _Seeded) -> None:
    """Point the module-level tool-execution context at the seeded tenant."""
    set_tool_context(
        ToolExecutionContext(tenant_id=seeded.tenant_id, database_url=seeded.database_url)
    )


# --- Statistics happy path -------------------------------------------------


@pytest_asyncio.fixture
async def seeded_stats(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> _Seeded:
    """Two active investments, each with four aligned actual NAV points."""
    tenant_id = await _seed_tenant(superuser_engine, "Analysis-Stats Tenant")

    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="stats@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="pe_class", display_name="Private Equity"
        )
        inv_repo = InvestmentRepository(session)
        nav_repo = InvestmentNavRepository(session)

        alpha = await inv_repo.create(
            name="Alpha Fund",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
        )
        beta = await inv_repo.create(
            name="Beta Fund",
            investment_type="real_estate",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
        )

        nav_dates = (
            date(2021, 12, 31),
            date(2022, 12, 31),
            date(2023, 12, 31),
            date(2024, 12, 31),
        )
        for inv, base in ((alpha, 100.0), (beta, 200.0)):
            for i, d in enumerate(nav_dates):
                await nav_repo.upsert(
                    investment_id=inv.id,
                    as_of_date=d,
                    nav_kind="actual",
                    nav_value=Decimal(str(base * (1.0 + 0.1 * i))),
                    currency="EUR",
                    source="test-seed",
                    created_by=actor.id,
                )

    return _Seeded(database_url=DATABASE_URL, tenant_id=tenant_id)


async def test_get_portfolio_statistics_happy_path(
    seeded_stats: _Seeded,
) -> None:
    """Both investments, their Sharpe ratios, and a correlation block appear."""
    _set_context(seeded_stats)
    result = get_portfolio_statistics()

    assert "Alpha Fund" in result
    assert "Beta Fund" in result
    assert "Sharpe=" in result
    assert "ann.return=" in result
    assert "Pairwise correlations (Pearson)" in result
    assert "risk-free rate 0.00%" in result


async def test_get_portfolio_statistics_runs_twice(
    seeded_stats: _Seeded,
) -> None:
    """Calling twice in one session both succeed (cross-loop engine guard)."""
    _set_context(seeded_stats)
    first = get_portfolio_statistics()
    second = get_portfolio_statistics()
    assert "Alpha Fund" in first
    assert "Alpha Fund" in second


async def test_get_portfolio_statistics_drops_unknown_names(
    seeded_stats: _Seeded,
) -> None:
    """Unknown names are dropped and reported; known names still resolve."""
    _set_context(seeded_stats)
    result = get_portfolio_statistics(investment_names=["Alpha Fund", "Ghost Fund"])
    assert "Unknown names dropped: Ghost Fund" in result
    assert "Alpha Fund" in result


async def test_get_portfolio_statistics_threads_risk_free_rate(
    seeded_stats: _Seeded,
) -> None:
    """The risk-free rate is echoed in the summary header."""
    _set_context(seeded_stats)
    result = get_portfolio_statistics(risk_free_rate=0.02)
    assert "risk-free rate 2.00%" in result


# --- Limit coverage happy path ---------------------------------------------


@pytest_asyncio.fixture
async def seeded_limits(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> _Seeded:
    """AUM series + one investment with NAVs + one SAA limit set."""
    tenant_id = await _seed_tenant(superuser_engine, "Analysis-Limits Tenant")

    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="limits@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="pe_class", display_name="Private Equity"
        )
        inv_repo = InvestmentRepository(session)
        nav_repo = InvestmentNavRepository(session)

        investment = await inv_repo.create(
            name="Alpha Fund",
            investment_type="private_equity",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
        )
        for d, value in (
            (date(2023, 11, 30), Decimal("3000000")),
            (date(2023, 12, 31), Decimal("3200000")),
        ):
            await nav_repo.upsert(
                investment_id=investment.id,
                as_of_date=d,
                nav_kind="actual",
                nav_value=value,
                currency="EUR",
                source="test-seed",
                created_by=actor.id,
            )

        # The rest of the 10,000,000 book, held as cash rather than asserted
        # as an AUM row (ADR-0103 §2). The fund's coverage percentages are
        # therefore the same ones the retired portfolio_aum row produced.
        cash_class = await AssetClassRepository(session).create(
            code="cash_class", display_name="Cash"
        )
        cash = await inv_repo.create(
            name="Cash EUR",
            investment_type="cash",
            asset_class_id=cash_class.id,
            currency="EUR",
            created_by=actor.id,
        )
        for d, value in (
            (date(2023, 11, 30), Decimal("7000000")),
            (date(2023, 12, 31), Decimal("6800000")),
        ):
            await nav_repo.upsert(
                investment_id=cash.id,
                as_of_date=d,
                nav_kind="actual",
                nav_value=value,
                currency="EUR",
                source="test-seed",
                created_by=actor.id,
            )

        limits_repo = LimitsRepository(session)
        await limits_repo.create_set_with_limits(
            family="saa",
            effective_from=date(2020, 1, 1),
            label="SAA 2020",
            notes=None,
            limits={"pe_class": Decimal("30.0")},
            created_by=actor.id,
        )
        # The engine evaluates BOTH families at every Stichtag and raises
        # LimitSetNotEffective if either has no effective set — so an AnlV
        # set must exist too. The investment carries no anlv_code, so it
        # lands in the AnlV unallocated bucket; this set only needs to be
        # in force.
        await limits_repo.create_set_with_limits(
            family="anlv",
            effective_from=date(2020, 1, 1),
            label="AnlV 2020",
            notes=None,
            limits={"anlv_real_estate": Decimal("25.0")},
            created_by=actor.id,
        )

    return _Seeded(database_url=DATABASE_URL, tenant_id=tenant_id)


async def test_get_limit_coverage_happy_path(seeded_limits: _Seeded) -> None:
    """A seeded coverage scenario yields the KPI strip and a family row."""
    _set_context(seeded_limits)
    result = get_limit_coverage(from_date="2023-11-30", to_date="2023-12-31")

    assert "Limit coverage as of 2023-12-31" in result
    assert "present and historical only" in result
    assert "KPI strip" in result
    assert "SAA coverage:" in result
    assert "pe_class" in result


async def test_get_limit_coverage_empty_universe(
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> None:
    """A tenant with no book gets the explanatory empty message.

    ADR-0103 §2 redefined this case: the tool used to say "no AUM data", and
    now says "no NAV" — because the denominator is the book itself.
    """
    empty_tenant = await _seed_tenant(superuser_engine, "Empty-Book Tenant")
    set_tool_context(ToolExecutionContext(tenant_id=empty_tenant, database_url=DATABASE_URL))
    result = get_limit_coverage()
    assert "No limit coverage is available" in result


async def test_get_limit_coverage_rejects_bad_date(
    seeded_limits: _Seeded,
) -> None:
    """A malformed date returns clear guidance, not an error."""
    _set_context(seeded_limits)
    result = get_limit_coverage(from_date="31-12-2023")
    assert "Invalid from_date" in result
    assert "YYYY-MM-DD" in result


# --- SAA-hypothetical DB plumbing (no configuration) -----------------------


async def test_get_saa_hypothetical_no_configuration_db(
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> None:
    """End-to-end against a real DB: no SAA config → the documented string."""
    tenant_id = await _seed_tenant(superuser_engine, "No-SAA Tenant")
    set_tool_context(ToolExecutionContext(tenant_id=tenant_id, database_url=DATABASE_URL))
    result = get_saa_hypothetical_comparison()
    assert "no SAA configuration" in result


# --- Portfolio overview happy path (ADR-0070) ------------------------------


@pytest_asyncio.fixture
async def seeded_overview(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> _Seeded:
    """One investment with NAVs and cashflows + an AUM row.

    Enough for a non-empty review bundle (so IRR / TVPI / DPI populate)
    and an authoritative AUM hero distinct from the invested book.
    """
    tenant_id = await _seed_tenant(superuser_engine, "Analysis-Overview Tenant")

    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="overview@example.com", password_hash="x" * 8
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="pe_class", display_name="Private Equity"
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
        )
        for d, value in (
            (date(2022, 12, 31), Decimal("2500000")),
            (date(2023, 12, 31), Decimal("3200000")),
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

        await cf_repo.create(
            investment_id=alpha.id,
            flow_timestamp=datetime(2022, 1, 15, 12, tzinfo=timezone.utc),
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-2000000"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
        await cf_repo.create(
            investment_id=alpha.id,
            flow_timestamp=datetime(2023, 6, 30, 12, tzinfo=timezone.utc),
            flow_type="distribution",
            flow_kind="actual",
            amount=Decimal("1000000"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )

        # The 10,000,000 book, completed: 3,200,000 of fund plus 6,800,000
        # of cash. Under ADR-0103 §2 the AUM the tool reports is that sum —
        # the identical figure the retired ``portfolio_aum`` row asserted,
        # now read off the book instead.
        cash_class = await AssetClassRepository(session).create(
            code="cash_class", display_name="Cash"
        )
        cash = await inv_repo.create(
            name="Cash EUR",
            investment_type="cash",
            asset_class_id=cash_class.id,
            currency="EUR",
            created_by=actor.id,
        )
        await nav_repo.upsert(
            investment_id=cash.id,
            as_of_date=date(2023, 12, 31),
            nav_kind="actual",
            nav_value=Decimal("6800000"),
            currency="EUR",
            source="test-seed",
            created_by=actor.id,
        )

    return _Seeded(database_url=DATABASE_URL, tenant_id=tenant_id)


async def test_get_portfolio_overview_happy_path(
    seeded_overview: _Seeded,
) -> None:
    """The headline KPI strip reads real data end-to-end."""
    _set_context(seeded_overview)
    result = get_portfolio_overview()

    assert "Portfolio overview as of 2023-12-31" in result
    # The same three figures the pre-ADR-0103 strip reported — AUM is now
    # Σ NAV over the book (fund + cash) rather than a persisted row, and Cash
    # is read off the cash position rather than inferred as a residual.
    assert "AUM: 10,000,000 EUR" in result
    assert "Invested capital: 3,200,000 EUR" in result
    assert "Cash (explicit cash positions): 6,800,000 EUR" in result
    assert "Active investments: 2" in result
    assert "IRR since inception:" in result
    assert "TVPI:" in result
    assert "DPI:" in result


async def test_get_portfolio_overview_empty_universe(
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> None:
    """A tenant with no investments gets the explanatory empty message."""
    empty_tenant = await _seed_tenant(superuser_engine, "Empty-Overview Tenant")
    set_tool_context(ToolExecutionContext(tenant_id=empty_tenant, database_url=DATABASE_URL))
    result = get_portfolio_overview()
    assert "investment universe is empty" in result


async def test_get_portfolio_overview_rejects_bad_date(
    seeded_overview: _Seeded,
) -> None:
    """A malformed as-of date returns clear guidance, not an error."""
    _set_context(seeded_overview)
    result = get_portfolio_overview(as_of_date="2023/12/31")
    assert "Invalid as_of_date" in result
    assert "YYYY-MM-DD" in result


# --- SAA configuration happy path (ADR-0070) -------------------------------


@pytest_asyncio.fixture
async def seeded_saa(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> _Seeded:
    """One active SAA configuration: two named asset classes + a correlation."""
    tenant_id = await _seed_tenant(superuser_engine, "Analysis-SAA Tenant")

    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email="saa@example.com", password_hash="x" * 8)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = SAAService(
            configurations=SAAConfigurationRepository(session),
            asset_classes=AssetClassRepository(session),
            inputs=SAAAssetClassInputRepository(session),
            correlations=SAACorrelationRepository(session),
        )
        pe = await service.create_asset_class(code="pe", display_name="Private Equity")
        re_core = await service.create_asset_class(code="re", display_name="Real Estate Core")
        config = await service.create_configuration(
            name="Strategic 2026",
            risk_free_rate=0.025,
            n_frontier_points=50,
            created_by=actor.id,
        )
        await service.save_inputs_and_correlations(
            config.id,
            [
                SAAAssetClassInputSpec(
                    asset_class_id=pe.id,
                    expected_return=0.12,
                    volatility=0.18,
                    min_weight=0.05,
                    max_weight=0.40,
                ),
                SAAAssetClassInputSpec(
                    asset_class_id=re_core.id,
                    expected_return=0.06,
                    volatility=0.10,
                    min_weight=0.10,
                    max_weight=0.35,
                ),
            ],
            [
                SAACorrelationSpec(
                    asset_class_a_id=pe.id,
                    asset_class_b_id=re_core.id,
                    correlation=0.35,
                ),
            ],
        )
        await service.activate_configuration(config.id)

    return _Seeded(database_url=DATABASE_URL, tenant_id=tenant_id)


async def test_get_saa_configuration_happy_path(seeded_saa: _Seeded) -> None:
    """The active configuration's assumptions read end-to-end, names resolved."""
    _set_context(seeded_saa)
    result = get_saa_configuration()

    assert "Strategic 2026" in result
    assert "(active)" in result
    assert "risk-free rate 2.50%" in result
    assert "Private Equity" in result
    assert "Real Estate Core" in result
    assert "E[r]=12.00%" in result
    assert "vol=18.00%" in result
    # The pairwise correlation, name-resolved (pair order is normalised).
    assert (
        "Private Equity ↔ Real Estate Core: +0.35" in result
        or "Real Estate Core ↔ Private Equity: +0.35" in result
    )


async def test_get_saa_configuration_prints_names_not_uuids(
    seeded_saa: _Seeded,
) -> None:
    """Asset-class ids are resolved to display names — no raw UUID leaks."""
    _set_context(seeded_saa)
    result = get_saa_configuration()
    assert re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-", result) is None


async def test_get_saa_configuration_by_name_case_insensitive(
    seeded_saa: _Seeded,
) -> None:
    """A configuration_name resolves case-insensitively."""
    _set_context(seeded_saa)
    result = get_saa_configuration(configuration_name="strategic 2026")
    assert "Strategic 2026" in result
    assert "Private Equity" in result


async def test_get_saa_configuration_unknown_name_lists_available(
    seeded_saa: _Seeded,
) -> None:
    """An unknown name returns guidance naming the available configurations."""
    _set_context(seeded_saa)
    result = get_saa_configuration(configuration_name="Nonexistent")
    assert "No SAA configuration named 'Nonexistent'" in result
    assert "Strategic 2026" in result


async def test_get_saa_configuration_no_configuration_db(
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> None:
    """A tenant with no SAA configuration gets the explanatory empty message."""
    tenant_id = await _seed_tenant(superuser_engine, "No-SAA-Config Tenant")
    set_tool_context(ToolExecutionContext(tenant_id=tenant_id, database_url=DATABASE_URL))
    result = get_saa_configuration()
    assert "No SAA configurations exist" in result
