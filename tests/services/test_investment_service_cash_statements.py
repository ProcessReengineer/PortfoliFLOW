# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Statement-to-ledger derivation for cash positions (ADR-0103 §3/§4).

The ``Cash`` sheet reports **levels**; the ledger stores **flows**. These
live-DB tests exercise the seam that converts one into the other inside the
Excel transform: the first statement becomes the ``'excel'`` ``opening``,
every subsequent one a signed ``transfer`` of the delta, every statement
date a unity ``instrument_prices`` row — and the unchanged ADR-0098 service
then materialises ``holdings × 1.0000 = balance`` as ordinary ``'actual'``
NAV rows.

Coverage
--------
* **CS-01** first import: auto-created cash position is ``'unitised'``;
  opening + transfers + unity prices land; NAVs materialise to the statement
  balances with ``'system'`` / ``computed`` provenance; a zero delta writes
  no transfer.
* **CS-02** idempotent re-import: an unchanged sheet is a full no-op —
  row-identical ledger and prices, ``updated_at`` untouched, every counter
  zero (the ADR-0103 §4 sentence).
* **CS-03** restated balance: the affected transfer is updated in place and
  the NAV series re-materialises from that date.
* **CS-04** removed statement date: the stranded ``'excel'`` transfer and
  its unity price are deleted, and the NAV on that date disappears.
* **CS-05** ``'manual'`` ledger rows survive a re-import untouched.
* **CS-06** multi-currency: EUR and USD cash positions keep separate
  ledgers and per-currency unity prices.
* **CS-07** a still-``'reported'`` cash row (ADR-0100, pre-§9-migration)
  gets its ledger and prices but is **not** flipped, materialises nothing,
  keeps its balances as ``'excel'`` NAV rows, and raises one warning.
* **CS-08** a backdated statement inserted between two existing ones — the
  case that pins why the ledger diff classifies against the committed
  target rather than writing row by row.
* **CS-10** a foreign ledger row driving the statement-derived balance
  negative no longer aborts the import (ADR-0130): the reconcile has no
  non-negativity check left, because every position it writes is cash.
