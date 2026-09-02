# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""InvestmentService.create_cash_position — the MD-3 inline creation seam.

A cash position is a **triple** (ADR-0103 §1): the ``investments`` row, a
unity ``instrument_prices`` row, and — when it opens with a balance — the
``opening`` ledger row. Only the whole triple is a position: without the
unity row the ADR-0098 materialisation has no date to value, so the position
carries no NAV and is invisible to AUM while looking perfectly healthy in
the investment list.

These live-DB tests pin the triple, and the boundaries around it, against
the compose Postgres:

* **CP-01** happy path — all three rows land, and ``holdings × 1``
  materialises the balance as an ``'actual'`` NAV.
* **CP-02** zero balance — investment and unity price only; no ledger row
  (``ck_position_transactions_sign`` requires ``units > 0`` on an opening,
  and an unchanged balance is not an event), hence no NAV.
* **CP-03** negative balance — ``ValidationError`` before the first write.
* **CP-04** blank name — likewise.
* **CP-05** no ``cash`` asset class in the tenant catalogue —
  ``ValidationError`` naming the real remedy; nothing written.
* **CP-06** duplicate name — the raw ``IntegrityError`` the CRUD surface
  already maps to ``409`` (the documented contract S4a's mini-form consumes).
* **CP-07** a service constructed without ``asset_classes`` fails loudly.

Every row this seam writes carries ``ingest_origin='manual'``: the Cash-sheet
importer owns its ``'excel'`` rows and reconciles them by classify-then-write,
so the two writers must never mistake each other's rows for their own.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import ValidationError
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
from services.investments.unity_price import UNITY_PRICE


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _service(session) -> InvestmentService:
    """Build the service wired exactly as the S4a composer route must wire it.

    All three optional repositories the seam touches: ``asset_classes`` to
    resolve the ``cash`` class, ``instrument_prices`` for the unity row (and
    the materialisation that reads it), ``position_transactions`` for the
    opening.
    """
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=InstrumentPriceRepository(session),
        asset_classes=AssetClassRepository(session),
    )


