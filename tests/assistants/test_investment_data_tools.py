# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the ``get_investment_data`` structured-data access tool.

``get_investment_data`` (ADR-0048, Axis 1) is the data-acquisition
half of the two-axis chart architecture. It lives in
:mod:`services.tools.investment_tools` alongside the three prose
investment tools and shares their ``_tool_session`` loop-local-engine
plumbing — so this file mirrors ``test_investment_tools.py``'s two
layers:

1. **Context-not-set path** — no database needed. With the
   tool-execution context cleared the tool returns its graceful
   explanatory string rather than raising.
2. **DB-backed happy path** — seeds one tenant with investments,
   NAVs, and cashflows, points a ``ToolExecutionContext`` at the test
   database, and asserts each of the four bundles
   (``catalogue`` / ``nav_series`` / ``cashflow_series`` /
   ``return_metrics``) returns a **summary string carrying a data
   handle** — not the envelope itself (ADR-0048, amended) — whose
   handle resolves to a well-formed structured-data envelope in the
   turn-scoped cache. Unknown names and invalid bundles return clear
   guidance; the double-call regression guard pins the loop-local-engine
   fix.

The DB fixtures are defined locally (not imported from
``tests._db_fixtures``) so the autouse ``reset_schema`` there does not
leak into the context-not-set tests above.
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
    clear_tool_data,
    get_tool_data,
    set_tool_context,
)
from services.tools.investment_tools import get_investment_data

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB investment-data-tool tests.",
            allow_module_level=False,
        )


# ---------------------------------------------------------------------------
# Shared: context teardown so a turn's context never leaks into the next test
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


def test_without_context_degrades_gracefully() -> None:
    """``get_investment_data`` returns the explanatory string, does not raise."""
    result = get_investment_data(bundle="catalogue")
    assert "context was not set" in result
    assert "web chat surface" in result


def test_invalid_bundle_without_context_still_degrades_gracefully() -> None:
    """The context check fires before the bundle validation."""
    result = get_investment_data(bundle="garbage")
    assert "context was not set" in result


# ---------------------------------------------------------------------------
# Layer 2 — DB-backed happy path
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Seeded:
    """Handle returned by the ``seeded`` fixture for the DB-backed tests."""

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

    Fixture-only — the tool under test builds its own short-lived,
    loop-local engine from the database URL inside the fresh
    ``run_async_in_fresh_loop`` event loop (ADR-0047, amended).
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
    """Seed one tenant with two investments, NAVs, and cashflows.

    - ``Alpha Fund`` — three actual NAVs + one plan NAV, one capital
      call + one distribution (so the return-metrics analytics have
      enough to compute against).
    - ``Beta Fund`` — one actual NAV, no cashflows.
    """
    tenant_id = await _seed_tenant(superuser_engine, "Investment-Data Tenant")

    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="invdata@example.com", password_hash="x" * 8
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
        await inv_repo.create(
            name="Beta Fund",
            investment_type="real_estate",
            asset_class_id=asset_class.id,
            currency="USD",
            created_by=actor.id,
        )

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

    return _Seeded(database_url=DATABASE_URL, tenant_id=tenant_id)


def _set_context(seeded: _Seeded) -> None:
    """Point the module-level tool-execution context at the seeded tenant."""
    set_tool_context(
        ToolExecutionContext(tenant_id=seeded.tenant_id, database_url=seeded.database_url)
    )


def _handle_of(summary: str) -> str:
    """Extract the ``data_handle`` value from a get_investment_data summary."""
    for line in summary.splitlines():
        if line.startswith("data_handle:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no data_handle line in summary:\n{summary}")


