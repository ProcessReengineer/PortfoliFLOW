# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""End-to-end Phase-7 importer test: v21 workbook → DB.

Loads ``data/sample/PortfoliFLOW_Testdaten_v21.xlsx`` through
:func:`modules.front_office.data_import.load_excel`, persists the
resulting sheets as a real ``data_uploads`` row, and runs
:meth:`InvestmentService.transform_upload_to_investments` with the
new opt-in repositories wired up (anlv_categories, limits). Verifies the
documented Block-A acceptance criteria:

* 7 investments created with expected ``anlv_code``.
* No AUM is persisted. ADR-0103 §3 demoted the AUM sheet to an optional
  reconciliation control (compared against Σ NAV, reported on, never
  written), and ADR-0103 §7 then dropped the ``portfolio_aum`` table
  outright (migration b030) — so there is no longer a table to assert the
  absence of rows in. The ~5,479 daily rows this test once asserted have no
  destination left; the schema-level proof lives in
  ``tests/regression/test_rls_schema_invariants.py``.
* 2 limit sets for ``family='saa'``, 2 for ``family='anlv'``.
* Sum-to-100 holds for every persisted set.

Plus two negative paths:

* Sum-not-100 synthetic sheet → :class:`LimitValidationError`.
* Duplicate import of an already-persisted limit set →
  :class:`LimitValidationError` referencing immutability.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from cli.bootstrap import (
    install_default_asset_classes,
    install_unclassified_asset_class,
)
from core.exceptions import LimitValidationError
from core.repositories import (
    AnlVCategoryRepository,
    AssetClassRepository,
    DataUploadRepository,
    InvestmentCashflowRepository,
    InvestmentNavRepository,
    InvestmentRepository,
    LimitsRepository,
    UserRepository,
    tenant_context,
)
from services.data_normalization.excel_workbook_loader import load_excel
from services.data_normalization import InvestmentExtractor
from services.investments import InvestmentService

V21_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "sample" / "PortfoliFLOW_Testdaten_v21.xlsx"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_actor(app_engine: AsyncEngine, tenant_id: UUID, *, email: str):
    async with tenant_context(app_engine, tenant_id) as session:
        return await UserRepository(session).create(email=email, password_hash="x" * 8)


async def _bootstrap_catalogues(app_engine: AsyncEngine, tenant_id: UUID, user_id: UUID) -> None:
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        repo = AssetClassRepository(session)
        await install_unclassified_asset_class(repo)
        await install_default_asset_classes(repo)


async def _create_upload(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    *,
    sheets: dict[str, pd.DataFrame],
    filename: str = "PortfoliFLOW_Testdaten_v21.xlsx",
):
    import hashlib

    digest = hashlib.sha256(filename.encode("utf-8")).hexdigest()
    file_hash = digest[:64]
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        return await DataUploadRepository(session).create_upload(
            uploaded_by=user_id,
            filename=filename,
            file_hash=file_hash,
            size_bytes=1024,
            format_version="v2",
            sheets=sheets,
        )


def _service(session) -> InvestmentService:
    return InvestmentService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        cashflows=InvestmentCashflowRepository(session),
    )


async def _run_transform(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    user_id: UUID,
    upload_id: UUID,
    *,
    with_anlagegrenzen: bool = True,
):
    async with tenant_context(app_engine, tenant_id, user_id=user_id) as session:
        service = _service(session)
        kwargs: dict = dict(
            user_id=user_id,
            asset_class_repository=AssetClassRepository(session),
            data_upload_repository=DataUploadRepository(session),
            extractor=InvestmentExtractor(),
        )
        if with_anlagegrenzen:
            kwargs["anlv_category_repository"] = AnlVCategoryRepository(session)
            kwargs["limits_repository"] = LimitsRepository(session)
        return await service.transform_upload_to_investments(upload_id, **kwargs)


# ---------------------------------------------------------------------------
# IT-V21: v21 end-to-end roundtrip
# ---------------------------------------------------------------------------


