# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Live-ingest write-path tests for ``InvestmentService`` (ADR-0092 + ADR-0098 §4).

Exercises the ingest write path end-to-end against the live compose Postgres.
Strand S3 (ADR-0098 §4) retired the S0 blanket per-share guard and replaced it
with **mode-aware routing**: for a ``'unitised'`` investment ``nav_price`` lands
in ``instrument_prices`` (then materialises into ``investment_navs`` as
``'system'`` rows) and per-share ``dividend`` / ``coupon`` are scaled by
holdings into ``investment_cashflows``; for a ``'reported'`` investment those
per-share kinds are refused (``skipped_unit_mismatch``), and a currency ≠ the
investment currency is refused on both re-routed paths
(``skipped_currency_mismatch``).

Coverage:

* Reported-mode per-share refusal — ``nav_price`` / ``dividend`` / ``coupon``
  (series and the single-point ``NormalizedQuote`` wrapper) against a
  ``'reported'`` investment are counted as ``skipped_unit_mismatch`` and reach
  no repository (findings F1/F6, the regression that keeps the P0 closed).
* F1 evidence — a refused reported ``nav_price`` leaves a pre-existing
  ``'excel'`` NAV row byte-identical.
* Unitised routing matrix — ``nav_price`` writes ``instrument_prices`` rows,
  materialises ``'system'`` NAVs, and makes **zero** direct live NAV writes;
  per-share ``dividend`` is scaled by holdings; a zero-holdings date is
  skipped; a currency mismatch is skipped on both paths.
* Position-level kinds unchanged — the ADR-0092 Excel-precedence guard, live
  self-insert, and idempotency are exercised through ``distribution``.
* Origin wiring — manual ``add_cashflow`` stamps ``'manual'``.
* Weight kinds still raise ``NotImplementedError`` — distinct from the refusal.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
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
from services.market_data.dto import (
    NormalizedIdentifier,
    NormalizedQuote,
    NormalizedSeries,
    SeriesPoint,
)

_PROVIDER = "synthetic"
_IDENT = NormalizedIdentifier(scheme="ticker", value="ACME")


async def _seed_investment(
    app_engine: AsyncEngine, tenant_id, *, email: str, unitised: bool = False
):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac = await AssetClassRepository(session).create(
            code="listed_equity", display_name="Listed Equity"
        )
        inv = await InvestmentRepository(session).create(
            name="Live Fund",
            investment_type="listed_equity",
            asset_class_id=ac.id,
            currency="EUR",
            created_by=actor.id,
        )
        if unitised:
            # The mode flip is an operator act (ADR-0097 §6); tests set it
            # directly rather than driving the (S5) web surface.
            await session.execute(
                text("UPDATE investments SET valuation_mode = 'unitised' WHERE id = :id"),
                {"id": inv.id},
            )
    return actor, inv


def _series(
    kind: str, points: list[tuple[date, str]], *, currency: str = "EUR"
) -> NormalizedSeries:
    """Build a normalised series of ``kind`` from ``(date, decimal-string)``."""
    return NormalizedSeries(
        ident=_IDENT,
        provider=_PROVIDER,
        kind=kind,
        currency=currency,
        points=tuple(SeriesPoint(as_of_date=d, value=Decimal(v)) for d, v in points),
    )


def _service(session, *, with_position_model: bool = False) -> InvestmentService:
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
        position_transactions=(
            PositionTransactionRepository(session) if with_position_model else None
        ),
        instrument_prices=(InstrumentPriceRepository(session) if with_position_model else None),
    )


def _noon(day: date) -> datetime:
    return datetime.combine(day, time(12, 0), tzinfo=timezone.utc)


async def _open_position(app_engine, tenant_id, actor, inv, *, units: str, on: date) -> None:
    """Add an ``opening`` transaction of ``units`` on ``on`` (unitised path)."""
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session, with_position_model=True).add_position_transaction(
            investment_id=inv.id,
            txn_type="opening",
            trade_date=on,
            units=Decimal(units),
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
        )


# ---------------------------------------------------------------------------
# Reported-mode per-share refusal — the mode-aware guard (findings F1/F6)
# ---------------------------------------------------------------------------