"""

from __future__ import annotations

from datetime import date
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
from services.investments.holdings import holdings_as_of
from services.investments.unity_price import UNITY_PRICE


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _attributes_df(investments: dict[str, dict[str, object]]) -> pd.DataFrame:
    columns = list(investments)
    labels = ["Investment Type", "Asset Class", "Währung"]
    data = [[investments[c].get(label) for c in columns] for label in labels]
    return pd.DataFrame(data, index=labels, columns=columns)


def _timeseries_df(
    by_investment: dict[str, list[tuple[str, float]]], names: list[str]
) -> pd.DataFrame:
    all_dates = sorted({d for series in by_investment.values() for d, _ in series})
    return pd.DataFrame(
        {name: [dict(by_investment.get(name, [])).get(d) for d in all_dates] for name in names},
        index=pd.to_datetime(all_dates),
        columns=names,
    )


def _cash(currency: str = "EUR") -> dict[str, object]:
    return {
        "Investment Type": "cash",
        "Asset Class": "cash",
        "Währung": currency,
    }


async def _seed(app_engine: AsyncEngine, tenant_id: UUID, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassRepository(session)
        await repo.create(code="unclassified", display_name="Unclassified")
        await repo.create(code="cash", display_name="Cash")
    return actor


async def _import(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    investments: dict[str, dict[str, object]],
    statements: dict[str, list[tuple[str, float]]],
    file_hash: str,
    navs: dict[str, list[tuple[str, float]]] | None = None,
):
    """Upload a synthetic workbook and run the transform against it."""
    names = list(investments)
    sheets = {
        "attributes": _attributes_df(investments),
        "cash": _timeseries_df(statements, names),
    }
    if navs is not None:
        sheets["navs_actual"] = _timeseries_df(navs, names)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        upload = await DataUploadRepository(session).create_upload(
            uploaded_by=user_id,
            filename="cash.xlsx",
            file_hash=file_hash[:64].ljust(64, "0"),
            size_bytes=1024,
            format_version="v2",
            sheets=sheets,
        )

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = InvestmentService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            cashflows=InvestmentCashflowRepository(session),
            position_transactions=PositionTransactionRepository(session),
            instrument_prices=InstrumentPriceRepository(session),
        )
        return await service.transform_upload_to_investments(
            upload.id,
            user_id=user_id,
            asset_class_repository=AssetClassRepository(session),
            data_upload_repository=DataUploadRepository(session),
            extractor=InvestmentExtractor(),
        )


async def _book(app_engine: AsyncEngine, tenant_id: UUID, name: str):
    """Return ``(investment, ledger, prices, actual_navs)`` for one position."""
    async with tenant_context(app_engine, tenant_id) as session:
        investment = await InvestmentRepository(session).get_by_name(name)
        ledger = await PositionTransactionRepository(session).list_for_investment(investment.id)
        prices = await InstrumentPriceRepository(session).list_by_investment(investment.id)
        navs = await InvestmentNavRepository(session).list_by_investment_and_kind(
            investment.id, "actual"
        )
    return investment, ledger, prices, navs


def _transfers(ledger):
    return sorted(
        (t for t in ledger if t.txn_type == "transfer"),
        key=lambda t: t.trade_date,
    )


def _opening(ledger):
    return next(t for t in ledger if t.txn_type == "opening")


# The reference series: opening, a rise, a fall, and a flat month.
_SERIES = [
    ("2024-01-31", 1_000.0),
    ("2024-02-29", 1_500.0),  # +500
    ("2024-03-31", 1_200.0),  # -300
    ("2024-04-30", 1_200.0),  # zero delta — writes nothing
]


# ---------------------------------------------------------------------------
# CS-01: first import
# ---------------------------------------------------------------------------


async def test_cs01_first_import_derives_ledger_prices_and_navs(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cs01@example.com")

    result = await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={"Cash EUR": _SERIES},
        file_hash="cs01",
    )
    assert result.errors == ()
    assert result.investments_created == 1
    assert result.cash_statement_rows == 4

    investment, ledger, prices, navs = await _book(app_engine, tenant_id, "Cash EUR")

    # The position is created already unitised — its balance *is* its
    # holdings (ADR-0103 §1). Creation is the only moment the import may
    # set the mode.
    assert investment.valuation_mode == "unitised"
    assert investment.investment_type == "cash"

    # Opening at the first statement, carrying that balance.
    opening = _opening(ledger)
    assert opening.ingest_origin == "excel"
    assert opening.trade_date == date(2024, 1, 31)
    assert opening.units == Decimal("1000.00000000")
    assert opening.price_per_unit is None

    # One transfer per *changed* balance; the flat month writes nothing.
    assert [(t.trade_date, t.units) for t in _transfers(ledger)] == [
        (date(2024, 2, 29), Decimal("500.00000000")),
        (date(2024, 3, 31), Decimal("-300.00000000")),
    ]
    assert all(t.ingest_origin == "excel" for t in _transfers(ledger))
    assert all(t.price_per_unit is None for t in _transfers(ledger))
    assert all(t.consideration is None for t in _transfers(ledger))
    assert result.cash_ledger_inserted == 3  # opening + two transfers

    # One unity price per statement date — including the flat one, which is
    # a price observation even though it is not a flow.
    assert [p.as_of_date for p in prices] == [
        date(2024, 1, 31),
        date(2024, 2, 29),
        date(2024, 3, 31),
        date(2024, 4, 30),
    ]
    assert all(p.price == UNITY_PRICE for p in prices)
    assert all(p.currency == "EUR" for p in prices)
    assert all(p.ingest_origin == "excel" for p in prices)
    assert result.cash_prices_written == 4

    # ``holdings × 1.0000`` reproduces the statement balance on every date,
    # through the unchanged ADR-0098 materialisation.
    assert {(n.as_of_date, n.nav_value) for n in navs} == {
        (date(2024, 1, 31), Decimal("1000.0000")),
        (date(2024, 2, 29), Decimal("1500.0000")),
        (date(2024, 3, 31), Decimal("1200.0000")),
        (date(2024, 4, 30), Decimal("1200.0000")),
    }
    assert all(n.ingest_origin == "system" for n in navs)
    assert all(n.basis == "computed" for n in navs)
    assert all(n.currency == "EUR" for n in navs)


# ---------------------------------------------------------------------------
# CS-02: idempotent re-import — the ADR-0103 §4 no-op
# ---------------------------------------------------------------------------


async def test_cs02_unchanged_reimport_is_a_full_no_op(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Byte-identical ledger and price state; no ``updated_at`` bumped."""
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cs02@example.com")

    await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={"Cash EUR": _SERIES},
        file_hash="cs02-1",
    )
    _inv, ledger_before, prices_before, navs_before = await _book(app_engine, tenant_id, "Cash EUR")

    result = await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={"Cash EUR": _SERIES},
        file_hash="cs02-2",
    )

    # Every cash counter zero — the operator-visible idempotency signal.
    assert result.cash_ledger_inserted == 0
    assert result.cash_ledger_updated == 0
    assert result.cash_ledger_deleted == 0
    assert result.cash_prices_written == 0
    assert result.cash_prices_deleted == 0

    _inv, ledger_after, prices_after, navs_after = await _book(app_engine, tenant_id, "Cash EUR")

    # Row-identical: same ids, same values, same updated_at — no write was
    # issued at all against the ledger or the price series.
    assert [(t.id, t.trade_date, t.units, t.updated_at) for t in ledger_after] == [
        (t.id, t.trade_date, t.units, t.updated_at) for t in ledger_before
    ]
    assert [(p.id, p.as_of_date, p.price, p.updated_at) for p in prices_after] == [
        (p.id, p.as_of_date, p.price, p.updated_at) for p in prices_before
    ]

    # The NAV rows are value-identical. They are *re-created* rather than
    # preserved, because the transform's replace-by-investment step clears
    # an imported investment's NAV history before the reconcile runs — the
    # same treatment every other Excel-imported investment gets. The ADR-0103
    # §4 byte-identity claim is about ledger and price state, which hold
    # above.
    assert {(n.as_of_date, n.nav_value, n.ingest_origin) for n in navs_after} == {
        (n.as_of_date, n.nav_value, n.ingest_origin) for n in navs_before
    }


