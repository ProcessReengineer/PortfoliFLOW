# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""End-to-end importer test: the example portfolio → DB, limit sets included.

Loads the committed ``sample_data/PortfoliFLOW_example_portfolio.xlsx``
through :func:`services.data_normalization.excel_workbook_loader.load_excel`,
persists the sheets as a real ``data_uploads`` row, and runs
:meth:`InvestmentService.transform_upload_to_investments` with the opt-in
AnlV and limits repositories wired up. Verifies:

* the 21 lettered investments and the two cash positions are created,
  each with the ``anlv_code`` its ``AnlV`` attribute names (the cash
  positions carry none);
* the two ``Limit Set SAA`` sets and the two ``Limit Set 2`` (AnlV) sets
  land with their effective dates, each summing to 100, with the 0 % rows
  dropped.

AUM is not asserted: ADR-0103 §3 demoted the AUM sheet to a reconciliation
control and ADR-0103 §7 dropped the ``portfolio_aum`` table (migration
b030), so there is nothing left to persist; the schema-level proof lives in
``tests/regression/test_rls_schema_invariants.py``.

Plus two negative paths on synthetic sheets:

* importing an already-persisted limit set again →
  :class:`LimitValidationError` referencing immutability;
* a set whose limits sum to 90 → :class:`LimitValidationError`.

The workbook is tracked in the repository, so no test here skips for a
missing file.
"""

from __future__ import annotations

import hashlib
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
from services.data_normalization import InvestmentExtractor
from services.data_normalization.excel_workbook_loader import load_excel
from services.investments import InvestmentService

_WORKBOOK_PATH = (
    Path(__file__).resolve().parents[2] / "sample_data" / "PortfoliFLOW_example_portfolio.xlsx"
)

#: ``anlv_code`` per position, read off the workbook's ``AnlV`` attribute
#: row (``"Nr. 12"`` → ``"anlv_12"``). The two cash positions carry none.
_EXPECTED_ANLV: dict[str, str | None] = {
    **{f"Investment {c}": "anlv_12" for c in "ABCH"},
    **{f"Investment {c}": "anlv_14" for c in "DS"},
    **{f"Investment {c}": "anlv_13" for c in "EFGNU"},
    **{f"Investment {c}": "anlv_7" for c in "IJKLM"},
    **{f"Investment {c}": "anlv_17" for c in "OPQR"},
    "Investment T": "anlv_15",
    "Cash USD": None,
    "Cash EUR": None,
}


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
    filename: str = "PortfoliFLOW_example_portfolio.xlsx",
):
    file_hash = hashlib.sha256(filename.encode("utf-8")).hexdigest()
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


def _synthetic_attributes() -> pd.DataFrame:
    """A one-investment ``attributes`` sheet that keeps the upload invariants happy."""
    return pd.DataFrame(
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
    )


# ---------------------------------------------------------------------------
# The example portfolio end to end
# ---------------------------------------------------------------------------


async def test_example_portfolio_roundtrip_lands_investments_and_limits(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("example-roundtrip")
    actor = await _seed_actor(app_engine, tenant_id, email="example-actor@example.com")
    await _bootstrap_catalogues(app_engine, tenant_id, actor.id)

    datasets = load_excel(_WORKBOOK_PATH)
    upload = await _create_upload(app_engine, tenant_id, actor.id, sheets=datasets)

    await _run_transform(app_engine, tenant_id, actor.id, upload.id)

    # ---- Investments and their AnlV codes --------------------------------
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        investments = await InvestmentRepository(session).list_all()
    assert {i.name: i.anlv_code for i in investments} == _EXPECTED_ANLV

    # ---- Limit sets ------------------------------------------------------
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

    # ---- Sum-to-100 for every persisted set; 0 % rows dropped ------------
    async with tenant_context(app_engine, tenant_id, user_id=actor.id) as session:
        limits_repo = LimitsRepository(session)
        for limit_set in [*saa_sets, *anlv_sets]:
            rows = await limits_repo.list_limits(limit_set.id)
            total = sum((r.max_pct for r in rows), Decimal("0"))
            assert abs(total - Decimal("100")) < Decimal("0.01"), (
                f"Set {limit_set.label!r} sums to {total}, expected 100"
            )
        # The initial SAA set lists 12 class keys; its three 0 % rows
        # (infra_debt, hedge_funds, cash) are dropped by the importer, as
        # the DB CHECK refuses a zero limit.
        saa_first = await limits_repo.list_limits(
            next(s for s in saa_sets if s.effective_from == date(2016, 1, 1)).id
        )
        assert len(saa_first) == 9
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
    """Persisted limit sets are immutable: importing them again is refused.

    Only the sheets the limit path needs ride along — a synthetic
    one-investment ``attributes`` sheet and the workbook's two limit-set
    sheets — so the two transforms stay cheap.
    """
    tenant_id = await seed_tenant("example-dup")
    actor = await _seed_actor(app_engine, tenant_id, email="example-dup@example.com")
    await _bootstrap_catalogues(app_engine, tenant_id, actor.id)

    limit_sheets = {
        key: frame
        for key, frame in load_excel(_WORKBOOK_PATH).items()
        if key in ("limit_set_saa", "limit_set_2")
    }
    assert set(limit_sheets) == {"limit_set_saa", "limit_set_2"}
    sheets = {"attributes": _synthetic_attributes(), **limit_sheets}

    upload = await _create_upload(
        app_engine, tenant_id, actor.id, sheets=sheets, filename="limits-first.xlsx"
    )
    await _run_transform(app_engine, tenant_id, actor.id, upload.id)

    upload2 = await _create_upload(
        app_engine, tenant_id, actor.id, sheets=sheets, filename="limits-again.xlsx"
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
    tenant_id = await seed_tenant("example-bad-sum")
    actor = await _seed_actor(app_engine, tenant_id, email="example-bad-sum@example.com")
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
    sheets = {"attributes": _synthetic_attributes(), "limit_set_saa": saa_bad}
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