async def test_live_nav_price_refused_as_unit_mismatch(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A reported-mode ``nav_price`` series is refused, writes no NAV row (F1)."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="li-navprice-refused@example.com"
    )
    series = _series(
        "nav_price", [(date(2024, 6, 30), "100.0000"), (date(2024, 12, 31), "110.0000")]
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )

    # Both points counted as unit-mismatch; nothing else moved.
    assert report.skipped_unit_mismatch == 2
    assert report.inserted == 0
    assert report.updated_live == 0
    assert report.skipped_excel == 0
    assert report.skipped_manual == 0
    assert report.noop_live == 0
    assert report.total == 2  # total still equals the point count

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentNavRepository(session).list_by_investment(inv.id)
    assert rows == []  # the guard sits before the repository


async def test_live_dividend_refused_as_unit_mismatch(app_engine: AsyncEngine, seed_tenant) -> None:
    """A reported-mode per-share ``dividend`` series is refused, writes nothing (F6)."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="li-div-refused@example.com")
    series = _series("dividend", [(date(2024, 6, 1), "1.25")])

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )
    assert report.skipped_unit_mismatch == 1
    assert report.inserted == 0

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentCashflowRepository(session).list_by_investment(inv.id)
    assert rows == []


async def test_live_coupon_refused_as_unit_mismatch(app_engine: AsyncEngine, seed_tenant) -> None:
    """A reported-mode per-share ``coupon`` series is refused, writes nothing (F6)."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="li-coupon-refused@example.com"
    )
    series = _series("coupon", [(date(2024, 3, 1), "2.5000")])

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )
    assert report.skipped_unit_mismatch == 1
    assert report.inserted == 0

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentCashflowRepository(session).list_by_investment(inv.id)
    assert rows == []