def _envelope(result: str, bundle: str) -> dict:
    """Assert the summary's shape, then resolve and check the cached envelope.

    ``get_investment_data`` no longer returns the envelope: it returns
    a compact summary carrying a data handle (ADR-0048, amended). This
    helper pins both halves — the model-facing summary *and* the
    handle↔data linkage — so the tests below can keep asserting on the
    envelope shape.
    """
    # The summary is a compact string, never the JSON envelope.
    assert result.startswith(f"Fetched {bundle} ")
    assert "data_handle:" in result
    assert "render_chart" in result
    assert "__data__" not in result

    parsed = get_tool_data(_handle_of(result))
    assert parsed is not None, "the summary's handle is not in the data cache"
    assert parsed["__data__"] == "investment_data"
    assert parsed["bundle"] == bundle
    assert isinstance(parsed["columns"], list)
    assert isinstance(parsed["rows"], list)
    assert isinstance(parsed["meta"], dict)
    assert parsed["meta"]["row_count"] == len(parsed["rows"])
    assert parsed["meta"]["truncated"] is False

    # The summary's stated row count and column names match the cached
    # envelope — this pins the handle↔data linkage.
    assert f"{len(parsed['rows'])} row(s)" in result
    for column in parsed["columns"]:
        assert column in result
    return parsed


async def test_catalogue_bundle_returns_one_row_per_investment(
    seeded: _Seeded,
) -> None:
    """The ``catalogue`` bundle carries every investment as a tidy row."""
    _set_context(seeded)
    envelope = _envelope(get_investment_data(bundle="catalogue"), "catalogue")

    assert envelope["investment_name"] is None
    assert envelope["meta"]["investment_count"] == 2
    assert len(envelope["rows"]) == 2
    assert envelope["columns"][0] == "name"
    names = {row[0] for row in envelope["rows"]}
    assert names == {"Alpha Fund", "Beta Fund"}


async def test_nav_series_bundle_returns_all_nav_rows(
    seeded: _Seeded,
) -> None:
    """The ``nav_series`` bundle carries the full NAV history, plan + actual."""
    _set_context(seeded)
    envelope = _envelope(
        get_investment_data(bundle="nav_series", investment_name="Alpha Fund"),
        "nav_series",
    )

    assert envelope["investment_name"] == "Alpha Fund"
    assert envelope["columns"] == ["as_of_date", "nav_value", "nav_kind"]
    # Three actual NAVs + one plan NAV.
    assert len(envelope["rows"]) == 4
    assert envelope["meta"]["currency"] == "EUR"
    kinds = {row[2] for row in envelope["rows"]}
    assert kinds == {"actual", "plan"}


async def test_cashflow_series_bundle_returns_all_cashflows(
    seeded: _Seeded,
) -> None:
    """The ``cashflow_series`` bundle carries the signed cashflow history."""
    _set_context(seeded)
    envelope = _envelope(
        get_investment_data(bundle="cashflow_series", investment_name="Alpha Fund"),
        "cashflow_series",
    )

    assert envelope["investment_name"] == "Alpha Fund"
    assert envelope["columns"] == [
        "flow_timestamp",
        "flow_type",
        "flow_kind",
        "amount",
    ]
    assert len(envelope["rows"]) == 2
    amounts = sorted(row[3] for row in envelope["rows"])
    assert amounts == [-2000000.0, 1500000.0]


async def test_return_metrics_bundle_returns_computed_series(
    seeded: _Seeded,
) -> None:
    """The ``return_metrics`` bundle carries the computed analytics series."""
    _set_context(seeded)
    envelope = _envelope(
        get_investment_data(bundle="return_metrics", investment_name="Alpha Fund"),
        "return_metrics",
    )

    assert envelope["investment_name"] == "Alpha Fund"
    assert envelope["columns"][0] == "as_of_date"
    for metric in ("total_return", "tvpi", "dpi", "rvpi", "rolling_irr"):
        assert metric in envelope["columns"]
    # Three actual NAV observations drive the rolling metrics.
    assert len(envelope["rows"]) == 3
    # ``meta.series`` names the metric columns that actually carry data.
    assert "tvpi" in envelope["meta"]["series"]


async def test_unknown_investment_returns_clear_string(
    seeded: _Seeded,
) -> None:
    """An unknown investment name returns clear guidance, not an envelope."""
    _set_context(seeded)
    result = get_investment_data(bundle="nav_series", investment_name="Nonexistent Fund")
    assert "No investment named 'Nonexistent Fund'" in result
    assert "list_investments" in result
    assert "__data__" not in result