async def _seed(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    with_cash_class: bool = True,
):
    """Seed the acting user and, unless suppressed, the ``cash`` asset class.

    ``with_cash_class=False`` reproduces a tenant whose seed catalogue was
    never run (CP-05) — the one tenant-configuration failure this seam has
    to state in operator language.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    if with_cash_class:
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await AssetClassRepository(session).create(code="cash", display_name="Cash")
    return actor


async def _book(app_engine: AsyncEngine, tenant_id, investment_id):
    """Return ``(ledger, prices, actual_navs)`` for one position."""
    async with tenant_context(app_engine, tenant_id) as session:
        ledger = await PositionTransactionRepository(session).list_for_investment(investment_id)
        prices = await InstrumentPriceRepository(session).list_by_investment(investment_id)
        navs = await InvestmentNavRepository(session).list_by_investment_and_kind(
            investment_id, "actual"
        )
    return ledger, prices, navs


async def _investments(app_engine: AsyncEngine, tenant_id):
    async with tenant_context(app_engine, tenant_id) as session:
        return await InvestmentRepository(session).list_all()


# ---------------------------------------------------------------------------
# CP-01: happy path — the whole triple
# ---------------------------------------------------------------------------


async def test_cp01_opening_balance_writes_the_whole_triple(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cp01@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        cash_class = await AssetClassRepository(session).get_by_code("cash")
        created = await _service(session).create_cash_position(
            name="Cash USD",
            currency="USD",
            opening_balance=Decimal("2500.50"),
            opening_date=date(2026, 1, 31),
            created_by=actor.id,
        )

    # 1) The investment row: a cash position is unitised from birth — its
    #    balance *is* its holdings (ADR-0103 §1).
    assert created.investment_type == "cash"
    assert created.valuation_mode == "unitised"
    assert created.currency == "USD"
    assert created.is_active is True
    assert created.asset_class_id == cash_class.id
    assert created.name == "Cash USD"

    ledger, prices, navs = await _book(app_engine, tenant_id, created.id)

    # 2) Exactly one unity price, on the opening date, in the position's own
    #    currency, stamped 'manual' so the Cash-sheet importer never claims it.
    assert len(prices) == 1
    assert prices[0].as_of_date == date(2026, 1, 31)
    assert prices[0].price == UNITY_PRICE
    assert prices[0].currency == "USD"
    assert prices[0].ingest_origin == "manual"

    # 3) Exactly one opening, carrying the balance as units, unpriced.
    assert [t.txn_type for t in ledger] == ["opening"]
    assert ledger[0].trade_date == date(2026, 1, 31)
    assert ledger[0].units == Decimal("2500.50000000")
    assert ledger[0].currency == "USD"
    assert ledger[0].ingest_origin == "manual"
    assert ledger[0].price_per_unit is None

    # 4) …and the ADR-0098 materialisation, which ran in-transaction on the
    #    ledger write, values holdings × 1 back to the balance.
    assert [(n.as_of_date, n.nav_value) for n in navs] == [
        (date(2026, 1, 31), Decimal("2500.5000"))
    ]
    assert navs[0].currency == "USD"
    assert navs[0].ingest_origin == "system"
    assert navs[0].basis == "computed"


# ---------------------------------------------------------------------------
# CP-02: zero balance — an empty position is a position
# ---------------------------------------------------------------------------


async def test_cp02_zero_balance_writes_no_ledger_row(app_engine: AsyncEngine, seed_tenant) -> None:
    """The position exists, empty; its first NAV arrives with its first flow."""
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cp02@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        created = await _service(session).create_cash_position(
            name="Cash EUR",
            currency="EUR",
            opening_balance=Decimal("0"),
            opening_date=date(2026, 2, 28),
            created_by=actor.id,
        )

    ledger, prices, navs = await _book(app_engine, tenant_id, created.id)

    assert created.valuation_mode == "unitised"
    assert [p.as_of_date for p in prices] == [date(2026, 2, 28)]
    assert prices[0].price == UNITY_PRICE
    assert ledger == []
    assert navs == []


# ---------------------------------------------------------------------------
# CP-03 / CP-04: input validation precedes the first write
# ---------------------------------------------------------------------------


async def test_cp03_negative_balance_rejected_and_nothing_written(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cp03@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(ValidationError) as excinfo:
            await _service(session).create_cash_position(
                name="Cash GBP",
                currency="GBP",
                opening_balance=Decimal("-1"),
                opening_date=date(2026, 3, 31),
                created_by=actor.id,
            )
    assert excinfo.value.field == "opening_balance"

    # Not even the investment row: validation runs before the first write.
    assert await _investments(app_engine, tenant_id) == []


async def test_cp04_blank_name_rejected_and_nothing_written(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cp04@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(ValidationError) as excinfo:
            await _service(session).create_cash_position(
                name="   ",
                currency="EUR",
                opening_balance=Decimal("100"),
                opening_date=date(2026, 3, 31),
                created_by=actor.id,
            )
    assert excinfo.value.field == "name"

    assert await _investments(app_engine, tenant_id) == []


# ---------------------------------------------------------------------------
# CP-05: a tenant whose seed catalogue holds no 'cash' class
# ---------------------------------------------------------------------------


async def test_cp05_missing_cash_asset_class_names_the_remedy(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A tenant-configuration fault, stated as one operator-facing sentence."""
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cp05@example.com", with_cash_class=False)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(ValidationError) as excinfo:
            await _service(session).create_cash_position(
                name="Cash CHF",
                currency="CHF",
                opening_balance=Decimal("10"),
                opening_date=date(2026, 4, 30),
                created_by=actor.id,
            )
    # No field: the operator's input is fine, the tenant's catalogue is not.
    assert excinfo.value.field is None
    assert "seed" in str(excinfo.value)

    assert await _investments(app_engine, tenant_id) == []


# ---------------------------------------------------------------------------
# CP-06: duplicate name — the contract the route maps to 409
# ---------------------------------------------------------------------------


async def test_cp06_duplicate_name_raises_integrity_error(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """No pre-check: check-then-write would race ``uq_investments_tenant_name``."""
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cp06@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session).create_cash_position(
            name="Cash EUR",
            currency="EUR",
            opening_balance=Decimal("100"),
            opening_date=date(2026, 5, 31),
            created_by=actor.id,
        )

    with pytest.raises(IntegrityError):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            await _service(session).create_cash_position(
                name="Cash EUR",
                currency="EUR",
                opening_balance=Decimal("200"),
                opening_date=date(2026, 6, 30),
                created_by=actor.id,
            )

    assert [i.name for i in await _investments(app_engine, tenant_id)] == ["Cash EUR"]


# ---------------------------------------------------------------------------
# CP-07: loud wiring
# ---------------------------------------------------------------------------


async def test_cp07_missing_asset_class_repository_fails_loudly(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """An unwired dependency is a programming error, not a user error."""
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cp07@example.com")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        service = InvestmentService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            cashflows=InvestmentCashflowRepository(session),
            position_transactions=PositionTransactionRepository(session),
            instrument_prices=InstrumentPriceRepository(session),
        )
        with pytest.raises(RuntimeError, match="asset-class repository"):
            await service.create_cash_position(
                name="Cash EUR",
                currency="EUR",
                opening_balance=Decimal("100"),
                opening_date=date(2026, 7, 31),
                created_by=actor.id,
            )

    assert await _investments(app_engine, tenant_id) == []