async def test_live_nav_price_quote_refused_as_unit_mismatch(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The single-point ``NormalizedQuote`` wrapper is refused for reported nav_price."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="li-quote-refused@example.com")
    quote = NormalizedQuote(
        ident=_IDENT,
        provider=_PROVIDER,
        kind="nav_price",
        currency="EUR",
        as_of_date=date(2024, 12, 31),
        value=Decimal("42.0000"),
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session).ingest_normalized_series(
            quote, investment_id=inv.id, user_id=actor.id
        )
    assert report.skipped_unit_mismatch == 1
    assert report.inserted == 0

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentNavRepository(session).list_by_investment(inv.id)
    assert rows == []


async def test_refused_nav_price_leaves_excel_row_byte_identical(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """F1 evidence: refusing a reported nav_price never disturbs an 'excel' NAV.

    The reported-mode refusal sits *before* the write path, so Excel
    precedence (ADR-0092) is preserved trivially — the book-of-record row is
    not even read.
    """
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="li-navprice-excel@example.com"
    )
    day = date(2024, 12, 31)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        excel = await InvestmentNavRepository(session).upsert(
            investment_id=inv.id,
            as_of_date=day,
            nav_kind="actual",
            nav_value=Decimal("100.0000"),
            currency="EUR",
            source="excel-import",
            created_by=actor.id,
            ingest_origin="excel",
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session).ingest_normalized_series(
            _series("nav_price", [(day, "999.0000")]),
            investment_id=inv.id,
            user_id=actor.id,
        )
    # Refused as unit-mismatch, not classified as a skipped-excel write.
    assert report.skipped_unit_mismatch == 1
    assert report.skipped_excel == 0
    assert report.inserted == 0
    assert report.updated_live == 0

    async with tenant_context(app_engine, tenant_id) as session:
        after = await InvestmentNavRepository(session).get_by_id(excel.id)
    # Byte-identical: value, origin, source, and updated_at all untouched.
    assert after.nav_value == Decimal("100.0000")
    assert after.ingest_origin == "excel"
    assert after.source == "excel-import"
    assert after.updated_at == excel.updated_at


# ---------------------------------------------------------------------------
# Unitised routing — nav_price → instrument_prices → materialised 'system' NAVs
# ---------------------------------------------------------------------------


async def test_unitised_nav_price_writes_prices_and_materialises_navs(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """nav_price on a unitised investment lands in instrument_prices and the
    computed NAVs materialise as 'system' rows — with zero direct live NAV
    writes (the F1 structural fix)."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="li-unit-navprice@example.com", unitised=True
    )
    # Hold 100 units from 2024-01-01; prices arrive on two later dates.
    await _open_position(app_engine, tenant_id, actor, inv, units="100", on=date(2024, 1, 1))
    series = _series(
        "nav_price",
        [(date(2024, 6, 30), "10.0000"), (date(2024, 12, 31), "11.0000")],
    )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session, with_position_model=True).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )

    # The report counts the *price* writes.
    assert report.inserted == 2
    assert report.skipped_unit_mismatch == 0
    assert report.skipped_currency_mismatch == 0

    async with tenant_context(app_engine, tenant_id) as session:
        prices = await InstrumentPriceRepository(session).list_by_investment(inv.id)
        navs = await InvestmentNavRepository(session).list_by_investment_and_kind(inv.id, "actual")

    # Two live prices in instrument_prices.
    assert {p.as_of_date for p in prices} == {
        date(2024, 6, 30),
        date(2024, 12, 31),
    }
    assert {p.ingest_origin for p in prices} == {"live"}

    # Materialised NAVs: holdings(100) × price, as 'system'/'computed' rows.
    by_date = {n.as_of_date: n for n in navs}
    assert set(by_date) == {date(2024, 6, 30), date(2024, 12, 31)}
    assert by_date[date(2024, 6, 30)].nav_value == Decimal("1000.0000")
    assert by_date[date(2024, 12, 31)].nav_value == Decimal("1100.0000")
    # The structural F1 fix: the NAVs are materialised 'system' rows, and no
    # per-share price ever became a live NAV row.
    assert {n.ingest_origin for n in navs} == {"system"}
    assert all(n.ingest_origin != "live" for n in navs)


async def test_unitised_nav_price_reruns_are_idempotent(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A second identical nav_price ingest is a price no-op and re-materialises
    without change."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="li-unit-idem@example.com", unitised=True
    )
    await _open_position(app_engine, tenant_id, actor, inv, units="100", on=date(2024, 1, 1))
    series = _series("nav_price", [(date(2024, 6, 30), "10.0000")])

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        first = await _service(session, with_position_model=True).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )
    assert first.inserted == 1

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        second = await _service(session, with_position_model=True).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )
    assert second.inserted == 0
    assert second.noop_live == 1

    async with tenant_context(app_engine, tenant_id) as session:
        navs = await InvestmentNavRepository(session).list_by_investment_and_kind(inv.id, "actual")
    assert len(navs) == 1
    assert navs[0].nav_value == Decimal("1000.0000")


async def test_unitised_nav_price_currency_mismatch_is_skipped(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A USD nav_price on a EUR unitised investment is skipped, never converted
    (ADR-0097 §5); no price and no NAV is written."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="li-unit-fx@example.com", unitised=True
    )
    await _open_position(app_engine, tenant_id, actor, inv, units="100", on=date(2024, 1, 1))
    series = _series("nav_price", [(date(2024, 6, 30), "10.0000")], currency="USD")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session, with_position_model=True).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )
    assert report.skipped_currency_mismatch == 1
    assert report.inserted == 0

    async with tenant_context(app_engine, tenant_id) as session:
        prices = await InstrumentPriceRepository(session).list_by_investment(inv.id)
        navs = await InvestmentNavRepository(session).list_by_investment(inv.id)
    assert prices == []
    assert navs == []


async def test_unitised_nav_price_never_overwrites_excel_price(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The re-routed live price still honours ADR-0092: an 'excel' price row is
    left byte-identical and the materialised NAV follows the book price."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="li-unit-excelprice@example.com", unitised=True
    )
    await _open_position(app_engine, tenant_id, actor, inv, units="100", on=date(2024, 1, 1))
    day = date(2024, 6, 30)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        book = await InstrumentPriceRepository(session).upsert(
            investment_id=inv.id,
            as_of_date=day,
            price=Decimal("10.0000"),
            currency="EUR",
            source="excel-import",
            created_by=actor.id,
            ingest_origin="excel",
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session, with_position_model=True).ingest_normalized_series(
            _series("nav_price", [(day, "999.0000")]),
            investment_id=inv.id,
            user_id=actor.id,
        )
    assert report.skipped_excel == 1
    assert report.inserted == 0

    async with tenant_context(app_engine, tenant_id) as session:
        after = await InstrumentPriceRepository(session).get_by_id(book.id)
        navs = await InvestmentNavRepository(session).list_by_investment_and_kind(inv.id, "actual")
    # Book price untouched; the materialised NAV uses the book price, not 999.
    assert after.price == Decimal("10.0000")
    assert after.ingest_origin == "excel"
    assert after.updated_at == book.updated_at
    assert len(navs) == 1
    assert navs[0].nav_value == Decimal("1000.0000")


# ---------------------------------------------------------------------------
# Unitised routing — per-share dividend scaled by holdings (F6)
# ---------------------------------------------------------------------------


async def test_unitised_dividend_is_scaled_by_holdings(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A per-share dividend on a unitised investment is scaled to position
    level (per-share × holdings) before the cashflow write (F6)."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="li-unit-div@example.com", unitised=True
    )
    await _open_position(app_engine, tenant_id, actor, inv, units="100", on=date(2024, 1, 1))
    series = _series("dividend", [(date(2024, 6, 1), "1.2500")])

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session, with_position_model=True).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )
    assert report.inserted == 1
    assert report.skipped_zero_holdings == 0

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentCashflowRepository(session).list_by_investment(inv.id)
    assert len(rows) == 1
    assert rows[0].flow_type == "dividend"
    assert rows[0].ingest_origin == "live"
    # 1.2500 per share × 100 units held on the ex-date = 125.
    assert rows[0].amount == Decimal("125.0000")


async def test_unitised_dividend_rerun_is_idempotent(app_engine: AsyncEngine, seed_tenant) -> None:
    """Re-ingesting the same scaled dividend is a dedup-key no-op (ADR-0092)."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="li-unit-div-idem@example.com", unitised=True
    )
    await _open_position(app_engine, tenant_id, actor, inv, units="100", on=date(2024, 1, 1))
    series = _series("dividend", [(date(2024, 6, 1), "1.2500")])

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await _service(session, with_position_model=True).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        second = await _service(session, with_position_model=True).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )
    assert second.inserted == 0
    assert second.noop_live == 1

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentCashflowRepository(session).list_by_investment(inv.id)
    assert len(rows) == 1