# ---------------------------------------------------------------------------
# CS-03: a restated balance
# ---------------------------------------------------------------------------


async def test_cs03_restated_balance_updates_transfer_in_place(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cs03@example.com")

    await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={"Cash EUR": _SERIES},
        file_hash="cs03-1",
    )
    _inv, ledger_before, _prices, _navs = await _book(app_engine, tenant_id, "Cash EUR")
    february_id = _transfers(ledger_before)[0].id

    # February restated 1,500 → 1,800. Its delta changes (+500 → +800) and
    # so does March's (-300 → -600); April's stays zero.
    restated = [
        ("2024-01-31", 1_000.0),
        ("2024-02-29", 1_800.0),
        ("2024-03-31", 1_200.0),
        ("2024-04-30", 1_200.0),
    ]
    result = await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={"Cash EUR": restated},
        file_hash="cs03-2",
    )

    assert result.cash_ledger_updated == 2
    assert result.cash_ledger_inserted == 0
    assert result.cash_ledger_deleted == 0
    assert result.cash_prices_written == 0  # dates unchanged

    _inv, ledger_after, _prices, navs = await _book(app_engine, tenant_id, "Cash EUR")
    transfers = _transfers(ledger_after)
    # Updated in place: the February row keeps its identity.
    assert transfers[0].id == february_id
    assert [(t.trade_date, t.units) for t in transfers] == [
        (date(2024, 2, 29), Decimal("800.00000000")),
        (date(2024, 3, 31), Decimal("-600.00000000")),
    ]

    # The NAV series re-materialises to the restated levels.
    assert {(n.as_of_date, n.nav_value) for n in navs} == {
        (date(2024, 1, 31), Decimal("1000.0000")),
        (date(2024, 2, 29), Decimal("1800.0000")),
        (date(2024, 3, 31), Decimal("1200.0000")),
        (date(2024, 4, 30), Decimal("1200.0000")),
    }


