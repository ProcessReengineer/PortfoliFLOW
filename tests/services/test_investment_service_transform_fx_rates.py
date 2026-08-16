# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Integration tests for ``InvestmentService.transform_fx_rates_from_upload`` (ADR-0099 §5).

Live-DB tests against the compose Postgres. Each test seeds a tenant +
user, builds a Phase-2 upload row with an ``FX rates`` sheet, and
asserts on the ``fx_rates`` DB state after the service transforms the
snapshot.

Coverage:

* IT-FX-01 happy path: two currencies + sparse rows persist with the
  correct ``(currency, reference_currency, rate)`` triples,
  ``ingest_origin='excel'``.
* IT-FX-02 idempotent re-import: an identical workbook leaves exactly
  the same rows (upsert on the natural key, not a duplicate).
* IT-FX-03 changed-rate re-import: the Excel producer overwrites its
  own prior row in place (ADR-0092).
* IT-FX-04 zero-result: a workbook without an ``FX rates`` sheet writes
  nothing.
* IT-FX-05 acceptance round-trip: a hand-built ``.xlsx`` with an ``FX
  rates`` sheet goes through the real ``load_excel`` parse and lands
  ``fx_rates`` rows whose triples match the headers and cells exactly.
"""

from __future__ import annotations

import datetime
import hashlib
import pathlib
from datetime import date
from decimal import Decimal
from uuid import UUID

import openpyxl
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    DataUploadRepository,
    FxRateRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from services.data_normalization.excel_workbook_loader import load_excel
from services.investments import InvestmentService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fx_rates_df(
    headers: list[str],
    rows: list[tuple[str, list[object]]],
) -> pd.DataFrame:
    """Build the ``FX rates`` DataFrame in the loader's output shape."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in rows], name="Date")
    return pd.DataFrame(
        [vals for _, vals in rows],
        index=idx,
        columns=headers,
    )


def _build_service(session) -> InvestmentService:
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
    )


