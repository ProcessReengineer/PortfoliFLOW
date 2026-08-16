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
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    PositionTransactionRepository,
    UserRepository,
    tenant_context,
)
from services.investments import InvestmentService


def _service(session) -> InvestmentService:
    """Build an InvestmentService with the ledger repo wired in."""
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        position_transactions=PositionTransactionRepository(session),
    )


async def _seed_investment(
    app_engine: AsyncEngine, tenant_id, *, email: str, currency: str = "EUR"
):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="listed_class", display_name="Listed Class"
        )
        inv = await InvestmentRepository(session).create(
            name="Listed Fund",
            investment_type="listed_equity",
            asset_class_id=ac.id,
            currency=currency,
            created_by=actor.id,
        )
    return actor, inv


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
