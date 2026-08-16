# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The AUM sheet as a reconciliation control (ADR-0103 §3).

ADR-0103 retires the cash residual and defines AUM uniformly as
``Σ nav_functional(t)`` over every investment, cash rows included. The
``AUM`` sheet therefore stops being data: it is demoted to an **optional
control**, compared against the book and reported on, never persisted. The
ADR-0055 institutional finding (custodian reconciliation is the treasurer's
anchor) survives as the control it always was, without a parallel data model
behind it.

Coverage
--------
* **Nothing persists.** An import carrying an AUM sheet leaves
  nothing persisted (ADR-0103 §7 dropped ``portfolio_aum`` outright).
* **Agreement is silent.** A stated figure matching Σ NAV within the
  ``Numeric(20, 4)`` quantum of the NAV column produces no warning.
* **Deviation is a finding.** One warning per offending date, naming stated
  against computed — and a *warning*, never an import failure.
* **An absent sheet reads nothing.**
* **Zero-read (ADR-0102).** A single-currency book runs the control without
  loading one FX row — spy-asserted.
* **Multi-currency converts through the ADR-0099 §4 seam**, so a foreign
  position reconciles at its own date's rate.

The route-level assertion that the write branch persists no AUM row lives in
``tests/web/test_data_import_phase7_wiring.py``; the transform-level one in
``tests/services/test_investment_service_transform_limits_aum.py``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    DataUploadRepository,
    FxRateRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    TenantRepository,
    UserRepository,
    tenant_context,
)
from services.investments import InvestmentService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aum_df(rows: list[tuple[str, float]]) -> pd.DataFrame:
    """The market-reference shape: one ``AUM total`` column, date index."""
    return pd.DataFrame(
        {"AUM total": [value for _, value in rows]},
        index=pd.to_datetime([as_of for as_of, _ in rows]),
    )


async def _seed(app_engine: AsyncEngine, tenant_id: UUID, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        repo = AssetClassRepository(session)
        await repo.create(code="unclassified", display_name="Unclassified")
        await repo.create(code="equities", display_name="Equities")
    return actor


async def _add_investment(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    name: str,
    currency: str,
    navs: list[tuple[date, str]],
) -> None:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        asset_class = await AssetClassRepository(session).get_by_code("equities")
        investment = await InvestmentRepository(session).create(
            name=name,
            investment_type="listed_equity",
            asset_class_id=asset_class.id,
            currency=currency,
            created_by=user_id,
        )
        nav_repo = InvestmentNavRepository(session)
        for as_of, value in navs:
            await nav_repo.upsert(
                investment_id=investment.id,
                as_of_date=as_of,
                nav_kind="actual",
                nav_value=Decimal(value),
                currency=currency,
                source=None,
                created_by=user_id,
            )


async def _reconcile(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    aum_rows: list[tuple[str, float]] | None,
    fx_repo_factory=FxRateRepository,
):
    """Upload a workbook carrying only an AUM sheet, then run the control."""
    sheets: dict[str, pd.DataFrame] = {
        "attributes": pd.DataFrame(
            [["listed_equity"], ["equities"], ["EUR"]],
            index=["Investment Type", "Asset Class", "Währung"],
            columns=["Ignored"],
        )
    }
    if aum_rows is not None:
        sheets["aum"] = _aum_df(aum_rows)

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        upload = await DataUploadRepository(session).create_upload(
            uploaded_by=user_id,
            filename="aum.xlsx",
            # Unique per call: the dedup index is per (tenant, hash), and a
            # test may reconcile more than one sheet against the same book.
            file_hash=uuid4().hex.ljust(64, "0"),
            size_bytes=512,
            format_version="v2",
            sheets=sheets,
        )

    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = InvestmentService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            cashflows=InvestmentCashflowRepository(session),
        )
        return await service.reconcile_aum_sheet(
            upload.id,
            data_upload_repository=DataUploadRepository(session),
            tenant_repository=TenantRepository(session),
            fx_rate_repository=fx_repo_factory(session),
        )


# ---------------------------------------------------------------------------
# Agreement and deviation
# ---------------------------------------------------------------------------


async def test_matching_aum_produces_no_warning(app_engine: AsyncEngine, seed_tenant) -> None:
    """Σ NAV agrees with the sheet — the control is silent."""
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="aum-ok@example.com")
    await _add_investment(
        app_engine,
        tenant_id,
        actor.id,
        name="Fund A",
        currency="EUR",
        navs=[(date(2024, 1, 31), "600"), (date(2024, 2, 29), "700")],
    )
    await _add_investment(
        app_engine,
        tenant_id,
        actor.id,
        name="Fund B",
        currency="EUR",
        navs=[(date(2024, 1, 31), "400")],
    )

    warnings = await _reconcile(
        app_engine,
        tenant_id,
        actor.id,
        aum_rows=[
            ("2024-01-31", 1_000.0),  # 600 + 400
            # Fund B has no February statement: its January NAV carries
            # forward (ADR-0060), so the book is still 700 + 400.
            ("2024-02-29", 1_100.0),
        ],
    )
    assert warnings == ()