async def test_v21_roundtrip_landing_investments_aum_and_limits(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    if not V21_PATH.exists():
        pytest.skip(f"v21 testdata not at {V21_PATH}; skipping roundtrip.")
    tenant_id = await seed_tenant("v21-roundtrip")
    actor = await _seed_actor(app_engine, tenant_id, email="v21-actor@example.com")
    await _bootstrap_catalogues(app_engine, tenant_id, actor.id)

    datasets = load_excel(V21_PATH)
    upload = await _create_upload(app_engine, tenant_id, actor.id, sheets=datasets)

    await _run_transform(app_engine, tenant_id, actor.id, upload.id)

    # ---- Assert investments ---------------------------------------------
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        inv_repo = InvestmentRepository(session)
        investments = await inv_repo.list_all()
        anlv_by_name = {i.name: i.anlv_code for i in investments}
    assert {i.name for i in investments} == {f"Investment {c}" for c in "ABCDEFG"}
    assert anlv_by_name["Investment A"] == "anlv_15"
    assert anlv_by_name["Investment B"] == "anlv_15"
    assert anlv_by_name["Investment C"] == "anlv_15"
    assert anlv_by_name["Investment D"] == "anlv_14"
    assert anlv_by_name["Investment E"] == "anlv_13"
    assert anlv_by_name["Investment F"] == "anlv_13"
    assert anlv_by_name["Investment G"] == "anlv_13"

    # ---- Assert limit sets ----------------------------------------------
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        limits_repo = LimitsRepository(session)
        saa_sets = await limits_repo.list_sets("saa")
        anlv_sets = await limits_repo.list_sets("anlv")
    assert len(saa_sets) == 2
    assert {s.effective_from for s in saa_sets} == {
        date(2016, 1, 1),
        date(2024, 7, 1),
    }
    assert len(anlv_sets) == 2
    assert {s.effective_from for s in anlv_sets} == {
        date(2016, 1, 1),
        date(2025, 4, 1),
    }

    # ---- Sum-to-100 sanity for every persisted set ----------------------
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        limits_repo = LimitsRepository(session)
        for limit_set in [*saa_sets, *anlv_sets]:
            rows = await limits_repo.list_limits(limit_set.id)
            total = sum((r.max_pct for r in rows), Decimal("0"))
            assert abs(total - Decimal("100")) < Decimal("0.01"), (
                f"Set {limit_set.label!r} sums to {total}, expected 100"
            )
        # 12 class keys for SAA (one set per the two effective_from
        # dates) and 8 for AnlV; cash_keys excluded class keys with 0%.
        saa_first = await limits_repo.list_limits(
            next(s for s in saa_sets if s.effective_from == date(2016, 1, 1)).id
        )
        # The v21 SAA "initial" set has 4 zero-pct rows (infra_debt,
        # hedge_funds, cash, real_estate is non-zero); zeros are
        # dropped by the importer so the count is < 12.
        assert len(saa_first) >= 7
        anlv_first = await limits_repo.list_limits(
            next(s for s in anlv_sets if s.effective_from == date(2016, 1, 1)).id
        )
        assert len(anlv_first) == 8


# ---------------------------------------------------------------------------
# Negative: duplicate import raises LimitValidationError
# ---------------------------------------------------------------------------


async def test_duplicate_import_raises_limit_validation_error(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    if not V21_PATH.exists():
        pytest.skip(f"v21 testdata not at {V21_PATH}; skipping.")
    tenant_id = await seed_tenant("v21-dup")
    actor = await _seed_actor(app_engine, tenant_id, email="v21-dup@example.com")
    await _bootstrap_catalogues(app_engine, tenant_id, actor.id)

    datasets = load_excel(V21_PATH)
    upload = await _create_upload(app_engine, tenant_id, actor.id, sheets=datasets)
    await _run_transform(app_engine, tenant_id, actor.id, upload.id)

    upload2 = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=datasets,
        filename="duplicate-v21.xlsx",
    )

    with pytest.raises(LimitValidationError) as excinfo:
        await _run_transform(app_engine, tenant_id, actor.id, upload2.id)
    assert "immutable" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Negative: synthetic sum-not-100 sheet raises LimitValidationError
# ---------------------------------------------------------------------------


async def test_sum_not_100_raises_limit_validation_error(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("v21-bad-sum")
    actor = await _seed_actor(app_engine, tenant_id, email="v21-bad-sum@example.com")
    await _bootstrap_catalogues(app_engine, tenant_id, actor.id)

    # Synthetic limit-set sheet: a single set whose limits sum to 90,
    # not 100. Class keys are valid SAA codes so the failure surfaces
    # exclusively as the sum-to-100 violation.
    saa_bad = pd.DataFrame(
        [
            [pd.Timestamp("2024-01-01")],
            ["bad sum set"],
            ["weights sum to 90"],
            [25],
            [25],
            [25],
            [15],
        ],
        index=[
            "effective_from",
            "label",
            "notes",
            "equities",
            "private_equity",
            "real_estate",
            "ig_credit",
        ],
        columns=[0],
    )
    sheets = {
        # Attributes + at least one row sheet keep the upload-side
        # invariants happy; the limits sheet is what matters here.
        "attributes": pd.DataFrame(
            data=[
                ["Aktien"],
                [None],
                ["EUR"],
                ["equities"],
                ["GP"],
            ],
            index=[
                "Investment Type",
                "Investment Sub-Class",
                "Währung",
                "Asset Class",
                "Manager / Fondsname",
            ],
            columns=["Investment Z"],
        ),
        "limit_set_saa": saa_bad,
    }
    upload = await _create_upload(
        app_engine,
        tenant_id,
        actor.id,
        sheets=sheets,
        filename="bad-sum.xlsx",
    )

    with pytest.raises(LimitValidationError) as excinfo:
        await _run_transform(app_engine, tenant_id, actor.id, upload.id)
    msg = str(excinfo.value).lower()
    assert "sum" in msg or "100" in msg
