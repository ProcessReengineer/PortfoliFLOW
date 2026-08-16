# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Cash plan-path materialisation against a live DB (ADR-0103 §6).

The forward path — ``balance(t₀) + Σ signed plan flows`` — projected onto each
cash position as ``nav_kind='plan'`` / ``basis='computed'`` /
``ingest_origin='system'`` / ``source='computed:cash-plan'`` NAV rows, and the
triggers that keep it in step with the book inside one transaction.

The pure formula is unit-tested in ``test_cash_plan_projection.py``; this suite
owns the DB shell — the anchor, ownership and precedence, idempotency, the
multi-position and no-position rules, and every trigger.

Coverage
--------
* **CP-01** the formula end-to-end: anchor at the latest actual NAV; a plan
  call debits, a plan distribution credits, an investor flow moves both ways;
  one row per distinct event date; full provenance pinned.
* **CP-02** a projected funding gap materialises **negative** rows — no guard.
* **CP-03** the ``t₀`` boundary: flows on and before the anchor contribute
  nothing; one day after does.
* **CP-04** currency separation *by flow currency* — a EUR-denominated flow on
  a USD investment settles the **EUR** path.
* **CP-05** flows in a currency with no active cash position: skipped, counted.
* **CP-06** two active cash positions in one currency: the earliest-created
  settles, the other is named in a warning and counted (ADR-0103 §10).
* **CP-07** a cash position with no actual NAV has no anchor: skipped, counted.
* **CP-08** an inactive investment's plan flows do not settle.
* **CP-09** idempotency: an immediate re-run is a full no-op, ``updated_at``
  untouched.
* **CP-10** a removed plan flow deletes its stranded projected row.
* **CP-11** a new statement moves ``t₀``: rows at or below it are deleted and
  the remainder re-anchors.
* **CP-12** ownership and precedence: an ``'excel'`` (and a ``'manual'``) plan
  row on an event date is skipped and survives byte-identical; actual rows are
  never touched.
* **CP-13** trigger — statement import (end-to-end through the Cash sheet).
* **CP-14** trigger — the transform's plan flows move the projection in the
  same import.
* **CP-15** trigger — ``add_cashflow`` / ``update_cashflow`` /
  ``delete_cashflow``, including a **non**-investor plan flow edit (the
  deliberate superset of §6's trigger list), an update that moves a flow
  across currencies (both paths move), and one that promotes or demotes its
  ``flow_kind`` (the old **or** new state is what arms the trigger).
* **CP-16** actual-kind flow mutations trigger nothing.
* **CP-17** wiring posture: the three-repository service — every construction
  site the codebase has — projects the plan path without raising.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    DataUploadRepository,
    InstrumentPriceRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    PositionTransactionRepository,
    UserRepository,
    tenant_context,
)
from services.data_normalization import InvestmentExtractor
from services.investments import InvestmentService
from services.investments.cash_plan_materialisation import (
    CashPlanMaterialisationService,
)

_CASH_PLAN_SOURCE = "computed:cash-plan"
_LOGGER = "portfoliflow.services.investments.cash_plan_materialisation"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _ts(day: str) -> datetime:
    """The 12:00-UTC flow timestamp convention (ADR-0043 §1)."""
    return datetime.combine(date.fromisoformat(day), datetime.min.time()).replace(
        hour=12, tzinfo=timezone.utc
    )


def _service(session) -> InvestmentService:
    """The three-repository construction site — the plan path's floor."""
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
    )


def _full_service(session) -> InvestmentService:
    """The import construction site (ledger + prices wired)."""
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
    )


def _materialiser(session) -> CashPlanMaterialisationService:
    return CashPlanMaterialisationService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
    )