# ---------------------------------------------------------------------------
# CS-04: a statement date leaves the sheet
# ---------------------------------------------------------------------------


async def test_cs04_removed_statement_date_strands_transfer_and_price(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cs04@example.com")

    await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={"Cash EUR": _SERIES},
        file_hash="cs04-1",
    )

    # March is withdrawn from the sheet entirely.
    without_march = [s for s in _SERIES if s[0] != "2024-03-31"]
    result = await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={"Cash EUR": without_march},
        file_hash="cs04-2",
    )

    assert result.cash_ledger_deleted == 1
    assert result.cash_prices_deleted == 1

    _inv, ledger, prices, navs = await _book(app_engine, tenant_id, "Cash EUR")
    # The March transfer is gone; April's delta now spans from February
    # (1,500 → 1,200 = -300) and is written in its place.
    assert [(t.trade_date, t.units) for t in _transfers(ledger)] == [
        (date(2024, 2, 29), Decimal("500.00000000")),
        (date(2024, 4, 30), Decimal("-300.00000000")),
    ]
    assert date(2024, 3, 31) not in {p.as_of_date for p in prices}
    # No price, no NAV: the date leaves the materialised set entirely.
    assert date(2024, 3, 31) not in {n.as_of_date for n in navs}
    assert {(n.as_of_date, n.nav_value) for n in navs} == {
        (date(2024, 1, 31), Decimal("1000.0000")),
        (date(2024, 2, 29), Decimal("1500.0000")),
        (date(2024, 4, 30), Decimal("1200.0000")),
    }


# ---------------------------------------------------------------------------
# CS-05: manual ledger rows are never touched
# ---------------------------------------------------------------------------


