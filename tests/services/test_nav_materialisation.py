# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""NavMaterialisationService tests (ADR-0098 §2, strand S2).

Exercises the computed-NAV materialisation end to end against the live
compose Postgres under the unprivileged ``portfoliflow_app`` role:

* insert / update / value-equal no-op classification of ``'system'`` rows;
* idempotency — a re-run is byte-identical, no ``updated_at`` bumps;
* stranded-``'system'``-row deletion on a backdated sale and on a price
  deletion, never touching another origin;
* ``'excel'`` / ``'manual'`` precedence skips and the ``'live'`` skip-with-
  warning;
* the ``reported``-mode whole-investment no-op (regression evidence).

The service delegates the pure holdings step function to
``services.investments.holdings`` and owns the join, classify, write, and
delete. Every repository shares one session, so each run is a single
in-transaction unit.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    InstrumentPriceRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    PositionTransactionRepository,
    UserRepository,
    tenant_context,
)
from services.investments import NavMaterialisationService

_D1 = date(2025, 1, 1)
_D2 = date(2025, 1, 2)


async def _seed_investment(
    app_engine: AsyncEngine,
    tenant_id,
    *,
    email: str,
    unitised: bool,
):
    """Create a user + asset class + investment; optionally flip to unitised.

    The operator flip is strand S5; tests arrange the mode directly with a
    tenant-scoped ``UPDATE`` (allowed under RLS for the tenant's own row).
    """
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


async def _add_opening(app_engine, tenant_id, actor, inv, *, units, on):
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await PositionTransactionRepository(session).add(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=on,
            units=Decimal(units),
            currency="EUR",
            ingest_origin="excel",
            created_by=actor.id,
        )


async def _add_sell(app_engine, tenant_id, actor, inv, *, units, on, price):
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await PositionTransactionRepository(session).add(
            investment_id=inv.id,
            txn_type="sell",
            trade_date=on,
            units=Decimal(units),
            price_per_unit=Decimal(price),
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
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


def _materialiser(session) -> NavMaterialisationService:
    return NavMaterialisationService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        prices=InstrumentPriceRepository(session),
        transactions=PositionTransactionRepository(session),
    )


async def _actual_navs(app_engine, tenant_id, inv):
    async with tenant_context(app_engine, tenant_id) as session:
        return await InvestmentNavRepository(session).list_by_investment_and_kind(inv.id, "actual")


# ---------------------------------------------------------------------------
# NM-01/02: insert then idempotent re-run
# ---------------------------------------------------------------------------


async def test_nm01_inserts_then_idempotent_rerun(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="nm01@example.com", unitised=True
    )
    await _add_opening(app_engine, tenant_id, actor, inv, units="100", on=_D1)
    await _set_price(app_engine, tenant_id, actor, inv, on=_D1, price="10")
    await _set_price(app_engine, tenant_id, actor, inv, on=_D2, price="11")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _materialiser(session).materialise(inv.id, acting_user=actor.id)
    assert (report.inserted, report.updated, report.noop, report.deleted) == (
        2,
        0,
        0,
        0,
    )

    rows = await _actual_navs(app_engine, tenant_id, inv)
    by_date = {r.as_of_date: r for r in rows}
    assert by_date[_D1].nav_value == Decimal("1000.0000")
    assert by_date[_D1].ingest_origin == "system"
    assert by_date[_D1].source == "computed:units×price"
    assert by_date[_D2].nav_value == Decimal("1100.0000")
    stamps = {r.as_of_date: r.updated_at for r in rows}

    # Re-run: byte-identical — all no-op, no updated_at bump.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        rerun = await _materialiser(session).materialise(inv.id, acting_user=actor.id)
    assert (rerun.inserted, rerun.updated, rerun.noop, rerun.deleted) == (
        0,
        0,
        2,
        0,
    )
    again = {r.as_of_date: r.updated_at for r in await _actual_navs(app_engine, tenant_id, inv)}
    assert again == stamps


# ---------------------------------------------------------------------------
# NM-03: a changed price refreshes only that date's 'system' row
# ---------------------------------------------------------------------------


async def test_nm03_price_change_updates_that_date_only(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="nm03@example.com", unitised=True
    )
    await _add_opening(app_engine, tenant_id, actor, inv, units="100", on=_D1)
    await _set_price(app_engine, tenant_id, actor, inv, on=_D1, price="10")
    await _set_price(app_engine, tenant_id, actor, inv, on=_D2, price="11")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _materialiser(session).materialise(inv.id, acting_user=actor.id)

    await _set_price(app_engine, tenant_id, actor, inv, on=_D2, price="12")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _materialiser(session).materialise(inv.id, acting_user=actor.id)
    assert (report.updated, report.noop, report.inserted, report.deleted) == (
        1,
        1,
        0,
        0,
    )
    by_date = {r.as_of_date: r for r in await _actual_navs(app_engine, tenant_id, inv)}
    assert by_date[_D2].nav_value == Decimal("1200.0000")


# ---------------------------------------------------------------------------
# NM-04: a backdated sale strands the later 'system' row (deleted)
# ---------------------------------------------------------------------------