async def _seed(app_engine: AsyncEngine, tenant_id: UUID, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassRepository(session)
        await repo.create(code="unclassified", display_name="Unclassified")
        asset_class = await repo.create(code="cash", display_name="Cash")
    return actor, asset_class


async def _cash_position(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    actor_id: UUID,
    asset_class_id: UUID,
    *,
    name: str,
    currency: str = "EUR",
    anchor: tuple[str, str] | None = None,
    is_active: bool = True,
):
    """Create a cash position, optionally with its actual-NAV anchor.

    Each call runs in its own transaction, which matters for the
    multi-position rule: ``created_at`` defaults to ``func.now()`` — the
    *transaction* timestamp — so two positions created in one transaction
    would be indistinguishable by creation order.
    """
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        svc = _service(session)
        investment = await svc.create_investment(
            name=name,
            investment_type="cash",
            asset_class_id=asset_class_id,
            currency=currency,
            created_by=actor_id,
            is_active=is_active,
        )
        if anchor is not None:
            day, balance = anchor
            await svc.add_nav(
                investment_id=investment.id,
                as_of_date=date.fromisoformat(day),
                nav_kind="actual",
                nav_value=Decimal(balance),
                currency=currency,
                source="statement",
                created_by=actor_id,
            )
    return investment


async def _investment(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    actor_id: UUID,
    asset_class_id: UUID,
    *,
    name: str,
    currency: str = "EUR",
    investment_type: str = "private_equity",
    is_active: bool = True,
):
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        return await _service(session).create_investment(
            name=name,
            investment_type=investment_type,
            asset_class_id=asset_class_id,
            currency=currency,
            created_by=actor_id,
            is_active=is_active,
        )


async def _flow(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    actor_id: UUID,
    *,
    investment_id: UUID,
    day: str,
    amount: str,
    flow_type: str = "capital_call",
    flow_kind: str = "plan",
    currency: str = "EUR",
):
    """Write a cashflow through the **repository** — no service trigger.

    The core tests drive the materialiser explicitly; seeding through
    :meth:`InvestmentService.add_cashflow` would fire the trigger and blur what
    each test is actually asserting. The trigger tests (CP-13..CP-16) go
    through the service seam on purpose.
    """
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        return await InvestmentCashflowRepository(session).create(
            investment_id=investment_id,
            flow_timestamp=_ts(day),
            flow_type=flow_type,
            flow_kind=flow_kind,
            amount=Decimal(amount),
            currency=currency,
            description=None,
            created_by=actor_id,
            ingest_origin="excel",
        )


async def _run(app_engine: AsyncEngine, tenant_id: UUID, actor_id: UUID, **kw):
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        return await _materialiser(session).materialise_all(acting_user=actor_id, **kw)


async def _plan_rows(app_engine: AsyncEngine, tenant_id: UUID, iid: UUID):
    async with tenant_context(app_engine, tenant_id) as session:
        return await InvestmentNavRepository(session).list_by_investment_and_kind(iid, "plan")


async def _actual_rows(app_engine: AsyncEngine, tenant_id: UUID, iid: UUID):
    async with tenant_context(app_engine, tenant_id) as session:
        return await InvestmentNavRepository(session).list_by_investment_and_kind(iid, "actual")


def _pairs(rows):
    return [(r.as_of_date, r.nav_value) for r in rows]


# ---------------------------------------------------------------------------
# CP-01: the formula
# ---------------------------------------------------------------------------


async def test_cp01_projects_the_forward_path_from_the_actual_anchor(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Anchor + signed plan flows → one projected row per distinct event date."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp01@example.com")

    cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        anchor=("2024-03-31", "1000"),
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund A")

    # A call debits, a distribution credits, an investor flow does both.
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-04-30",
        amount="-250",
        flow_type="capital_call",
    )
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-05-31",
        amount="400",
        flow_type="distribution",
    )
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=cash.id,
        day="2024-06-30",
        amount="1000",
        flow_type="investor_flow",
    )
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=cash.id,
        day="2024-07-31",
        amount="-300",
        flow_type="investor_flow",
    )

    report = await _run(app_engine, tenant_id, actor.id)
    assert report.positions == 1
    assert report.inserted == 4
    assert (report.updated, report.noop, report.deleted) == (0, 0, 0)

    rows = await _plan_rows(app_engine, tenant_id, cash.id)
    assert _pairs(rows) == [
        (date(2024, 4, 30), Decimal("750.0000")),
        (date(2024, 5, 31), Decimal("1150.0000")),
        (date(2024, 6, 30), Decimal("2150.0000")),
        (date(2024, 7, 31), Decimal("1850.0000")),
    ]

    # Provenance — the whole ADR-0103 §6 stamp.
    for row in rows:
        assert row.nav_kind == "plan"
        assert row.basis == "computed"
        assert row.ingest_origin == "system"
        assert row.source == _CASH_PLAN_SOURCE
        assert row.currency == "EUR"