async def test_cs05_manual_ledger_rows_survive_reimport(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The importer owns its ``'excel'`` rows and nothing else."""
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cs05@example.com")

    await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={"Cash EUR": _SERIES},
        file_hash="cs05-1",
    )
    investment, _ledger, _prices, _navs = await _book(app_engine, tenant_id, "Cash EUR")

    # An operator books a manual transfer between two statements.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        manual = await PositionTransactionRepository(session).add(
            investment_id=investment.id,
            txn_type="transfer",
            trade_date=date(2024, 2, 15),
            units=Decimal("25"),
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
            note="operator adjustment",
        )

    result = await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={"Cash EUR": _SERIES},
        file_hash="cs05-2",
    )
    assert result.cash_ledger_deleted == 0
    assert result.cash_ledger_updated == 0

    _inv, ledger, _prices, _navs = await _book(app_engine, tenant_id, "Cash EUR")
    survivor = next(t for t in ledger if t.id == manual.id)
    assert survivor.ingest_origin == "manual"
    assert survivor.units == Decimal("25.00000000")
    assert survivor.note == "operator adjustment"
    # And the importer's own rows are still exactly the statement deltas.
    assert [(t.trade_date, t.units) for t in _transfers(ledger) if t.ingest_origin == "excel"] == [
        (date(2024, 2, 29), Decimal("500.00000000")),
        (date(2024, 3, 31), Decimal("-300.00000000")),
    ]


# ---------------------------------------------------------------------------
# CS-06: multi-currency
# ---------------------------------------------------------------------------


async def test_cs06_per_currency_ledgers_and_prices(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cs06@example.com")

    result = await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={
            "Cash EUR": _cash("EUR"),
            "Cash USD": _cash("USD"),
        },
        statements={
            "Cash EUR": [("2024-01-31", 1_000.0), ("2024-02-29", 1_500.0)],
            "Cash USD": [("2024-01-31", 400.0), ("2024-02-29", 250.0)],
        },
        file_hash="cs06",
    )
    assert result.errors == ()
    assert result.investments_created == 2

    _eur, eur_ledger, eur_prices, eur_navs = await _book(app_engine, tenant_id, "Cash EUR")
    _usd, usd_ledger, usd_prices, usd_navs = await _book(app_engine, tenant_id, "Cash USD")

    # Each ledger is denominated in its own position currency — never
    # converted (ADR-0097 §5).
    assert all(t.currency == "EUR" for t in eur_ledger)
    assert all(t.currency == "USD" for t in usd_ledger)
    assert _transfers(eur_ledger)[0].units == Decimal("500.00000000")
    assert _transfers(usd_ledger)[0].units == Decimal("-150.00000000")

    # Unity prices are per currency: a unity price in the wrong currency
    # would be a 1:1 FX conversion smuggled into the write path.
    assert all(p.currency == "EUR" and p.price == UNITY_PRICE for p in eur_prices)
    assert all(p.currency == "USD" and p.price == UNITY_PRICE for p in usd_prices)

    assert {(n.as_of_date, n.nav_value) for n in usd_navs} == {
        (date(2024, 1, 31), Decimal("400.0000")),
        (date(2024, 2, 29), Decimal("250.0000")),
    }
    assert all(n.currency == "USD" for n in usd_navs)
    assert {n.nav_value for n in eur_navs} == {
        Decimal("1000.0000"),
        Decimal("1500.0000"),
    }


# ---------------------------------------------------------------------------
# CS-07: an ADR-0100 row that has not yet been migrated
# ---------------------------------------------------------------------------


async def test_cs07_reported_cash_row_is_not_flipped_and_keeps_its_navs(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The import never flips; the row keeps a NAV series until §9 runs.

    Defensive path — operationally v32 arrives after the S1.4 migration.
    The row gets its ledger and unity prices (which materialise nothing
    while ``'reported'``, an ADR-0098 no-op), keeps the statement balances
    as ordinary ``'excel'`` NAV rows so it does not silently drop out of
    every aggregate, and raises one warning naming the migration.
    """
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cs07@example.com")

    # An ADR-0100-era cash row: 'reported', NAV-fed, no ledger.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        asset_class = await AssetClassRepository(session).get_by_code("cash")
        await InvestmentRepository(session).create(
            name="Cash EUR",
            investment_type="cash",
            asset_class_id=asset_class.id,
            currency="EUR",
            created_by=actor.id,
        )

    result = await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={"Cash EUR": _SERIES},
        file_hash="cs07",
    )

    investment, ledger, prices, navs = await _book(app_engine, tenant_id, "Cash EUR")

    # Not flipped — the import never changes an existing row's mode.
    assert investment.valuation_mode == "reported"

    # The ledger and the unity prices are written all the same, so the §9
    # migration finds them already in place.
    assert _opening(ledger).units == Decimal("1000.00000000")
    assert len(_transfers(ledger)) == 2
    assert len(prices) == 4
    assert all(p.price == UNITY_PRICE for p in prices)

    # Materialisation is a no-op on a 'reported' row, so the balances are
    # carried by ordinary 'excel' NAV rows instead — value-identical to what
    # the computed series will be once the migration flips it.
    assert {(n.as_of_date, n.nav_value) for n in navs} == {
        (date(2024, 1, 31), Decimal("1000.0000")),
        (date(2024, 2, 29), Decimal("1500.0000")),
        (date(2024, 3, 31), Decimal("1200.0000")),
        (date(2024, 4, 30), Decimal("1200.0000")),
    }
    assert all(n.ingest_origin == "excel" for n in navs)

    migration_warnings = [w for w in result.warnings if w.action == "cash_not_yet_unitised"]
    assert len(migration_warnings) == 1
    assert "ADR-0103 §9" in migration_warnings[0].message


