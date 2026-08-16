# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Integration tests for the liquid-archetype import path (ADR-0081).

Live-DB tests against the compose Postgres. Each seeds a tenant + user
+ the ``unclassified`` asset class, builds a ``data_uploads`` row that
carries the four new liquid sheets (income pair + three tidy reference
sheets), runs ``transform_upload_to_investments`` with the three new
reference repositories, and verifies the resulting
``investment_bond_analytics`` / ``investment_rating_weight`` /
``investment_maturity_weight`` / ``investment_cashflows`` state.

Coverage

* Mini end-to-end: rows land in all three reference tables plus
  dividend + coupon income flows, all reference rows ``basis="reported"``
  and ``investment_navs.basis`` unchanged (NULL).
* Idempotency: a second identical run leaves the same final state.
* Partial success: one bad rating-bucket row is dropped without
  aborting the import.
* Additive no-op: omitting the four sheets and the three repos writes
  no reference rows and leaves the new counters at zero (opt-in
  invariant).
"""

from __future__ import annotations

from uuid import UUID

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    AssetClassRepository,
    DataUploadRepository,
    InvestmentBondAnalyticsRepository,
    InvestmentCashflowRepository,
    InvestmentMaturityWeightsRepository,
    InvestmentNavRepository,
    InvestmentRatingWeightsRepository,
    InvestmentRepository,
    UserRepository,
    tenant_context,
)
from services.data_normalization import InvestmentExtractor
from services.investments import InvestmentService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_actor_and_unclassified(app_engine: AsyncEngine, tenant_id: UUID, *, email: str):
    """Seed a user and the bootstrap ``unclassified`` asset class.

    Other asset classes (``listed_equity`` / ``listed_bonds``) are
    auto-created by the transform from the ``Asset Class`` column.
    """
    async with tenant_context(app_engine, tenant_id) as session:
        actor = await UserRepository(session).create(email=email, password_hash="x" * 8)
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        await AssetClassRepository(session).create(code="unclassified", display_name="Unclassified")
    return actor


def _attributes_df(investments: dict[str, dict[str, object]]) -> pd.DataFrame:
    columns = list(investments.keys())
    rows = ["Investment Type", "Investment Sub-Class", "Asset Class", "Währung"]
    data = [[investments[col].get(label) for col in columns] for label in rows]
    return pd.DataFrame(data, index=rows, columns=columns)


def _wide_df(columns: list[str], rows: list[tuple[str, list[object]]]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in rows], name="Date")
    return pd.DataFrame([vals for _, vals in rows], index=idx, columns=columns)


def _tidy_df(columns: list[str], rows: list[list[object]]) -> pd.DataFrame:
    # ISO-string dates mirror the loader's parser output so the
    # to_json(orient="split") round-trip is deterministic.
    return pd.DataFrame(rows, columns=columns)


_BOND_COLS = ["as_of_date", "investment", "ytm", "eff_duration", "oas", "convexity"]
_RATING_COLS = ["as_of_date", "investment", "rating_bucket", "weight_pct"]
_MATURITY_COLS = ["as_of_date", "investment", "maturity_bucket", "weight_pct"]


def _build_liquid_sheets(*, bad_rating_row: bool = False) -> dict[str, pd.DataFrame]:
    """Two listed investments + income + the three tidy reference sheets."""
    investments = {
        "Equity Fund": {
            "Investment Type": "Aktien",
            "Asset Class": "listed_equity",
            "Währung": "EUR",
        },
        "Credit Fund": {
            "Investment Type": "Credit",  # alias → listed_bonds
            "Asset Class": "listed_bonds",
            "Währung": "EUR",
        },
    }
    names = list(investments.keys())
    rating_rows = [
        ["2024-01-31", "Credit Fund", "AAA", 60.0],
        ["2024-01-31", "Credit Fund", "BBB", 40.0],
    ]
    if bad_rating_row:
        rating_rows.append(["2024-01-31", "Credit Fund", "ZZZ", 25.0])

    return {
        "attributes": _attributes_df(investments),
        "navs_actual": _wide_df(
            names,
            [
                ("2024-01-31", [100.0, 200.0]),
                ("2024-02-29", [101.0, 201.0]),
            ],
        ),
        # Equity pays dividends, Credit pays coupons (type-derived).
        "cash_flow_income_actual": _wide_df(names, [("2024-01-31", [5.0, 9.0])]),
        "cash_flow_income_plan": _wide_df(names, [("2024-06-30", [6.0, 10.0])]),
        "bond_analytics": _tidy_df(
            _BOND_COLS,
            [
                ["2024-01-31", "Credit Fund", 0.045, 3.2, 0.012, None],
                ["2024-02-29", "Credit Fund", 0.046, 3.1, None, None],
            ],
        ),
        "rating_weights": _tidy_df(_RATING_COLS, rating_rows),
        "maturity_weights": _tidy_df(
            _MATURITY_COLS,
            [["2024-01-31", "Credit Fund", "1-3y", 100.0]],
        ),
    }


async def _create_upload(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    sheets: dict[str, pd.DataFrame],
    file_hash: str,
):
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        return await DataUploadRepository(session).create_upload(
            uploaded_by=user_id,
            filename="liquid.xlsx",
            file_hash=file_hash,
            size_bytes=2048,
            format_version="v2",
            sheets=sheets,
        )


async def _run_transform(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    upload_id: UUID,
    *,
    with_liquid_repos: bool = True,
    dry_run: bool = False,
):
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = InvestmentService(
            investments=InvestmentRepository(session),
            navs=InvestmentNavRepository(session),
            cashflows=InvestmentCashflowRepository(session),
        )
        kwargs: dict = dict(
            user_id=user_id,
            asset_class_repository=AssetClassRepository(session),
            data_upload_repository=DataUploadRepository(session),
            extractor=InvestmentExtractor(),
            dry_run=dry_run,
        )
        if with_liquid_repos:
            kwargs.update(
                bond_analytics_repository=InvestmentBondAnalyticsRepository(session),
                rating_weights_repository=InvestmentRatingWeightsRepository(session),
                maturity_weights_repository=(InvestmentMaturityWeightsRepository(session)),
            )
        return await service.transform_upload_to_investments(upload_id, **kwargs)


async def _investment_id(app_engine: AsyncEngine, tenant_id: UUID, name: str) -> UUID:
    async with tenant_context(app_engine, tenant_id) as session:
        inv = await InvestmentRepository(session).get_by_name(name)
        assert inv is not None
        return inv.id


# ---------------------------------------------------------------------------
# LIQ-01: mini end-to-end
# ---------------------------------------------------------------------------


async def test_liq01_mini_end_to_end(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor_and_unclassified(app_engine, tenant_id, email="liq01@example.com")
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=_build_liquid_sheets(),
        file_hash="l1" + "a" * 62,
    )

    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)

    assert result.errors == ()
    assert result.bond_analytics_replaced == 2
    assert result.rating_weights_replaced == 2
    assert result.maturity_weights_replaced == 1

    credit_id = await _investment_id(app_engine, tenant_id, "Credit Fund")
    equity_id = await _investment_id(app_engine, tenant_id, "Equity Fund")

    async with tenant_context(app_engine, tenant_id) as session:
        ba = await InvestmentBondAnalyticsRepository(session).list_for_investment(credit_id)
        assert [r.as_of_date.isoformat() for r in ba] == ["2024-01-31", "2024-02-29"]
        assert all(r.basis == "reported" for r in ba)
        assert ba[0].oas is not None and ba[1].oas is None

        rw = await InvestmentRatingWeightsRepository(session).list_for_investment(credit_id)
        assert {r.rating_bucket for r in rw} == {"AAA", "BBB"}
        assert all(r.basis == "reported" for r in rw)

        mw = await InvestmentMaturityWeightsRepository(session).list_for_investment(credit_id)
        assert [r.maturity_bucket for r in mw] == ["1-3y"]
        assert mw[0].basis == "reported"

        # Income: Credit → coupon, Equity → dividend.
        cf_repo = InvestmentCashflowRepository(session)
        credit_flows = await cf_repo.list_by_investment(credit_id)
        assert {c.flow_type for c in credit_flows} == {"coupon"}
        assert {c.flow_kind for c in credit_flows} == {"actual", "plan"}
        assert all(c.amount > 0 for c in credit_flows)

        equity_flows = await cf_repo.list_by_investment(equity_id)
        assert {c.flow_type for c in equity_flows} == {"dividend"}

        # NAV basis stays NULL (unchanged write path).
        nav_basis = await session.execute(text("SELECT DISTINCT basis FROM investment_navs"))
        assert nav_basis.scalars().all() == [None]


# ---------------------------------------------------------------------------
# LIQ-02: idempotency
# ---------------------------------------------------------------------------


async def test_liq02_idempotent_on_second_run(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor_and_unclassified(app_engine, tenant_id, email="liq02@example.com")
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=_build_liquid_sheets(),
        file_hash="l2" + "b" * 62,
    )

    first = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    second = await _run_transform(app_engine, tenant_id, actor.id, upload.id)

    assert second.bond_analytics_replaced == first.bond_analytics_replaced == 2
    assert second.rating_weights_replaced == first.rating_weights_replaced == 2
    assert second.maturity_weights_replaced == first.maturity_weights_replaced == 1

    credit_id = await _investment_id(app_engine, tenant_id, "Credit Fund")
    async with tenant_context(app_engine, tenant_id) as session:
        ba = await InvestmentBondAnalyticsRepository(session).list_for_investment(credit_id)
        rw = await InvestmentRatingWeightsRepository(session).list_for_investment(credit_id)
        mw = await InvestmentMaturityWeightsRepository(session).list_for_investment(credit_id)
        credit_flows = await InvestmentCashflowRepository(session).list_by_investment(credit_id)
    # No duplication: the row counts equal a single generation.
    assert len(ba) == 2
    assert len(rw) == 2
    assert len(mw) == 1
    assert len(credit_flows) == 2  # one actual + one plan coupon


# ---------------------------------------------------------------------------
# LIQ-03: partial success — a bad bucket row does not abort the import
# ---------------------------------------------------------------------------


async def test_liq03_partial_success_bad_bucket(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor_and_unclassified(app_engine, tenant_id, email="liq03@example.com")
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=_build_liquid_sheets(bad_rating_row=True),
        file_hash="l3" + "c" * 62,
    )

    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)

    # The bad bucket row is dropped, the rest of the import succeeds.
    assert result.rating_weights_replaced == 2
    assert result.investments_created == 2
    assert any(e.sheet == "rating_weights" and e.column == "rating_bucket" for e in result.errors)

    credit_id = await _investment_id(app_engine, tenant_id, "Credit Fund")
    async with tenant_context(app_engine, tenant_id) as session:
        rw = await InvestmentRatingWeightsRepository(session).list_for_investment(credit_id)
    assert {r.rating_bucket for r in rw} == {"AAA", "BBB"}


# ---------------------------------------------------------------------------
# LIQ-04: additive no-op — absent sheets + unpassed repos ⇒ no new rows
# ---------------------------------------------------------------------------


async def test_liq04_additive_no_op_invariant(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant()
    actor = await _seed_actor_and_unclassified(app_engine, tenant_id, email="liq04@example.com")
    # Sheets WITHOUT the four liquid sheets; only attributes + NAVs.
    investments = {
        "Equity Fund": {
            "Investment Type": "Aktien",
            "Asset Class": "listed_equity",
            "Währung": "EUR",
        },
    }
    sheets = {
        "attributes": _attributes_df(investments),
        "navs_actual": _wide_df(["Equity Fund"], [("2024-01-31", [100.0])]),
    }
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets,
        file_hash="l4" + "d" * 62,
    )

    # And the three repos are NOT passed.
    result = await _run_transform(
        app_engine, tenant_id, actor.id, upload.id, with_liquid_repos=False
    )

    assert result.bond_analytics_replaced == 0
    assert result.rating_weights_replaced == 0
    assert result.maturity_weights_replaced == 0
    assert result.investments_created == 1
    assert result.errors == ()

    equity_id = await _investment_id(app_engine, tenant_id, "Equity Fund")
    async with tenant_context(app_engine, tenant_id) as session:
        # No reference rows anywhere.
        for table in (
            "investment_bond_analytics",
            "investment_rating_weight",
            "investment_maturity_weight",
        ):
            count = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
            assert count.scalar_one() == 0
        # No income (no income sheets) → no cashflows for the equity fund.
        flows = await InvestmentCashflowRepository(session).list_by_investment(equity_id)
        assert flows == []


# ---------------------------------------------------------------------------
# LIQ-05: the Excel transform stamps ingest_origin='excel' (ADR-0092)
# ---------------------------------------------------------------------------


async def test_liq05_excel_transform_writes_ingest_origin_excel(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Covers the remaining Excel-produced families — NAV, cashflow, rating,
    maturity — all stamped ingest_origin='excel' so a live fetch treats
    them as book-of-record (ADR-0092). Together with the region/sector
    coverage in the weights suite this exercises all six families the Excel
    producer writes (``investment_country_weights`` has no Excel producer).
    """
    tenant_id = await seed_tenant()
    actor = await _seed_actor_and_unclassified(app_engine, tenant_id, email="liq05@example.com")
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=_build_liquid_sheets(),
        file_hash="l5" + "a" * 62,
    )
    result = await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    assert result.errors == ()

    async with tenant_context(app_engine, tenant_id) as session:
        for table in (
            "investment_navs",
            "investment_cashflows",
            "investment_rating_weight",
            "investment_maturity_weight",
        ):
            origins = (
                (await session.execute(text(f"SELECT DISTINCT ingest_origin FROM {table}")))
                .scalars()
                .all()
            )
            assert origins == ["excel"], f"{table}: {origins}"