async def test_invalid_bundle_returns_clear_string(seeded: _Seeded) -> None:
    """An invalid bundle name returns the validation message."""
    _set_context(seeded)
    result = get_investment_data(bundle="garbage", investment_name="Alpha Fund")
    assert "Invalid bundle 'garbage'" in result
    assert "catalogue" in result and "nav_series" in result


async def test_per_investment_bundle_without_name_returns_clear_string(
    seeded: _Seeded,
) -> None:
    """A per-investment bundle with no investment_name returns clear guidance."""
    _set_context(seeded)
    result = get_investment_data(bundle="nav_series")
    assert "needs an investment_name" in result
    assert "__data__" not in result


async def test_empty_nav_series_returns_clear_string(seeded: _Seeded) -> None:
    """An investment with no NAV rows returns a clear string, not an envelope."""
    _set_context(seeded)
    result = get_investment_data(bundle="nav_series", investment_name="Beta Fund")
    assert "has no NAV rows" in result
    assert "__data__" not in result


async def test_tool_succeeds_when_called_twice_in_a_row(
    seeded: _Seeded,
) -> None:
    """Calling the tool twice in one session must both succeed.

    Regression guard for the cross-loop engine bug (ADR-0047,
    amended): each call builds and disposes its own loop-local engine
    inside the fresh ``run_async_in_fresh_loop`` event loop.
    """
    _set_context(seeded)
    first = get_investment_data(bundle="catalogue")
    second = get_investment_data(bundle="catalogue")

    assert _envelope(first, "catalogue")["meta"]["investment_count"] == 2
    assert _envelope(second, "catalogue")["meta"]["investment_count"] == 2


# ---------------------------------------------------------------------------
# portfolio_nav_series bundle — multi-investment long-form NAV envelope
# ---------------------------------------------------------------------------
#
# These fixtures seed dedicated portfolio shapes so each scenario exercises
# exactly one branch of ``_portfolio_nav_series_envelope`` (multi-investment
# happy path, mixed currencies, empty portfolio, investments-without-NAVs,
# all-investments-empty, truncation diagnostic).


async def _create_actor(app_engine: AsyncEngine, tenant_id: UUID, email: str) -> UUID:
    """Create the bootstrap user for a freshly-seeded tenant."""
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    return actor.id


async def _seed_portfolio(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    actor_id: UUID,
    specs: list[tuple[str, list[tuple[date, Decimal, str]]]],
) -> None:
    """Create each named investment and its NAV rows.

    ``specs`` is a list of ``(investment_name, [(as_of_date, value,
    currency), ...])`` pairs. An empty NAV list creates the investment
    with no NAV rows.
    """
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code="default_class", display_name="Default Class"
        )
        inv_repo = InvestmentRepository(session)
        nav_repo = InvestmentNavRepository(session)
        for name, nav_rows in specs:
            inv = await inv_repo.create(
                name=name,
                investment_type="private_equity",
                asset_class_id=asset_class.id,
                currency=nav_rows[0][2] if nav_rows else "EUR",
                created_by=actor_id,
            )
            for as_of, value, currency in nav_rows:
                await nav_repo.upsert(
                    investment_id=inv.id,
                    as_of_date=as_of,
                    nav_kind="actual",
                    nav_value=value,
                    currency=currency,
                    source="test-seed",
                    created_by=actor_id,
                )