# ---------------------------------------------------------------------------
# CP-02: negative balances
# ---------------------------------------------------------------------------


async def test_cp02_a_projected_funding_gap_materialises_negative_rows(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """No non-negativity guard fires on the plan path (ADR-0103 §6)."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp02@example.com")

    cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        anchor=("2024-03-31", "100"),
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund A")
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-04-30",
        amount="-250",
        flow_type="capital_call",
    )
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-05-31",
        amount="-100",
        flow_type="capital_call",
    )

    await _run(app_engine, tenant_id, actor.id)

    assert _pairs(await _plan_rows(app_engine, tenant_id, cash.id)) == [
        (date(2024, 4, 30), Decimal("-150.0000")),
        (date(2024, 5, 31), Decimal("-250.0000")),
    ]


# ---------------------------------------------------------------------------
# CP-03: the t₀ boundary
# ---------------------------------------------------------------------------


async def test_cp03_flows_on_and_before_the_anchor_contribute_nothing(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Stale history: the statement level already contains them."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp03@example.com")

    cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        anchor=("2024-03-31", "1000"),
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund A")
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-02-29",
        amount="-9999",
        flow_type="capital_call",
    )
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-03-31",
        amount="-9999",
        flow_type="capital_call",
    )
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-04-01",
        amount="-250",
        flow_type="capital_call",
    )

    await _run(app_engine, tenant_id, actor.id)

    # Only the day *after* t₀ projects — and it projects off the anchor, not
    # off the stale flows.
    assert _pairs(await _plan_rows(app_engine, tenant_id, cash.id)) == [
        (date(2024, 4, 1), Decimal("750.0000"))
    ]


# ---------------------------------------------------------------------------
# CP-04: currency separation — by the flow's own currency
# ---------------------------------------------------------------------------


async def test_cp04_flows_settle_by_their_own_currency_not_the_investments(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A EUR-denominated flow on a **USD** investment settles the EUR path.

    Settlement is "the cash path of its settlement currency" (ADR-0104), and
    the flow carries that currency itself. Pinning the cross case explicitly:
    reading the currency off the owning investment would put this flow on the
    wrong path.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp04@example.com")

    eur_cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        currency="EUR",
        anchor=("2024-03-31", "1000"),
    )
    usd_cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash USD",
        currency="USD",
        anchor=("2024-03-31", "2000"),
    )
    usd_fund = await _investment(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="US Fund",
        currency="USD",
    )

    # Its own currency: settles USD.
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=usd_fund.id,
        day="2024-04-30",
        amount="-500",
        currency="USD",
    )
    # A EUR-denominated flow *on the USD fund*: settles EUR.
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=usd_fund.id,
        day="2024-05-31",
        amount="-200",
        currency="EUR",
    )

    await _run(app_engine, tenant_id, actor.id)

    assert _pairs(await _plan_rows(app_engine, tenant_id, usd_cash.id)) == [
        (date(2024, 4, 30), Decimal("1500.0000"))
    ]
    assert _pairs(await _plan_rows(app_engine, tenant_id, eur_cash.id)) == [
        (date(2024, 5, 31), Decimal("800.0000"))
    ]


# ---------------------------------------------------------------------------
# CP-05 / CP-06 / CP-07: the position-resolution rules
# ---------------------------------------------------------------------------


async def test_cp05_flows_without_a_cash_position_are_skipped_and_counted(
    app_engine: AsyncEngine, seed_tenant, caplog
) -> None:
    """Nothing to anchor to — counted and logged, never raised."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp05@example.com")

    eur_cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        anchor=("2024-03-31", "1000"),
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="US Fund", currency="USD")
    # USD flows, but the tenant models no USD cash position.
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-04-30",
        amount="-500",
        currency="USD",
    )
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-05-31",
        amount="-100",
        currency="USD",
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        report = await _run(app_engine, tenant_id, actor.id)

    assert report.skipped_flows_no_position == 2
    assert report.positions == 1  # the EUR path still ran
    assert "USD" in caplog.text

    # The unanchorable USD flows landed nowhere — least of all on the EUR path.
    assert await _plan_rows(app_engine, tenant_id, eur_cash.id) == []


