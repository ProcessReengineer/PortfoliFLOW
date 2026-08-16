# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""End-to-end tests for the unitised-opening reconcile in the transform.

Exercises the opt-in ledger path (ADR-0097 §7): an investment carrying a
``Units`` row synthesises a single ``excel``-origin ``opening``
transaction, reconciled in place across re-imports. The path is gated on
the constructor-injected ``position_transactions`` repository — creation
flows through :meth:`InvestmentService.add_position_transaction`, the
single sanctioned write seam.

Coverage
--------
* OP-01 first import: a units row → one ``excel`` opening, ``trade_date``
  defaulted to the earliest actual NAV, ``price_per_unit`` NULL; an
  investment without a units row gets no opening; the import never flips
  ``valuation_mode`` (stays ``'reported'``).
* OP-02 idempotent re-import: identical workbook → still exactly one
  opening, unchanged (no duplicate, no constraint violation).
* OP-03 update: a restated units count updates the opening **in place**
  (same row id), never a second opening.
* OP-04 units-row-free workbook: zero openings created.
* OP-05 not wired: without the ledger repository the transform is
  byte-identical to the pre-strand path — zero openings, identical
  investment/NAV counts.
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


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _attributes_df(investments: dict[str, dict[str, object]]) -> pd.DataFrame:
    """Build an Attributes DataFrame with scalar attrs + Units rows.

    Each investment dict may carry ``"Units"`` / ``"Units As Of"`` keys;
    a missing key leaves that cell blank. Both rows are always emitted so
    an omitted key models "units removed from this workbook version".
    """
    columns = list(investments.keys())
    labels = [
        "Investment Type",
        "Asset Class",
        "Währung",
        "Units",
        "Units As Of",
    ]
    data = [[investments[col].get(label) for col in columns] for label in labels]
    return pd.DataFrame(data, index=labels, columns=columns)


def _navs_df(
    navs_by_inv: dict[str, list[tuple[str, float]]],
    names: list[str],
) -> pd.DataFrame:
    """Build a date-indexed actual-NAV DataFrame (ISO index on serialise)."""
    all_dates = sorted({d for series in navs_by_inv.values() for d, _ in series})
    idx = pd.to_datetime(all_dates)
    data = {name: [dict(navs_by_inv.get(name, [])).get(d) for d in all_dates] for name in names}
    return pd.DataFrame(data, index=idx, columns=names)


async def _seed(app_engine: AsyncEngine, tenant_id: UUID, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        ac_repo = AssetClassRepository(session)
        await ac_repo.create(code="unclassified", display_name="Unclassified")
        await ac_repo.create(code="listed_equity", display_name="Listed Equity")
        await ac_repo.create(code="private_equity", display_name="Private Equity")
    return actor


async def _create_upload(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    investments: dict[str, dict[str, object]],
    navs_by_inv: dict[str, list[tuple[str, float]]],
    file_hash: str,
):
    names = list(investments.keys())
    sheets = {
        "attributes": _attributes_df(investments),
        "navs_actual": _navs_df(navs_by_inv, names),
    }
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        return await DataUploadRepository(session).create_upload(
            uploaded_by=user_id,
            filename="units.xlsx",
            file_hash=file_hash[:64].ljust(64, "0"),
            size_bytes=1024,
            format_version="v2",
            sheets=sheets,
        )


async def _run_transform(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    upload_id: UUID,
    *,
    wire_positions: bool = True,
):
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        extra = {}
        if wire_positions:
            extra = dict(
                position_transactions=PositionTransactionRepository(session),
                instrument_prices=InstrumentPriceRepository(session),
            )
        service = InvestmentService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            cashflows=InvestmentCashflowRepository(session),
            **extra,
        )
        return await service.transform_upload_to_investments(
            upload_id,
            user_id=user_id,
            asset_class_repository=AssetClassRepository(session),
            data_upload_repository=DataUploadRepository(session),
            extractor=InvestmentExtractor(),
        )


def _listed(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "Investment Type": "listed_equity",
        "Asset Class": "listed_equity",
        "Währung": "EUR",
    }
    base.update(extra)
    return base


def _illiquid(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "Investment Type": "private_equity",
        "Asset Class": "private_equity",
        "Währung": "EUR",
    }
    base.update(extra)
    return base


_NAVS = {
    "Fund Listed": [("2020-01-01", 1_000_000.0), ("2021-01-01", 1_100_000.0)],
    "Fund Illiquid": [("2020-06-01", 500_000.0)],
}


async def _openings(app_engine, tenant_id, name):
    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name(name)
        rows = await PositionTransactionRepository(session).list_for_investment(inv.id)
    return inv, [r for r in rows if r.txn_type == "opening"]