@pytest_asyncio.fixture
async def seeded_two_eur(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> _Seeded:
    """Two EUR investments with overlapping NAV date ranges."""
    tenant_id = await _seed_tenant(superuser_engine, "Two-EUR Portfolio")
    actor_id = await _create_actor(app_engine, tenant_id, "two-eur@example.com")
    await _seed_portfolio(
        app_engine,
        tenant_id,
        actor_id,
        [
            (
                "Investment One",
                [
                    (date(2021, 12, 31), Decimal("1000000"), "EUR"),
                    (date(2022, 12, 31), Decimal("1200000"), "EUR"),
                ],
            ),
            (
                "Investment Two",
                [
                    (date(2022, 12, 31), Decimal("2000000"), "EUR"),
                    (date(2023, 12, 31), Decimal("2500000"), "EUR"),
                ],
            ),
        ],
    )
    return _Seeded(database_url=DATABASE_URL, tenant_id=tenant_id)


@pytest_asyncio.fixture
async def seeded_multi_currency(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> _Seeded:
    """Three investments: two in EUR, one in USD."""
    tenant_id = await _seed_tenant(superuser_engine, "Multi-Currency Portfolio")
    actor_id = await _create_actor(app_engine, tenant_id, "multi-curr@example.com")
    await _seed_portfolio(
        app_engine,
        tenant_id,
        actor_id,
        [
            (
                "Euro Fund A",
                [(date(2022, 12, 31), Decimal("1000000"), "EUR")],
            ),
            (
                "Euro Fund B",
                [(date(2022, 12, 31), Decimal("2000000"), "EUR")],
            ),
            (
                "Dollar Fund",
                [(date(2022, 12, 31), Decimal("3000000"), "USD")],
            ),
        ],
    )
    return _Seeded(database_url=DATABASE_URL, tenant_id=tenant_id)


@pytest_asyncio.fixture
async def seeded_empty_portfolio(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> _Seeded:
    """A tenant with zero investments."""
    tenant_id = await _seed_tenant(superuser_engine, "Empty Portfolio")
    await _create_actor(app_engine, tenant_id, "empty@example.com")
    return _Seeded(database_url=DATABASE_URL, tenant_id=tenant_id)


@pytest_asyncio.fixture
async def seeded_all_investments_empty(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
    reset_schema: None,
) -> _Seeded:
    """Two investments, neither with any NAV rows."""
    tenant_id = await _seed_tenant(superuser_engine, "All-Empty-NAVs Portfolio")
    actor_id = await _create_actor(app_engine, tenant_id, "all-empty@example.com")
    await _seed_portfolio(
        app_engine,
        tenant_id,
        actor_id,
        [
            ("Investment One", []),
            ("Investment Two", []),
        ],
    )
    return _Seeded(database_url=DATABASE_URL, tenant_id=tenant_id)


async def test_portfolio_nav_series_happy_path_single_currency(
    seeded_two_eur: _Seeded,
) -> None:
    """Long-form envelope, multi-investment, single currency."""
    _set_context(seeded_two_eur)
    envelope = _envelope(
        get_investment_data(bundle="portfolio_nav_series"),
        "portfolio_nav_series",
    )

    assert envelope["investment_name"] is None
    assert envelope["columns"] == [
        "as_of_date",
        "investment_name",
        "nav_value",
        "nav_kind",
    ]
    assert envelope["meta"]["investment_count"] == 2
    assert envelope["meta"]["currencies"] == ["EUR"]
    # Two NAVs per investment.
    assert len(envelope["rows"]) == 4
    # Every row carries the correct investment_name.
    names = {row[1] for row in envelope["rows"]}
    assert names == {"Investment One", "Investment Two"}

    # No global resort: rows iterate Investment One ascending, then
    # Investment Two ascending. ``list_all`` sorts by name, so the
    # alphabetical order pins the iteration deterministically.
    expected_order = [
        ("2021-12-31", "Investment One"),
        ("2022-12-31", "Investment One"),
        ("2022-12-31", "Investment Two"),
        ("2023-12-31", "Investment Two"),
    ]
    actual_order = [(row[0], row[1]) for row in envelope["rows"]]
    assert actual_order == expected_order


async def test_portfolio_nav_series_flags_mixed_currencies(
    seeded_multi_currency: _Seeded,
) -> None:
    """Mixed-currency portfolios surface a ``mixed currencies`` summary line."""
    _set_context(seeded_multi_currency)
    result = get_investment_data(bundle="portfolio_nav_series")
    envelope = _envelope(result, "portfolio_nav_series")

    assert envelope["meta"]["currencies"] == ["EUR", "USD"]
    assert envelope["meta"]["investment_count"] == 3
    assert "mixed currencies" in result
    assert "EUR" in result and "USD" in result


async def test_portfolio_nav_series_empty_portfolio_returns_clear_string(
    seeded_empty_portfolio: _Seeded,
) -> None:
    """Zero investments → the standard catalogue empty-portfolio message."""
    _set_context(seeded_empty_portfolio)
    result = get_investment_data(bundle="portfolio_nav_series")
    # Must match the catalogue / list_investments message verbatim.
    assert result == (
        "No investments are present in the persistent investment "
        "database. Import portfolio data via the Front Office Data "
        "Import section first."
    )
    assert "__data__" not in result


async def test_portfolio_nav_series_skips_investments_without_navs(
    seeded: _Seeded,
) -> None:
    """Zero-row investments are silently skipped — they yield no rows.

    The ``seeded`` fixture has Alpha Fund (4 NAV rows) and Beta Fund
    (no NAV rows). ``investment_count`` reports only Alpha — Beta
    contributes nothing to the envelope.
    """
    _set_context(seeded)
    envelope = _envelope(
        get_investment_data(bundle="portfolio_nav_series"),
        "portfolio_nav_series",
    )

    assert envelope["meta"]["investment_count"] == 1
    names = {row[1] for row in envelope["rows"]}
    assert names == {"Alpha Fund"}
    # Alpha's 3 actual NAVs + 1 plan NAV.
    assert len(envelope["rows"]) == 4


async def test_portfolio_nav_series_all_empty_returns_clear_string(
    seeded_all_investments_empty: _Seeded,
) -> None:
    """Investments exist but none have NAV rows → the dedicated message."""
    _set_context(seeded_all_investments_empty)
    result = get_investment_data(bundle="portfolio_nav_series")
    assert result == "No NAV rows are recorded for any investment in the portfolio."
    assert "__data__" not in result


async def test_portfolio_nav_series_truncation_diagnostic(
    seeded_two_eur: _Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truncation enriches ``meta`` with ``row_count_uncapped``.

    Drops the cap to 2 so the 4-row fixture trips truncation; verifies
    the negative case in the same test by reading the un-monkeypatched
    cap envelope first.
    """
    _set_context(seeded_two_eur)

    # Negative case first — under the real cap, no truncation, no
    # ``row_count_uncapped`` key in meta.
    untruncated = _envelope(
        get_investment_data(bundle="portfolio_nav_series"),
        "portfolio_nav_series",
    )
    assert untruncated["meta"]["truncated"] is False
    assert "row_count_uncapped" not in untruncated["meta"]

    # Now monkeypatch the cap and trip it.
    import services.tools.investment_tools as inv_tools

    monkeypatch.setattr(inv_tools, "_DATA_ROW_CAP", 2)

    truncated_result = get_investment_data(bundle="portfolio_nav_series")
    # Cannot use ``_envelope`` here — its assertion that
    # ``meta["truncated"]`` is ``False`` is exactly what we're flipping.
    assert truncated_result.startswith("Fetched portfolio_nav_series ")
    handle = _handle_of(truncated_result)
    cached = get_tool_data(handle)
    assert cached is not None
    assert cached["meta"]["truncated"] is True
    assert cached["meta"]["row_count"] == 2
    assert cached["meta"]["row_count_uncapped"] == 4
    assert len(cached["rows"]) == 2


# ---------------------------------------------------------------------------
# Subset / date-window / nav_kind filters (Prompt 6)
# ---------------------------------------------------------------------------
#
# All filters are additive: the tests above (which omit the new params)
# pin that today's behaviour is unchanged. These exercise each new filter
# against the seeded harness above.


async def test_portfolio_nav_series_subset_filter(
    seeded_multi_currency: _Seeded,
) -> None:
    """``investment_names`` keeps only the named subset of investments.

    The ``seeded_multi_currency`` fixture has three investments
    (Euro Fund A, Euro Fund B, Dollar Fund); requesting two of them
    must yield an envelope whose distinct ``investment_name`` values are
    exactly that subset — the third is excluded.
    """
    _set_context(seeded_multi_currency)
    envelope = _envelope(
        get_investment_data(
            bundle="portfolio_nav_series",
            investment_names=["Euro Fund A", "Dollar Fund"],
        ),
        "portfolio_nav_series",
    )

    names = {row[1] for row in envelope["rows"]}
    assert names == {"Euro Fund A", "Dollar Fund"}
    assert envelope["meta"]["investment_count"] == 2


async def test_portfolio_nav_series_date_window_filter(
    seeded_two_eur: _Seeded,
) -> None:
    """``start_date`` / ``end_date`` window every row's ``as_of_date``.

    The fixture's NAVs span 2021–2023; a 2022-only window keeps only the
    two 2022-12-31 rows.
    """
    _set_context(seeded_two_eur)
    envelope = _envelope(
        get_investment_data(
            bundle="portfolio_nav_series",
            start_date="2022-01-01",
            end_date="2022-12-31",
        ),
        "portfolio_nav_series",
    )

    lower = date.fromisoformat("2022-01-01")
    upper = date.fromisoformat("2022-12-31")
    for row in envelope["rows"]:
        row_date = date.fromisoformat(row[0][:10])
        assert lower <= row_date <= upper
    # Both investments have a 2022-12-31 observation.
    assert len(envelope["rows"]) == 2


async def test_portfolio_nav_series_nav_kind_filter(
    seeded: _Seeded,
) -> None:
    """``nav_kind='actual'`` keeps only actual rows, dropping the plan NAV.

    Alpha Fund has three actual NAVs and one plan NAV; the filter keeps
    the three actuals.
    """
    _set_context(seeded)
    envelope = _envelope(
        get_investment_data(bundle="portfolio_nav_series", nav_kind="actual"),
        "portfolio_nav_series",
    )

    kinds = {row[3] for row in envelope["rows"]}
    assert kinds == {"actual"}
    assert len(envelope["rows"]) == 3


async def test_nav_series_date_window_filter(seeded: _Seeded) -> None:
    """A windowed single-investment ``nav_series`` still works and bounds rows.

    Alpha Fund's NAVs are 2021/2022/2023 actual + a 2024 plan; the
    2022–2023 window keeps the two actual rows in range.
    """
    _set_context(seeded)
    envelope = _envelope(
        get_investment_data(
            bundle="nav_series",
            investment_name="Alpha Fund",
            start_date="2022-01-01",
            end_date="2023-12-31",
        ),
        "nav_series",
    )

    assert envelope["investment_name"] == "Alpha Fund"
    lower = date.fromisoformat("2022-01-01")
    upper = date.fromisoformat("2023-12-31")
    for row in envelope["rows"]:
        row_date = date.fromisoformat(row[0][:10])
        assert lower <= row_date <= upper
    # 2022-12-31 + 2023-12-31; 2021 and the 2024 plan fall outside.
    assert len(envelope["rows"]) == 2


async def test_invalid_start_date_returns_message_not_raise(
    seeded: _Seeded,
) -> None:
    """A malformed ``start_date`` returns the YYYY-MM-DD guidance, no raise."""
    _set_context(seeded)
    result = get_investment_data(bundle="portfolio_nav_series", start_date="2019/01/01")
    assert "YYYY-MM-DD" in result
    assert "2019/01/01" in result
    assert "__data__" not in result


async def test_window_matching_nothing_returns_clear_string(
    seeded_two_eur: _Seeded,
) -> None:
    """A window with no matching rows returns the clear no-rows message."""
    _set_context(seeded_two_eur)
    result = get_investment_data(
        bundle="portfolio_nav_series",
        start_date="1990-01-01",
        end_date="1990-12-31",
    )
    assert result == "No NAV rows match the requested investments / date window / nav_kind."
    assert "__data__" not in result