async def test_cp06_earliest_created_cash_position_settles_the_currency(
    app_engine: AsyncEngine, seed_tenant, caplog
) -> None:
    """Two active EUR cash positions: the earliest-created one settles.

    ADR-0103 §10 puts multi-custodian sub-balances out of scope, so the ADR
    fixes no meaning for this shape — but an import must not fail on it. The
    rule is deterministic and the ignored position is named in a warning.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp06@example.com")

    # Separate transactions → distinct created_at (func.now() is per-txn).
    first = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR (Custodian A)",
        anchor=("2024-03-31", "1000"),
    )
    second = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR (Custodian B)",
        anchor=("2024-03-31", "5000"),
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund A")
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-04-30",
        amount="-250",
        flow_type="capital_call",
    )

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        report = await _run(app_engine, tenant_id, actor.id)

    assert report.positions == 1
    assert report.ignored_positions == 1
    assert "Cash EUR (Custodian B)" in caplog.text

    # The earliest-created position carries the path; the other is untouched.
    assert _pairs(await _plan_rows(app_engine, tenant_id, first.id)) == [
        (date(2024, 4, 30), Decimal("750.0000"))
    ]
    assert await _plan_rows(app_engine, tenant_id, second.id) == []


async def test_cp07_a_cash_position_without_an_anchor_is_skipped(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """No actual NAV → no anchor → nothing projected, nothing written."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp07@example.com")

    cash = await _cash_position(
        app_engine, tenant_id, actor.id, ac.id, name="Cash EUR", anchor=None
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund A")
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-04-30",
        amount="-250",
        flow_type="capital_call",
    )

    report = await _run(app_engine, tenant_id, actor.id)

    assert report.skipped_no_anchor == 1
    assert report.positions == 0
    assert report.inserted == 0
    assert await _plan_rows(app_engine, tenant_id, cash.id) == []


async def test_cp08_an_inactive_investments_plan_flows_do_not_settle(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A deactivated investment's plan flows leave the projection alone."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp08@example.com")

    cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        anchor=("2024-03-31", "1000"),
    )
    live = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund Live")
    dead = await _investment(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Fund Dead",
        is_active=False,
    )
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=live.id,
        day="2024-04-30",
        amount="-250",
        flow_type="capital_call",
    )
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=dead.id,
        day="2024-05-31",
        amount="-9999",
        flow_type="capital_call",
    )

    await _run(app_engine, tenant_id, actor.id)

    assert _pairs(await _plan_rows(app_engine, tenant_id, cash.id)) == [
        (date(2024, 4, 30), Decimal("750.0000"))
    ]


# ---------------------------------------------------------------------------
# CP-09 / CP-10 / CP-11: idempotency and stranded rows
# ---------------------------------------------------------------------------


async def test_cp09_rerun_is_a_full_noop_with_updated_at_untouched(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Idempotency is a property, not a mode (the ADR-0098 idiom)."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp09@example.com")

    cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        anchor=("2024-03-31", "1000"),
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund A")
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-04-30",
        amount="-250",
        flow_type="capital_call",
    )
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-05-31",
        amount="400",
        flow_type="distribution",
    )

    first = await _run(app_engine, tenant_id, actor.id)
    assert first.inserted == 2
    before = await _plan_rows(app_engine, tenant_id, cash.id)

    second = await _run(app_engine, tenant_id, actor.id)

    assert second.noop == 2
    assert (second.inserted, second.updated, second.deleted) == (0, 0, 0)

    after = await _plan_rows(app_engine, tenant_id, cash.id)
    assert _pairs(after) == _pairs(before)
    assert [r.updated_at for r in after] == [r.updated_at for r in before]


