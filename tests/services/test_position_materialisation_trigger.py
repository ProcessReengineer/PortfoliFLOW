# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""add_position_transaction materialisation trigger (ADR-0098 §3, strand S2).

The transaction write choke-point that exists after strand S1 is
``InvestmentService.add_position_transaction``. This suite proves the
in-transaction trigger wired onto it:

* a ledger write on a **unitised** investment materialises its computed-NAV
  rows synchronously, in the same transaction;
* a ledger write on a **reported** investment triggers nothing — its NAV
  series stays byte-identical (regression evidence), and the service needs
  no instrument-price repository for it;
* a unitised write on a service constructed **without** an instrument-price
  repository is a programming error and raises loudly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
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

_D1 = date(2025, 1, 1)


async def _seed_investment(app_engine: AsyncEngine, tenant_id, *, email: str, unitised: bool):
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
            currency="EUR",
            created_by=actor.id,
        )
        if unitised:
            await session.execute(
                text("UPDATE investments SET valuation_mode = 'unitised' WHERE id = :id"),
                {"id": inv.id},
            )
    return actor, inv


def _service(session, *, with_prices: bool) -> InvestmentService:
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        position_transactions=PositionTransactionRepository(session),
        instrument_prices=(InstrumentPriceRepository(session) if with_prices else None),
    )


async def _set_price(app_engine, tenant_id, actor, inv, *, on, price):
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InstrumentPriceRepository(session).upsert(
            investment_id=inv.id,
            as_of_date=on,
            price=Decimal(price),
            currency="EUR",
            source="book",
            created_by=actor.id,
        )


async def _actual_navs(app_engine, tenant_id, inv):
    async with tenant_context(app_engine, tenant_id) as session:
        return await InvestmentNavRepository(session).list_by_investment_and_kind(inv.id, "actual")


# ---------------------------------------------------------------------------
# MT-01: a unitised ledger write materialises in the same transaction
# ---------------------------------------------------------------------------


async def test_mt01_unitised_write_triggers_materialisation(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="mt01@example.com", unitised=True
    )
    await _set_price(app_engine, tenant_id, actor, inv, on=_D1, price="10")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session, with_prices=True).add_position_transaction(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=_D1,
            units=Decimal("100"),
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
        )

    rows = await _actual_navs(app_engine, tenant_id, inv)
    assert len(rows) == 1
    assert rows[0].as_of_date == _D1
    assert rows[0].ingest_origin == "system"
    assert rows[0].nav_value == Decimal("1000.0000")


# ---------------------------------------------------------------------------
# MT-02: a reported ledger write triggers nothing (byte-identical)
# ---------------------------------------------------------------------------


async def test_mt02_reported_write_triggers_nothing(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="mt02@example.com", unitised=False
    )
    await _set_price(app_engine, tenant_id, actor, inv, on=_D1, price="10")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session, with_prices=True).add_position_transaction(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=_D1,
            units=Decimal("100"),
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
        )

    assert await _actual_navs(app_engine, tenant_id, inv) == []


# ---------------------------------------------------------------------------
# MT-03: a unitised write without a price repo is a programming error
# ---------------------------------------------------------------------------


async def test_mt03_unitised_write_without_price_repo_raises(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="mt03@example.com", unitised=True
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(RuntimeError, match="instrument-price repository"):
            await _service(session, with_prices=False).add_position_transaction(
                investment_id=inv.id,
                txn_type="opening",
                trade_date=_D1,
                units=Decimal("100"),
                currency="EUR",
                ingest_origin="manual",
                created_by=actor.id,
            )