# ---------------------------------------------------------------------------
# OP-01: first import synthesises one excel opening, defaulted date
# ---------------------------------------------------------------------------


async def test_op01_first_import_creates_single_opening(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="op01@example.com")

    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments={
            "Fund Listed": _listed(Units=250_000),
            "Fund Illiquid": _illiquid(),
        },
        navs_by_inv=_NAVS,
        file_hash="op01",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    assert result.errors == ()

    listed_inv, listed_openings = await _openings(app_engine, tenant_id, "Fund Listed")
    assert len(listed_openings) == 1
    op = listed_openings[0]
    assert op.ingest_origin == "excel"
    assert op.units == Decimal("250000.00000000")
    assert op.trade_date == date(2020, 1, 1)  # earliest actual NAV
    assert op.price_per_unit is None
    # The import never flips valuation_mode.
    assert listed_inv.valuation_mode == "reported"

    # The units-row-free investment gets no opening.
    _illiquid_inv, illiquid_openings = await _openings(app_engine, tenant_id, "Fund Illiquid")
    assert illiquid_openings == []


# ---------------------------------------------------------------------------
# OP-02: idempotent re-import — still exactly one, unchanged
# ---------------------------------------------------------------------------


async def test_op02_reimport_is_idempotent(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="op02@example.com")

    invs = {"Fund Listed": _listed(Units=250_000)}
    navs = {"Fund Listed": _NAVS["Fund Listed"]}

    upload_1 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments=invs,
        navs_by_inv=navs,
        file_hash="op02-1",
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_1.id)
    _inv, openings_1 = await _openings(app_engine, tenant_id, "Fund Listed")
    assert len(openings_1) == 1

    upload_2 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments=invs,
        navs_by_inv=navs,
        file_hash="op02-2",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload_2.id)
    assert result.errors == ()

    _inv, openings_2 = await _openings(app_engine, tenant_id, "Fund Listed")
    assert len(openings_2) == 1
    # Same row, same values — reconciled to a no-op.
    assert openings_2[0].id == openings_1[0].id
    assert openings_2[0].units == Decimal("250000.00000000")
    assert openings_2[0].trade_date == date(2020, 1, 1)


# ---------------------------------------------------------------------------
# OP-03: a restated units count updates the opening in place
# ---------------------------------------------------------------------------


async def test_op03_restated_units_updates_in_place(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="op03@example.com")
    navs = {"Fund Listed": _NAVS["Fund Listed"]}

    upload_1 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Fund Listed": _listed(Units=250_000)},
        navs_by_inv=navs,
        file_hash="op03-1",
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_1.id)
    _inv, openings_1 = await _openings(app_engine, tenant_id, "Fund Listed")
    original_id = openings_1[0].id

    # Re-import with a restated count and an explicit earlier date.
    upload_2 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Fund Listed": _listed(Units=300_000, **{"Units As Of": "2019-12-01"})},
        navs_by_inv=navs,
        file_hash="op03-2",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload_2.id)
    assert result.errors == ()

    _inv, openings_2 = await _openings(app_engine, tenant_id, "Fund Listed")
    assert len(openings_2) == 1  # never a second opening
    assert openings_2[0].id == original_id  # updated in place
    assert openings_2[0].units == Decimal("300000.00000000")
    assert openings_2[0].trade_date == date(2019, 12, 1)


# ---------------------------------------------------------------------------
# OP-04: a units-row-free workbook creates no openings
# ---------------------------------------------------------------------------


async def test_op04_units_free_workbook_creates_no_openings(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="op04@example.com")

    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Fund Listed": _listed(), "Fund Illiquid": _illiquid()},
        navs_by_inv=_NAVS,
        file_hash="op04",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    assert result.errors == ()

    _l, listed_openings = await _openings(app_engine, tenant_id, "Fund Listed")
    _i, illiquid_openings = await _openings(app_engine, tenant_id, "Fund Illiquid")
    assert listed_openings == []
    assert illiquid_openings == []


# ---------------------------------------------------------------------------
# OP-05: without the ledger repository the path is byte-identical
# ---------------------------------------------------------------------------


async def test_op05_not_wired_is_byte_identical(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="op05@example.com")

    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        investments={"Fund Listed": _listed(Units=250_000)},
        navs_by_inv={"Fund Listed": _NAVS["Fund Listed"]},
        file_hash="op05",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id, wire_positions=False)
    assert result.errors == ()
    assert result.investments_created == 1
    assert result.navs_replaced == 2

    # No ledger rows written when the path is not wired.
    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name("Fund Listed")
        rows = await PositionTransactionRepository(session).list_for_investment(inv.id)
    assert rows == []
