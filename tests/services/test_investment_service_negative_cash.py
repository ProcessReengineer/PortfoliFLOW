# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentService.list_negative_cash / negative_cash_for — the A-11 seam.

The one derivation behind the negative-cash indicator (T-5 D-Z). Two
surfaces read it — the Transactions Area banner and the cash position's own
detail page — and they live in two route modules that may not import each
other, so the derivation lives in the service and these live-DB tests are
where it is pinned.

What the tests establish:

* **NC-01** the whole book — only cash positions, only negative ones,
  ordered ``(currency, name)``, never summed across currencies.
* **NC-02** a non-cash investment with a negative ledger is not a
  negative-cash position, whatever its ledger says.
* **NC-03** an **inactive** cash position is listed (T-5 D-Y): deactivating
  a position does not settle its overdraft.
* **NC-04** ``negative_cash_for`` — the single-position form: ``None`` for a
  healthy position, ``None`` for a non-cash one, ``None`` for an unknown id,
  the row for an overdrawn one.
* **NC-05** the ``on`` semantics — a future-dated inflow that only takes the
  balance to zero tomorrow leaves the position listed today. This is why the
  indicator does not read ``get_position_summary``, whose ``holdings_units``
  is the *last ledger point* and so may be future-dated.
* **NC-06** ``since`` is the start of the **current** run, not the first-ever
  negative date — the service-level end of the ``negative_since`` pin.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InstrumentPriceRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    PositionTransactionRepository,
    UserRepository,
    tenant_context,
)
from services.investments import InvestmentService


_TODAY = date(2026, 9, 7)
_OPENING = date(2026, 1, 2)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _service(session) -> InvestmentService:
    """The service wired as both reading routes wire it."""
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
        asset_classes=AssetClassRepository(session),
    )