async def test_unitised_dividend_with_zero_holdings_is_skipped(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A per-share dividend on a date with zero holdings has nothing to scale
    by and is counted on skipped_zero_holdings — no cashflow is written."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="li-unit-div-zero@example.com", unitised=True
    )
    # Position opens 2024-06-01; the dividend ex-date 2024-03-01 predates it,
    # so holdings on the ex-date are zero.
    await _open_position(app_engine, tenant_id, actor, inv, units="100", on=date(2024, 6, 1))
    series = _series("dividend", [(date(2024, 3, 1), "1.2500")])

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session, with_position_model=True).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )
    assert report.skipped_zero_holdings == 1
    assert report.inserted == 0

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentCashflowRepository(session).list_by_investment(inv.id)
    assert rows == []


async def test_unitised_coupon_currency_mismatch_is_skipped(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A USD coupon on a EUR unitised investment is skipped, never converted."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(
        app_engine, tenant_id, email="li-unit-coupon-fx@example.com", unitised=True
    )
    await _open_position(app_engine, tenant_id, actor, inv, units="100", on=date(2024, 1, 1))
    series = _series("coupon", [(date(2024, 6, 1), "2.5000")], currency="USD")

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session, with_position_model=True).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )
    assert report.skipped_currency_mismatch == 1
    assert report.inserted == 0

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentCashflowRepository(session).list_by_investment(inv.id)
    assert rows == []


# ---------------------------------------------------------------------------
# Position-level cashflow kinds — unchanged (exercised via ``distribution``)
# ---------------------------------------------------------------------------