async def test_nm04_backdated_sale_strands_later_row(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="nm04@example.com", unitised=True
    )
    await _add_opening(app_engine, tenant_id, actor, inv, units="100", on=_D1)
    await _set_price(app_engine, tenant_id, actor, inv, on=_D1, price="10")
    await _set_price(app_engine, tenant_id, actor, inv, on=_D2, price="11")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _materialiser(session).materialise(inv.id, acting_user=actor.id)

    # Sell the whole position on D2 → holdings 0 from D2 onward.
    await _add_sell(app_engine, tenant_id, actor, inv, units="-100", on=_D2, price="11")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _materialiser(session).materialise(inv.id, acting_user=actor.id, since=_D2)
    assert (report.deleted, report.inserted, report.updated) == (1, 0, 0)

    rows = await _actual_navs(app_engine, tenant_id, inv)
    assert [r.as_of_date for r in rows] == [_D1]  # D2 stranded and removed
    assert rows[0].ingest_origin == "system"


# ---------------------------------------------------------------------------
# NM-05: a deleted price strands that date's 'system' row
# ---------------------------------------------------------------------------


async def test_nm05_deleted_price_strands_row(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="nm05@example.com", unitised=True
    )
    await _add_opening(app_engine, tenant_id, actor, inv, units="100", on=_D1)
    await _set_price(app_engine, tenant_id, actor, inv, on=_D1, price="10")
    await _set_price(app_engine, tenant_id, actor, inv, on=_D2, price="11")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _materialiser(session).materialise(inv.id, acting_user=actor.id)

    # Delete the D2 price row.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        prices = await InstrumentPriceRepository(session).list_by_investment(inv.id)
        d2_price = next(p for p in prices if p.as_of_date == _D2)
        await InstrumentPriceRepository(session).delete(d2_price.id)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _materialiser(session).materialise(inv.id, acting_user=actor.id)
    assert (report.deleted, report.noop, report.inserted) == (1, 1, 0)
    rows = await _actual_navs(app_engine, tenant_id, inv)
    assert [r.as_of_date for r in rows] == [_D1]


# ---------------------------------------------------------------------------
# NM-06: a 'live' row is skipped with a warning, never overwritten
# ---------------------------------------------------------------------------


async def test_nm06_live_row_skipped_with_warning(
    app_engine: AsyncEngine, seed_tenant, caplog
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="nm06@example.com", unitised=True
    )
    await _add_opening(app_engine, tenant_id, actor, inv, units="100", on=_D1)
    await _set_price(app_engine, tenant_id, actor, inv, on=_D1, price="10")
    # A pre-existing 'live' NAV on D1 (must not happen for unitised, but the
    # service must never touch it if it does).
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentNavRepository(session).upsert_live(
            investment_id=inv.id,
            as_of_date=_D1,
            nav_kind="actual",
            nav_value=Decimal("999.0000"),
            currency="EUR",
            source="provider",
            basis="reported",
            created_by=actor.id,
        )

    with caplog.at_level(
        logging.WARNING,
        logger="portfoliflow.services.investments.nav_materialisation",
    ):
        async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
            report = await _materialiser(session).materialise(inv.id, acting_user=actor.id)

    assert (report.skipped_live, report.inserted, report.updated) == (1, 0, 0)
    assert "live" in caplog.text.lower()
    rows = await _actual_navs(app_engine, tenant_id, inv)
    assert len(rows) == 1
    assert rows[0].ingest_origin == "live"
    assert rows[0].nav_value == Decimal("999.0000")  # untouched


# ---------------------------------------------------------------------------
# NM-07: 'excel' / 'manual' rows take precedence (skipped, untouched)
# ---------------------------------------------------------------------------


async def _assert_precedence_skip(app_engine, seed_tenant, *, email, origin, expected_attr) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email=email, unitised=True)
    await _add_opening(app_engine, tenant_id, actor, inv, units="100", on=_D1)
    await _set_price(app_engine, tenant_id, actor, inv, on=_D1, price="10")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentNavRepository(session).upsert(
            investment_id=inv.id,
            as_of_date=_D1,
            nav_kind="actual",
            nav_value=Decimal("500.0000"),
            currency="EUR",
            source="book",
            created_by=actor.id,
            ingest_origin=origin,
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _materialiser(session).materialise(inv.id, acting_user=actor.id)
    assert getattr(report, expected_attr) == 1
    assert report.inserted == 0
    rows = await _actual_navs(app_engine, tenant_id, inv)
    assert len(rows) == 1
    assert rows[0].ingest_origin == origin
    assert rows[0].nav_value == Decimal("500.0000")  # untouched


async def test_nm07a_excel_precedence(app_engine: AsyncEngine, seed_tenant) -> None:
    await _assert_precedence_skip(
        app_engine,
        seed_tenant,
        email="nm07a@example.com",
        origin="excel",
        expected_attr="skipped_excel",
    )


async def test_nm07b_manual_precedence(app_engine: AsyncEngine, seed_tenant) -> None:
    await _assert_precedence_skip(
        app_engine,
        seed_tenant,
        email="nm07b@example.com",
        origin="manual",
        expected_attr="skipped_manual",
    )


# ---------------------------------------------------------------------------
# NM-08: reported-mode investment is a whole-investment no-op
# ---------------------------------------------------------------------------


async def test_nm08_reported_mode_is_noop(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="nm08@example.com", unitised=False
    )
    # A ledger and prices exist, but the investment is 'reported'.
    await _add_opening(app_engine, tenant_id, actor, inv, units="100", on=_D1)
    await _set_price(app_engine, tenant_id, actor, inv, on=_D1, price="10")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _materialiser(session).materialise(inv.id, acting_user=actor.id)
    assert (
        report.inserted,
        report.updated,
        report.noop,
        report.deleted,
        report.skipped_excel,
    ) == (0, 0, 0, 0, 0)
    assert await _actual_navs(app_engine, tenant_id, inv) == []