async def _seed_actor(app_engine: AsyncEngine, tenant_id: UUID, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        return await UserRepository(session).create(email=email, password_hash="x" * 8)


async def _create_upload(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    sheets: dict[str, pd.DataFrame],
    filename: str = "fx.xlsx",
    file_hash: str | None = None,
):
    if file_hash is None:
        file_hash = hashlib.sha256(filename.encode()).hexdigest()
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        return await DataUploadRepository(session).create_upload(
            uploaded_by=user_id,
            filename=filename,
            file_hash=file_hash,
            size_bytes=512,
            format_version="v2",
            sheets=sheets,
        )


async def _run_transform(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    upload_id: UUID,
):
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = _build_service(session)
        return await service.transform_fx_rates_from_upload(
            upload_id,
            user_id=user_id,
            data_upload_repository=DataUploadRepository(session),
            fx_rate_repository=FxRateRepository(session),
        )


# ---------------------------------------------------------------------------
# IT-FX-01: round-trip — rates persist with correct triples
# ---------------------------------------------------------------------------


async def test_itfx01_roundtrip_persists_rates(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, "itfx01@example.com")

    sheets = {
        "fx_rates": _fx_rates_df(
            ["USD/EUR", "GBP/EUR"],
            [
                # GBP blank on day 1 — sparse series, no error.
                ("2026-01-01", [0.92, None]),
                ("2026-01-02", [0.93, 1.17]),
            ],
        ),
    }
    upload = await _create_upload(app_engine, tenant_id, actor.id, sheets=sheets)

    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    assert result.n_rates == 3
    assert result.currencies == ["GBP", "USD"]
    assert result.warnings == []

    async with tenant_context(app_engine, tenant_id) as session:
        repo = FxRateRepository(session)
        usd = await repo.list_by_currency("USD")
        gbp = await repo.list_by_currency("GBP")

    assert [(r.as_of_date, r.rate_to_reference) for r in usd] == [
        (date(2026, 1, 1), Decimal("0.92")),
        (date(2026, 1, 2), Decimal("0.93")),
    ]
    assert [(r.as_of_date, r.rate_to_reference) for r in gbp] == [
        (date(2026, 1, 2), Decimal("1.17")),
    ]
    # Every row is self-describing and stamped as the Excel producer.
    for r in [*usd, *gbp]:
        assert r.reference_currency == "EUR"
        assert r.ingest_origin == "excel"
        assert r.source == "excel-import"


# ---------------------------------------------------------------------------
# IT-FX-02: idempotent re-import
# ---------------------------------------------------------------------------


async def test_itfx02_reimport_is_idempotent(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, "itfx02@example.com")
    sheets = {
        "fx_rates": _fx_rates_df(
            ["USD/EUR"],
            [
                ("2026-01-01", [0.92]),
                ("2026-01-02", [0.93]),
            ],
        ),
    }

    upload_a = await _create_upload(
        app_engine, tenant_id, actor.id, sheets=sheets, filename="a.xlsx"
    )
    first = await _run_transform(app_engine, tenant_id, actor.id, upload_a.id)
    upload_b = await _create_upload(
        app_engine, tenant_id, actor.id, sheets=sheets, filename="b.xlsx"
    )
    second = await _run_transform(app_engine, tenant_id, actor.id, upload_b.id)

    assert first.n_rates == second.n_rates == 2

    async with tenant_context(app_engine, tenant_id) as session:
        usd = await FxRateRepository(session).list_by_currency("USD")
    # Idempotent: still exactly two rows, not four.
    assert len(usd) == 2
    assert [r.rate_to_reference for r in usd] == [
        Decimal("0.92"),
        Decimal("0.93"),
    ]


# ---------------------------------------------------------------------------
# IT-FX-03: changed-rate re-import wins (Excel producer overwrites its own row)
# ---------------------------------------------------------------------------


async def test_itfx03_changed_rate_reimport_wins(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, "itfx03@example.com")

    sheets_v1 = {
        "fx_rates": _fx_rates_df(["USD/EUR"], [("2026-01-01", [0.92])]),
    }
    upload_v1 = await _create_upload(
        app_engine, tenant_id, actor.id, sheets=sheets_v1, filename="v1.xlsx"
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_v1.id)

    # Same (currency, date) natural key, different rate.
    sheets_v2 = {
        "fx_rates": _fx_rates_df(["USD/EUR"], [("2026-01-01", [0.95])]),
    }
    upload_v2 = await _create_upload(
        app_engine, tenant_id, actor.id, sheets=sheets_v2, filename="v2.xlsx"
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload_v2.id)

    async with tenant_context(app_engine, tenant_id) as session:
        usd = await FxRateRepository(session).list_by_currency("USD")
    # One row on the natural key, updated in place to the new rate.
    assert len(usd) == 1
    assert usd[0].as_of_date == date(2026, 1, 1)
    assert usd[0].rate_to_reference == Decimal("0.95")


# ---------------------------------------------------------------------------
# IT-FX-04: zero-result path writes nothing
# ---------------------------------------------------------------------------


async def test_itfx04_no_fx_sheet_writes_nothing(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, "itfx04@example.com")

    # A workbook with an unrelated sheet and no ``FX rates``.
    sheets = {
        "navs_actual": _fx_rates_df(["Investment A"], [("2026-01-01", [100.0])]),
    }
    upload = await _create_upload(app_engine, tenant_id, actor.id, sheets=sheets)
    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    assert result.n_rates == 0
    assert result.currencies == []
    assert result.warnings == []

    async with tenant_context(app_engine, tenant_id) as session:
        usd = await FxRateRepository(session).list_by_currency("USD")
    assert usd == []


# ---------------------------------------------------------------------------
# IT-FX-05: acceptance — hand-built .xlsx round-trips through load_excel
# ---------------------------------------------------------------------------


async def test_itfx05_handbuilt_workbook_roundtrips(
    app_engine: AsyncEngine, seed_tenant, tmp_path: pathlib.Path
) -> None:
    """A real workbook with an ``FX rates`` sheet lands matching rows.

    Exercises the loader registration (Deliverable 1) end-to-end: build
    an ``.xlsx`` on the market-reference layout, parse it with the
    production :func:`load_excel`, persist the snapshot, transform, and
    assert every ``(currency, reference_currency, rate)`` triple matches
    the sheet's headers and cells exactly (acceptance criterion #3).
    """
    tenant_id = await seed_tenant()
    actor = await _seed_actor(app_engine, tenant_id, "itfx05@example.com")

    # Market-reference sheet layout: row 1 = [None, headers...], rows 2-3
    # empty (no type / sub-class metadata), rows 4+ = [date, values...].
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("FX rates")
    ws.append([None, "USD/EUR", "GBP/EUR"])
    ws.append([None, None, None])
    ws.append([None, None, None])
    ws.append([datetime.date(2026, 1, 1), 0.92, 1.17])
    ws.append([datetime.date(2026, 1, 2), 0.93, None])  # GBP sparse
    xlsx_path = tmp_path / "handbuilt_fx.xlsx"
    wb.save(xlsx_path)

    datasets = load_excel(xlsx_path)
    # Loader registration: the sheet resolves to the canonical key.
    assert "fx_rates" in datasets

    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=datasets,
        filename="handbuilt_fx.xlsx",
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    assert result.n_rates == 3

    async with tenant_context(app_engine, tenant_id) as session:
        repo = FxRateRepository(session)
        usd = await repo.list_by_currency("USD")
        gbp = await repo.list_by_currency("GBP")

    triples = {(r.currency, r.reference_currency, r.rate_to_reference) for r in [*usd, *gbp]}
    assert triples == {
        ("USD", "EUR", Decimal("0.92")),
        ("USD", "EUR", Decimal("0.93")),
        ("GBP", "EUR", Decimal("1.17")),
    }
