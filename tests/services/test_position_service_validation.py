# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentService.add_position_transaction validation (ADR-0097 §4/§5).

Exercises the service-layer write seam against the live compose Postgres:

* Happy path — an opening then a buy persist and read back in order.
* Currency equality (§5) — a mismatched currency raises
  ``CurrencyMismatchError`` and writes nothing.
* Non-negativity (§4) — a sell that overdraws raises
  ``NonNegativeHoldingsError`` and leaves the ledger unchanged.
* A missing investment raises ``ValidationError``.

PS-05 through PS-07 are the ADR-0130 regression anchors, one per side of
the split the emission engine must be able to rely on: **cash exempt on
every path** (add, update, delete) and **instrument unconditional on every
path** (add is PS-03; update and delete are PS-07).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import (
    CurrencyMismatchError,
    NonNegativeHoldingsError,
    ValidationError,
)
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
from services.investments.holdings import holdings_as_of


def _service(session) -> InvestmentService:
    """Build an InvestmentService with the ledger repo wired in.

    ``instrument_prices`` is wired too so a ``'unitised'`` target may be
    seeded: a ledger write on one triggers the ADR-0098 materialisation,
    which needs the price repository. With no price rows the pass is an
    all-zero no-op, so the ``'reported'`` cases below are unaffected.
    """
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
    )


async def _seed_investment(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    currency: str = "EUR",
    investment_type: str = "listed_equity",
    valuation_mode: str = "reported",
):
    """Seed one investment (plus its actor and asset class) in ``tenant_id``.

    ``investment_type`` carries the ADR-0130 split: ``'cash'`` is exempt from
    the non-negativity guard, every other type is not. The asset-class code
    and the investment name are derived from it so a test may seed one of
    each in the same tenant without colliding on either unique key.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code=f"{investment_type}_class",
            display_name=f"{investment_type.replace('_', ' ').title()} Class",
        )
        inv = await InvestmentRepository(session).create(
            name=f"{investment_type.replace('_', ' ').title()} Fund",
            investment_type=investment_type,
            asset_class_id=ac.id,
            currency=currency,
            created_by=actor.id,
            valuation_mode=valuation_mode,
        )
    return actor, inv


async def _ledger(app_engine: AsyncEngine, tenant_id, investment_id):
    """Return the investment's ledger rows, newest state, in canonical order."""
    async with tenant_context(app_engine, tenant_id) as session:
        return await PositionTransactionRepository(session).list_for_investment(investment_id)


# ---------------------------------------------------------------------------
# PS-01: happy path
# ---------------------------------------------------------------------------