# ---------------------------------------------------------------------------
# CS-08: a backdated statement between two existing ones
# ---------------------------------------------------------------------------


async def test_cs08_backdated_statement_insert_does_not_trip_non_negativity(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The invariant is a property of the committed ledger, not of each write.

    The setup is the one that breaks a naive per-write guard. The book holds
    ``opening 100`` and a ``-80`` transfer (balance 20). The new sheet
    inserts a statement *between* them at balance 10, which restates both
    deltas: ``-90`` at the new date, and ``-80 → +10`` at the old one.

    Applied one row at a time, the ledger is transiently ``100, -90, -80`` —
    holdings ``-70`` — even though the ledger it is on its way to is
    ``100, 10, 20`` and never goes near zero. A per-write guard would reject
    this import outright; the reconcile instead classifies against the
    **target** and then applies the diff.

    ADR-0130 has since removed the non-negativity check from this path
    altogether (cash is exempt on every write path, and every position this
    seam writes is cash), so what this test now pins is the diff mechanics
    themselves: the committed ledger is the statement series, whatever order
    the individual writes happen to take.
    """
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cs08@example.com")

    await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={"Cash EUR": [("2024-01-31", 100.0), ("2024-03-31", 20.0)]},
        file_hash="cs08-1",
    )
    _inv, ledger, _prices, _navs = await _book(app_engine, tenant_id, "Cash EUR")
    assert [(t.trade_date, t.units) for t in _transfers(ledger)] == [
        (date(2024, 3, 31), Decimal("-80.00000000"))
    ]

    result = await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={
            "Cash EUR": [
                ("2024-01-31", 100.0),
                ("2024-02-29", 10.0),  # backdated statement
                ("2024-03-31", 20.0),
            ]
        },
        file_hash="cs08-2",
    )
    assert result.errors == ()
    assert result.cash_ledger_inserted == 1
    assert result.cash_ledger_updated == 1

    _inv, ledger, prices, navs = await _book(app_engine, tenant_id, "Cash EUR")
    assert [(t.trade_date, t.units) for t in _transfers(ledger)] == [
        (date(2024, 2, 29), Decimal("-90.00000000")),
        (date(2024, 3, 31), Decimal("10.00000000")),
    ]
    assert len(prices) == 3
    # The committed book is exactly the statement levels.
    assert {(n.as_of_date, n.nav_value) for n in navs} == {
        (date(2024, 1, 31), Decimal("100.0000")),
        (date(2024, 2, 29), Decimal("10.0000")),
        (date(2024, 3, 31), Decimal("20.0000")),
    }


# ---------------------------------------------------------------------------
# CS-09: the ledger path is not wired
# ---------------------------------------------------------------------------


async def test_cs09_unwired_ledger_path_keeps_the_balances_as_navs(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """No ledger repositories → no ledger, but never a lost balance.

    The Cash sheet takes precedence over the NAV sheets in the extractor,
    which is DB-blind: it drops the cash column's NAV rows whether or not
    the caller wired the ledger path. Without that path nothing materialises,
    so the position would be left with an empty NAV series and would silently
    vanish from every aggregate. The transform writes the statement levels as
    ordinary ``'excel'`` NAV rows instead — the v31 representation.
    """
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cs09@example.com")

    names = ["Cash EUR"]
    sheets = {
        "attributes": _attributes_df({"Cash EUR": _cash()}),
        "cash": _timeseries_df({"Cash EUR": _SERIES}, names),
    }
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        upload = await DataUploadRepository(session).create_upload(
            uploaded_by=actor.id,
            filename="cash.xlsx",
            file_hash="cs09".ljust(64, "0"),
            size_bytes=1024,
            format_version="v2",
            sheets=sheets,
        )

    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        # Deliberately no position_transactions / instrument_prices.
        service = InvestmentService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            cashflows=InvestmentCashflowRepository(session),
        )
        result = await service.transform_upload_to_investments(
            upload.id,
            user_id=actor.id,
            asset_class_repository=AssetClassRepository(session),
            data_upload_repository=DataUploadRepository(session),
            extractor=InvestmentExtractor(),
        )
    assert result.errors == ()

    investment, ledger, prices, navs = await _book(app_engine, tenant_id, "Cash EUR")
    assert investment.valuation_mode == "reported"
    assert ledger == []
    assert prices == []
    # The balances survive as NAV rows — nothing is lost.
    assert {(n.as_of_date, n.nav_value) for n in navs} == {
        (date(2024, 1, 31), Decimal("1000.0000")),
        (date(2024, 2, 29), Decimal("1500.0000")),
        (date(2024, 3, 31), Decimal("1200.0000")),
        (date(2024, 4, 30), Decimal("1200.0000")),
    }
    assert all(n.ingest_origin == "excel" for n in navs)


# ---------------------------------------------------------------------------
# CS-10: a foreign row driving the derived balance negative (ADR-0130)
# ---------------------------------------------------------------------------


async def test_cs10_foreign_row_driving_statement_ledger_negative_no_longer_raises(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The reconcile records the overdraft instead of refusing the import.

    The shape the old check existed to catch: a ``'manual'`` row the importer
    neither owns nor reads as a target takes the combined balance below zero.
    Under ADR-0097 §4 that aborted the whole re-import with
    ``NonNegativeHoldingsError``; under ADR-0130 the cash exemption applies on
    every write path, so the import — the book of record — mirrors reality and
    the negative balance is surfaced rather than refused.

    CS-05's contract is unaffected: the ``'excel'`` rows are exactly the
    statement deltas and the foreign row survives untouched.
    """
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="cs10@example.com")

    await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={"Cash EUR": _SERIES},
        file_hash="cs10-1",
    )
    investment, ledger, _prices, _navs = await _book(app_engine, tenant_id, "Cash EUR")
    excel_before = {(t.id, t.trade_date, t.units) for t in ledger}

    # An operator books a withdrawal larger than any balance the series ever
    # reaches — an overdraft on the account, not an impossible state.
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        manual = await PositionTransactionRepository(session).add(
            investment_id=investment.id,
            txn_type="transfer",
            trade_date=date(2024, 2, 15),
            units=Decimal("-2500"),
            currency="EUR",
            ingest_origin="manual",
            created_by=actor.id,
            note="operator withdrawal",
        )

    result = await _import(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Cash EUR": _cash()},
        statements={"Cash EUR": _SERIES},
        file_hash="cs10-2",
    )
    assert result.errors == ()

    _inv, ledger, _prices, _navs = await _book(app_engine, tenant_id, "Cash EUR")

    # The importer's own rows are untouched by the re-import ...
    assert {
        (t.id, t.trade_date, t.units) for t in ledger if t.ingest_origin == "excel"
    } == excel_before
    # ... and the foreign row survives (CS-05 semantics).
    survivor = next(t for t in ledger if t.id == manual.id)
    assert survivor.ingest_origin == "manual"
    assert survivor.units == Decimal("-2500.00000000")
    assert survivor.note == "operator withdrawal"

    # The derived balance is negative from the withdrawal onward.
    assert holdings_as_of(ledger, date(2024, 1, 31)) == Decimal("1000.00000000")
    assert holdings_as_of(ledger, date(2024, 2, 15)) == Decimal("-1500.00000000")
    assert holdings_as_of(ledger, date(2024, 4, 30)) == Decimal("-1300.00000000")