async def test_live_distribution_skips_excel_row_no_duplicate(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="li-dist-excel@example.com")
    day = date(2024, 6, 1)

    # Seed an 'excel' distribution whose full dedup key (source included) will
    # match the incoming live point, so the skip branch is exercised.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        cf = await InvestmentCashflowRepository(session).create(
            investment_id=inv.id,
            flow_timestamp=_noon(day),
            flow_type="distribution",
            flow_kind="actual",
            amount=Decimal("10.0000"),
            currency="EUR",
            description=None,
            created_by=actor.id,
            source=_PROVIDER,
            ingest_origin="excel",
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session).ingest_normalized_series(
            _series("distribution", [(day, "10.0000")]),
            investment_id=inv.id,
            user_id=actor.id,
        )
    assert report.skipped_excel == 1
    assert report.inserted == 0

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentCashflowRepository(session).list_by_investment(inv.id)
    # No duplicate inserted; the excel row is untouched.
    assert len(rows) == 1
    assert rows[0].id == cf.id
    assert rows[0].ingest_origin == "excel"
    assert rows[0].updated_at == cf.updated_at


async def test_live_distribution_skips_manual_row(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="li-dist-manual@example.com")
    day = date(2024, 6, 1)

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await InvestmentCashflowRepository(session).create(
            investment_id=inv.id,
            flow_timestamp=_noon(day),
            flow_type="distribution",
            flow_kind="actual",
            amount=Decimal("10.0000"),
            currency="EUR",
            description=None,
            created_by=actor.id,
            source=_PROVIDER,
            ingest_origin="manual",
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session).ingest_normalized_series(
            _series("distribution", [(day, "10.0000")]),
            investment_id=inv.id,
            user_id=actor.id,
        )
    assert report.skipped_manual == 1
    assert report.inserted == 0

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentCashflowRepository(session).list_by_investment(inv.id)
    assert len(rows) == 1


async def test_live_distribution_inserts_then_idempotent(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="li-dist-idem@example.com")
    series = _series("distribution", [(date(2024, 3, 1), "5.0000"), (date(2024, 6, 1), "6.0000")])

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        first = await _service(session).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )
    assert first.inserted == 2

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        second = await _service(session).ingest_normalized_series(
            series, investment_id=inv.id, user_id=actor.id
        )
    assert second.inserted == 0
    assert second.noop_live == 2

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await InvestmentCashflowRepository(session).list_by_investment(inv.id)
    assert len(rows) == 2
    assert {r.ingest_origin for r in rows} == {"live"}
    assert {r.source for r in rows} == {_PROVIDER}


async def test_live_distribution_quote_wrapper_inserts_single_point(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="li-dist-quote@example.com")
    quote = NormalizedQuote(
        ident=_IDENT,
        provider=_PROVIDER,
        kind="distribution",
        currency="EUR",
        as_of_date=date(2024, 12, 31),
        value=Decimal("42.0000"),
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        report = await _service(session).ingest_normalized_series(
            quote, investment_id=inv.id, user_id=actor.id
        )
    assert report.inserted == 1


# ---------------------------------------------------------------------------
# Manual origin + weight kinds (unchanged by the S0 guard)
# ---------------------------------------------------------------------------


async def test_manual_add_cashflow_writes_manual_origin(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The manual CRUD path stamps ingest_origin='manual' (ADR-0092)."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="li-manual-cf@example.com")
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        cf = await _service(session).add_cashflow(
            investment_id=inv.id,
            flow_timestamp=_noon(date(2024, 6, 1)),
            flow_type="distribution",
            flow_kind="actual",
            amount=Decimal("50.0000"),
            currency="EUR",
            description=None,
            created_by=actor.id,
        )
    assert cf.ingest_origin == "manual"


async def test_live_weight_kind_raises_not_implemented(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Weight kinds still raise (distinct from the counted per-share refusal)."""
    tenant_id = await seed_tenant()
    actor, inv = await _seed_investment(app_engine, tenant_id, email="li-weight@example.com")
    weight_series = NormalizedSeries(
        ident=_IDENT,
        provider=_PROVIDER,
        kind="weight_region",
        currency="EUR",
        points=(SeriesPoint(as_of_date=date(2024, 12, 31), value=Decimal("60")),),
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        with pytest.raises(NotImplementedError, match="weight"):
            await _service(session).ingest_normalized_series(
                weight_series, investment_id=inv.id, user_id=actor.id
            )