async def _seed_actor(app_engine: AsyncEngine, tenant_id, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        return await UserRepository(session).create(email=email, password_hash="x" * 8)


async def _seed_position(
    app_engine: AsyncEngine,
    tenant_id,
    actor_id,
    *,
    name: str,
    currency: str = "EUR",
    investment_type: str = "cash",
    is_active: bool = True,
    ledger: tuple[tuple[str, date, str], ...] = (),
):
    """Create one position and write its ledger rows verbatim.

    Rows are written through the repository rather than through
    ``add_position_transaction``: the service's write path runs the ADR-0097
    §4 non-negativity guard, and NC-02 needs a *non-cash* ledger that goes
    negative — a state the guard exists to prevent and the reader must
    nonetheless not mistake for a cash overdraft.
    """
    async with tenant_context(app_engine, tenant_id, user_id=actor_id) as session:
        asset_class = await AssetClassRepository(session).create(
            code=f"ac-{uuid4().hex[:8]}",
            display_name=f"AC {name}",
        )
        investment = await InvestmentRepository(session).create(
            name=name,
            investment_type=investment_type,
            asset_class_id=asset_class.id,
            currency=currency,
            created_by=actor_id,
            is_active=is_active,
            valuation_mode="unitised",
        )
        transactions = PositionTransactionRepository(session)
        for txn_type, trade_date, units in ledger:
            await transactions.add(
                investment_id=investment.id,
                txn_type=txn_type,
                trade_date=trade_date,
                units=Decimal(units),
                currency=currency,
                ingest_origin="manual",
                created_by=actor_id,
            )
        return investment


async def _standard_book(app_engine: AsyncEngine, tenant_id, actor_id):
    """Three cash positions — EUR at −42,310, USD at −1,250, GBP at +10."""
    eur = await _seed_position(
        app_engine,
        tenant_id,
        actor_id,
        name="Cash EUR · Main custody",
        currency="EUR",
        ledger=(
            ("opening", _OPENING, "100000"),
            ("transfer", date(2026, 9, 5), "-142310"),
        ),
    )
    usd = await _seed_position(
        app_engine,
        tenant_id,
        actor_id,
        name="Cash USD",
        currency="USD",
        ledger=(
            ("opening", _OPENING, "5000"),
            ("transfer", date(2026, 9, 2), "-6250"),
        ),
    )
    gbp = await _seed_position(
        app_engine,
        tenant_id,
        actor_id,
        name="Cash GBP",
        currency="GBP",
        ledger=(("opening", _OPENING, "10"),),
    )
    return eur, usd, gbp


# ---------------------------------------------------------------------------
# NC-01 / NC-02: the whole book
# ---------------------------------------------------------------------------


async def test_nc01_lists_only_negative_cash_ordered_by_currency_then_name(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="nc01@example.com")
    eur, usd, _gbp = await _standard_book(app_engine, tenant_id, actor.id)
    # A second EUR position, alphabetically before the first, to pin that the
    # ordering is (currency, name) rather than insertion order.
    await _seed_position(
        app_engine,
        tenant_id,
        actor.id,
        name="Cash EUR · Broker",
        currency="EUR",
        ledger=(("opening", _OPENING, "1"), ("transfer", date(2026, 8, 1), "-6")),
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        rows = await _service(session).list_negative_cash(on=_TODAY)

    assert [(row.currency, row.name) for row in rows] == [
        ("EUR", "Cash EUR · Broker"),
        ("EUR", "Cash EUR · Main custody"),
        ("USD", "Cash USD"),
    ]
    by_id = {row.investment_id: row for row in rows}
    assert by_id[eur.id].balance == Decimal("-42310.00000000")
    assert by_id[eur.id].since == date(2026, 9, 5)
    assert by_id[usd.id].balance == Decimal("-1250.00000000")
    assert by_id[usd.id].since == date(2026, 9, 2)
    # The healthy GBP position is simply not here — self-clearing by
    # construction, not by a rule that excludes it.
    assert all(row.currency != "GBP" for row in rows)
    assert all(row.is_active for row in rows)


async def test_nc02_a_non_cash_negative_ledger_is_not_negative_cash(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The seam is cash-only: it asks ``list_by_type('cash')``, not the book."""
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="nc02@example.com")
    equity = await _seed_position(
        app_engine,
        tenant_id,
        actor.id,
        name="Alpha Global Equity Fund",
        investment_type="listed_equity",
        ledger=(("opening", _OPENING, "100"), ("transfer", date(2026, 5, 1), "-400")),
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _service(session)
        rows = await service.list_negative_cash(on=_TODAY)
        single = await service.negative_cash_for(equity.id, on=_TODAY)

    assert rows == []
    assert single is None


# ---------------------------------------------------------------------------
# NC-03: an inactive cash position (T-5 D-Y)
# ---------------------------------------------------------------------------


async def test_nc03_an_inactive_cash_position_is_still_listed(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Deactivating a position does not settle its overdraft."""
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="nc03@example.com")
    retired = await _seed_position(
        app_engine,
        tenant_id,
        actor.id,
        name="Cash EUR · Retired",
        is_active=False,
        ledger=(("opening", _OPENING, "10"), ("transfer", date(2026, 4, 1), "-60")),
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        rows = await _service(session).list_negative_cash(on=_TODAY)

    assert [row.investment_id for row in rows] == [retired.id]
    assert rows[0].is_active is False
    assert rows[0].balance == Decimal("-50.00000000")


# ---------------------------------------------------------------------------
# NC-04: negative_cash_for — the single-position form
# ---------------------------------------------------------------------------


async def test_nc04_negative_cash_for_answers_only_for_an_overdrawn_cash_row(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="nc04@example.com")
    eur, _usd, gbp = await _standard_book(app_engine, tenant_id, actor.id)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _service(session)
        overdrawn = await service.negative_cash_for(eur.id, on=_TODAY)
        healthy = await service.negative_cash_for(gbp.id, on=_TODAY)
        unknown = await service.negative_cash_for(uuid4(), on=_TODAY)

    assert healthy is None
    assert unknown is None
    assert overdrawn is not None
    assert overdrawn.investment_id == eur.id
    assert overdrawn.name == "Cash EUR · Main custody"
    assert overdrawn.currency == "EUR"
    assert overdrawn.balance == Decimal("-42310.00000000")
    assert overdrawn.since == date(2026, 9, 5)
    assert overdrawn.is_active is True


# ---------------------------------------------------------------------------
# NC-05: the `on` semantics
# ---------------------------------------------------------------------------


async def test_nc05_a_future_dated_inflow_does_not_clear_today(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Tomorrow's repair is not today's balance.

    ``get_position_summary`` reports ``points[-1]`` — the last ledger point,
    which here is tomorrow's. The indicator asks ``holdings_as_of(…, today)``
    instead, and the two must not be confused.
    """
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="nc05@example.com")
    position = await _seed_position(
        app_engine,
        tenant_id,
        actor.id,
        name="Cash EUR · Awaiting funding",
        ledger=(
            ("opening", _OPENING, "100"),
            ("transfer", date(2026, 9, 4), "-600"),
            ("transfer", date(2026, 9, 8), "900"),  # lands tomorrow
        ),
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = _service(session)
        today = await service.list_negative_cash(on=_TODAY)
        tomorrow = await service.list_negative_cash(on=date(2026, 9, 8))
        summary = await service.get_position_summary(position.id)

    assert [row.investment_id for row in today] == [position.id]
    assert today[0].balance == Decimal("-500.00000000")
    assert tomorrow == []
    # The summary is unchanged and still speaks for the last ledger point.
    assert summary is not None
    assert summary.holdings_as_of_date == date(2026, 9, 8)
    assert summary.holdings_units == Decimal("400.00000000")


# ---------------------------------------------------------------------------
# NC-06: `since` is the current run
# ---------------------------------------------------------------------------


async def test_nc06_since_is_the_current_run_not_the_first_ever(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, email="nc06@example.com")
    position = await _seed_position(
        app_engine,
        tenant_id,
        actor.id,
        name="Cash EUR · Twice",
        ledger=(
            ("opening", _OPENING, "100"),
            ("transfer", date(2026, 3, 1), "-300"),  # −200
            ("transfer", date(2026, 6, 1), "500"),  # +300, recovered
            ("transfer", date(2026, 9, 1), "-800"),  # −500, negative again
        ),
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        row = await _service(session).negative_cash_for(position.id, on=_TODAY)

    assert row is not None
    assert row.since == date(2026, 9, 1)
    assert row.balance == Decimal("-500.00000000")