async def test_cp10_removing_a_plan_flow_deletes_its_stranded_row(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The event date left the set — its projected row goes with it."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp10@example.com")

    cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        anchor=("2024-03-31", "1000"),
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund A")
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-04-30",
        amount="-250",
        flow_type="capital_call",
    )
    doomed = await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-05-31",
        amount="400",
        flow_type="distribution",
    )

    await _run(app_engine, tenant_id, actor.id)
    assert len(await _plan_rows(app_engine, tenant_id, cash.id)) == 2

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentCashflowRepository(session).delete(doomed.id)

    report = await _run(app_engine, tenant_id, actor.id)

    assert report.deleted == 1
    assert _pairs(await _plan_rows(app_engine, tenant_id, cash.id)) == [
        (date(2024, 4, 30), Decimal("750.0000"))
    ]


async def test_cp11_a_new_statement_moves_t0_and_restates_the_path(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A later anchor deletes the rows it overtook and re-bases the rest."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp11@example.com")

    cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        anchor=("2024-03-31", "1000"),
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund A")
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-04-30",
        amount="-250",
        flow_type="capital_call",
    )
    await _flow(
        app_engine,
        tenant_id,
        actor.id,
        investment_id=fund.id,
        day="2024-06-30",
        amount="400",
        flow_type="distribution",
    )

    await _run(app_engine, tenant_id, actor.id)
    assert _pairs(await _plan_rows(app_engine, tenant_id, cash.id)) == [
        (date(2024, 4, 30), Decimal("750.0000")),
        (date(2024, 6, 30), Decimal("1150.0000")),
    ]

    # A May statement lands: the April plan row is now history, and the June
    # row re-anchors on the *actual* May balance rather than the old chain.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session).add_nav(
            investment_id=cash.id,
            as_of_date=date(2024, 5, 31),
            nav_kind="actual",
            nav_value=Decimal("900"),
            currency="EUR",
            source="statement",
            created_by=actor.id,
        )

    report = await _run(app_engine, tenant_id, actor.id)

    assert report.deleted == 1  # the stale April row
    assert _pairs(await _plan_rows(app_engine, tenant_id, cash.id)) == [
        (date(2024, 6, 30), Decimal("1300.0000"))  # 900 + 400
    ]


# ---------------------------------------------------------------------------
# CP-12: ownership and precedence
# ---------------------------------------------------------------------------