async def test_deviating_aum_warns_once_per_offending_date(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """A deviation is a control finding, not an import failure."""
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="aum-dev@example.com")
    await _add_investment(
        app_engine,
        tenant_id,
        actor.id,
        name="Fund A",
        currency="EUR",
        navs=[(date(2024, 1, 31), "600"), (date(2024, 2, 29), "700")],
    )

    warnings = await _reconcile(
        app_engine,
        tenant_id,
        actor.id,
        aum_rows=[
            ("2024-01-31", 600.0),  # agrees
            ("2024-02-29", 950.0),  # deviates by 250 — an unmodelled float
        ],
    )

    assert len(warnings) == 1
    finding = warnings[0]
    assert finding.field == "aum_reconciliation"
    assert finding.action == "aum_deviation"
    assert finding.investment_name is None
    assert "2024-02-29" in finding.message
    assert "950" in finding.message  # stated
    assert "700" in finding.message  # computed
    assert "250" in finding.message  # deviation


async def test_deviation_within_the_nav_quantum_is_not_a_finding(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The tolerance is the finest difference the NAV column can represent."""
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="aum-eps@example.com")
    await _add_investment(
        app_engine,
        tenant_id,
        actor.id,
        name="Fund A",
        currency="EUR",
        navs=[(date(2024, 1, 31), "1000.0000")],
    )

    # Exactly one quantum out — inside the tolerance.
    inside = await _reconcile(
        app_engine,
        tenant_id,
        actor.id,
        aum_rows=[("2024-01-31", 1000.0001)],
    )
    assert inside == ()


async def test_absent_aum_sheet_produces_no_warnings(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="aum-none@example.com")
    await _add_investment(
        app_engine,
        tenant_id,
        actor.id,
        name="Fund A",
        currency="EUR",
        navs=[(date(2024, 1, 31), "600")],
    )

    warnings = await _reconcile(app_engine, tenant_id, actor.id, aum_rows=None)
    assert warnings == ()


# ---------------------------------------------------------------------------
# ADR-0102 zero-read property
# ---------------------------------------------------------------------------


async def test_single_currency_book_reads_no_fx_rows(app_engine: AsyncEngine, seed_tenant) -> None:
    """The zero-read guarantee holds by construction, not by assertion.

    Every position is already in the functional currency, so
    ``build_portfolio_fx_converter`` short-circuits to the identity and the
    rate frame is never loaded. Spy-asserted, in the style of the other
    ADR-0102 conversion suites.
    """
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="aum-zero@example.com")
    await _add_investment(
        app_engine,
        tenant_id,
        actor.id,
        name="Fund A",
        currency="EUR",
        navs=[(date(2024, 1, 31), "600")],
    )

    calls: list[tuple] = []

    def _spying_repo(session):
        repo = FxRateRepository(session)
        original = repo.load_rates_frame

        async def _spy(*args, **kwargs):
            calls.append((args, kwargs))
            return await original(*args, **kwargs)

        repo.load_rates_frame = _spy  # type: ignore[method-assign]
        return repo

    warnings = await _reconcile(
        app_engine,
        tenant_id,
        actor.id,
        aum_rows=[("2024-01-31", 600.0), ("2024-02-29", 600.0)],
        fx_repo_factory=_spying_repo,
    )

    assert warnings == ()
    assert calls == [], (
        "The AUM control loaded an FX frame for a single-currency book; the "
        "ADR-0102 zero-read property is broken."
    )


# ---------------------------------------------------------------------------
# Multi-currency — conversion through the ADR-0099 §4 seam
# ---------------------------------------------------------------------------


async def test_foreign_position_reconciles_at_its_own_dates_rate(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Σ nav_functional converts point-in-time, so the control compares
    like-for-like against a functional-currency AUM figure."""
    tenant_id = await seed_tenant()
    actor = await _seed(app_engine, tenant_id, email="aum-fx@example.com")
    await _add_investment(
        app_engine,
        tenant_id,
        actor.id,
        name="Euro Fund",
        currency="EUR",
        navs=[(date(2024, 1, 31), "1000")],
    )
    await _add_investment(
        app_engine,
        tenant_id,
        actor.id,
        name="Dollar Fund",
        currency="USD",
        navs=[(date(2024, 1, 31), "200"), (date(2024, 2, 29), "200")],
    )
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        fx_repo = FxRateRepository(session)
        for as_of, rate in (
            (date(2024, 1, 31), "0.90"),
            (date(2024, 2, 29), "0.80"),
        ):
            await fx_repo.upsert(
                currency="USD",
                as_of_date=as_of,
                rate_to_reference=Decimal(rate),
                reference_currency="EUR",
                source="excel",
                created_by=actor.id,
            )

    # January: 1000 EUR + 200 USD × 0.90 = 1180 EUR.
    # February: the EUR fund carries forward at 1000; the USD fund is still
    # 200 USD but the rate has moved to 0.80 → 1160 EUR. The FX effect is
    # inside the book, so the control sees it too.
    warnings = await _reconcile(
        app_engine,
        tenant_id,
        actor.id,
        aum_rows=[("2024-01-31", 1_180.0), ("2024-02-29", 1_160.0)],
    )
    assert warnings == ()

    # A stated figure that ignores the FX move is caught.
    deviating = await _reconcile(
        app_engine,
        tenant_id,
        actor.id,
        aum_rows=[("2024-02-29", 1_180.0)],
    )
    assert len(deviating) == 1
    assert deviating[0].action == "aum_deviation"
    assert "1160" in deviating[0].message.replace(".0000", "")