async def test_ps01_opening_then_buy_persist(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="ps01@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _service(session)
        await svc.add_position_transaction(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=date(2025, 1, 1),
            units=Decimal("100"),
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )
        await svc.add_position_transaction(
            investment_id=inv.id,
            txn_type="buy",
            trade_date=date(2025, 6, 1),
            units=Decimal("50"),
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
            price_per_unit=Decimal("12.5"),
        )

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await PositionTransactionRepository(session).list_for_investment(inv.id)
    assert [r.txn_type for r in rows] == ["opening", "buy"]


# ---------------------------------------------------------------------------
# PS-02: currency equality (§5)
# ---------------------------------------------------------------------------


async def test_ps02_currency_mismatch_rejected(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="ps02@example.com", currency="EUR"
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _service(session)
        with pytest.raises(CurrencyMismatchError):
            await svc.add_position_transaction(
                investment_id=inv.id,
                txn_type="opening",
                trade_date=date(2025, 1, 1),
                units=Decimal("100"),
                currency="USD",  # investment is EUR
                ingest_origin="excel",
                created_by=actor.id,
            )

    # Nothing was written.
    async with tenant_context(app_engine, tenant_id) as session:
        rows = await PositionTransactionRepository(session).list_for_investment(inv.id)
    assert rows == []


# ---------------------------------------------------------------------------
# PS-03: non-negativity (§4)
# ---------------------------------------------------------------------------


async def test_ps03_overdraw_rejected_and_ledger_unchanged(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="ps03@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _service(session)
        await svc.add_position_transaction(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=date(2025, 1, 1),
            units=Decimal("100"),
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )
        with pytest.raises(NonNegativeHoldingsError):
            await svc.add_position_transaction(
                investment_id=inv.id,
                txn_type="sell",
                trade_date=date(2025, 6, 1),
                units=Decimal("-150"),  # overdraws the 100 held
                currency="EUR",
                ingest_origin="manual",
                created_by=actor.id,
                price_per_unit=Decimal("10"),
            )

    # Only the opening survives; the overdrawing sell was rejected.
    async with tenant_context(app_engine, tenant_id) as session:
        rows = await PositionTransactionRepository(session).list_for_investment(inv.id)
    assert [r.txn_type for r in rows] == ["opening"]


# ---------------------------------------------------------------------------
# PS-04: missing investment
# ---------------------------------------------------------------------------


async def test_ps04_missing_investment_rejected(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(
            email="ps04@example.com", password_hash="x" * 8
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _service(session)
        with pytest.raises(ValidationError):
            await svc.add_position_transaction(
                investment_id=uuid4(),
                txn_type="opening",
                trade_date=date(2025, 1, 1),
                units=Decimal("100"),
                currency="EUR",
                ingest_origin="excel",
                created_by=actor.id,
            )


# ---------------------------------------------------------------------------
# PS-05: cash is exempt on the add path (ADR-0130)
# ---------------------------------------------------------------------------


async def test_ps05_cash_add_below_zero_is_accepted(app_engine: AsyncEngine, seed_tenant) -> None:
    """A transfer that overdraws a cash position is booked, not refused.

    The ADR-0130 anchor for the cash side of the add path. An overdraft is an
    economic fact the book must be able to record (ADR-0128 Q-2), so the
    ADR-0097 §4 guard does not fire on an ``investment_type='cash'`` target;
    the resulting negative balance is surfaced elsewhere, never refused here.
    """
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine,
        tenant_id,
        email="ps05@example.com",
        investment_type="cash",
        valuation_mode="unitised",
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _service(session)
        await svc.add_position_transaction(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=date(2025, 1, 1),
            units=Decimal("1000"),
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
        )
        await svc.add_position_transaction(
            investment_id=inv.id,
            txn_type="transfer",
            trade_date=date(2025, 6, 1),
            units=Decimal("-1500"),  # overdraws the 1000 held
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
            price_per_unit=Decimal("1.0000"),
        )

    rows = await _ledger(app_engine, tenant_id, inv.id)
    assert [r.txn_type for r in rows] == ["opening", "transfer"]
    assert holdings_as_of(rows, date(2025, 5, 31)) == Decimal("1000.00000000")
    assert holdings_as_of(rows, date(2025, 6, 1)) == Decimal("-500.00000000")
    assert holdings_as_of(rows, date(2025, 12, 31)) == Decimal("-500.00000000")


# ---------------------------------------------------------------------------
# PS-06: cash is exempt on the update and delete paths (ADR-0130)
# ---------------------------------------------------------------------------


async def test_ps06_cash_update_and_delete_below_zero_are_accepted(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The exemption is a property of the target, not of the write path.

    The correction path ADR-0128 §6 depends on stays open in every state of
    the book: restating a cash row into an overdraft, and removing the
    opening beneath one, are both ordinary edits.
    """
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine,
        tenant_id,
        email="ps06@example.com",
        investment_type="cash",
        valuation_mode="unitised",
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _service(session)
        opening = await svc.add_position_transaction(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=date(2025, 1, 1),
            units=Decimal("1000"),
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
        )
        transfer = await svc.add_position_transaction(
            investment_id=inv.id,
            txn_type="transfer",
            trade_date=date(2025, 6, 1),
            units=Decimal("-400"),
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
            price_per_unit=Decimal("1.0000"),
        )

    # Restate the transfer to a value the remaining balance cannot cover.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        updated = await _service(session).update_position_transaction(
            investment_id=inv.id,
            transaction_id=transfer.id,
            trade_date=date(2025, 6, 1),
            units=Decimal("-1500"),
            acting_user=actor.id,
            price_per_unit=Decimal("1.0000"),
        )
    assert updated is not None
    assert updated.units == Decimal("-1500.00000000")

    rows = await _ledger(app_engine, tenant_id, inv.id)
    assert holdings_as_of(rows, date(2025, 6, 1)) == Decimal("-500.00000000")

    # Remove the opening beneath the overdraft.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        assert await _service(session).delete_position_transaction(
            investment_id=inv.id,
            transaction_id=opening.id,
            acting_user=actor.id,
        )

    rows = await _ledger(app_engine, tenant_id, inv.id)
    assert [r.txn_type for r in rows] == ["transfer"]
    assert holdings_as_of(rows, date(2025, 6, 1)) == Decimal("-1500.00000000")


# ---------------------------------------------------------------------------
# PS-07: an instrument target is guarded on every path (ADR-0097 §4)
# ---------------------------------------------------------------------------


async def test_ps07_instrument_update_and_delete_below_zero_are_rejected(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The other side of the ADR-0130 split, completing PS-03.

    ADR-0130 narrows the guard to non-cash targets and to nothing else: for
    an instrument it still holds unconditionally, on the update and delete
    paths as much as on the add path PS-03 pins. Each refusal leaves the
    ledger byte-identical.
    """
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine,
        tenant_id,
        email="ps07@example.com",
        valuation_mode="unitised",
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        svc = _service(session)
        opening = await svc.add_position_transaction(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=date(2025, 1, 1),
            units=Decimal("100"),
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )
        sell = await svc.add_position_transaction(
            investment_id=inv.id,
            txn_type="sell",
            trade_date=date(2025, 6, 1),
            units=Decimal("-60"),
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
            price_per_unit=Decimal("10"),
        )

    before = await _ledger(app_engine, tenant_id, inv.id)

    # Restating the sell beyond the units held is refused.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(NonNegativeHoldingsError):
            await _service(session).update_position_transaction(
                investment_id=inv.id,
                transaction_id=sell.id,
                trade_date=date(2025, 6, 1),
                units=Decimal("-150"),
                acting_user=actor.id,
                price_per_unit=Decimal("10"),
            )

    # So is deleting the opening the sell depends on.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(NonNegativeHoldingsError):
            await _service(session).delete_position_transaction(
                investment_id=inv.id,
                transaction_id=opening.id,
                acting_user=actor.id,
            )

    after = await _ledger(app_engine, tenant_id, inv.id)
    assert [(r.id, r.trade_date, r.units) for r in after] == [
        (r.id, r.trade_date, r.units) for r in before
    ]