async def test_cp12_foreign_plan_rows_are_skipped_and_actuals_untouched(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The service owns only its own ``'system'`` + cash-plan rows.

    An ``'excel'`` plan row (a workbook plan column) and a ``'manual'`` one
    (an operator edit) on event dates are precedence-protected: never written,
    never deleted, counted as skipped. Actual rows — the anchor included —
    are outside the write set entirely (a different ``nav_kind``).
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp12@example.com")

    cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        anchor=("2024-03-31", "1000"),
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund A")
    for day, amount in (
        ("2024-04-30", "-250"),
        ("2024-05-31", "-100"),
        ("2024-06-30", "-50"),
    ):
        await _flow(
            app_engine,
            tenant_id,
            actor.id,
            investment_id=fund.id,
            day=day,
            amount=amount,
            flow_type="capital_call",
        )

    # A book-of-record plan row and an operator plan row, both on event dates.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        navs = InvestmentNavRepository(session)
        await navs.upsert(
            investment_id=cash.id,
            as_of_date=date(2024, 4, 30),
            nav_kind="plan",
            nav_value=Decimal("42"),
            currency="EUR",
            source="excel-import",
            created_by=actor.id,
            ingest_origin="excel",
        )
        await _service(session).add_nav(  # stamped 'manual'
            investment_id=cash.id,
            as_of_date=date(2024, 5, 31),
            nav_kind="plan",
            nav_value=Decimal("7"),
            currency="EUR",
            source="operator",
            created_by=actor.id,
        )

    before_actual = await _actual_rows(app_engine, tenant_id, cash.id)

    report = await _run(app_engine, tenant_id, actor.id)

    assert report.skipped_excel == 1
    assert report.skipped_manual == 1
    assert report.inserted == 1  # only the June date was free
    assert report.deleted == 0

    rows = {r.as_of_date: r for r in await _plan_rows(app_engine, tenant_id, cash.id)}

    # The foreign rows survive byte-identical — value, origin and source.
    excel_row = rows[date(2024, 4, 30)]
    assert (excel_row.nav_value, excel_row.ingest_origin) == (
        Decimal("42.0000"),
        "excel",
    )
    manual_row = rows[date(2024, 5, 31)]
    assert (manual_row.nav_value, manual_row.ingest_origin) == (
        Decimal("7.0000"),
        "manual",
    )

    # Only the free date carries a projected row.
    own = rows[date(2024, 6, 30)]
    assert own.ingest_origin == "system"
    assert own.source == _CASH_PLAN_SOURCE

    # Actual rows — a different nav_kind — were never in reach.
    assert _pairs(await _actual_rows(app_engine, tenant_id, cash.id)) == _pairs(before_actual)


# ---------------------------------------------------------------------------
# CP-13 / CP-14: the import triggers
# ---------------------------------------------------------------------------


def _attributes_df(investments: dict[str, dict[str, object]]) -> pd.DataFrame:
    columns = list(investments)
    labels = ["Investment Type", "Asset Class", "Währung"]
    data = [[investments[c].get(label) for c in columns] for label in labels]
    return pd.DataFrame(data, index=labels, columns=columns)


def _timeseries_df(
    by_investment: dict[str, list[tuple[str, float]]], names: list[str]
) -> pd.DataFrame:
    all_dates = sorted({d for s in by_investment.values() for d, _ in s})
    return pd.DataFrame(
        {name: [dict(by_investment.get(name, [])).get(d) for d in all_dates] for name in names},
        index=pd.to_datetime(all_dates),
        columns=names,
    )


async def _import(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    investments: dict[str, dict[str, object]],
    sheets: dict[str, dict[str, list[tuple[str, float]]]],
    file_hash: str,
):
    """Upload a synthetic workbook (Cash + flow sheets) and transform it."""
    names = list(investments)
    frames: dict[str, pd.DataFrame] = {"attributes": _attributes_df(investments)}
    for key, series in sheets.items():
        frames[key] = _timeseries_df(series, names)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        upload = await DataUploadRepository(session).create_upload(
            uploaded_by=user_id,
            filename="plan.xlsx",
            file_hash=file_hash[:64].ljust(64, "0"),
            size_bytes=1024,
            format_version="v2",
            sheets=frames,
        )

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        return await _full_service(session).transform_upload_to_investments(
            upload.id,
            user_id=user_id,
            asset_class_repository=AssetClassRepository(session),
            data_upload_repository=DataUploadRepository(session),
            extractor=InvestmentExtractor(),
        )


async def test_cp13_cp14_the_import_triggers_move_the_projection(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """One import: the Cash sheet sets the anchor, the plan sheets the events.

    CP-13 (statement import) and CP-14 (plan-flow import) in a single
    workbook, which is how they actually arrive — the anchor and the events
    move together, and the projection must be right at the end of the *same*
    transaction. The cash column's plan flows are ``investor_flow`` rows by
    the ADR-0103 §5 sheet convention; the fund column's are capital calls.
    """
    tenant_id = await seed_tenant()
    actor, _ = await _seed(app_engine, tenant_id, email="cp13@example.com")

    result = await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={
            "Cash EUR": {
                "Investment Type": "cash",
                "Asset Class": "cash",
                "Währung": "EUR",
            },
            "Fund A": {
                "Investment Type": "private_equity",
                "Asset Class": "unclassified",
                "Währung": "EUR",
            },
        },
        sheets={
            # Statements → ledger → unity prices → materialised actual NAVs.
            "cash": {"Cash EUR": [("2024-02-29", 800.0), ("2024-03-31", 1000.0)]},
            # A planned capital call on the fund (Out sheet → negative).
            "cash_flow_out_plan": {"Fund A": [("2024-04-30", -250.0)]},
            # A planned investor contribution on the cash column (In sheet).
            "cash_flow_in_plan": {"Cash EUR": [("2024-05-31", 1000.0)]},
        },
        file_hash="cp13",
    )
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        cash = await InvestmentRepository(session).get_by_name("Cash EUR")

    # The anchor is the *materialised* actual NAV of the last statement.
    assert _pairs(await _actual_rows(app_engine, tenant_id, cash.id)) == [
        (date(2024, 2, 29), Decimal("800.0000")),
        (date(2024, 3, 31), Decimal("1000.0000")),
    ]

    # ...and the plan path runs forward from it, through both plan sheets.
    rows = await _plan_rows(app_engine, tenant_id, cash.id)
    assert _pairs(rows) == [
        (date(2024, 4, 30), Decimal("750.0000")),
        (date(2024, 5, 31), Decimal("1750.0000")),
    ]
    assert all(r.source == _CASH_PLAN_SOURCE for r in rows)
    assert all(r.ingest_origin == "system" for r in rows)

    # A re-import of the same workbook is a value-equal no-op on the path.
    await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={
            "Cash EUR": {
                "Investment Type": "cash",
                "Asset Class": "cash",
                "Währung": "EUR",
            },
            "Fund A": {
                "Investment Type": "private_equity",
                "Asset Class": "unclassified",
                "Währung": "EUR",
            },
        },
        sheets={
            "cash": {"Cash EUR": [("2024-02-29", 800.0), ("2024-03-31", 1000.0)]},
            "cash_flow_out_plan": {"Fund A": [("2024-04-30", -250.0)]},
            "cash_flow_in_plan": {"Cash EUR": [("2024-05-31", 1000.0)]},
        },
        file_hash="cp13b",
    )
    assert _pairs(await _plan_rows(app_engine, tenant_id, cash.id)) == [
        (date(2024, 4, 30), Decimal("750.0000")),
        (date(2024, 5, 31), Decimal("1750.0000")),
    ]


# ---------------------------------------------------------------------------
# CP-15 / CP-16 / CP-17: the CRUD triggers
# ---------------------------------------------------------------------------


async def test_cp15_add_update_delete_cashflow_all_move_the_projection(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The three CRUD seams, on a **non**-investor plan flow.

    §6 names "investor-flow import or edit", but a manually edited plan
    ``capital_call`` moves the same projection, and §6's own synchronicity
    requirement forbids leaving it stale until the next import. This is the
    superset case, and the service seam owns it.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp15@example.com")

    cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        anchor=("2024-03-31", "1000"),
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund A")

    # add_cashflow → the row appears in the same transaction.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        flow = await _service(session).add_cashflow(
            investment_id=fund.id,
            flow_timestamp=_ts("2024-04-30"),
            flow_type="capital_call",
            flow_kind="plan",
            amount=Decimal("-250"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
    assert _pairs(await _plan_rows(app_engine, tenant_id, cash.id)) == [
        (date(2024, 4, 30), Decimal("750.0000"))
    ]

    # update_cashflow → the projection restates.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session).update_cashflow(
            flow.id, acting_user=actor.id, amount=Decimal("-400")
        )
    assert _pairs(await _plan_rows(app_engine, tenant_id, cash.id)) == [
        (date(2024, 4, 30), Decimal("600.0000"))
    ]

    # ...including a re-dating, which strands the old date.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session).update_cashflow(
            flow.id, acting_user=actor.id, flow_timestamp=_ts("2024-05-31")
        )
    assert _pairs(await _plan_rows(app_engine, tenant_id, cash.id)) == [
        (date(2024, 5, 31), Decimal("600.0000"))
    ]

    # delete_cashflow → the stranded row goes.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session).delete_cashflow(flow.id, acting_user=actor.id)
    assert await _plan_rows(app_engine, tenant_id, cash.id) == []


async def test_cp15b_an_update_across_currencies_moves_both_projections(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Old currency and new currency both recompute."""
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp15b@example.com")

    eur_cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        currency="EUR",
        anchor=("2024-03-31", "1000"),
    )
    usd_cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash USD",
        currency="USD",
        anchor=("2024-03-31", "2000"),
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund A")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        flow = await _service(session).add_cashflow(
            investment_id=fund.id,
            flow_timestamp=_ts("2024-04-30"),
            flow_type="capital_call",
            flow_kind="plan",
            amount=Decimal("-250"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
    assert len(await _plan_rows(app_engine, tenant_id, eur_cash.id)) == 1
    assert await _plan_rows(app_engine, tenant_id, usd_cash.id) == []

    # Re-denominate it: the EUR path must unwind, the USD path must book it.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session).update_cashflow(flow.id, acting_user=actor.id, currency="USD")

    assert await _plan_rows(app_engine, tenant_id, eur_cash.id) == []
    assert _pairs(await _plan_rows(app_engine, tenant_id, usd_cash.id)) == [
        (date(2024, 4, 30), Decimal("1750.0000"))
    ]


async def test_cp15c_promoting_and_demoting_a_flow_kind_moves_the_path(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The trigger reads the **old or new** state, so both directions move it.

    Promoting an actual flow to plan books a new event; demoting a plan flow
    to actual unbooks it. Reading only the new state would leave the
    projection stale on the demotion.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp15c@example.com")

    cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        anchor=("2024-03-31", "1000"),
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund A")

    # Booked as actual → the plan path stays empty.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        flow = await _service(session).add_cashflow(
            investment_id=fund.id,
            flow_timestamp=_ts("2024-04-30"),
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-250"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
    assert await _plan_rows(app_engine, tenant_id, cash.id) == []

    # Promote it to plan → the event appears.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session).update_cashflow(flow.id, acting_user=actor.id, flow_kind="plan")
    assert _pairs(await _plan_rows(app_engine, tenant_id, cash.id)) == [
        (date(2024, 4, 30), Decimal("750.0000"))
    ]

    # Demote it back → the row is stranded and removed.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session).update_cashflow(flow.id, acting_user=actor.id, flow_kind="actual")
    assert await _plan_rows(app_engine, tenant_id, cash.id) == []


async def test_cp16_actual_flow_mutations_trigger_nothing(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Actual flows are informational on cash (ADR-0103 §5) — no projection.

    Actual balances come from statement levels, so an actual flow must never
    move the plan path (and never double-count into it).
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp16@example.com")

    cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        anchor=("2024-03-31", "1000"),
    )
    fund = await _investment(app_engine, tenant_id, actor.id, ac.id, name="Fund A")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _service(session)
        actual = await svc.add_cashflow(
            investment_id=fund.id,
            flow_timestamp=_ts("2024-04-30"),
            flow_type="capital_call",
            flow_kind="actual",
            amount=Decimal("-250"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
        await svc.update_cashflow(actual.id, acting_user=actor.id, amount=Decimal("-400"))
        await svc.delete_cashflow(actual.id, acting_user=actor.id)

    assert await _plan_rows(app_engine, tenant_id, cash.id) == []


async def test_cp17_the_three_repository_service_projects_the_plan_path(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Wiring posture: the plan path has no optional dependency to lose.

    ADR-0098's loud failure exists because the *price* repository is optional
    and a unitised write without it is a programming error. The plan path
    needs only investments, NAVs and cashflows — all three mandatory on every
    construction site — so a plan-kind mutation on the minimal service must
    project, not raise. That is the posture, satisfied structurally.
    """
    tenant_id = await seed_tenant()
    actor, ac = await _seed(app_engine, tenant_id, email="cp17@example.com")

    cash = await _cash_position(
        app_engine,
        tenant_id,
        actor.id,
        ac.id,
        name="Cash EUR",
        anchor=("2024-03-31", "1000"),
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        minimal = InvestmentService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            cashflows=InvestmentCashflowRepository(session),
        )
        await minimal.add_cashflow(
            investment_id=cash.id,
            flow_timestamp=_ts("2024-04-30"),
            flow_type="investor_flow",
            flow_kind="plan",
            amount=Decimal("500"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )

    assert _pairs(await _plan_rows(app_engine, tenant_id, cash.id)) == [
        (date(2024, 4, 30), Decimal("1500.0000"))
    ]
